"""Common gateway interface, HTTP helper and payload redaction."""

import json
from dataclasses import dataclass, field
from urllib.parse import urlencode

import frappe
import requests

HTTP_TIMEOUT = 30

# Normalised payment states every adapter maps into.
PENDING, SUCCESSFUL, FAILED, CANCELLED, EXPIRED = (
	"Pending", "Successful", "Failed", "Cancelled", "Expired",
)

_SECRET_KEYS = ("password", "secret", "token", "passkey", "authorization", "consumer_key", "signature")
_PHONE_KEYS = ("phone", "phonenumber", "msisdn", "partya")


class ProviderError(Exception):
	"""Gateway refused or failed a request. The message is safe to show staff
	(no credentials) and is stored on the transaction."""

	def __init__(self, message: str, *, retryable: bool = False, payload=None):
		super().__init__(message)
		self.retryable = retryable
		self.payload = payload


@dataclass
class ProviderResult:
	"""Authoritative status read back from the gateway."""

	status: str
	receipt: str | None = None
	reason: str | None = None
	amount: float | None = None
	currency: str | None = None
	payment_method: str | None = None
	raw: dict = field(default_factory=dict)


@dataclass
class CallbackInfo:
	"""What a callback tells us. Status is always re-read from the gateway
	before it is trusted, even for gateways that sign callbacks."""

	provider_request_id: str | None = None
	merchant_reference: str | None = None
	receipt: str | None = None
	amount: float | None = None
	ack: dict = field(default_factory=dict)


def redact(value):
	"""Deep copy with secrets removed and phone numbers masked, for logs/events."""
	if isinstance(value, dict):
		out = {}
		for k, v in value.items():
			lk = str(k).lower()
			if any(s in lk for s in _SECRET_KEYS):
				out[k] = "***"
			elif any(s in lk.replace("_", "") for s in _PHONE_KEYS) and isinstance(v, (str, int)):
				s = str(v)
				out[k] = (s[:4] + "*" * max(0, len(s) - 7) + s[-3:]) if len(s) > 7 else "***"
			else:
				out[k] = redact(v)
		return out
	if isinstance(value, list):
		if value and all(isinstance(i, dict) and "Name" in i for i in value):
			# Daraja CallbackMetadata items: [{"Name": "PhoneNumber", "Value": ...}]
			return [
				{**i, "Value": redact({i["Name"]: i.get("Value")})[i["Name"]]} for i in value
			]
		return [redact(v) for v in value]
	return value


def with_query(url: str, params: dict, safe: str = "") -> str:
	"""Append query params to a URL (keeps existing ones)."""
	from urllib.parse import parse_qsl, urlsplit, urlunsplit

	parts = urlsplit(url)
	query = urlencode(parse_qsl(parts.query) + list(params.items()), safe=safe)
	return urlunsplit(parts._replace(query=query))


def to_json(value, limit: int = 10000) -> str:
	text = json.dumps(redact(value), default=str, indent=1)
	return text if len(text) <= limit else text[:limit] + "\n…(truncated)"


class PaymentGateway:
	"""Adapter interface. `self.settings` is the gateway's Single settings doc."""

	key = ""
	live_url = ""
	sandbox_url = ""
	token_ttl_margin = 60

	def __init__(self, settings):
		self.settings = settings

	# ── helpers ──────────────────────────────────────────────────────────
	@property
	def is_live(self) -> bool:
		return self.settings.environment == "Live"

	@property
	def base_url(self) -> str:
		return self.live_url if self.is_live else self.sandbox_url

	def secret(self, fieldname: str) -> str:
		value = self.settings.get_password(fieldname, raise_exception=False)
		if not value:
			raise ProviderError(f"{self.key}: {self.settings.meta.get_label(fieldname)} is not configured")
		return value

	def callback_url(self) -> str:
		"""Where the gateway sends results. Each gateway has its own endpoint
		(api/callbacks.<key>) and the URL carries the gateway's secret token."""
		base = (self.settings.callback_base_url or frappe.utils.get_url()).rstrip("/")
		qs = urlencode({"token": self.secret("callback_token")})
		return f"{base}/api/method/payment_integration.payment_integration.api.callbacks.{self.key.lower()}?{qs}"

	def http(self, method: str, path: str, *, retryable_statuses=(429, 500, 502, 503, 504), **kwargs) -> dict:
		"""JSON request to the gateway. Raises ProviderError with a sanitised
		message; never includes credentials in errors or logs."""
		url = path if path.startswith("http") else f"{self.base_url}{path}"
		kwargs.setdefault("timeout", HTTP_TIMEOUT)
		try:
			resp = requests.request(method, url, **kwargs)
		except requests.RequestException as e:
			raise ProviderError(f"{self.key} unreachable: {type(e).__name__}", retryable=True) from None
		try:
			data = resp.json()
		except ValueError:
			data = {"raw": (resp.text or "")[:500]}
		if resp.status_code >= 400:
			raise ProviderError(
				f"{self.key} HTTP {resp.status_code}: {self.error_message(data)}",
				retryable=resp.status_code in retryable_statuses,
				payload=data,
			)
		return data

	def error_message(self, data: dict) -> str:
		return str(data)[:300]

	def get_access_token(self, force_refresh: bool = False) -> str:
		from payment_integration.payment_integration.gateways import token_cache_key

		key = token_cache_key(self.key)
		if not force_refresh:
			cached = frappe.cache.get_value(key, expires=True)
			if cached:
				return cached
		token, ttl = self.fetch_access_token()
		frappe.cache.set_value(key, token, expires_in_sec=max(30, int(ttl) - self.token_ttl_margin))
		return token

	# ── overridable ──────────────────────────────────────────────────────
	def validate_settings(self):
		"""Gateway-specific checks when its settings are saved."""

	def test_connection(self):
		"""Raise if the saved credentials do not work."""
		self.get_access_token(force_refresh=True)

	def authenticate_callback(self, params: dict, raw_body: bytes, headers) -> bool:
		"""Default: the secret token in the callback URL. Gateways that sign
		their callbacks (Stripe) override this."""
		import hmac

		expected = self.settings.get_password("callback_token", raise_exception=False) or ""
		return bool(expected) and hmac.compare_digest(expected, str(params.get("token") or ""))

	def fetch_access_token(self) -> tuple[str, int]:
		raise NotImplementedError

	def initiate(self, txn, *, return_url: str | None = None, embed: bool = False) -> dict:
		"""Start the payment. Returns {"provider_request_id", ..., "next_action": {...}}.

		`embed`: the payer stays on the merchant page. Gateways that support it
		return an on-page next_action (iframe, embedded checkout, inline popup)
		instead of {"type": "redirect"}; a gateway that cannot embed (or lacks
		the public key it needs) still returns a redirect."""
		raise NotImplementedError

	def query_status(self, txn) -> ProviderResult:
		raise NotImplementedError

	def parse_callback(self, params: dict, body: dict) -> CallbackInfo:
		raise NotImplementedError
