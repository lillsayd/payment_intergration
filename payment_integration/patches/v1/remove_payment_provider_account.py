"""Gateways moved from one shared "Payment Provider Account" DocType to a
Single settings DocType per gateway (Daraja/Pesapal/Stripe Settings)."""

import frappe


def execute():
	if frappe.db.exists("DocType", "Payment Provider Account"):
		frappe.delete_doc("DocType", "Payment Provider Account", force=True, ignore_missing=True)
