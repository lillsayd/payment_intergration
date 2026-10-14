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
from payment_integration.payment_integration.providers import clear_token_cache
from payment_integration.payment_integration.providers.base import redact
from payment_integration.payment_integration.providers.daraja import normalize_phone
from payment_integration.payment_integration.services import payments

DARAJA = "PI-Test M-Pesa"
PESAPAL = "PI-Test Pesapal"
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
		frappe.get_doc({
			"doctype": "Payment Provider Account", "account_name": DARAJA, "provider": "Daraja",
			"enabled": 1, "environment": "Sandbox", "currency": "KES", "is_default": 1,
			"callback_base_url": "https://pay.example.com",
			"daraja_consumer_key": "ck", "daraja_consumer_secret": "cs",
			"daraja_shortcode": "174379", "daraja_passkey": "passkey123",
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Payment Provider Account", "account_name": PESAPAL, "provider": "Pesapal",
			"enabled": 1, "environment": "Sandbox", "currency": "KES",
			"callback_base_url": "https://pay.example.com",
			"allowed_return_origins": "http://localhost:3000",
			"pesapal_consumer_key": "pk", "pesapal_consumer_secret": "ps",
		}).insert(ignore_permissions=True)
		frappe.db.commit()
		for acc in (DARAJA, PESAPAL):
			clear_token_cache(acc)

		self.gw = FakeGateway()
		self.gw.on("/oauth/v1/generate", data={"access_token": "dar-token", "expires_in": "3599"})
		self.gw.on("/api/Auth/RequestToken", data={"token": "pes-token", "status": "200", "error": None})
		patcher = patch("payment_integration.payment_integration.providers.base.requests.request", self.gw)
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
							   filters={"provider_account": ("in", [DARAJA, PESAPAL])}, pluck="name")
		if names:
			frappe.db.delete("Payment Transaction Event", {"parent": ("in", names)})
			frappe.db.delete("Payment Transaction", {"name": ("in", names)})
		frappe.db.delete("Payment Provider Account", {"name": ("in", [DARAJA, PESAPAL])})
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
			amount=amount, account=DARAJA, reference_doctype="ToDo", reference_name=self.todo.name,
			payer="Administrator", phone="0712345678", description="Course fee", **kw)

	def _daraja_callback(self, result_code=0, amount=100, receipt="SGR7ABC123", checkout="ws_CO_1"):
		cb = {"MerchantRequestID": "mr-1", "CheckoutRequestID": checkout,
			  "ResultCode": result_code, "ResultDesc": "ok"}
		if result_code == 0:
			cb["CallbackMetadata"] = {"Item": [
				{"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt},
				{"Name": "PhoneNumber", "Value": 254712345678}]}
		acc = frappe.get_doc("Payment Provider Account", DARAJA)
		return payments.handle_callback(acc, {}, {"Body": {"stkCallback": cb}})

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
		acc = frappe.get_doc("Payment Provider Account", DARAJA)
		self.assertEqual(parse_qs(cb.query)["token"][0], acc.get_password("callback_token"))

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
		acc = frappe.get_doc("Payment Provider Account", DARAJA)
		token = acc.get_password("callback_token")
		self.assertIsNotNone(payments.account_for_callback(acc.callback_id, token, "1.2.3.4"))
		self.assertIsNone(payments.account_for_callback(acc.callback_id, "wrong", "1.2.3.4"))
		self.assertIsNone(payments.account_for_callback("nope", token, "1.2.3.4"))
		frappe.db.set_value("Payment Provider Account", DARAJA, "callback_ip_allowlist", "196.201.214.200")
		self.assertIsNone(payments.account_for_callback(acc.callback_id, token, "1.2.3.4"))
		self.assertIsNotNone(payments.account_for_callback(acc.callback_id, token, "196.201.214.200"))

	def test_forged_callback_endpoint_is_rejected(self):
		res = self._pay_mpesa()
		acc = frappe.get_doc("Payment Provider Account", DARAJA)
		body = {"Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_1", "ResultCode": 0}}}
		frappe.local.request = frappe._dict(method="POST", get_json=lambda silent=True: body)
		frappe.local.request_ip = "1.2.3.4"
		frappe.local.form_dict = frappe._dict(cb=acc.callback_id, token="forged")
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
			amount=2500.5, account=PESAPAL, reference_doctype="ToDo", reference_name=self.todo.name,
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
			payments.create_payment(amount=10, account=PESAPAL, payer="Administrator",
									return_url="https://evil.example/steal")

	def test_pesapal_ipn_completed(self):
		res = self._pay_pesapal()
		self.gw.on("/api/Transactions/GetTransactionStatus", data={
			"status_code": 1, "payment_status_description": "Completed", "confirmation_code": "PSPL-77",
			"payment_method": "Visa", "amount": 2500.5, "currency": "KES", "error": None, "status": "200"})
		acc = frappe.get_doc("Payment Provider Account", PESAPAL)
		ack = payments.handle_callback(acc, {"OrderTrackingId": "otid-1", "OrderMerchantReference": res["name"],
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
	def test_disabled_account_refuses_new_payments(self):
		frappe.db.set_value("Payment Provider Account", DARAJA, "enabled", 0)
		with self.assertRaises(frappe.ValidationError):
			self._pay_mpesa()
		self.assertNotIn(DARAJA, [a["account"] for a in payments.enabled_accounts()])

	def test_enabled_accounts_expose_no_secrets(self):
		text = json.dumps(payments.enabled_accounts())
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
		stale = payments.create_payment(amount=50, account=DARAJA, payer="Administrator", phone="0712345678")
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
