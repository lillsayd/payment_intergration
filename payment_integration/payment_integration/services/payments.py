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

from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from payment_integration.payment_integration.doctype.payment_transaction.payment_transaction import (
	TERMINAL_STATUSES,
)
from payment_integration.payment_integration.gateways import (
	GATEWAYS,
	gateway_meta,
	get_gateway,
	settings_doc,
	settings_for,
)
from payment_integration.payment_integration.services import receipts
from payment_integration.payment_integration.gateways.base import (
	PENDING,
	SUCCESSFUL,
	ProviderError,
	ProviderResult,
	to_json,
)

TXN = "Payment Transaction"
# A second click within this window returns the same pending payment instead
# of sending another STK prompt / creating another order.
DUPLICATE_WINDOW_SECONDS = 120
MAX_FULFILMENT_ATTEMPTS = 5
# How long a pending payment's on-page checkout can be resumed (matches the
# shortest gateway session: Stripe Checkout lasts 24h, PayPal orders ~3h).
NEXT_ACTION_TTL_SECONDS = 2 * 60 * 60


# ── gateways ─────────────────────────────────────────────────────────────

def enabled_gateways() -> list[dict]:
	"""Public checkout options: enabled gateways only, no credentials."""
	doc = settings_doc()
	options = []
	for key, meta in GATEWAYS.items():
		cfg = settings_for(key, doc=doc)
		if not int(cfg.enabled or 0):
			continue
		options.append({
			"gateway": key,
			"label": cfg.display_label or meta["title"],
			"currency": cfg.currency,
			"environment": cfg.environment,
			"requires_phone": meta["requires_phone"],
			"flow": meta["flow"],
			# True: with embed=1 the payer completes payment without leaving the page.
			"embedded": not meta.get("embed_key") or bool(cfg.get(meta["embed_key"])),
			"icon": meta["icon"],
			"_order": int(cfg.sort_order or 0),
		})
	options.sort(key=lambda o: (o.pop("_order"), o["label"]))
	return options


def resolve_gateway(gateway: str | None = None):
	"""Settings doc of the requested (or first enabled) gateway; must be on."""
	if not gateway:
		enabled = enabled_gateways()
		if not enabled:
			frappe.throw(_("No payment method is switched on"), frappe.ValidationError)
		gateway = enabled[0]["gateway"]
	gateway_meta(gateway)  # unknown key -> error
	settings = settings_for(gateway)
	if not settings.enabled:
		frappe.throw(_("{0} payments are currently switched off").format(
			settings.display_label or GATEWAYS[gateway]["title"]))
	return settings


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


def check_return_url(settings, return_url: str | None) -> str | None:
	"""Allow redirects only back to configured origins (prevents open redirects)."""
	if not return_url:
		return None
	parts = urlsplit(return_url)
	origin = f"{parts.scheme}://{parts.netloc}".lower()
	allowed = {o.strip().rstrip("/").lower()
			   for o in (settings.get("allowed_return_origins") or "").splitlines() if o.strip()}
	site = urlsplit(frappe.utils.get_url())
	allowed.add(f"{site.scheme}://{site.netloc}".lower())
	if parts.scheme not in ("http", "https") or origin not in allowed:
		frappe.throw(_("Return URL {0} is not an allowed origin").format(origin))
	return return_url


# ── creation ─────────────────────────────────────────────────────────────

def create_payment(*, amount, currency=None, gateway=None,
				   reference_doctype=None, reference_name=None, payer=None,
				   phone=None, email=None, description=None, idempotency_key=None,
				   return_url=None, embed=False) -> dict:
	"""Create a transaction and start it with the gateway. Server-side only:
	callers must compute `amount` themselves."""
	settings = resolve_gateway(gateway)
	gateway = settings.gateway
	amount = float(amount or 0)
	currency = currency or settings.currency
	if amount <= 0:
		frappe.throw(_("Amount must be greater than zero"))
	if currency != settings.currency:
		frappe.throw(_("{0} is set up for {1}, not {2}").format(
			settings.display_label or gateway, settings.currency, currency))
	return_url = check_return_url(settings, return_url)

	if idempotency_key:
		existing = frappe.db.get_value(TXN, {"idempotency_key": idempotency_key}, "name")
		if existing:
			return resume_view(existing)
	if reference_doctype and reference_name and payer:
		recent = frappe.db.get_value(TXN, {
			"reference_doctype": reference_doctype, "reference_name": reference_name,
			"payer": payer, "provider": gateway, "status": PENDING,
			"amount": amount,
			"creation": (">=", add_to_date(now_datetime(), seconds=-DUPLICATE_WINDOW_SECONDS)),
		}, "name")
		if recent:
			return resume_view(recent)

	txn = frappe.new_doc(TXN)
	txn.update({
		"provider": gateway, "environment": settings.environment, "status": "Initiated", "amount": amount,
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
		started = get_gateway(settings).initiate(txn, return_url=return_url, embed=bool(embed))
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
	frappe.cache.set_value(_next_action_key(txn.name), started.get("next_action"),
						   expires_in_sec=NEXT_ACTION_TTL_SECONDS)
	view = transaction_view(txn.name)
	view["next_action"] = started.get("next_action")
	return view


def _next_action_key(name: str) -> str:
	return f"payment_integration:next_action:{name}"


def resume_view(name: str) -> dict:
	"""A repeated request for a pending payment: hand back how to continue it
	(same iframe / embedded session / popup) rather than starting another."""
	view = transaction_view(name)
	if view["status"] == PENDING:
		view["next_action"] = frappe.cache.get_value(_next_action_key(name)) or (
			{"type": "redirect", "url": view["redirect_url"]} if view.get("redirect_url") else None)
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
		receipts.assign_number(txn)
	elif after in TERMINAL_STATUSES:
		txn.completed_at = now_datetime()
		txn.failure_reason = message
	elif message:
		txn.failure_reason = message
	txn.save(ignore_permissions=True)

	if after == SUCCESSFUL:
		_fulfil(txn)
		if txn.receipt_number:
			receipts.queue(txn.name)
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
		result = get_gateway(txn.provider).query_status(txn)
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

def handle_callback(gateway: str, params: dict, body: dict, *, raw_body: bytes = b"",
					headers=None, ip: str | None = None) -> dict | None:
	"""Authenticate a gateway callback, record it, then confirm the status
	with the gateway. Returns the acknowledgement body, or None when the
	callback is not authentic (caller answers 403)."""
	settings = settings_for(gateway)
	adapter = get_gateway(settings)
	if not adapter.authenticate_callback(params, raw_body, headers or {}):
		return None
	allow = [i.strip() for i in (settings.get("callback_ip_allowlist") or "").splitlines() if i.strip()]
	if allow and ip not in allow:
		return None

	info = adapter.parse_callback(params, body)
	filters = {"provider": gateway}
	if info.provider_request_id:
		filters["provider_request_id"] = info.provider_request_id
	elif info.merchant_reference:
		filters["merchant_reference"] = info.merchant_reference
	else:
		return info.ack
	name = frappe.db.get_value(TXN, filters, "name")
	if not name:
		frappe.log_error(to_json({"params": params, "body": body}), f"{gateway} callback for unknown payment")
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
		"amount": t.amount, "currency": t.currency,
		"reference_doctype": t.reference_doctype, "reference_name": t.reference_name,
		"receipt": t.provider_receipt, "payment_method": t.payment_method,
		"failure_reason": t.failure_reason if t.status != SUCCESSFUL else None,
		"redirect_url": t.redirect_url if t.status == PENDING else None,
		"fulfilment_status": t.fulfilment_status,
		"receipt_number": t.receipt_number,
		"receipt_download_path": receipts.DOWNLOAD_PATH + t.name if t.receipt_number else None,
		"created": str(t.creation), "completed_at": str(t.completed_at) if t.completed_at else None,
	}
