"""Scheduled reconciliation: catches missed or failed callbacks, expires
abandoned payments and retries failed fulfilment."""

import frappe
from frappe.utils import add_to_date, now_datetime

from payment_integration.payment_integration.services import payments

TXN = "Payment Transaction"
BATCH = 100
# How long a payment may stay open before it is expired.
EXPIRE_AFTER_MINUTES = {
	"Daraja": 120,           # STK prompts time out within minutes
	"Pesapal": 48 * 60,
	"Stripe": 25 * 60,       # Checkout Sessions expire after 24 h
	"PayPal": 72 * 60,       # approved orders can be captured for 3 days
	"Flutterwave": 48 * 60,
	"Paystack": 48 * 60,
}
INITIATED_EXPIRE_MINUTES = 30


def reconcile_pending():
	now = now_datetime()

	# Never reached the gateway (crash between insert and provider call).
	for name in frappe.get_all(TXN, filters={
		"status": "Initiated", "creation": ("<", add_to_date(now, minutes=-INITIATED_EXPIRE_MINUTES)),
	}, pluck="name", limit=BATCH):
		_expire(name, "Never confirmed by the provider")

	for row in frappe.get_all(TXN, filters={"status": "Pending"},
							  fields=["name", "provider", "creation", "last_checked_at", "check_count"],
							  order_by="last_checked_at asc", limit=BATCH):
		if not _due(row, now):
			continue
		try:
			status = payments.refresh_status(row.name, source="reconcile")
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"Reconcile {row.name} failed")
			continue
		limit = EXPIRE_AFTER_MINUTES.get(row.provider, 24 * 60)
		if status == "Pending" and row.creation < add_to_date(now, minutes=-limit):
			_expire(row.name, f"No final status from {row.provider} after {limit // 60} hours")

	for name in frappe.get_all(TXN, filters={
		"status": "Successful", "fulfilment_status": "Failed",
		"fulfilment_attempts": ("<", payments.MAX_FULFILMENT_ATTEMPTS),
	}, pluck="name", limit=BATCH):
		payments.retry_fulfilment(name)


def _due(row, now) -> bool:
	"""Back off: first minutes check often, then rarely."""
	if not row.last_checked_at:
		return True
	n = row.check_count or 0
	gap_minutes = 1 if n < 5 else 5 if n < 20 else 30
	return row.last_checked_at <= add_to_date(now, minutes=-gap_minutes)


def _expire(name: str, reason: str):
	from payment_integration.payment_integration.gateways.base import EXPIRED, ProviderResult

	try:
		payments.apply_result(name, ProviderResult(status=EXPIRED, reason=reason), "reconcile")
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), f"Expire {name} failed")
