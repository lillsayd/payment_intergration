"""Test helpers: configure gateway tabs on Payment Integration Settings, then
put the site's real configuration back exactly (including encrypted secrets)."""

import frappe

from payment_integration.payment_integration.gateways import GATEWAYS, SETTINGS, clear_token_cache


def snapshot() -> dict:
	return {
		"singles": frappe.db.sql("SELECT field, value FROM `tabSingles` WHERE doctype=%s", (SETTINGS,), as_dict=True),
		"auth": frappe.db.sql(
			"SELECT fieldname, password, encrypted FROM `__Auth` WHERE doctype=%s AND name=%s",
			(SETTINGS, SETTINGS), as_dict=True),
	}


def restore(snap: dict):
	frappe.db.sql("DELETE FROM `tabSingles` WHERE doctype=%s", (SETTINGS,))
	for row in snap["singles"]:
		frappe.db.sql("INSERT INTO `tabSingles` (doctype, field, value) VALUES (%s, %s, %s)",
					  (SETTINGS, row.field, row.value))
	frappe.db.sql("DELETE FROM `__Auth` WHERE doctype=%s AND name=%s", (SETTINGS, SETTINGS))
	for row in snap["auth"]:
		frappe.db.sql(
			"INSERT INTO `__Auth` (doctype, name, fieldname, password, encrypted) VALUES (%s, %s, %s, %s, %s)",
			(SETTINGS, SETTINGS, row.fieldname, row.password, row.encrypted))
	frappe.clear_document_cache(SETTINGS, SETTINGS)
	for key in GATEWAYS:
		clear_token_cache(key)
	frappe.db.commit()


def configure(key: str, values: dict):
	"""Overwrite one gateway's tab for a test (unprefixed names, secrets included)."""
	prefix = GATEWAYS[key]["prefix"]
	doc = frappe.get_doc(SETTINGS, SETTINGS)
	for name, value in values.items():
		doc.set(f"{prefix}_{name}", value)
	doc.save(ignore_permissions=True)
	clear_token_cache(key)
	frappe.db.commit()
	return doc
