"""Payment orchestration — the only code that creates Payment Transactions or
changes their status. Other apps use create_payment()/refresh_status() and
plug in through the `payment_integration_handlers` hook:

	# in the consuming app's hooks.py
	payment_integration_handlers = {
		"LMS Enrollment": {
			# (reference_name, user) -> {"amount", "currency", "description", ...};
			# raise frappe.PermissionError if `user` may not pay for it.
			"get_payment_details": "my_app.payments.enrollment_payment_details",
			# (transaction) -> None. Must be idempotent; runs once per success.
			"on_success": "my_app.payments.enrollment_paid",
			"on_failure": "my_app.payments.enrollment_payment_failed",  # optional
		}
	}
"""

import hmac
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from payment_integration.payment_integration.doctype.payment_transaction.payment_transaction import (
	TERMINAL_STATUSES,
)
from payment_integration.payment_integration.providers import get_provider
from payment_integration.payment_integration.providers.base import (
	PENDING,
	SUCCESSFUL,
	ProviderError,
	ProviderResult,
	to_json,
)

TXN = "Payment Transaction"
ACCOUNT = "Payment Provider Account"
# A second click within this window returns the same pending payment instead
# of sending another STK prompt / creating another order.
DUPLICATE_WINDOW_SECONDS = 120
MAX_FULFILMENT_ATTEMPTS = 5


# ── accounts ─────────────────────────────────────────────────────────────

def enabled_accounts() -> list[dict]:
	"""Public checkout options: enabled accounts only, no credentials."""
	rows = frappe.get_all(
		ACCOUNT, filters={"enabled": 1},
		fields=["name", "provider", "display_label", "currency", "environment", "is_default"],
		order_by="is_default desc, provider asc, name asc",
	)
	return [{
		"account": r.name,
		"provider": r.provider,
		"label": r.display_label or ("M-Pesa" if r.provider == "Daraja" else r.provider),
		"currency": r.currency,
		"environment": r.environment,
		"requires_phone": r.provider == "Daraja",
		"flow": "stk_push" if r.provider == "Daraja" else "redirect",
	} for r in rows]


def resolve_account(account: str | None = None, provider: str | None = None):
	"""Enabled account by name, or the default (else first) enabled one for a provider."""
	if account:
		doc = frappe.get_doc(ACCOUNT, account)
	else:
		filters = {"enabled": 1}
		if provider:
			filters["provider"] = provider
		name = frappe.db.get_value(ACCOUNT, filters, "name", order_by="is_default desc, creation asc")
		if not name:
			frappe.throw(_("No enabled payment provider is configured"), frappe.ValidationError)
		doc = frappe.get_doc(ACCOUNT, name)
	if not doc.enabled:
		frappe.throw(_("{0} payments are currently switched off").format(doc.display_label or doc.provider))
	return doc


def _handler(reference_doctype: str | None, event: str) -> list[str]:
	if not reference_doctype:
		return []
	entry = (frappe.get_hooks("payment_integration_handlers") or {}).get(reference_doctype) or {}
	paths = entry.get(event) or []
	return [paths] if isinstance(paths, str) else list(paths)


def payment_details_for(reference_doctype: str, reference_name: str, user: str) -> dict:
	"""Ask the owning app what this reference costs (never trust a client amount)."""
	paths = _handler(reference_doctype, "get_payment_details")
	if not paths:
		frappe.throw(_("{0} cannot be paid for online").format(reference_doctype))
	details = frappe.get_attr(paths[-1])(reference_name=reference_name, user=user)
	if not details or not details.get("amount"):
		frappe.throw(_("Nothing to pay for this {0}").format(reference_doctype))
	return details


def check_return_url(account, return_url: str | None) -> str | None:
	"""Allow redirects only back to configured origins (prevents open redirects)."""
	if not return_url:
		return None
	parts = urlsplit(return_url)
	origin = f"{parts.scheme}://{parts.netloc}".lower()
	allowed = {o.strip().rstrip("/").lower() for o in (account.allowed_return_origins or "").splitlines() if o.strip()}
	site = urlsplit(frappe.utils.get_url())
	allowed.add(f"{site.scheme}://{site.netloc}".lower())
	if parts.scheme not in ("http", "https") or origin not in allowed:
		frappe.throw(_("Return URL {0} is not an allowed origin").format(origin))
	return return_url


# ── creation ─────────────────────────────────────────────────────────────

def create_payment(*, amount, currency=None, account=None, provider=None,
				   reference_doctype=None, reference_name=None, payer=None,
				   phone=None, email=None, description=None, idempotency_key=None,
				   return_url=None) -> dict:
	"""Create a transaction and start it with the gateway. Server-side only:
	callers must compute `amount` themselves."""
	acc = resolve_account(account, provider)
	amount = float(amount or 0)
	currency = currency or acc.currency
	if amount <= 0:
		frappe.throw(_("Amount must be greater than zero"))
	if currency != acc.currency:
		frappe.throw(_("{0} is configured for {1}, not {2}").format(acc.name, acc.currency, currency))
	return_url = check_return_url(acc, return_url)

	if idempotency_key:
		existing = frappe.db.get_value(TXN, {"idempotency_key": idempotency_key}, "name")
		if existing:
			return transaction_view(existing)
	if reference_doctype and reference_name and payer:
		recent = frappe.db.get_value(TXN, {
			"reference_doctype": reference_doctype, "reference_name": reference_name,
			"payer": payer, "provider_account": acc.name, "status": PENDING,
			"amount": amount,
			"creation": (">=", add_to_date(now_datetime(), seconds=-DUPLICATE_WINDOW_SECONDS)),
		}, "name")
		if recent:
			return transaction_view(recent)

	txn = frappe.new_doc(TXN)
	txn.update({
		"provider_account": acc.name, "status": "Initiated", "amount": amount,
		"currency": currency, "reference_doctype": reference_doctype,
		"reference_name": reference_name, "payer": payer, "phone": phone,
		"email": email, "description": description,
		"idempotency_key": idempotency_key or None,
		"fulfilment_status": "Pending" if _handler(reference_doctype, "on_success") else "Not Required",
	})
	txn.insert(ignore_permissions=True)
	txn.db_set("merchant_reference", txn.name, update_modified=False)
	# Commit before calling out: a fast callback must be able to find this row.
	frappe.db.commit()

	if payer and payer != "Guest":
		txn.payer_name = frappe.db.get_value("User", payer, "full_name")
	try:
		started = get_provider(acc).initiate(txn, return_url=return_url)
	except ProviderError as e:
		_append_event(txn, "initiate", "Initiated", "Failed", str(e), e.payload)
		txn.status = "Failed"
		txn.failure_reason = str(e)
		txn.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.throw(_("Could not start the payment: {0}").format(str(e)))

	txn.update({
		"status": PENDING,
		"provider_request_id": started.get("provider_request_id"),
		"provider_merchant_request_id": started.get("provider_merchant_request_id"),
		"redirect_url": started.get("redirect_url"),
		"phone": started.get("phone") or txn.phone,
	})
	_append_event(txn, "initiate", "Initiated", PENDING, "Started with provider", started.get("raw"))
	txn.save(ignore_permissions=True)
	frappe.db.commit()
	view = transaction_view(txn.name)
	view["next_action"] = started.get("next_action")
	return view


# ── status changes ───────────────────────────────────────────────────────

def _append_event(txn, source, before, after, message=None, payload=None):
	txn.append("events", {
		"occurred_at": now_datetime(), "source": source,
		"status_before": before, "status_after": after,
		"message": (message or "")[:1000],
		"payload": to_json(payload) if payload else None,
	})


def apply_result(name: str, result: ProviderResult, source: str, *, receipt_hint=None,
				 amount_hint=None, payload=None) -> str:
	"""Move a transaction to the gateway-confirmed status. Row-locked and
	idempotent: terminal transactions never change, success fires once."""
	locked = frappe.db.sql(f"SELECT status FROM `tab{TXN}` WHERE name = %s FOR UPDATE", (name,))
	if not locked:
		frappe.throw(_("Payment {0} not found").format(name), frappe.DoesNotExistError)
	before = locked[0][0]
	if before in TERMINAL_STATUSES:
		frappe.db.commit()
		return before

	txn = frappe.get_doc(TXN, name)
	txn.last_checked_at = now_datetime()
	txn.check_count = (txn.check_count or 0) + 1
	receipt = result.receipt or receipt_hint
	if receipt and not txn.provider_receipt:
		txn.provider_receipt = receipt
	if result.payment_method:
		txn.payment_method = result.payment_method

	after = result.status
	message = result.reason
	if after == SUCCESSFUL:
		paid = result.amount if result.amount is not None else amount_hint
		if paid is not None and abs(float(paid) - float(txn.amount)) > 0.009:
			after, message = PENDING, f"Amount mismatch: expected {txn.amount}, provider reported {paid}. Needs review."
			frappe.log_error(message, f"Payment {name} amount mismatch")
		elif result.currency and result.currency != txn.currency:
			after, message = PENDING, f"Currency mismatch: expected {txn.currency}, got {result.currency}. Needs review."
			frappe.log_error(message, f"Payment {name} currency mismatch")

	if after != before or message or payload:
		_append_event(txn, source, before, after, message, payload or result.raw)
	txn.status = after
	if after == SUCCESSFUL:
		txn.completed_at = now_datetime()
		txn.failure_reason = None
	elif after in TERMINAL_STATUSES:
		txn.completed_at = now_datetime()
		txn.failure_reason = message
	elif message:
		txn.failure_reason = message
	txn.save(ignore_permissions=True)

	if after == SUCCESSFUL:
		_fulfil(txn)
	elif after in TERMINAL_STATUSES:
		_notify_failure(txn)
	frappe.db.commit()
	return after


def refresh_status(name: str, source: str = "query", **hints) -> str:
	"""Read the authoritative status from the gateway and apply it."""
	txn = frappe.get_doc(TXN, name)
	if txn.status in TERMINAL_STATUSES:
		return txn.status
	try:
		result = get_provider(txn.provider_account).query_status(txn)
	except ProviderError as e:
		frappe.db.set_value(TXN, name, {"last_checked_at": now_datetime(),
										 "check_count": (txn.check_count or 0) + 1},
							update_modified=False)
		frappe.db.commit()
		if not e.retryable:
			frappe.log_error(str(e), f"Payment {name} status check failed")
		return txn.status
	return apply_result(name, result, source, **hints)


# ── fulfilment (consumer hooks) ──────────────────────────────────────────

def _rollback_to(savepoint: str):
	"""Undo a handler's partial writes. If the handler committed (releasing the
	savepoint) there is nothing left to undo, and the payment status — already
	saved before handlers run — stays as is."""
	try:
		frappe.db.rollback(save_point=savepoint)
	except Exception:
		pass


def _fulfil(txn):
	paths = _handler(txn.reference_doctype, "on_success")
	if not paths:
		txn.db_set("fulfilment_status", "Not Required", update_modified=False)
		return
	attempts = (txn.fulfilment_attempts or 0) + 1
	frappe.db.savepoint("payment_fulfilment")
	try:
		for path in paths:
			frappe.get_attr(path)(transaction=txn)
	except Exception as e:
		_rollback_to("payment_fulfilment")
		frappe.log_error(frappe.get_traceback(), f"Payment {txn.name} fulfilment failed")
		txn.db_set({"fulfilment_status": "Failed", "fulfilment_attempts": attempts,
					"fulfilment_error": str(e)[:1000]}, update_modified=False)
		return
	txn.db_set({"fulfilment_status": "Done", "fulfilment_attempts": attempts,
				"fulfilment_error": None}, update_modified=False)


def _notify_failure(txn):
	for path in _handler(txn.reference_doctype, "on_failure"):
		frappe.db.savepoint("payment_failure")
		try:
			frappe.get_attr(path)(transaction=txn)
		except Exception:
			_rollback_to("payment_failure")
			frappe.log_error(frappe.get_traceback(), f"Payment {txn.name} on_failure handler failed")


def retry_fulfilment(name: str) -> str:
	txn = frappe.get_doc(TXN, name)
	if txn.status == SUCCESSFUL and txn.fulfilment_status == "Failed":
		_fulfil(txn)
		frappe.db.commit()
	return frappe.db.get_value(TXN, name, "fulfilment_status")


# ── callbacks ────────────────────────────────────────────────────────────

def account_for_callback(callback_id: str, token: str, ip: str | None):
	"""Authenticate a callback: known callback_id + matching secret token
	(+ IP allowlist when configured). Returns the account doc or None."""
	if not callback_id or not token:
		return None
	name = frappe.db.get_value(ACCOUNT, {"callback_id": callback_id}, "name")
	if not name:
		return None
	acc = frappe.get_doc(ACCOUNT, name)
	expected = acc.get_password("callback_token", raise_exception=False) or ""
	if not expected or not hmac.compare_digest(expected, token):
		return None
	allow = [i.strip() for i in (acc.callback_ip_allowlist or "").splitlines() if i.strip()]
	if allow and ip not in allow:
		return None
	return acc


def handle_callback(acc, params: dict, body: dict) -> dict:
	"""Record a callback, then confirm the status with the gateway. Returns the
	acknowledgement body the provider expects."""
	info = get_provider(acc).parse_callback(params, body)
	filters = {"provider_account": acc.name}
	if info.provider_request_id:
		filters["provider_request_id"] = info.provider_request_id
	elif info.merchant_reference:
		filters["merchant_reference"] = info.merchant_reference
	else:
		return info.ack
	name = frappe.db.get_value(TXN, filters, "name")
	if not name:
		frappe.log_error(to_json({"params": params, "body": body}),
						 f"{acc.provider} callback for unknown payment")
		return info.ack
	if info.receipt:
		# Daraja only reveals the receipt here (the status query omits it), so
		# keep it even if the confirming query below fails and is retried later.
		frappe.db.sql(
			f"""UPDATE `tab{TXN}` SET provider_receipt = %s
			WHERE name = %s AND IFNULL(provider_receipt, '') = ''""",
			(info.receipt, name),
		)
		frappe.db.commit()
	refresh_status(name, source="callback", receipt_hint=info.receipt,
				   amount_hint=info.amount, payload={"params": params, "body": body})
	return info.ack


# ── views ────────────────────────────────────────────────────────────────

def transaction_view(name: str) -> dict:
	"""Payer-safe view of a transaction (no event payloads)."""
	t = frappe.get_doc(TXN, name)
	return {
		"name": t.name, "status": t.status, "provider": t.provider,
		"provider_account": t.provider_account, "amount": t.amount, "currency": t.currency,
		"reference_doctype": t.reference_doctype, "reference_name": t.reference_name,
		"receipt": t.provider_receipt, "payment_method": t.payment_method,
		"failure_reason": t.failure_reason if t.status != SUCCESSFUL else None,
		"redirect_url": t.redirect_url if t.status == PENDING else None,
		"fulfilment_status": t.fulfilment_status,
		"created": str(t.creation), "completed_at": str(t.completed_at) if t.completed_at else None,
	}
