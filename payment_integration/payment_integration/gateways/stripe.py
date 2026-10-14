"""Stripe Checkout (hosted payment page) via the REST API — no SDK needed.

Flow: create a Checkout Session → redirect the payer → Stripe redirects back
to `return_url` and sends a signed webhook. Webhooks must carry both our URL
token and a valid Stripe-Signature; the session is then re-read from Stripe
before anything is marked paid.
"""

import hashlib
import hmac
import time

import frappe

from payment_integration.payment_integration.gateways.base import (
	EXPIRED,
	FAILED,
	PENDING,
	SUCCESSFUL,
	CallbackInfo,
	PaymentGateway,
	ProviderError,
	ProviderResult,
	with_query,
)

API = "https://api.stripe.com"
SIGNATURE_TOLERANCE_SECONDS = 300
# Stripe minor-unit rules: https://docs.stripe.com/currencies#zero-decimal
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv",
				"xaf", "xof", "xpf"}
THREE_DECIMAL = {"bhd", "jod", "kwd", "omr", "tnd"}


def minor_factor(currency: str) -> int:
	c = (currency or "").lower()
	return 1 if c in ZERO_DECIMAL else 1000 if c in THREE_DECIMAL else 100


def to_minor(amount: float, currency: str) -> int:
	"""Amount in the currency's minor unit. Half-up rounding via Decimal
	(Python's round() is half-even and float maths drifts: 12.345 → 12344.99…)."""
	from decimal import ROUND_HALF_UP, Decimal

	value = Decimal(str(amount)) * minor_factor(currency)
	if currency.lower() in THREE_DECIMAL:
		# Stripe requires the last digit to be 0 for three-decimal currencies.
		return int((value / 10).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 10)
	return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _with_params(url: str, params: dict) -> str:
	"""Append query params; {CHECKOUT_SESSION_ID} must stay unescaped."""
	return with_query(url, params, safe="{}")


class StripeGateway(PaymentGateway):
	key = "Stripe"
	live_url = API
	sandbox_url = API  # test vs live is decided by the key, not the host

	def _auth(self):
		return {"Authorization": f"Bearer {self.secret('secret_key')}"}

	def error_message(self, data: dict) -> str:
		err = data.get("error") if isinstance(data, dict) else None
		if isinstance(err, dict):
			return str(err.get("message") or err.get("code") or err)[:300]
		return str(data)[:300]

	def _plain_secret_key(self) -> str:
		value = self.settings.secret_key or ""
		if not value or set(value) == {"*"}:
			value = self.settings.get_password("secret_key", raise_exception=False) or ""
		return value

	def validate_settings(self):
		key = self._plain_secret_key()
		if not key:
			return
		live = self.settings.environment == "Live"
		ok_prefixes = ("sk_live_", "rk_live_") if live else ("sk_test_", "rk_test_")
		if not key.startswith(ok_prefixes):
			frappe.throw(
				f"This is not a {'live' if live else 'test'} Stripe key. "
				f"In {self.settings.environment} mode use a key starting with {ok_prefixes[0]}…"
			)

	def test_connection(self):
		self.http("GET", "/v1/checkout/sessions", params={"limit": 1}, headers=self._auth())

	def initiate(self, txn, *, return_url=None):
		if not return_url:
			raise ProviderError("A return URL is required for Stripe Checkout")
		currency = (txn.currency or "").lower()
		data = {
			"mode": "payment",
			"line_items[0][quantity]": 1,
			"line_items[0][price_data][currency]": currency,
			"line_items[0][price_data][unit_amount]": to_minor(txn.amount, currency),
			"line_items[0][price_data][product_data][name]": (txn.description or "Payment")[:250],
			"success_url": _with_params(return_url, {"ref": txn.name, "session_id": "{CHECKOUT_SESSION_ID}"}),
			"cancel_url": _with_params(return_url, {"ref": txn.name, "cancelled": "1"}),
			"client_reference_id": txn.name,
			"metadata[transaction]": txn.name,
			"payment_intent_data[metadata][transaction]": txn.name,
		}
		if txn.email:
			data["customer_email"] = txn.email
		if self.settings.statement_descriptor:
			data["payment_intent_data[statement_descriptor_suffix]"] = self.settings.statement_descriptor[:22]
		# Idempotency-Key: a retried request can never create a second session.
		session = self.http("POST", "/v1/checkout/sessions", data=data,
							headers={**self._auth(), "Idempotency-Key": f"checkout-{txn.name}"})
		if not session.get("id") or not session.get("url"):
			raise ProviderError("Stripe did not return a checkout URL", payload=session)
		return {
			"provider_request_id": session["id"],
			"redirect_url": session["url"],
			"raw": {k: session.get(k) for k in ("id", "status", "payment_status", "amount_total", "currency")},
			"next_action": {"type": "redirect", "url": session["url"]},
		}

	def query_status(self, txn):
		if not txn.provider_request_id:
			return ProviderResult(status=PENDING, reason="No Checkout Session yet")
		s = self.http("GET", f"/v1/checkout/sessions/{txn.provider_request_id}", headers=self._auth())
		currency = (s.get("currency") or txn.currency or "").upper()
		amount = s.get("amount_total")
		amount = amount / minor_factor(currency) if amount is not None else None
		raw = {k: s.get(k) for k in ("id", "status", "payment_status", "amount_total", "currency", "payment_intent")}
		if s.get("payment_status") in ("paid", "no_payment_required"):
			return ProviderResult(status=SUCCESSFUL, receipt=s.get("payment_intent") or s["id"], amount=amount,
								  currency=currency, payment_method="Card", raw=raw)
		if s.get("status") == "expired":
			return ProviderResult(status=EXPIRED, reason="Checkout session expired", raw=raw)
		if s.get("status") == "complete" and s.get("payment_intent"):
			# Delayed methods: completed checkout, payment still settling (or failed).
			pi = self.http("GET", f"/v1/payment_intents/{s['payment_intent']}", headers=self._auth())
			if pi.get("status") in ("canceled", "requires_payment_method"):
				reason = ((pi.get("last_payment_error") or {}).get("message")) or "Payment failed"
				return ProviderResult(status=FAILED, reason=reason, raw=raw)
		return ProviderResult(status=PENDING, raw=raw)

	def authenticate_callback(self, params, raw_body, headers) -> bool:
		"""URL token AND a valid Stripe-Signature (HMAC-SHA256 of
		"<timestamp>.<raw body>" with the webhook signing secret)."""
		if not super().authenticate_callback(params, raw_body, headers):
			return False
		header = (headers.get("Stripe-Signature") or "") if headers else ""
		pairs = [p.split("=", 1) for p in header.split(",") if "=" in p]
		ts = next((v for k, v in pairs if k == "t"), None)
		sigs = [v for k, v in pairs if k == "v1"]
		if not ts or not sigs:
			return False
		try:
			if abs(time.time() - int(ts)) > SIGNATURE_TOLERANCE_SECONDS:
				return False
		except ValueError:
			return False
		secret = self.settings.get_password("webhook_secret", raise_exception=False) or ""
		if not secret:
			return False
		expected = hmac.new(secret.encode(), f"{ts}.".encode() + (raw_body or b""), hashlib.sha256).hexdigest()
		return any(hmac.compare_digest(expected, s) for s in sigs)

	def parse_callback(self, params, body):
		obj = ((body or {}).get("data") or {}).get("object") or {}
		session_id = obj.get("id") if str(obj.get("object")) == "checkout.session" else None
		return CallbackInfo(
			provider_request_id=session_id,
			merchant_reference=None if session_id else (obj.get("metadata") or {}).get("transaction"),
			ack={"received": True},
		)
