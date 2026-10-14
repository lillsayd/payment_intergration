"""Payment receipts: sequential numbers + protected PDF per successful payment.

The design is Payment Integration Settings → Receipt HTML (Jinja), or the
built-in receipts/default_receipt.html when empty. Numbers are assigned in
the same transaction that marks the payment Successful; the PDF is rendered
in the background and stored as a private File (rendered lazily on download
if that has not happened yet).
"""

import os

import frappe
from frappe.model.naming import make_autoname
from frappe.utils import fmt_money, format_datetime, now_datetime

from common_utils.pdf import (
	image_data_uri,
	read_file_bytes,
	render_protected_pdf,
	save_private_pdf,
)

TXN = "Payment Transaction"
SETTINGS = "Payment Integration Settings"
PROVIDER_LABELS = {"Daraja": "M-Pesa", "Pesapal": "Pesapal"}
DOWNLOAD_PATH = "/api/method/payment_integration.payment_integration.api.payments.download_receipt?transaction="


def settings():
	return frappe.get_cached_doc(SETTINGS)


def receipts_enabled() -> bool:
	value = frappe.db.get_single_value(SETTINGS, "receipts_enabled")
	return True if value is None else bool(int(value))


def default_receipt_html() -> str:
	path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "receipts", "default_receipt.html")
	with open(path) as f:
		return f.read()


def money(amount, currency: str | None) -> str:
	"""ISO code + amount (e.g. "KES 1,500.00"); clearer than local symbols."""
	return f"{currency or ''} {fmt_money(amount, precision=2)}".strip()


def _mask_phone(phone: str | None) -> str | None:
	if not phone or len(phone) < 7:
		return phone
	return phone[:4] + "*" * (len(phone) - 7) + phone[-3:]


def _org_context(s) -> dict:
	return {
		"organization_name": s.organization_name or frappe.db.get_single_value("Website Settings", "app_name") or "",
		"organization_address": s.organization_address or "",
		"organization_email": s.organization_email or "",
		"organization_phone": s.organization_phone or "",
		"tax_pin": s.tax_pin or "",
		"logo": image_data_uri(s.logo),
		"footer": s.receipt_footer or "",
	}


def context_for(txn) -> dict:
	payer_name = frappe.db.get_value("User", txn.payer, "full_name") if txn.payer else None
	paid_on = txn.completed_at or txn.modified
	return {
		**_org_context(settings()),
		"receipt_number": txn.receipt_number,
		"issued_on": format_datetime(txn.receipt_issued_on or now_datetime(), "d MMM yyyy, HH:mm"),
		"paid_on": format_datetime(paid_on, "d MMM yyyy, HH:mm") if paid_on else "",
		"payer_name": payer_name or txn.email or txn.payer or "",
		"payer_email": txn.email or "",
		"payer_phone": _mask_phone(txn.phone),
		"amount": money(txn.amount, txn.currency),
		"amount_value": txn.amount,
		"currency": txn.currency,
		"description": txn.description or "",
		"provider": PROVIDER_LABELS.get(txn.provider, txn.provider),
		"payment_method": txn.payment_method or "",
		"provider_receipt": txn.provider_receipt or "",
		"transaction": txn.name,
		"reference_doctype": txn.reference_doctype or "",
		"reference_name": txn.reference_name or "",
	}


def sample_context(s=None) -> dict:
	"""Fake data to validate/preview a design."""
	s = s or settings()
	return {
		**_org_context(s),
		"receipt_number": "RCPT-2026-00001", "issued_on": "14 Oct 2026, 10:30", "paid_on": "14 Oct 2026, 10:29",
		"payer_name": "Jane Wanjiru Doe", "payer_email": "jane@example.com", "payer_phone": "2547****678",
		"amount": money(1500, "KES"), "amount_value": 1500, "currency": "KES",
		"description": "Sample course fee", "provider": "M-Pesa", "payment_method": "",
		"provider_receipt": "SGR7ABC123", "transaction": "PAY-2026-00001",
		"reference_doctype": "LMS Enrollment", "reference_name": "LMS-E-00001",
	}


def assign_number(txn) -> str | None:
	"""Called inside apply_result when a payment turns Successful."""
	if txn.receipt_number or not receipts_enabled():
		return txn.receipt_number
	prefix = (frappe.db.get_single_value(SETTINGS, "receipt_prefix") or "RCPT-").strip()
	txn.receipt_number = make_autoname(f"{prefix}.YYYY.-.#####")
	txn.receipt_issued_on = now_datetime()
	return txn.receipt_number


def render_pdf(txn) -> bytes:
	s = settings()
	return render_protected_pdf(
		s.receipt_html or default_receipt_html(), context_for(txn),
		title=f"Receipt {txn.receipt_number}", author=s.organization_name or None,
		page_size=s.receipt_page_size or "A4", orientation="Portrait",
		margin_mm=12 if s.receipt_margin_mm is None else s.receipt_margin_mm,
	)


def generate(name: str) -> str | None:
	"""Render and store the receipt PDF once. Returns the private file URL."""
	txn = frappe.get_doc(TXN, name)
	if txn.status != "Successful" or not txn.receipt_number:
		return None
	if txn.receipt_file:
		return txn.receipt_file
	url = save_private_pdf(render_pdf(txn), f"{txn.receipt_number}.pdf", TXN, txn.name, "receipt_file")
	frappe.db.set_value(TXN, txn.name, "receipt_file", url, update_modified=False)
	return url


def generate_job(transaction: str):
	"""Background: render the PDF and optionally email it."""
	try:
		url = generate(transaction)
		frappe.db.commit()
		if url and frappe.db.get_single_value(SETTINGS, "email_receipts"):
			email(transaction)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), f"Receipt for {transaction} failed")


def email(transaction: str):
	txn = frappe.get_doc(TXN, transaction)
	if not txn.email or not txn.receipt_file:
		return
	org = frappe.db.get_single_value(SETTINGS, "organization_name") or ""
	frappe.sendmail(
		recipients=[txn.email],
		subject=f"Receipt {txn.receipt_number}" + (f" — {org}" if org else ""),
		message=(f"<p>Thank you for your payment of {money(txn.amount, txn.currency)}.</p>"
				 f"<p>Your receipt <b>{txn.receipt_number}</b> is attached.</p>"),
		attachments=[{"fname": f"{txn.receipt_number}.pdf", "fcontent": read_file_bytes(txn.receipt_file)}],
		reference_doctype=TXN, reference_name=txn.name,
	)


def queue(transaction: str):
	frappe.enqueue(
		"payment_integration.payment_integration.services.receipts.generate_job",
		transaction=transaction, queue="short", enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
	)


def pdf_bytes(name: str) -> bytes:
	url = frappe.db.get_value(TXN, name, "receipt_file") or generate(name)
	if not url:
		frappe.throw("No receipt is available for this payment", frappe.DoesNotExistError)
	frappe.db.commit()
	return read_file_bytes(url)
