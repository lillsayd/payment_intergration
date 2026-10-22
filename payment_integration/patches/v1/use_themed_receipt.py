"""A saved Receipt HTML that is an untouched copy of the old built-in design
would hide the new themed one. Clear it so the built-in design (with the site's
logo and colours) is used; edited designs are left alone."""

import hashlib

import frappe

OLD_BUILT_IN_SHA256 = "17ab7e99059962f071e9eedd425da9455f165fd388e542c2d48bb8d6d450a7de"


def execute():
	html = frappe.db.get_single_value("Payment Integration Settings", "receipt_html") or ""
	if html and hashlib.sha256(html.strip().encode()).hexdigest() == OLD_BUILT_IN_SHA256:
		frappe.db.set_single_value("Payment Integration Settings", "receipt_html", None)
