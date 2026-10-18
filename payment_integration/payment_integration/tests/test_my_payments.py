"""My payments: a payer sees only their own transactions.

    bench --site <test-site> run-tests --module payment_integration.payment_integration.tests.test_my_payments
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from payment_integration.payment_integration.api import payments as api

D = "@my-payments-test.example.com"


class TestMyPayments(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._cleanup()
		for key in ("payer", "other"):
			frappe.get_doc({"doctype": "User", "email": f"{key}{D}", "first_name": key, "send_welcome_email": 0}).insert(
				ignore_permissions=True)
		for payer, status, amount in ((f"payer{D}", "Successful", 1500), (f"payer{D}", "Failed", 500), (f"other{D}", "Successful", 900)):
			frappe.get_doc({"doctype": "Payment Transaction", "payer": payer, "status": status, "amount": amount,
							"currency": "KES", "provider": "Daraja", "description": f"Test {amount}"}).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.request = None
		self._cleanup()

	def _cleanup(self):
		frappe.db.delete("Payment Transaction", {"payer": ("like", f"%{D}")})
		for name in frappe.get_all("User", {"name": ("like", f"%{D}")}, pluck="name"):
			frappe.delete_doc("User", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def call(self, user, **kwargs):
		frappe.set_user(user)
		frappe.local.request = frappe._dict(headers={}, method="POST", path="/")
		frappe.local.response = frappe._dict()
		try:
			api.my_payments(**kwargs)
		finally:
			frappe.local.request = None
			frappe.set_user("Administrator")
		return frappe.local.response.get("http_status_code"), frappe.local.response.get("data")

	def test_only_own_payments_with_totals(self):
		status, data = self.call(f"payer{D}")
		self.assertEqual(status, 200)
		self.assertEqual(sorted(p["amount"] for p in data["payments"]), [500, 1500])
		self.assertEqual(data["paid_totals"], [{"currency": "KES", "total": 1500}])
		self.assertTrue(all("redirect_url" not in p for p in data["payments"]))
		status, data = self.call(f"payer{D}", status="Failed")
		self.assertEqual([p["amount"] for p in data["payments"]], [500])

	def test_guest_refused(self):
		# jwt_required turns guests away before the endpoint runs.
		try:
			status, _ = self.call("Guest")
		except frappe.AuthenticationError:
			status = 401
		self.assertEqual(status, 401)


class TestSettingsTrim(FrappeTestCase):
	def test_gateway_values_are_trimmed(self):
		from payment_integration.payment_integration.gateways import settings_for

		doc = frappe.get_doc("Payment Integration Settings")
		doc.daraja_shortcode = " 174379 "
		cfg = settings_for("Daraja", doc=doc)
		self.assertEqual(cfg.shortcode, "174379")
		self.assertEqual(cfg.get("shortcode"), "174379")
