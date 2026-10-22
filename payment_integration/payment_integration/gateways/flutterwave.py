"""Flutterwave Standard checkout (API v3).

Flow: POST /v3/payments returns a hosted payment link → payer pays → redirect
back to `return_url` and a webhook. Webhooks must carry our URL token and the
verif-hash header (the secret hash configured in the dashboard); the status is
then re-read with verify_by_reference before anything is marked paid.
"""

import hmac

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

API = "https://api.flutterwave.com"


class FlutterwaveGateway(PaymentGateway):
	key = "Flutterwave"
	live_url = API
	sandbox_url = API  # test vs live is decided by the key

	def _auth(self):
		return {"Authorization": f"Bearer {self.secret('secret_key')}", "Content-Type": "application/json"}

	def error_message(self, data: dict) -> str:
		return str((data or {}).get("message") or data)[:300] if isinstance(data, dict) else str(data)[:300]

	def _plain(self, name: str) -> str:
		value = self.settings.get(name) or ""
		if not value or set(value) == {"*"}:
			value = self.settings.get_password(name) or ""
		return value

	def validate_settings(self):
		live = self.settings.environment == "Live"
		pub = (self.settings.public_key or "").strip()
		if pub and pub.startswith("FLWPUBK_TEST") == live:
			frappe.throw(f"This is not a {'live' if live else 'test'} Flutterwave public key "
						 f"({'FLWPUBK-…' if live else 'FLWPUBK_TEST-…'} expected in {self.settings.environment} mode)")
		key = self._plain("secret_key")
		if not key:
			return
		is_test = key.startswith("FLWSECK_TEST")
		if live == is_test:
			frappe.throw(f"This is not a {'live' if live else 'test'} Flutterwave secret key "
						 f"({'FLWSECK-…' if live else 'FLWSECK_TEST-…'} expected in {self.settings.environment} mode)")

	def test_connection(self):
		# Cheap authenticated read: list of banks for Kenya.
		self.http("GET", "/v3/banks/KE", headers=self._auth())

	def initiate(self, txn, *, return_url=None, embed=False):
		public_key = (self.settings.public_key or "").strip()
		if not return_url and not (embed and public_key):
			raise ProviderError("A return URL is required for Flutterwave checkout")
		if not txn.email:
			raise ProviderError("Flutterwave needs the payer's email address")
		if embed and public_key:
			return self._inline(txn, public_key)
		payload = {
			"tx_ref": txn.name,
			"amount": round(float(txn.amount), 2),
			"currency": (txn.currency or "").upper(),
			"redirect_url": with_query(return_url, {"ref": txn.name}),
			"customer": {"email": txn.email, "phonenumber": txn.phone or "", "name": txn.get("payer_name") or ""},
			"customizations": {
				"title": frappe.db.get_single_value("Payment Integration Settings", "organization_name") or "Payment",
				"description": (txn.description or "Payment")[:100],
			},
			"meta": {"transaction": txn.name},
		}
		if self.settings.payment_options:
			payload["payment_options"] = self.settings.payment_options.replace(" ", "")
		if self.settings.logo_url:
			payload["customizations"]["logo"] = self.settings.logo_url
		data = self.http("POST", "/v3/payments", json=payload, headers=self._auth())
		link = ((data or {}).get("data") or {}).get("link")
		if data.get("status") != "success" or not link:
			raise ProviderError(f"Flutterwave refused the payment: {self.error_message(data)}", payload=data)
		return {
			"provider_request_id": txn.name,  # Flutterwave tracks us by tx_ref
			"redirect_url": link,
			"raw": {"status": data.get("status")},
			"next_action": {"type": "redirect", "url": link},
		}

	def _inline(self, txn, public_key: str) -> dict:
		"""Inline checkout (Flutterwave's modal over our page). The browser opens it
		with our tx_ref; the amount is confirmed against the transaction when the
		status is read back, so a tampered amount is never accepted."""
		return {
			"provider_request_id": txn.name,  # Flutterwave tracks us by tx_ref
			"raw": {"mode": "inline"},
			"next_action": {
				"type": "flutterwave_inline", "public_key": public_key, "tx_ref": txn.name,
				"amount": round(float(txn.amount), 2), "currency": (txn.currency or "").upper(),
				"payment_options": (self.settings.payment_options or "").replace(" ", "") or None,
				"customer": {"email": txn.email, "phone_number": txn.phone or "", "name": txn.get("payer_name") or ""},
				"customizations": {
					"title": frappe.db.get_single_value("Payment Integration Settings", "organization_name") or "Payment",
					"description": (txn.description or "Payment")[:100],
					"logo": self.settings.logo_url or None,
				},
				"meta": {"transaction": txn.name},
			},
		}

	def query_status(self, txn):
		try:
			data = self.http("GET", "/v3/transactions/verify_by_reference", params={"tx_ref": txn.name},
							 headers=self._auth())
		except ProviderError as e:
			if "No transaction was found" in str(e):
				return ProviderResult(status=PENDING, reason="Payer has not completed checkout yet")
			raise
		t = (data or {}).get("data") or {}
		raw = {k: t.get(k) for k in ("id", "tx_ref", "flw_ref", "status", "amount", "currency", "payment_type")}
		status = str(t.get("status") or "").lower()
		if status == "successful":
			return ProviderResult(status=SUCCESSFUL, receipt=t.get("flw_ref") or str(t.get("id") or ""),
								  amount=float(t["amount"]) if t.get("amount") is not None else None,
								  currency=t.get("currency"), payment_method=t.get("payment_type"), raw=raw)
		if status == "failed":
			return ProviderResult(status=FAILED, reason=t.get("processor_response") or "Payment failed", raw=raw)
		return ProviderResult(status=PENDING, raw=raw)

	def authenticate_callback(self, params, raw_body, headers) -> bool:
		if not super().authenticate_callback(params, raw_body, headers):
			return False
		expected = self.settings.get_password("secret_hash") or ""
		given = (headers.get("verif-hash") or headers.get("Verif-Hash") or "") if headers else ""
		return bool(expected) and hmac.compare_digest(expected, given)

	def parse_callback(self, params, body):
		data = (body or {}).get("data") or {}
		return CallbackInfo(merchant_reference=data.get("tx_ref"), ack={"received": True})
