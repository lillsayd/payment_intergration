# Copyright (c) 2026, Said Omar and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

TERMINAL_STATUSES = {"Successful", "Failed", "Cancelled", "Expired"}


class PaymentTransaction(Document):
	"""One payment attempt. Created and moved through its states only by
	payment_integration.payment_integration.services.payments — never edited by hand."""

	def validate(self):
		if not self.is_new() and self.has_value_changed("status"):
			before = self.get_doc_before_save()
			if before and before.status in TERMINAL_STATUSES:
				frappe.throw(_("Payment {0} is already {1}").format(self.name, before.status))

	def on_trash(self):
		if self.status == "Successful":
			frappe.throw(_("Successful payments are financial records and cannot be deleted"))
