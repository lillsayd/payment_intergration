"""Provider callbacks (server-to-server, unauthenticated by design).

Each URL carries `cb` (public account id) and `token` (per-account secret).
Bodies are never trusted for status: they only point at a transaction whose
status is then re-read from the gateway (see services.payments.handle_callback).
"""

import frappe

from payment_integration.payment_integration.services import payments


def _respond(body: dict, status: int = 200):
	# Top-level JSON (not wrapped in "message") — Pesapal checks these keys.
	frappe.local.response.update(body)
	frappe.local.response["http_status_code"] = status


def _handle(provider: str):
	params = {k: v for k, v in frappe.form_dict.items() if k not in ("cmd", "token")}
	body = {}
	if frappe.request and frappe.request.method == "POST":
		body = frappe.request.get_json(silent=True) or {}
	acc = payments.account_for_callback(
		frappe.form_dict.get("cb"), frappe.form_dict.get("token"), frappe.local.request_ip
	)
	if not acc or acc.provider != provider:
		return _respond({"status": 403, "message": "Forbidden"}, 403)
	try:
		ack = payments.handle_callback(acc, params, body)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), f"{provider} callback failed")
		# Non-2xx makes the provider retry later; reconciliation also catches it.
		return _respond({"status": 500, "message": "Error"}, 500)
	return _respond(ack)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def daraja(**kwargs):
	"""Daraja STK Push result callback."""
	_handle("Daraja")


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def pesapal(**kwargs):
	"""Pesapal IPN (registered as GET)."""
	_handle("Pesapal")
