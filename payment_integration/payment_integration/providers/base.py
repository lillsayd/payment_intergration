"""Common provider interface, HTTP helper and payload redaction."""

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

_SECRET_KEYS = ("password", "secret", "token", "passkey", "authorization", "consumer_key")
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
	"""What a callback tells us. Callbacks are unsigned for both Daraja and
	Pesapal, so this is only a hint — the status is always re-read from the
	gateway before it is trusted."""

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


def to_json(value, limit: int = 10000) -> str:
	text = json.dumps(redact(value), default=str, indent=1)
	return text if len(text) <= limit else text[:limit] + "\n…(truncated)"


class PaymentProvider:
	"""Adapter interface. Subclasses implement the gateway specifics."""

	name = ""
	live_url = ""
	sandbox_url = ""
	token_ttl_margin = 60

	def __init__(self, account):
		self.account = account

	# ── helpers ──────────────────────────────────────────────────────────
	@property
	def base_url(self) -> str:
		return self.live_url if self.account.environment == "Live" else self.sandbox_url

	def secret(self, fieldname: str) -> str:
		value = self.account.get_password(fieldname, raise_exception=False)
		if not value:
			raise ProviderError(f"{self.account.name}: {fieldname} is not configured")
		return value

	def callback_url(self, method: str) -> str:
		base = (self.account.callback_base_url or frappe.utils.get_url()).rstrip("/")
		qs = urlencode({"cb": self.account.callback_id, "token": self.secret("callback_token")})
		return f"{base}/api/method/payment_integration.payment_integration.api.callbacks.{method}?{qs}"

	def http(self, method: str, path: str, *, retryable_statuses=(429, 500, 502, 503, 504), **kwargs) -> dict:
		"""JSON request to the gateway. Raises ProviderError with a sanitised
		message; never includes credentials in errors or logs."""
		url = path if path.startswith("http") else f"{self.base_url}{path}"
		kwargs.setdefault("timeout", HTTP_TIMEOUT)
		try:
			resp = requests.request(method, url, **kwargs)
		except requests.RequestException as e:
			raise ProviderError(f"{self.name} unreachable: {type(e).__name__}", retryable=True) from None
		try:
			data = resp.json()
		except ValueError:
			data = {"raw": (resp.text or "")[:500]}
		if resp.status_code >= 400:
			raise ProviderError(
				f"{self.name} HTTP {resp.status_code}: {self.error_message(data)}",
				retryable=resp.status_code in retryable_statuses,
				payload=data,
			)
		return data

	def error_message(self, data: dict) -> str:
		return str(data)[:300]

	def get_access_token(self, force_refresh: bool = False) -> str:
		from payment_integration.payment_integration.providers import token_cache_key

		key = token_cache_key(self.account.name)
		if not force_refresh:
			cached = frappe.cache.get_value(key, expires=True)
			if cached:
				return cached
		token, ttl = self.fetch_access_token()
		frappe.cache.set_value(key, token, expires_in_sec=max(30, int(ttl) - self.token_ttl_margin))
		return token

	# ── to implement ─────────────────────────────────────────────────────
	def fetch_access_token(self) -> tuple[str, int]:
		raise NotImplementedError

	def initiate(self, txn, *, return_url: str | None = None) -> dict:
		"""Start the payment. Returns {"provider_request_id", ..., "next_action": {...}}."""
		raise NotImplementedError

	def query_status(self, txn) -> ProviderResult:
		raise NotImplementedError

	def parse_callback(self, params: dict, body: dict) -> CallbackInfo:
		raise NotImplementedError
