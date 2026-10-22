"""Paystack checkout.

Flow: POST /transaction/initialize returns an authorization_url → payer pays
→ redirect back to `return_url` and a webhook. Webhooks must carry our URL
token and an x-paystack-signature (HMAC-SHA512 of the raw body with the
secret key); the status is then re-read with /transaction/verify.
"""

import hashlib
import hmac
from decimal import ROUND_HALF_UP, Decimal

import frappe

from payment_integration.payment_integration.gateways.base import (
	FAILED,
	PENDING,
	SUCCESSFUL,
	CallbackInfo,
	PaymentGateway,
	ProviderError,
	ProviderResult,
	with_query,
)

API = "https://api.paystack.co"


def to_subunit(amount) -> int:
	"""Paystack amounts are in the currency's subunit (kobo, cents, ...)."""
	return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class PaystackGateway(PaymentGateway):
	key = "Paystack"
	live_url = API
	sandbox_url = API  # test vs live is decided by the key

	def _auth(self):
		return {"Authorization": f"Bearer {self.secret('secret_key')}", "Content-Type": "application/json"}

	def error_message(self, data: dict) -> str:
		return str((data or {}).get("message") or data)[:300] if isinstance(data, dict) else str(data)[:300]

	def validate_settings(self):
		value = self.settings.secret_key or ""
		if not value or set(value) == {"*"}:
			value = self.settings.get_password("secret_key") or ""
		if not value:
			return
		live = self.settings.environment == "Live"
		prefix = "sk_live_" if live else "sk_test_"
		if not value.startswith(prefix):
			frappe.throw(f"This is not a {'live' if live else 'test'} Paystack secret key ({prefix}… expected)")

	def test_connection(self):
		self.http("GET", "/transaction", params={"perPage": 1}, headers=self._auth())

	def initiate(self, txn, *, return_url=None, embed=False):
		if not return_url and not embed:
			raise ProviderError("A return URL is required for Paystack checkout")
		if not txn.email:
			raise ProviderError("Paystack needs the payer's email address")
		payload = {
			"email": txn.email,
			"amount": to_subunit(txn.amount),
			"currency": (txn.currency or "").upper(),
			"reference": txn.name,
			"metadata": {"transaction": txn.name, "description": (txn.description or "")[:100]},
		}
		if return_url:
			payload["callback_url"] = with_query(return_url, {"ref": txn.name})
		if self.settings.channels:
			payload["channels"] = [c.strip() for c in self.settings.channels.split(",") if c.strip()]
		data = self.http("POST", "/transaction/initialize", json=payload, headers=self._auth())
		url = ((data or {}).get("data") or {}).get("authorization_url")
		if not data.get("status") or not url:
			raise ProviderError(f"Paystack refused the payment: {self.error_message(data)}", payload=data)
		access_code = data["data"].get("access_code")
		return {
			"provider_request_id": txn.name,  # Paystack tracks us by reference
			"redirect_url": url,
			"raw": {"status": data.get("status"), "reference": data["data"].get("reference")},
			# Inline popup: resumes this same initialised transaction over our page.
			"next_action": ({"type": "paystack_inline", "access_code": access_code, "url": url}
							if embed and access_code else {"type": "redirect", "url": url}),
		}

	def query_status(self, txn):
		try:
			data = self.http("GET", f"/transaction/verify/{txn.name}", headers=self._auth())
		except ProviderError as e:
			if "HTTP 400" in str(e) or "HTTP 404" in str(e):
				return ProviderResult(status=PENDING, reason="Payer has not completed checkout yet")
			raise
		t = (data or {}).get("data") or {}
		raw = {k: t.get(k) for k in ("id", "reference", "status", "amount", "currency", "channel", "gateway_response")}
		status = str(t.get("status") or "").lower()
		if status == "success":
			return ProviderResult(status=SUCCESSFUL, receipt=str(t.get("id") or t.get("reference") or ""),
								  amount=(t["amount"] / 100) if t.get("amount") is not None else None,
								  currency=t.get("currency"), payment_method=t.get("channel"), raw=raw)
		if status in ("failed", "reversed"):
			return ProviderResult(status=FAILED, reason=t.get("gateway_response") or f"Payment {status}", raw=raw)
		return ProviderResult(status=PENDING, raw=raw)  # abandoned / ongoing / pending / queued

	def authenticate_callback(self, params, raw_body, headers) -> bool:
		if not super().authenticate_callback(params, raw_body, headers):
			return False
		secret = self.settings.get_password("secret_key") or ""
		given = (headers.get("x-paystack-signature") or headers.get("X-Paystack-Signature") or "") if headers else ""
		if not secret or not given:
			return False
		expected = hmac.new(secret.encode(), raw_body or b"", hashlib.sha512).hexdigest()
		return hmac.compare_digest(expected, given)

	def parse_callback(self, params, body):
		data = (body or {}).get("data") or {}
		return CallbackInfo(merchant_reference=data.get("reference"), ack={"received": True})
