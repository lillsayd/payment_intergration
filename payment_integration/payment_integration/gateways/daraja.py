"""Safaricom Daraja — Lipa Na M-Pesa Online (STK Push).

Flow: STK push → customer enters PIN on phone → Safaricom POSTs a callback.
Callbacks are unsigned, so success is only accepted after an STK Push Query
confirms it; the receipt number (only present in the callback) is kept.
"""

import base64
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from payment_integration.payment_integration.gateways.base import (
	CANCELLED,
	EXPIRED,
	FAILED,
	PENDING,
	SUCCESSFUL,
	CallbackInfo,
	PaymentGateway,
	ProviderError,
	ProviderResult,
)

# STK result codes → normalised status. Anything else non-zero is a failure.
RESULT_STATUS = {
	"0": SUCCESSFUL,
	"1032": CANCELLED,  # request cancelled by user
	"1037": EXPIRED,  # user unreachable / no response from handset
	"1019": EXPIRED,  # transaction expired
}
# Query while the customer has not finished yet.
PROCESSING_ERROR_CODES = {"500.001.1001"}
# Query result codes that mean "not decided yet" (seen live: 4999 "The transaction is still under processing").
PENDING_RESULT_CODES = {"4999"}


def normalize_phone(phone: str) -> str:
	"""07XX/01XX/+2547XX/2547XX → 2547XXXXXXXX (12 digits)."""
	digits = re.sub(r"\D", "", phone or "")
	if digits.startswith("0") and len(digits) == 10:
		digits = "254" + digits[1:]
	elif len(digits) == 9 and digits[0] in "17":
		digits = "254" + digits
	if not re.fullmatch(r"254[17]\d{8}", digits):
		raise ProviderError("Enter a valid Safaricom number, e.g. 0712 345 678")
	return digits


class DarajaGateway(PaymentGateway):
	key = "Daraja"
	live_url = "https://api.safaricom.co.ke"
	sandbox_url = "https://sandbox.safaricom.co.ke"

	def validate_settings(self):
		import frappe

		if (self.settings.currency or "KES") != "KES":
			frappe.throw("M-Pesa (Daraja) only supports KES")

	def error_message(self, data: dict) -> str:
		return str(data.get("errorMessage") or data.get("ResultDesc") or data.get("raw") or data)[:300]

	def fetch_access_token(self):
		try:
			data = self.http(
				"GET", "/oauth/v1/generate",
				params={"grant_type": "client_credentials"},
				auth=(self.settings.consumer_key or "", self.secret("consumer_secret")),
			)
		except ProviderError as e:
			if "HTTP 400" in str(e) or "HTTP 401" in str(e):
				# Safaricom answers bad credentials with an empty 400.
				raise ProviderError(
					"Safaricom rejected the Consumer Key/Secret. Copy them from your app on "
					f"developer.safaricom.co.ke ({'production' if self.is_live else 'sandbox'} app)."
				) from None
			raise
		token = data.get("access_token")
		if not token:
			raise ProviderError("Daraja did not return an access token")
		return token, int(data.get("expires_in") or 3599)

	def _auth_headers(self):
		return {"Authorization": f"Bearer {self.get_access_token()}"}

	def _password(self):
		"""Daraja expects the timestamp in Kenyan local time."""
		ts = datetime.now(ZoneInfo("Africa/Nairobi")).strftime("%Y%m%d%H%M%S")
		shortcode = self.settings.shortcode
		raw = f"{shortcode}{self.secret('passkey')}{ts}".encode()
		return base64.b64encode(raw).decode(), ts

	def initiate(self, txn, *, return_url=None):
		amount = float(txn.amount)
		if amount < 1 or amount != int(amount):
			raise ProviderError("M-Pesa amounts must be whole shillings (at least KES 1)")
		phone = normalize_phone(txn.phone)
		password, ts = self._password()
		tx_type = self.settings.transaction_type or "CustomerPayBillOnline"
		party_b = self.settings.shortcode
		if tx_type == "CustomerBuyGoodsOnline" and self.settings.party_b:
			party_b = self.settings.party_b
		payload = {
			"BusinessShortCode": self.settings.shortcode,
			"Password": password,
			"Timestamp": ts,
			"TransactionType": tx_type,
			"Amount": int(amount),
			"PartyA": phone,
			"PartyB": party_b,
			"PhoneNumber": phone,
			"CallBackURL": self.callback_url(),
			"AccountReference": txn.name[-12:],  # max 12 chars, shown on the prompt
			"TransactionDesc": (txn.description or "Payment")[:13],
		}
		data = self.http("POST", "/mpesa/stkpush/v1/processrequest", json=payload,
						 headers=self._auth_headers())
		if str(data.get("ResponseCode")) != "0":
			raise ProviderError(f"M-Pesa refused the request: {self.error_message(data)}", payload=data)
		return {
			"provider_request_id": data.get("CheckoutRequestID"),
			"provider_merchant_request_id": data.get("MerchantRequestID"),
			"phone": phone,
			"raw": data,
			"next_action": {
				"type": "stk_push",
				"message": data.get("CustomerMessage")
				or "Check your phone and enter your M-Pesa PIN to complete the payment.",
			},
		}

	def query_status(self, txn):
		if not txn.provider_request_id:
			return ProviderResult(status=PENDING, reason="No CheckoutRequestID yet")
		password, ts = self._password()
		try:
			data = self.http(
				"POST", "/mpesa/stkpushquery/v1/query",
				json={
					"BusinessShortCode": self.settings.shortcode,
					"Password": password,
					"Timestamp": ts,
					"CheckoutRequestID": txn.provider_request_id,
				},
				headers=self._auth_headers(),
			)
		except ProviderError as e:
			if str((e.payload or {}).get("errorCode")) in PROCESSING_ERROR_CODES:
				return ProviderResult(status=PENDING, reason="Customer has not completed the prompt yet",
									  raw=e.payload or {})
			raise
		code = str(data.get("ResultCode", "")).strip()
		if code == "" or code in PENDING_RESULT_CODES or "under processing" in str(data.get("ResultDesc", "")).lower():
			return ProviderResult(status=PENDING, reason=data.get("ResultDesc"), raw=data)
		return ProviderResult(
			status=RESULT_STATUS.get(code, FAILED),
			reason=None if code == "0" else data.get("ResultDesc"),
			raw=data,
		)

	def parse_callback(self, params, body):
		cb = ((body or {}).get("Body") or {}).get("stkCallback") or {}
		meta = {
			i.get("Name"): i.get("Value")
			for i in ((cb.get("CallbackMetadata") or {}).get("Item") or [])
			if isinstance(i, dict)
		}
		amount = meta.get("Amount")
		return CallbackInfo(
			provider_request_id=cb.get("CheckoutRequestID"),
			receipt=meta.get("MpesaReceiptNumber"),
			amount=float(amount) if amount is not None else None,
			ack={"ResultCode": 0, "ResultDesc": "Accepted"},
		)
