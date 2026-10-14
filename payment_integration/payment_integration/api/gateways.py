"""Admin API for the "Payment Gateways" page: one card per gateway, each with
its own on/off switch and a form generated from that gateway's tab on
Payment Integration Settings. Secrets are write-only: the API reports whether
one is set, never its value.
"""

import frappe
from frappe import _

from common_utils.api.auth import jwt_required
from common_utils.api.crud_utils import parse_request_data
from common_utils.api.utils import error_response, success_response
from payment_integration.payment_integration.gateways import (
	GATEWAYS,
	SETTINGS,
	gateway_meta,
	settings_doc,
	settings_for,
)

MANAGER_ROLES = {"System Manager", "Payments Manager", "Administrator"}
LAYOUT = {"Column Break", "Tab Break"}
# Managed by the system, shown read-only.
SYSTEM_FIELDS = {"callback_token", "last_tested_at", "last_test_result", "ipn_id", "ipn_url"}
EDITABLE_TYPES = {"Data", "Password", "Select", "Check", "Int", "Small Text", "Link", "Float"}


def _require_manager():
	if frappe.session.user == "Guest":
		frappe.throw(_("Authentication required"), frappe.AuthenticationError)
	if not MANAGER_ROLES.intersection(frappe.get_roles()):
		frappe.throw(_("Insufficient permissions"), frappe.PermissionError)


def _fail(fn, e):
	frappe.db.rollback()
	if isinstance(e, frappe.AuthenticationError):
		return error_response(str(e), http_status_code=401)
	if isinstance(e, frappe.PermissionError):
		return error_response(str(e), http_status_code=403)
	if isinstance(e, (frappe.ValidationError, frappe.DoesNotExistError)):
		return error_response(frappe.utils.strip_html(str(e)), http_status_code=400)
	frappe.log_error(frappe.get_traceback(), f"payment_integration.api.gateways.{fn}")
	return error_response(_("Request failed"), http_status_code=500)


def _own_fields(doc, prefix: str):
	"""This gateway's tab: its `<prefix>_*` fields in DocType order."""
	return [df for df in doc.meta.fields
			if df.fieldname.startswith(prefix + "_") and df.fieldtype not in ("Tab Break", "HTML")]


def _unprefix(value: str | None, prefix: str) -> str | None:
	if not value:
		return value
	return value.replace(f"doc.{prefix}_", "doc.").removeprefix(f"{prefix}_")


def _required(df, prefix: str) -> bool:
	return df.mandatory_depends_on == f"{prefix}_enabled" or bool(df.reqd)


def _schema(doc, prefix: str) -> list[dict]:
	"""Sections + fields of one gateway's tab, unprefixed, for a generic form."""
	sections, current = [], None
	for df in _own_fields(doc, prefix):
		if df.fieldtype in LAYOUT:
			continue
		if df.fieldtype == "Section Break":
			current = {"label": df.label or "", "collapsible": bool(df.collapsible), "fields": []}
			sections.append(current)
			continue
		if df.fieldtype not in EDITABLE_TYPES and df.fieldtype != "Datetime":
			continue
		if current is None:
			current = {"label": "", "collapsible": False, "fields": []}
			sections.append(current)
		name = df.fieldname[len(prefix) + 1:]
		current["fields"].append({
			"fieldname": name,
			"label": df.label,
			"fieldtype": df.fieldtype,
			"options": [o for o in (df.options or "").split("\n") if o] if df.fieldtype == "Select" else df.options,
			"description": df.description,
			"required_when_enabled": _required(df, prefix),
			"read_only": bool(df.read_only) or name in SYSTEM_FIELDS,
			"depends_on": _unprefix(df.depends_on, prefix),
			"default": df.default,
		})
	return [s for s in sections if s["fields"]]


def _values(doc, prefix: str) -> dict:
	out = {}
	for df in _own_fields(doc, prefix):
		if df.fieldtype in LAYOUT or df.fieldtype == "Section Break":
			continue
		name = df.fieldname[len(prefix) + 1:]
		if df.fieldtype == "Password":
			out[name] = {"is_set": bool(doc.get_password(df.fieldname, raise_exception=False))}
		else:
			value = doc.get(df.fieldname)
			out[name] = str(value) if df.fieldtype == "Datetime" and value else value
	return out


def _missing_required(doc, prefix: str) -> list[str]:
	missing = []
	for df in _own_fields(doc, prefix):
		if not _required(df, prefix):
			continue
		if df.fieldtype == "Password":
			filled = bool(doc.get_password(df.fieldname, raise_exception=False))
		else:
			filled = doc.get(df.fieldname) not in (None, "")
		if not filled:
			missing.append(df.label)
	return missing


def _card(key: str, include_schema: bool = False, doc=None) -> dict:
	from payment_integration.payment_integration.gateways import get_gateway

	meta = gateway_meta(key)
	doc = doc or settings_doc()
	cfg = settings_for(key, doc=doc)
	prefix = meta["prefix"]
	card = {
		"gateway": key,
		"title": meta["title"],
		"description": meta["description"],
		"icon": meta["icon"],
		"flow": meta["flow"],
		"docs": meta["docs"],
		"doctype": SETTINGS,
		"enabled": bool(int(cfg.enabled or 0)),
		"environment": cfg.environment,
		"display_label": cfg.display_label or meta["title"],
		"currency": cfg.currency,
		"missing": _missing_required(doc, prefix),
		"last_tested_at": str(cfg.last_tested_at) if cfg.last_tested_at else None,
		"last_test_result": cfg.last_test_result,
		"callback_url": None,
	}
	if cfg.get_password("callback_token"):
		card["callback_url"] = get_gateway(cfg).callback_url()
	if include_schema:
		card["sections"] = _schema(doc, prefix)
		card["values"] = _values(doc, prefix)
	return card


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@jwt_required
def list_gateways(**kwargs):
	try:
		_require_manager()
		return success_response(data={"gateways": [_card(k) for k in GATEWAYS]})
	except Exception as e:
		return _fail("list_gateways", e)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@jwt_required
def get_gateway(**kwargs):
	"""?gateway= — card + form schema + current values (secrets as is_set)."""
	try:
		_require_manager()
		return success_response(data=_card(parse_request_data(kwargs).get("gateway") or "", include_schema=True))
	except Exception as e:
		return _fail("get_gateway", e)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@jwt_required
def save_gateway(**kwargs):
	"""{gateway, values:{fieldname: value}} — unprefixed names; blank secrets keep the stored one."""
	try:
		_require_manager()
		data = parse_request_data(kwargs)
		key = data.get("gateway") or ""
		prefix = gateway_meta(key)["prefix"]
		values = data.get("values") or {}
		doc = settings_doc()
		editable = {df.fieldname[len(prefix) + 1:]: df for df in _own_fields(doc, prefix)
					if df.fieldtype in EDITABLE_TYPES and not df.read_only
					and df.fieldname[len(prefix) + 1:] not in SYSTEM_FIELDS}
		for name, value in values.items():
			df = editable.get(name)
			if not df:
				continue
			if df.fieldtype == "Password":
				if isinstance(value, str) and value.strip():
					doc.set(df.fieldname, value.strip())
				continue
			if df.fieldtype == "Check":
				value = 1 if value in (1, True, "1", "true") else 0
			doc.set(df.fieldname, value)
		doc.save()
		frappe.db.commit()
		return success_response(data=_card(key, include_schema=True))
	except Exception as e:
		return _fail("save_gateway", e)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@jwt_required
def set_enabled(**kwargs):
	"""{gateway, enabled} — the per-gateway on/off switch."""
	try:
		_require_manager()
		data = parse_request_data(kwargs)
		key = data.get("gateway") or ""
		prefix = gateway_meta(key)["prefix"]
		doc = settings_doc()
		enabled = 1 if data.get("enabled") in (1, True, "1", "true") else 0
		if enabled:
			missing = _missing_required(doc, prefix)
			if missing:
				frappe.throw(_("Fill in {0} before switching this on").format(", ".join(missing)))
		doc.set(f"{prefix}_enabled", enabled)
		doc.save()
		frappe.db.commit()
		return success_response(data=_card(key))
	except Exception as e:
		return _fail("set_enabled", e)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@jwt_required
def test_gateway(**kwargs):
	"""{gateway} — try the saved credentials against the gateway."""
	try:
		_require_manager()
		key = parse_request_data(kwargs).get("gateway") or ""
		gateway_meta(key)
		result = settings_doc().test_connection(key)
		frappe.db.commit()
		return success_response(data={**result, "card": _card(key)})
	except Exception as e:
		return _fail("test_gateway", e)
