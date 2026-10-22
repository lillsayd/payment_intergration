"""PayPal Checkout — Orders API v2.

Flow: create an order → payer approves on PayPal → PayPal redirects back to
`return_url` (and/or sends a webhook) → we CAPTURE the order server-side.
Capturing happens inside query_status, so the return page, webhooks and the
reconciliation job all finish an approved order the same way. Captures use a
PayPal-Request-Id, so a repeated capture can never charge twice.

Webhooks are verified by calling PayPal's verify-webhook-signature API.
"""

from decimal import ROUND_HALF_UP, Decimal

import frappe

from payment_integration.payment_integration.gateways.base import (
	CANCELLED,
	FAILED,
	PENDING,
	SUCCESSFUL,
	CallbackInfo,
	PaymentGateway,
	ProviderError,
	ProviderResult,
	with_query,
)

# PayPal amount rules: these currencies take no decimals.
ZERO_DECIMAL = {"HUF", "JPY", "TWD"}
# Currencies PayPal accepts for payments (KES is not among them).
SUPPORTED = {"AUD", "BRL", "CAD", "CNY", "CZK", "DKK", "EUR", "HKD", "HUF", "ILS", "JPY", "MYR", "MXN", "TWD",
			 "NZD", "NOK", "PHP", "PLN", "GBP", "SGD", "SEK", "CHF", "THB", "USD"}


def paypal_amount(amount, currency: str) -> str:
	places = Decimal("1") if currency.upper() in ZERO_DECIMAL else Decimal("0.01")
	return str(Decimal(str(amount)).quantize(places, rounding=ROUND_HALF_UP))


class PayPalGateway(PaymentGateway):
	key = "PayPal"
	live_url = "https://api-m.paypal.com"
	sandbox_url = "https://api-m.sandbox.paypal.com"

	def error_message(self, data: dict) -> str:
		if isinstance(data, dict):
			details = data.get("details") or []
			if details and isinstance(details[0], dict):
				return str(details[0].get("description") or details[0].get("issue"))[:300]
			return str(data.get("message") or data.get("error_description") or data.get("name") or data)[:300]
		return str(data)[:300]

	def validate_settings(self):
		currency = (self.settings.currency or "").upper()
		if currency and currency not in SUPPORTED:
			frappe.throw(f"PayPal does not accept {currency}. Choose one of: {', '.join(sorted(SUPPORTED))}")

	def fetch_access_token(self):
		data = self.http(
			"POST", "/v1/oauth2/token", data={"grant_type": "client_credentials"},
			auth=(self.settings.client_id or "", self.secret("client_secret")),
			headers={"Accept": "application/json"},
		)
		if not data.get("access_token"):
			raise ProviderError("PayPal did not return an access token")
		return data["access_token"], int(data.get("expires_in") or 3600)

	def _headers(self, **extra):
		return {"Authorization": f"Bearer {self.get_access_token()}", "Content-Type": "application/json", **extra}

	def initiate(self, txn, *, return_url=None, embed=False):
		if not return_url and not embed:
			raise ProviderError("A return URL is required for PayPal checkout")
		currency = (txn.currency or "").upper()
		brand = self.settings.brand_name or frappe.db.get_single_value(
			"Payment Integration Settings", "organization_name") or None
		context = {"user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING"}
		if not embed:
			# The PayPal buttons (embed) approve in PayPal's own popup and need no URLs.
			context.update({"return_url": with_query(return_url, {"ref": txn.name}),
							"cancel_url": with_query(return_url, {"ref": txn.name, "cancelled": "1"})})
		if brand:
			context["brand_name"] = brand[:127]
		order = self.http("POST", "/v2/checkout/orders", json={
			"intent": "CAPTURE",
			"purchase_units": [{
				"reference_id": txn.name,
				"custom_id": txn.name,
				"invoice_id": txn.name,
				"description": (txn.description or "Payment")[:127],
				"amount": {"currency_code": currency, "value": paypal_amount(txn.amount, currency)},
			}],
			"payment_source": {"paypal": {"experience_context": context}},
		}, headers=self._headers(**{"PayPal-Request-Id": f"order-{'embedded-' if embed else ''}{txn.name}"}))
		url = next((l.get("href") for l in order.get("links") or []
					if l.get("rel") in ("payer-action", "approve")), None)
		if embed and order.get("id"):
			return {
				"provider_request_id": order["id"],
				"redirect_url": url,
				"raw": {"id": order["id"], "status": order.get("status")},
				"next_action": {"type": "paypal_buttons", "order_id": order["id"], "client_id": self.settings.client_id,
								"currency": currency, "url": url},
			}
		if not order.get("id") or not url:
			raise ProviderError("PayPal did not return an approval link", payload=order)
		return {
			"provider_request_id": order["id"],
			"redirect_url": url,
			"raw": {"id": order["id"], "status": order.get("status")},
			"next_action": {"type": "redirect", "url": url},
		}

	@staticmethod
	def _capture_result(order: dict, currency_fallback: str) -> ProviderResult:
		units = order.get("purchase_units") or [{}]
		captures = ((units[0].get("payments") or {}).get("captures")) or []
		raw = {"id": order.get("id"), "status": order.get("status")}
		if not captures:
			return ProviderResult(status=PENDING, raw=raw)
		cap = captures[0]
		amount = cap.get("amount") or {}
		raw["capture"] = {"id": cap.get("id"), "status": cap.get("status")}
		status = cap.get("status")
		if status == "COMPLETED":
			return ProviderResult(status=SUCCESSFUL, receipt=cap.get("id"),
								  amount=float(amount["value"]) if amount.get("value") else None,
								  currency=amount.get("currency_code") or currency_fallback,
								  payment_method="PayPal", raw=raw)
		if status in ("DECLINED", "FAILED"):
			return ProviderResult(status=FAILED, reason=f"PayPal capture {status.lower()}", raw=raw)
		return ProviderResult(status=PENDING, reason=f"PayPal capture {str(status).lower()}", raw=raw)

	def query_status(self, txn):
		if not txn.provider_request_id:
			return ProviderResult(status=PENDING, reason="No PayPal order yet")
		order = self.http("GET", f"/v2/checkout/orders/{txn.provider_request_id}", headers=self._headers())
		status = order.get("status")
		if status == "APPROVED":
			# Payer approved: capture now (idempotent via PayPal-Request-Id).
			order = self.http("POST", f"/v2/checkout/orders/{txn.provider_request_id}/capture", json={},
							  headers=self._headers(**{"PayPal-Request-Id": f"capture-{txn.name}"}))
			status = order.get("status")
		if status == "COMPLETED":
			return self._capture_result(order, txn.currency)
		if status == "VOIDED":
			return ProviderResult(status=CANCELLED, reason="PayPal order voided", raw={"id": order.get("id")})
		return ProviderResult(status=PENDING, raw={"id": order.get("id"), "status": status})

	def authenticate_callback(self, params, raw_body, headers) -> bool:
		"""URL token, then PayPal's own verification of the webhook signature."""
		if not super().authenticate_callback(params, raw_body, headers):
			return False
		webhook_id = self.settings.webhook_id
		if not webhook_id or not headers:
			return False
		import json

		try:
			event = json.loads(raw_body or b"{}")
		except ValueError:
			return False
		names = ("PAYPAL-AUTH-ALGO", "PAYPAL-CERT-URL", "PAYPAL-TRANSMISSION-ID", "PAYPAL-TRANSMISSION-SIG",
				 "PAYPAL-TRANSMISSION-TIME")
		values = {n: headers.get(n) or headers.get(n.lower()) for n in names}
		if not all(values.values()):
			return False
		try:
			res = self.http("POST", "/v1/notifications/verify-webhook-signature", json={
				"auth_algo": values["PAYPAL-AUTH-ALGO"],
				"cert_url": values["PAYPAL-CERT-URL"],
				"transmission_id": values["PAYPAL-TRANSMISSION-ID"],
				"transmission_sig": values["PAYPAL-TRANSMISSION-SIG"],
				"transmission_time": values["PAYPAL-TRANSMISSION-TIME"],
				"webhook_id": webhook_id,
				"webhook_event": event,
			}, headers=self._headers())
		except ProviderError:
			return False
		return res.get("verification_status") == "SUCCESS"

	def parse_callback(self, params, body):
		resource = (body or {}).get("resource") or {}
		event = str((body or {}).get("event_type") or "")
		if event.startswith("CHECKOUT.ORDER."):
			order_id = resource.get("id")
		else:  # PAYMENT.CAPTURE.* — the order id sits in related_ids
			order_id = ((resource.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
		return CallbackInfo(
			provider_request_id=order_id,
			merchant_reference=None if order_id else resource.get("custom_id"),
			ack={"received": True},
		)
