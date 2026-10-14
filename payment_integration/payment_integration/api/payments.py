"""Payer-facing payment API (JWT via common_utils, same envelope as other apps).

The client never sends an amount: it names what it is paying for
(reference_doctype + reference_name) and the owning app prices it through its
`get_payment_details` hook, including the permission check.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from common_utils.api.auth import jwt_required
from common_utils.api.crud_utils import parse_request_data
from common_utils.api.utils import error_response, success_response
from payment_integration.payment_integration.services import payments

MANAGER_ROLES = {"System Manager", "Payments Manager", "Administrator"}
STATUS_RECHECK_SECONDS = 10


def _is_manager() -> bool:
	return bool(MANAGER_ROLES.intersection(frappe.get_roles()))


def _errors(fn_name: str, e: Exception):
	frappe.db.rollback()
	if isinstance(e, frappe.AuthenticationError):
		return error_response(str(e), http_status_code=401)
	if isinstance(e, frappe.PermissionError):
		return error_response(str(e), http_status_code=403)
	if isinstance(e, frappe.DoesNotExistError):
		return error_response(str(e), http_status_code=404)
	if isinstance(e, frappe.ValidationError):
		return error_response(frappe.utils.strip_html(str(e)), http_status_code=400)
	frappe.log_error(frappe.get_traceback(), f"payment_integration.api.payments.{fn_name}")
	return error_response(_("Payment request failed"), http_status_code=500)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@jwt_required
def list_providers(**kwargs):
	"""Enabled checkout options (no credentials)."""
	try:
		return success_response(data={"providers": payments.enabled_accounts()})
	except Exception as e:
		return _errors("list_providers", e)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@jwt_required
@rate_limit(limit=10, seconds=60)
def initiate(**kwargs):
	"""POST {reference_doctype, reference_name, account?|provider?, phone?, return_url?, idempotency_key?}"""
	try:
		user = frappe.session.user
		if user == "Guest":
			frappe.throw(_("Authentication required"), frappe.AuthenticationError)
		data = parse_request_data(kwargs)
		ref_dt = (data.get("reference_doctype") or "").strip()
		ref_name = (data.get("reference_name") or "").strip()
		if not ref_dt or not ref_name:
			return error_response(_("reference_doctype and reference_name are required"), http_status_code=400)
		details = payments.payment_details_for(ref_dt, ref_name, user)
		result = payments.create_payment(
			amount=details["amount"],
			currency=details.get("currency"),
			description=details.get("description"),
			account=(data.get("account") or "").strip() or None,
			provider=(data.get("provider") or "").strip() or None,
			reference_doctype=ref_dt,
			reference_name=ref_name,
			payer=user,
			phone=(data.get("phone") or details.get("phone") or "").strip() or None,
			email=details.get("email") or frappe.db.get_value("User", user, "email"),
			idempotency_key=(data.get("idempotency_key") or "").strip() or None,
			return_url=(data.get("return_url") or "").strip() or None,
		)
		return success_response(data=result)
	except Exception as e:
		return _errors("initiate", e)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@jwt_required
def get_status(**kwargs):
	"""GET ?transaction= — payer or payments staff. Re-checks the gateway for
	pending payments at most every STATUS_RECHECK_SECONDS."""
	try:
		data = parse_request_data(kwargs)
		name = (data.get("transaction") or "").strip()
		if not name or not frappe.db.exists("Payment Transaction", name):
			return error_response(_("Payment not found"), http_status_code=404)
		txn = frappe.db.get_value("Payment Transaction", name,
								  ["payer", "status", "last_checked_at"], as_dict=True)
		if txn.payer != frappe.session.user and not _is_manager():
			# Same response as a missing record: don't confirm the ID exists.
			return error_response(_("Payment not found"), http_status_code=404)
		if txn.status == "Pending":
			age = (frappe.utils.now_datetime() - txn.last_checked_at).total_seconds() if txn.last_checked_at else None
			if age is None or age >= STATUS_RECHECK_SECONDS:
				payments.refresh_status(name, source="query")
		return success_response(data=payments.transaction_view(name))
	except Exception as e:
		return _errors("get_status", e)
