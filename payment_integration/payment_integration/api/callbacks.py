"""Gateway callbacks (server-to-server). One endpoint per gateway.

Each is authenticated by the gateway's adapter (URL token, plus Stripe's
signature) and the optional IP allowlist. Bodies never set a status directly:
they only point at a transaction whose status is then re-read from the
gateway (services.payments.handle_callback).
"""

import json

import frappe

from payment_integration.payment_integration.services import payments


def _respond(body: dict, status: int = 200):
	# Top-level JSON (not wrapped in "message") — Pesapal checks these keys.
	frappe.local.response.update(body)
	frappe.local.response["http_status_code"] = status


def _handle(gateway: str):
	params = {k: v for k, v in frappe.form_dict.items() if k != "cmd"}
	raw_body, body = b"", {}
	if frappe.request and frappe.request.method == "POST":
		raw_body = frappe.request.get_data() or b""
		try:
			body = json.loads(raw_body or b"{}")
		except ValueError:
			body = {}
	headers = frappe.request.headers if frappe.request else {}
	try:
		ack = payments.handle_callback(gateway, params, body, raw_body=raw_body, headers=headers,
									   ip=frappe.local.request_ip)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), f"{gateway} callback failed")
		# Non-2xx makes the gateway retry later; reconciliation also catches it.
		return _respond({"status": 500, "message": "Error"}, 500)
	if ack is None:
		return _respond({"status": 403, "message": "Forbidden"}, 403)
	return _respond(ack)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def daraja(**kwargs):
	"""Daraja STK Push result callback."""
	_handle("Daraja")


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def pesapal(**kwargs):
	"""Pesapal IPN (registered as GET)."""
	_handle("Pesapal")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def stripe(**kwargs):
	"""Stripe webhook (checkout.session.* events)."""
	_handle("Stripe")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def paypal(**kwargs):
	"""PayPal webhook (CHECKOUT.ORDER.APPROVED, PAYMENT.CAPTURE.*)."""
	_handle("PayPal")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def flutterwave(**kwargs):
	"""Flutterwave webhook (charge.completed)."""
	_handle("Flutterwave")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def paystack(**kwargs):
	"""Paystack webhook (charge.success)."""
	_handle("Paystack")

