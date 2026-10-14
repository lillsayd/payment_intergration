"""Pesapal API 3.0 — hosted checkout (cards, M-Pesa, Airtel, bank).

Flow: SubmitOrderRequest → redirect payer to Pesapal → Pesapal sends an IPN
(GET) and redirects the payer back to `return_url`. IPNs are unsigned, so the
status is always read with GetTransactionStatus before it is trusted.
"""

from payment_integration.payment_integration.gateways.base import (
	FAILED,
	PENDING,
	SUCCESSFUL,
	CallbackInfo,
	PaymentGateway,
	ProviderError,
	ProviderResult,
)

# GetTransactionStatus status_code → normalised status.
# 0 = INVALID (not paid yet / abandoned), 1 = COMPLETED, 2 = FAILED, 3 = REVERSED.
STATUS_CODES = {0: PENDING, 1: SUCCESSFUL, 2: FAILED, 3: FAILED}


class PesapalGateway(PaymentGateway):
	key = "Pesapal"
	live_url = "https://pay.pesapal.com/v3"
	sandbox_url = "https://cybqa.pesapal.com/pesapalv3"
	token_ttl_margin = 30  # tokens live 5 minutes

	_json_headers = {"Accept": "application/json", "Content-Type": "application/json"}

	def error_message(self, data: dict) -> str:
		err = data.get("error") if isinstance(data, dict) else None
		if isinstance(err, dict):
			return str(err.get("message") or err.get("code") or err)[:300]
		return str(data.get("message") or err or data)[:300]

	def _check(self, data: dict, what: str) -> dict:
		"""Pesapal reports some errors with HTTP 200 and an `error` object."""
		if data.get("error") or str(data.get("status", "200")) not in ("200", ""):
			raise ProviderError(f"Pesapal {what} failed: {self.error_message(data)}", payload=data)
		return data

	def fetch_access_token(self):
		data = self._check(self.http(
			"POST", "/api/Auth/RequestToken",
			json={
				"consumer_key": self.settings.consumer_key or "",
				"consumer_secret": self.secret("consumer_secret"),
			},
			headers=self._json_headers,
		), "authentication")
		if not data.get("token"):
			raise ProviderError("Pesapal did not return an access token")
		return data["token"], 300

	def _headers(self):
		return {**self._json_headers, "Authorization": f"Bearer {self.get_access_token()}"}

	def register_ipn(self, force: bool = False) -> str:
		"""Register our IPN URL once; re-register if it changed."""
		import frappe

		url = self.callback_url()
		if not force and self.settings.ipn_id and self.settings.ipn_url == url:
			return self.settings.ipn_id
		data = self._check(self.http(
			"POST", "/api/URLSetup/RegisterIPN",
			json={"url": url, "ipn_notification_type": "GET"},
			headers=self._headers(),
		), "IPN registration")
		ipn_id = data.get("ipn_id")
		if not ipn_id:
			raise ProviderError("Pesapal did not return an IPN id")
		self.settings.set_values({"ipn_id": ipn_id, "ipn_url": url})
		return ipn_id

	def initiate(self, txn, *, return_url=None):
		if not return_url:
			raise ProviderError("A return URL is required for Pesapal checkout")
		ipn_id = self.register_ipn()
		first, _, last = (txn.get("payer_name") or "").partition(" ")
		payload = {
			"id": txn.merchant_reference,
			"currency": txn.currency,
			"amount": round(float(txn.amount), 2),
			"description": (txn.description or "Payment")[:100],
			"callback_url": return_url,
			"notification_id": ipn_id,
			"billing_address": {
				"email_address": txn.email or "",
				"phone_number": txn.phone or "",
				"country_code": self.settings.country_code or "KE",
				"first_name": first,
				"last_name": last,
			},
		}
		data = self._check(self.http("POST", "/api/Transactions/SubmitOrderRequest",
									 json=payload, headers=self._headers()), "order submission")
		if not data.get("redirect_url") or not data.get("order_tracking_id"):
			raise ProviderError("Pesapal did not return a checkout URL", payload=data)
		return {
			"provider_request_id": data["order_tracking_id"],
			"redirect_url": data["redirect_url"],
			"raw": data,
			"next_action": {"type": "redirect", "url": data["redirect_url"]},
		}

	def query_status(self, txn):
		if not txn.provider_request_id:
			return ProviderResult(status=PENDING, reason="No OrderTrackingId yet")
		data = self.http("GET", "/api/Transactions/GetTransactionStatus",
						 params={"orderTrackingId": txn.provider_request_id}, headers=self._headers())
		if isinstance(data.get("error"), dict) and data["error"].get("code"):
			raise ProviderError(f"Pesapal status check failed: {self.error_message(data)}",
								retryable=True, payload=data)
		try:
			code = int(data.get("status_code"))
		except (TypeError, ValueError):
			code = 0
		status = STATUS_CODES.get(code, PENDING)
		desc = data.get("payment_status_description")
		reason = None
		if status == FAILED:
			reason = data.get("description") or desc
			if code == 3:
				reason = f"Reversed: {reason or 'payment was reversed'}"
		amount = data.get("amount")
		return ProviderResult(
			status=status,
			receipt=data.get("confirmation_code") or None,
			reason=reason,
			amount=float(amount) if amount not in (None, "") else None,
			currency=data.get("currency"),
			payment_method=data.get("payment_method"),
			raw=data,
		)

	def parse_callback(self, params, body):
		src = {**(body or {}), **(params or {})}
		tracking = src.get("OrderTrackingId")
		ref = src.get("OrderMerchantReference")
		return CallbackInfo(
			provider_request_id=tracking,
			merchant_reference=ref,
			ack={
				"orderNotificationType": src.get("OrderNotificationType") or "IPNCHANGE",
				"orderTrackingId": tracking,
				"orderMerchantReference": ref,
				"status": 200,
			},
		)
