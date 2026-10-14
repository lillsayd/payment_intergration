"""Gateway settings moved from one Single per gateway (Daraja/Pesapal/Stripe
Settings) into tabs on Payment Integration Settings, as `<prefix>_<field>`.
Copies stored values and encrypted secrets verbatim, then drops the old
DocTypes."""

import frappe

TARGET = "Payment Integration Settings"
OLD = {"Daraja Settings": "daraja", "Pesapal Settings": "pesapal", "Stripe Settings": "stripe"}


def execute():
	for old, prefix in OLD.items():
		rows = frappe.db.sql("SELECT field, value FROM `tabSingles` WHERE doctype=%s", (old,), as_dict=True)
		for row in rows:
			field = f"{prefix}_{row.field}"
			frappe.db.sql("DELETE FROM `tabSingles` WHERE doctype=%s AND field=%s", (TARGET, field))
			frappe.db.sql("INSERT INTO `tabSingles` (doctype, field, value) VALUES (%s, %s, %s)",
						  (TARGET, field, row.value))
		secrets = frappe.db.sql(
			"SELECT fieldname, password, encrypted FROM `__Auth` WHERE doctype=%s AND name=%s",
			(old, old), as_dict=True)
		for row in secrets:
			field = f"{prefix}_{row.fieldname}"
			frappe.db.sql("DELETE FROM `__Auth` WHERE doctype=%s AND name=%s AND fieldname=%s",
						  (TARGET, TARGET, field))
			frappe.db.sql(
				"INSERT INTO `__Auth` (doctype, name, fieldname, password, encrypted) VALUES (%s, %s, %s, %s, %s)",
				(TARGET, TARGET, field, row.password, row.encrypted))
		frappe.db.sql("DELETE FROM `tabSingles` WHERE doctype=%s", (old,))
		frappe.db.sql("DELETE FROM `__Auth` WHERE doctype=%s", (old,))
		if frappe.db.exists("DocType", old):
			frappe.delete_doc("DocType", old, force=True, ignore_missing=True)
	frappe.clear_document_cache(TARGET, TARGET)
