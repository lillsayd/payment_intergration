"""Payment integration tests. Gateway HTTP is mocked; nothing leaves the box.

    bench --site <test-site> run-tests --module payment_integration.payment_integration.tests.test_payments

Services commit internally, so fixtures are removed explicitly in tearDown.
"""

import base64
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from payment_integration.payment_integration import tasks
from payment_integration.payment_integration.api import callbacks
from payment_integration.payment_integration.gateways import settings_for
from payment_integration.payment_integration.tests import gateway_fixtures
from payment_integration.payment_integration.gateways.base import redact
from payment_integration.payment_integration.gateways.daraja import normalize_phone
from payment_integration.payment_integration.services import payments

DARAJA = "Daraja"
PESAPAL = "Pesapal"
STRIPE = "Stripe"
PAYPAL, FLUTTERWAVE, PAYSTACK = "PayPal", "Flutterwave", "Paystack"
FULFILLED = []


def record_success(transaction):
	FULFILLED.append(transaction.name)


def explode(transaction):
	raise RuntimeError("downstream app is down")


class FakeResponse:
	def __init__(self, status=200, data=None):
		self.status_code = status
		self._data = data if data is not None else {}
		self.text = json.dumps(self._data)

	def json(self):
		return self._data


class FakeGateway:
	"""Routes mocked requests.request calls by URL path; records each call."""

	def __init__(self):
		self.calls = []
		self.routes = {}

	def on(self, path, status=200, data=None):
		self.routes[path] = FakeResponse(status, data)

	def __call__(self, method, url, **kwargs):
		self.calls.append({"method": method, "url": url, **kwargs})
		for path, resp in self.routes.items():
			if urlsplit(url).path.endswith(path):
				return resp
		raise AssertionError(f"Unexpected gateway call: {method} {url}")

	def last(self, path):
		return [c for c in self.calls if urlsplit(c["url"]).path.endswith(path)][-1]

	def count(self, path):
		return len([c for c in self.calls if urlsplit(c["url"]).path.endswith(path)])


class TestPayments(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._cleanup()
		FULFILLED.clear()
		self.todo = frappe.get_doc({"doctype": "ToDo", "description": "PI-Test thing to pay for"}).insert(
			ignore_permissions=True)
		# Tests write the real gateway settings, so restore them afterwards.
		self.addCleanup(gateway_fixtures.restore, gateway_fixtures.snapshot())
		gateway_fixtures.configure(DARAJA, {
			"enabled": 1, "environment": "Sandbox", "currency": "KES", "display_label": "M-Pesa",
			"callback_base_url": "https://pay.example.com", "callback_ip_allowlist": "",
			"consumer_key": "ck", "consumer_secret": "cs", "shortcode": "174379", "passkey": "passkey123",
			"transaction_type": "CustomerPayBillOnline",
		})
		gateway_fixtures.configure(PESAPAL, {
			"enabled": 1, "environment": "Sandbox", "currency": "KES", "callback_ip_allowlist": "",
			"callback_base_url": "https://pay.example.com", "allowed_return_origins": "http://localhost:3000",
			"consumer_key": "pk", "consumer_secret": "ps", "ipn_id": "", "ipn_url": "",
		})
		gateway_fixtures.configure(STRIPE, {"enabled": 0})
		for key in (PAYPAL, FLUTTERWAVE, PAYSTACK):
			gateway_fixtures.configure(key, {"enabled": 0})

		self.gw = FakeGateway()
		self.gw.on("/oauth/v1/generate", data={"access_token": "dar-token", "expires_in": "3599"})
		self.gw.on("/api/Auth/RequestToken", data={"token": "pes-token", "status": "200", "error": None})
		patcher = patch("payment_integration.payment_integration.gateways.base.requests.request", self.gw)
		patcher.start()
		self.addCleanup(patcher.stop)
		handler = patch.object(payments, "_handler", side_effect=self._handlers)
		handler.start()
		self.addCleanup(handler.stop)
		self.on_success = [f"{__name__}.record_success"]

	def _handlers(self, doctype, event):
		if doctype != "ToDo":
			return []
		return self.on_success if event == "on_success" else []

	def tearDown(self):
		frappe.set_user("Administrator")
		self._cleanup()

	def _cleanup(self):
		names = frappe.get_all("Payment Transaction",
							   filters={"provider": ("in", [DARAJA, PESAPAL, STRIPE, PAYPAL, FLUTTERWAVE, PAYSTACK])},
							   pluck="name")
		if names:
			frappe.db.delete("File", {"attached_to_doctype": "Payment Transaction", "attached_to_name": ("in", names)})
			frappe.db.delete("Payment Transaction Event", {"parent": ("in", names)})
			frappe.db.delete("Payment Transaction", {"name": ("in", names)})
		frappe.db.delete("ToDo", {"description": "PI-Test thing to pay for"})
		frappe.db.commit()

	# ── helpers ──────────────────────────────────────────────────────────
	def _stk_ok(self, checkout="ws_CO_1"):
		self.gw.on("/mpesa/stkpush/v1/processrequest", data={
			"MerchantRequestID": "mr-1", "CheckoutRequestID": checkout,
			"ResponseCode": "0", "CustomerMessage": "Success. Request accepted for processing",
		})

	def _pay_mpesa(self, amount=100, **kw):
		self._stk_ok()
		return payments.create_payment(
			amount=amount, gateway=DARAJA, reference_doctype="ToDo", reference_name=self.todo.name,
			payer="Administrator", phone="0712345678", description="Course fee", **kw)

	def _daraja_callback(self, result_code=0, amount=100, receipt="SGR7ABC123", checkout="ws_CO_1"):
		cb = {"MerchantRequestID": "mr-1", "CheckoutRequestID": checkout,
			  "ResultCode": result_code, "ResultDesc": "ok"}
		if result_code == 0:
			cb["CallbackMetadata"] = {"Item": [
				{"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt},
				{"Name": "PhoneNumber", "Value": 254712345678}]}
		return payments.handle_callback(DARAJA, {"token": self._token(DARAJA)}, {"Body": {"stkCallback": cb}})

	def _token(self, key):
		return settings_for(key).get_password("callback_token")

	def _query(self, code="0", desc="The service request is processed successfully."):
		self.gw.on("/mpesa/stkpushquery/v1/query", data={"ResponseCode": "0", "ResultCode": code, "ResultDesc": desc})

	def _status(self, name):
		return frappe.db.get_value("Payment Transaction", name, "status")

	# ── Daraja ───────────────────────────────────────────────────────────
	def test_phone_normalisation(self):
		for raw in ("0712345678", "+254712345678", "254712345678", "712345678", "0112345678"):
			self.assertRegex(normalize_phone(raw), r"^254[17]\d{8}$")
		with self.assertRaises(Exception):
			normalize_phone("0812345678")

	def test_stk_push_request_shape(self):
		res = self._pay_mpesa()
		self.assertEqual(res["status"], "Pending")
		self.assertEqual(res["next_action"]["type"], "stk_push")
		body = self.gw.last("/mpesa/stkpush/v1/processrequest")["json"]
		self.assertEqual(body["Amount"], 100)
		self.assertEqual(body["PhoneNumber"], "254712345678")
		self.assertLessEqual(len(body["AccountReference"]), 12)
		self.assertLessEqual(len(body["TransactionDesc"]), 13)
		decoded = base64.b64decode(body["Password"]).decode()
		self.assertEqual(decoded, f"174379passkey123{body['Timestamp']}")
		cb = urlsplit(body["CallBackURL"])
		self.assertEqual(cb.netloc, "pay.example.com")
		self.assertNotIn("m-pesa", body["CallBackURL"].lower())  # Daraja rejects such words
		self.assertTrue(cb.path.endswith("api.callbacks.daraja"))
		self.assertEqual(parse_qs(cb.query)["token"][0], self._token(DARAJA))

	def test_fractional_mpesa_amount_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._pay_mpesa(amount=99.5)
		self.assertEqual(self.gw.count("/mpesa/stkpush/v1/processrequest"), 0)

	def test_callback_success_is_confirmed_by_query_and_fulfils_once(self):
		res = self._pay_mpesa()
		self._query("0")
		self._daraja_callback()
		self.assertEqual(self._status(res["name"]), "Successful")
		txn = frappe.get_doc("Payment Transaction", res["name"])
		self.assertEqual(txn.provider_receipt, "SGR7ABC123")
		self.assertEqual(txn.fulfilment_status, "Done")
		self._daraja_callback()  # provider retries the callback
		self.assertEqual(FULFILLED, [res["name"]])

	def test_unconfirmed_callback_keeps_pending_but_saves_receipt(self):
		res = self._pay_mpesa()
		self.gw.on("/mpesa/stkpushquery/v1/query", status=500,
				   data={"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"})
		self._daraja_callback()
		txn = frappe.get_doc("Payment Transaction", res["name"])
		self.assertEqual(txn.status, "Pending")
		self.assertEqual(txn.provider_receipt, "SGR7ABC123")
		self.assertEqual(FULFILLED, [])

	def test_cancelled_by_user(self):
		res = self._pay_mpesa()
		self._query("1032", "Request cancelled by user")
		self._daraja_callback(result_code=1032)
		self.assertEqual(self._status(res["name"]), "Cancelled")

	def test_amount_mismatch_needs_review(self):
		res = self._pay_mpesa()
		self._query("0")
		self._daraja_callback(amount=1)
		txn = frappe.get_doc("Payment Transaction", res["name"])
		self.assertEqual(txn.status, "Pending")
		self.assertIn("Amount mismatch", txn.failure_reason)
		self.assertEqual(FULFILLED, [])

	def test_callback_authentication(self):
		res = self._pay_mpesa()
		body = {"Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_1", "ResultCode": 1032}}}
		self.assertIsNone(payments.handle_callback(DARAJA, {"token": "wrong"}, body))
		self.assertIsNone(payments.handle_callback(DARAJA, {}, body))
		# A token for one gateway does not open another gateway's endpoint.
		self.assertIsNone(payments.handle_callback(PESAPAL, {"token": self._token(DARAJA)}, {}))
		frappe.db.set_single_value("Payment Integration Settings", "daraja_callback_ip_allowlist", "196.201.214.200")
		self.assertIsNone(payments.handle_callback(DARAJA, {"token": self._token(DARAJA)}, body, ip="1.2.3.4"))
		self._query("1032", "Request cancelled by user")
		self.assertIsNotNone(payments.handle_callback(DARAJA, {"token": self._token(DARAJA)}, body,
													  ip="196.201.214.200"))
		self.assertEqual(self._status(res["name"]), "Cancelled")

	def test_forged_callback_endpoint_is_rejected(self):
		res = self._pay_mpesa()
		raw = json.dumps({"Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_1", "ResultCode": 0}}}).encode()
		frappe.local.request = frappe._dict(method="POST", headers={}, get_data=lambda: raw)
		frappe.local.request_ip = "1.2.3.4"
		frappe.local.form_dict = frappe._dict(token="forged")
		frappe.local.response = frappe._dict()
		try:
			callbacks.daraja()
		finally:
			frappe.local.request = None
		self.assertEqual(frappe.local.response.get("http_status_code"), 403)
		self.assertEqual(self._status(res["name"]), "Pending")
		self.assertEqual(self.gw.count("/mpesa/stkpushquery/v1/query"), 0)

	# ── Pesapal ──────────────────────────────────────────────────────────
	def _pesapal_routes(self):
		self.gw.on("/api/URLSetup/RegisterIPN", data={"ipn_id": "ipn-1", "status": "200", "error": None})
		self.gw.on("/api/Transactions/SubmitOrderRequest", data={
			"order_tracking_id": "otid-1", "merchant_reference": "x",
			"redirect_url": "https://cybqa.pesapal.com/iframe?OrderTrackingId=otid-1",
			"error": None, "status": "200"})

	def _pay_pesapal(self):
		self._pesapal_routes()
		return payments.create_payment(
			amount=2500.5, gateway=PESAPAL, reference_doctype="ToDo", reference_name=self.todo.name,
			payer="Administrator", email="admin@example.com", description="Course fee",
			return_url="http://localhost:3000/payments/return")

	def test_pesapal_checkout_registers_ipn_once(self):
		res = self._pay_pesapal()
		self.assertEqual(res["next_action"], {"type": "redirect",
											   "url": "https://cybqa.pesapal.com/iframe?OrderTrackingId=otid-1"})
		order = self.gw.last("/api/Transactions/SubmitOrderRequest")["json"]
		self.assertEqual(order["id"], res["name"])
		self.assertEqual(order["notification_id"], "ipn-1")
		self.assertEqual(order["amount"], 2500.5)
		self.assertEqual(self.gw.last("/api/URLSetup/RegisterIPN")["json"]["ipn_notification_type"], "GET")
		frappe.db.set_value("Payment Transaction", res["name"], "creation", add_to_date(now_datetime(), minutes=-10))
		self._pay_pesapal()
		self.assertEqual(self.gw.count("/api/URLSetup/RegisterIPN"), 1)

	def test_pesapal_rejects_foreign_return_url(self):
		self._pesapal_routes()
		with self.assertRaises(frappe.ValidationError):
			payments.create_payment(amount=10, gateway=PESAPAL, payer="Administrator",
									return_url="https://evil.example/steal")

	def test_pesapal_ipn_completed(self):
		res = self._pay_pesapal()
		self.gw.on("/api/Transactions/GetTransactionStatus", data={
			"status_code": 1, "payment_status_description": "Completed", "confirmation_code": "PSPL-77",
			"payment_method": "Visa", "amount": 2500.5, "currency": "KES", "error": None, "status": "200"})
		ack = payments.handle_callback(PESAPAL, {"token": self._token(PESAPAL), "OrderTrackingId": "otid-1",
												 "OrderMerchantReference": res["name"],
												 "OrderNotificationType": "IPNCHANGE"}, {})
		self.assertEqual(ack, {"orderNotificationType": "IPNCHANGE", "orderTrackingId": "otid-1",
							   "orderMerchantReference": res["name"], "status": 200})
		txn = frappe.get_doc("Payment Transaction", res["name"])
		self.assertEqual((txn.status, txn.provider_receipt, txn.payment_method), ("Successful", "PSPL-77", "Visa"))
		self.assertEqual(FULFILLED, [res["name"]])

	def test_pesapal_pending_and_failed(self):
		res = self._pay_pesapal()
		self.gw.on("/api/Transactions/GetTransactionStatus", data={"status_code": 0,
				   "payment_status_description": "INVALID", "error": None, "status": "200"})
		self.assertEqual(payments.refresh_status(res["name"]), "Pending")
		self.gw.on("/api/Transactions/GetTransactionStatus", data={"status_code": 2,
				   "payment_status_description": "Failed", "description": "Card declined",
				   "error": None, "status": "200"})
		self.assertEqual(payments.refresh_status(res["name"]), "Failed")
		self.assertEqual(frappe.db.get_value("Payment Transaction", res["name"], "failure_reason"), "Card declined")

	# ── service rules ────────────────────────────────────────────────────
	def test_disabled_gateway_refuses_new_payments(self):
		frappe.db.set_single_value("Payment Integration Settings", "daraja_enabled", 0)
		with self.assertRaises(frappe.ValidationError):
			self._pay_mpesa()
		keys = [a["gateway"] for a in payments.enabled_gateways()]
		self.assertNotIn(DARAJA, keys)
		self.assertIn(PESAPAL, keys)
		self.assertNotIn(STRIPE, keys)

	def test_enabled_gateways_expose_no_secrets(self):
		text = json.dumps(payments.enabled_gateways())
		for secret in ("cs", "passkey123", "ps", "callback_token"):
			self.assertNotIn(f'"{secret}"', text)

	def test_double_click_and_idempotency_return_same_payment(self):
		first = self._pay_mpesa()
		second = self._pay_mpesa()
		self.assertEqual(first["name"], second["name"])
		self.assertEqual(self.gw.count("/mpesa/stkpush/v1/processrequest"), 1)
		a = self._pay_mpesa(amount=200, idempotency_key="order-42")
		b = self._pay_mpesa(amount=200, idempotency_key="order-42")
		self.assertEqual(a["name"], b["name"])

	def test_failed_fulfilment_keeps_payment_and_retries(self):
		self.on_success = [f"{__name__}.explode"]
		res = self._pay_mpesa()
		self._query("0")
		self._daraja_callback()
		txn = frappe.get_doc("Payment Transaction", res["name"])
		self.assertEqual((txn.status, txn.fulfilment_status), ("Successful", "Failed"))
		self.on_success = [f"{__name__}.record_success"]
		self.assertEqual(payments.retry_fulfilment(res["name"]), "Done")
		self.assertEqual(FULFILLED, [res["name"]])

	def test_terminal_status_never_changes(self):
		res = self._pay_mpesa()
		self._query("1032")
		payments.refresh_status(res["name"])
		self._query("0")
		self.assertEqual(payments.refresh_status(res["name"]), "Cancelled")

	def test_reconcile_confirms_and_expires(self):
		res = self._pay_mpesa()
		self._query("0")
		tasks.reconcile_pending()
		self.assertEqual(self._status(res["name"]), "Successful")

		self.gw.on("/mpesa/stkpush/v1/processrequest", data={
			"MerchantRequestID": "mr-2", "CheckoutRequestID": "ws_CO_2", "ResponseCode": "0"})
		stale = payments.create_payment(amount=50, gateway=DARAJA, payer="Administrator", phone="0712345678")
		frappe.db.set_value("Payment Transaction", stale["name"], {
			"creation": add_to_date(now_datetime(), hours=-3), "last_checked_at": None})
		self.gw.on("/mpesa/stkpushquery/v1/query", status=500,
				   data={"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"})
		tasks.reconcile_pending()
		self.assertEqual(self._status(stale["name"]), "Expired")

	def test_event_payloads_are_redacted(self):
		data = redact({"Password": "x", "PhoneNumber": "254712345678",
					   "Item": [{"Name": "PhoneNumber", "Value": 254712345678}]})
		self.assertEqual(data["Password"], "***")
		self.assertNotIn("2345", data["PhoneNumber"])
		self.assertNotIn("2345", str(data["Item"][0]["Value"]))

	# ── receipts ─────────────────────────────────────────────────────────
	def _paid(self):
		res = self._pay_mpesa()
		self._query("0")
		self._daraja_callback()
		return frappe.get_doc("Payment Transaction", res["name"])

	def test_success_issues_numbered_protected_receipt(self):
		import io

		from pypdf import PdfReader

		from payment_integration.payment_integration.services import receipts

		txn = self._paid()
		self.assertRegex(txn.receipt_number or "", r"^RCPT-\d{4}-\d{5}$")
		self.assertTrue(txn.receipt_file, "PDF generated after success")
		reader = PdfReader(io.BytesIO(receipts.pdf_bytes(txn.name)))
		self.assertTrue(reader.is_encrypted and reader.decrypt(""))
		text = " ".join(reader.pages[0].extract_text().split())  # pypdf separates words with tabs
		self.assertIn(txn.receipt_number, text)
		self.assertIn("KES 100.00", text)
		self.assertEqual(payments.transaction_view(txn.name)["receipt_number"], txn.receipt_number)

	def test_receipt_download_is_payer_only(self):
		from payment_integration.payment_integration.api import payments as pay_api

		txn = self._paid()
		other = "pi-test-other@example.com"
		if not frappe.db.exists("User", other):
			frappe.get_doc({"doctype": "User", "email": other, "first_name": "Other",
							"send_welcome_email": 0}).insert(ignore_permissions=True)
			frappe.db.commit()
		try:
			frappe.set_user(other)
			frappe.local.request = frappe._dict(headers={}, method="GET", path="/")
			with self.assertRaises(frappe.DoesNotExistError):
				pay_api.download_receipt(transaction=txn.name)
			frappe.set_user("Administrator")
			frappe.local.response = frappe._dict()
			pay_api.download_receipt(transaction=txn.name)
			self.assertTrue(frappe.local.response.filecontent.startswith(b"%PDF"))
		finally:
			frappe.local.request = None
			frappe.set_user("Administrator")
			frappe.delete_doc("User", other, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_receipts_can_be_switched_off(self):
		frappe.db.set_single_value("Payment Integration Settings", "receipts_enabled", 0)
		try:
			txn = self._paid()
			self.assertEqual(txn.status, "Successful")
			self.assertFalse(txn.receipt_number)
		finally:
			frappe.db.set_single_value("Payment Integration Settings", "receipts_enabled", 1)

	# ── Stripe ───────────────────────────────────────────────────────────
	def _stripe_on(self):
		gateway_fixtures.configure(STRIPE, {
			"enabled": 1, "environment": "Test", "currency": "USD", "callback_base_url": "https://pay.example.com",
			"allowed_return_origins": "http://localhost:3000", "secret_key": "sk_test_abc",
			"webhook_secret": "whsec_test", "callback_ip_allowlist": "",
		})
		self.gw.on("/v1/checkout/sessions", data={"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1",
												  "status": "open", "payment_status": "unpaid"})

	def _pay_stripe(self, amount=49.99):
		self._stripe_on()
		return payments.create_payment(amount=amount, currency="USD", gateway=STRIPE,
									   reference_doctype="ToDo", reference_name=self.todo.name,
									   payer="Administrator", email="a@example.com", description="Course fee",
									   return_url="http://localhost:3000/payments/return")

	def _stripe_webhook(self, payload: dict, secret="whsec_test", ts=None, token=None):
		import hashlib
		import hmac as _hmac
		import time as _time

		raw = json.dumps(payload).encode()
		ts = int(ts or _time.time())
		sig = _hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
		return payments.handle_callback(STRIPE, {"token": token or self._token(STRIPE)}, payload, raw_body=raw,
										headers={"Stripe-Signature": f"t={ts},v1={sig}"})

	def test_stripe_checkout_session_request(self):
		res = self._pay_stripe()
		self.assertEqual(res["next_action"], {"type": "redirect", "url": "https://checkout.stripe.com/c/pay/cs_test_1"})
		call = self.gw.last("/v1/checkout/sessions")
		data = call["data"]
		self.assertEqual(data["line_items[0][price_data][unit_amount]"], 4999)
		self.assertEqual(data["line_items[0][price_data][currency]"], "usd")
		self.assertEqual(data["client_reference_id"], res["name"])
		self.assertIn("session_id={CHECKOUT_SESSION_ID}", data["success_url"])
		self.assertIn(f"ref={res['name']}", data["success_url"])
		self.assertEqual(call["headers"]["Idempotency-Key"], f"checkout-{res['name']}")
		self.assertEqual(call["headers"]["Authorization"], "Bearer sk_test_abc")

	def test_stripe_signed_webhook_confirms_payment(self):
		res = self._pay_stripe()
		self.gw.on("/v1/checkout/sessions/cs_test_1", data={
			"id": "cs_test_1", "status": "complete", "payment_status": "paid", "amount_total": 4999,
			"currency": "usd", "payment_intent": "pi_123"})
		event = {"type": "checkout.session.completed", "data": {"object": {"object": "checkout.session", "id": "cs_test_1"}}}
		self.assertEqual(self._stripe_webhook(event), {"received": True})
		txn = frappe.get_doc("Payment Transaction", res["name"])
		self.assertEqual((txn.status, txn.provider_receipt, txn.amount), ("Successful", "pi_123", 49.99))
		self.assertEqual(FULFILLED, [res["name"]])

	def test_stripe_rejects_bad_signature_stale_events_and_missing_token(self):
		res = self._pay_stripe()
		event = {"type": "checkout.session.completed", "data": {"object": {"object": "checkout.session", "id": "cs_test_1"}}}
		self.assertIsNone(self._stripe_webhook(event, secret="whsec_wrong"))
		self.assertIsNone(self._stripe_webhook(event, ts=1_000_000_000))
		self.assertIsNone(self._stripe_webhook(event, token="nope"))
		self.assertEqual(self._status(res["name"]), "Pending")

	def test_stripe_key_must_match_mode(self):
		with self.assertRaises(frappe.ValidationError):
			gateway_fixtures.configure(STRIPE, {"environment": "Live", "secret_key": "sk_test_abc"})

	def test_zero_decimal_currency_amounts(self):
		from payment_integration.payment_integration.gateways.stripe import to_minor

		self.assertEqual(to_minor(1500, "UGX"), 1500)
		self.assertEqual(to_minor(12.345, "KWD"), 12350)
		self.assertEqual(to_minor(10.5, "KES"), 1050)

	# ── single settings doc with one tab per gateway ─────────────────────
	def test_each_gateway_tab_is_isolated(self):
		from payment_integration.payment_integration.api import gateways as gw_api

		frappe.local.request = frappe._dict(headers={}, method="GET", path="/")
		try:
			frappe.local.response = frappe._dict()
			gw_api.get_gateway(gateway=STRIPE)
			card = frappe.local.response["data"]
		finally:
			frappe.local.request = None
		names = {f["fieldname"] for s in card["sections"] for f in s["fields"]}
		self.assertIn("secret_key", names)
		self.assertIn("webhook_secret", names)
		self.assertFalse({"passkey", "shortcode", "consumer_key"} & names, "no M-Pesa/Pesapal fields on Stripe")
		self.assertFalse(any(n.startswith("stripe_") for n in names), "frontend sees unprefixed names")
		self.assertEqual(card["values"]["secret_key"], {"is_set": False})

	def test_saving_one_gateway_does_not_touch_another(self):
		from payment_integration.payment_integration.api import gateways as gw_api

		frappe.local.request = frappe._dict(headers={}, method="POST", path="/")
		try:
			frappe.local.response = frappe._dict()
			gw_api.save_gateway(gateway=STRIPE, values={"secret_key": "sk_test_new", "display_label": "Card"})
			self.assertEqual(frappe.local.response.get("http_status_code"), 200)
		finally:
			frappe.local.request = None
		self.assertEqual(settings_for(STRIPE).get_password("secret_key"), "sk_test_new")
		self.assertEqual(settings_for(DARAJA).get_password("passkey"), "passkey123")
		self.assertEqual(settings_for(PESAPAL).get_password("consumer_secret"), "ps")

	# ── PayPal ───────────────────────────────────────────────────────────
	def _pay_paypal(self, amount=49.99):
		gateway_fixtures.configure(PAYPAL, {
			"enabled": 1, "environment": "Sandbox", "currency": "USD", "client_id": "cid", "client_secret": "csec",
			"webhook_id": "WH-1", "allowed_return_origins": "http://localhost:3000", "callback_ip_allowlist": ""})
		self.gw.on("/v1/oauth2/token", data={"access_token": "pp-token", "expires_in": 32400})
		self.gw.on("/v2/checkout/orders", data={"id": "ORDER1", "status": "PAYER_ACTION_REQUIRED", "links": [
			{"rel": "self", "href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/ORDER1"},
			{"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER1"}]})
		return payments.create_payment(amount=amount, currency="USD", gateway=PAYPAL, reference_doctype="ToDo",
									   reference_name=self.todo.name, payer="Administrator", email="a@example.com",
									   description="Course fee", return_url="http://localhost:3000/payments/return")

	def test_paypal_order_request(self):
		res = self._pay_paypal()
		self.assertEqual(res["next_action"]["url"], "https://www.sandbox.paypal.com/checkoutnow?token=ORDER1")
		call = self.gw.last("/v2/checkout/orders")
		unit = call["json"]["purchase_units"][0]
		self.assertEqual(unit["amount"], {"currency_code": "USD", "value": "49.99"})
		self.assertEqual(unit["custom_id"], res["name"])
		self.assertEqual(call["headers"]["PayPal-Request-Id"], f"order-{res['name']}")
		ctx = call["json"]["payment_source"]["paypal"]["experience_context"]
		self.assertIn(f"ref={res['name']}", ctx["return_url"])

	def test_paypal_approved_order_is_captured_once(self):
		res = self._pay_paypal()
		self.gw.on("/v2/checkout/orders/ORDER1", data={"id": "ORDER1", "status": "APPROVED"})
		self.gw.on("/ORDER1/capture", data={"id": "ORDER1", "status": "COMPLETED", "purchase_units": [{"payments": {
			"captures": [{"id": "CAP1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "49.99"}}]}}]})
		self.assertEqual(payments.refresh_status(res["name"]), "Successful")
		capture = self.gw.last("/ORDER1/capture")
		self.assertEqual(capture["headers"]["PayPal-Request-Id"], f"capture-{res['name']}")
		txn = frappe.get_doc("Payment Transaction", res["name"])
		self.assertEqual((txn.provider_receipt, txn.payment_method), ("CAP1", "PayPal"))
		payments.refresh_status(res["name"])  # terminal: no second capture
		self.assertEqual(self.gw.count("/ORDER1/capture"), 1)
		self.assertEqual(FULFILLED, [res["name"]])

	def _paypal_webhook(self, verification):
		self.gw.on("/v1/notifications/verify-webhook-signature", data={"verification_status": verification})
		event = {"event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": "ORDER1"}}
		headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://api.paypal.com/cert",
				   "PAYPAL-TRANSMISSION-ID": "t1", "PAYPAL-TRANSMISSION-SIG": "sig", "PAYPAL-TRANSMISSION-TIME": "now"}
		return payments.handle_callback(PAYPAL, {"token": self._token(PAYPAL)}, event,
										raw_body=json.dumps(event).encode(), headers=headers)

	def test_paypal_webhook_is_verified_with_paypal(self):
		res = self._pay_paypal()
		self.assertIsNone(self._paypal_webhook("FAILURE"))
		self.gw.on("/v2/checkout/orders/ORDER1", data={"id": "ORDER1", "status": "COMPLETED", "purchase_units": [
			{"payments": {"captures": [{"id": "CAP1", "status": "COMPLETED",
										"amount": {"currency_code": "USD", "value": "49.99"}}]}}]})
		self.assertEqual(self._paypal_webhook("SUCCESS"), {"received": True})
		self.assertEqual(self._status(res["name"]), "Successful")
		sent = self.gw.last("/v1/notifications/verify-webhook-signature")["json"]
		self.assertEqual((sent["webhook_id"], sent["transmission_id"]), ("WH-1", "t1"))

	def test_paypal_rejects_kes(self):
		with self.assertRaises(frappe.ValidationError):
			gateway_fixtures.configure(PAYPAL, {"currency": "KES"})

	# ── Flutterwave ──────────────────────────────────────────────────────
	def _pay_flutterwave(self):
		gateway_fixtures.configure(FLUTTERWAVE, {
			"enabled": 1, "environment": "Test", "currency": "KES", "secret_key": "FLWSECK_TEST-abc",
			"secret_hash": "hash-123", "payment_options": "card, mpesa",
			"allowed_return_origins": "http://localhost:3000", "callback_ip_allowlist": ""})
		self.gw.on("/v3/payments", data={"status": "success", "data": {"link": "https://checkout.flutterwave.com/v3/hosted/pay/abc"}})
		return payments.create_payment(amount=1500, currency="KES", gateway=FLUTTERWAVE, reference_doctype="ToDo",
									   reference_name=self.todo.name, payer="Administrator", email="a@example.com",
									   phone="0712345678", return_url="http://localhost:3000/payments/return")

	def test_flutterwave_checkout_and_verified_webhook(self):
		res = self._pay_flutterwave()
		body = self.gw.last("/v3/payments")["json"]
		self.assertEqual((body["tx_ref"], body["amount"], body["currency"]), (res["name"], 1500, "KES"))
		self.assertEqual(body["payment_options"], "card,mpesa")
		self.assertEqual(self.gw.last("/v3/payments")["headers"]["Authorization"], "Bearer FLWSECK_TEST-abc")
		event = {"event": "charge.completed", "data": {"tx_ref": res["name"], "status": "successful"}}
		token = {"token": self._token(FLUTTERWAVE)}
		self.assertIsNone(payments.handle_callback(FLUTTERWAVE, token, event, headers={"verif-hash": "wrong"}))
		self.gw.on("/v3/transactions/verify_by_reference", data={"status": "success", "data": {
			"id": 99, "tx_ref": res["name"], "flw_ref": "FLW-MOCK-1", "status": "successful", "amount": 1500,
			"currency": "KES", "payment_type": "mpesa"}})
		self.assertEqual(payments.handle_callback(FLUTTERWAVE, token, event, headers={"verif-hash": "hash-123"}),
						 {"received": True})
		txn = frappe.get_doc("Payment Transaction", res["name"])
		self.assertEqual((txn.status, txn.provider_receipt, txn.payment_method), ("Successful", "FLW-MOCK-1", "mpesa"))

	def test_flutterwave_key_must_match_mode(self):
		with self.assertRaises(frappe.ValidationError):
			gateway_fixtures.configure(FLUTTERWAVE, {"environment": "Live", "secret_key": "FLWSECK_TEST-abc"})

	# ── Paystack ─────────────────────────────────────────────────────────
	def _pay_paystack(self):
		gateway_fixtures.configure(PAYSTACK, {
			"enabled": 1, "environment": "Test", "currency": "KES", "secret_key": "sk_test_ps",
			"allowed_return_origins": "http://localhost:3000", "callback_ip_allowlist": ""})
		self.gw.on("/transaction/initialize", data={"status": True, "data": {
			"authorization_url": "https://checkout.paystack.com/xyz", "access_code": "xyz", "reference": "r"}})
		return payments.create_payment(amount=1500.5, currency="KES", gateway=PAYSTACK, reference_doctype="ToDo",
									   reference_name=self.todo.name, payer="Administrator", email="a@example.com",
									   return_url="http://localhost:3000/payments/return")

	def test_paystack_checkout_and_signed_webhook(self):
		import hashlib
		import hmac as _hmac

		res = self._pay_paystack()
		body = self.gw.last("/transaction/initialize")["json"]
		self.assertEqual((body["reference"], body["amount"], body["email"]), (res["name"], 150050, "a@example.com"))
		event = {"event": "charge.success", "data": {"reference": res["name"]}}
		raw = json.dumps(event).encode()
		good = _hmac.new(b"sk_test_ps", raw, hashlib.sha512).hexdigest()
		token = {"token": self._token(PAYSTACK)}
		self.assertIsNone(payments.handle_callback(PAYSTACK, token, event, raw_body=raw,
												   headers={"x-paystack-signature": "bad"}))
		self.gw.on(f"/transaction/verify/{res['name']}", data={"status": True, "data": {
			"id": 5551, "reference": res["name"], "status": "success", "amount": 150050, "currency": "KES",
			"channel": "mobile_money"}})
		self.assertEqual(payments.handle_callback(PAYSTACK, token, event, raw_body=raw,
												  headers={"x-paystack-signature": good}), {"received": True})
		txn = frappe.get_doc("Payment Transaction", res["name"])
		self.assertEqual((txn.status, txn.amount, txn.payment_method), ("Successful", 1500.5, "mobile_money"))

	def test_paystack_abandoned_stays_pending(self):
		res = self._pay_paystack()
		self.gw.on(f"/transaction/verify/{res['name']}", data={"status": True, "data": {
			"reference": res["name"], "status": "abandoned", "amount": 150050, "currency": "KES"}})
		self.assertEqual(payments.refresh_status(res["name"]), "Pending")

