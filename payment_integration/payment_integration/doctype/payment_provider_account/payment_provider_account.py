# Copyright (c) 2026, Said Omar and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

# Fields whose change makes any cached provider access token stale.
CREDENTIAL_FIELDS = (
	"environment", "daraja_consumer_key", "daraja_consumer_secret",
	"pesapal_consumer_key", "pesapal_consumer_secret",
)


class PaymentProviderAccount(Document):
	def validate(self):
		# Saved passwords load as a "*****" placeholder, so an empty field means
		# no token yet. (get_password() could return a stale secret left by a
		# deleted account of the same name, and an empty field would then make
		# Frappe delete the stored secret.)
		if not self.callback_token:
			self.callback_token = secrets.token_urlsafe(32)
		if not self.callback_id:
			self.callback_id = secrets.token_hex(8)
		if self.callback_base_url:
			url = self.callback_base_url.strip().rstrip("/")
			if not url.startswith("https://") and self.environment == "Live":
				frappe.throw(_("Live callbacks must use an https:// URL"))
			self.callback_base_url = url
		if self.provider == "Daraja" and (self.currency or "KES") != "KES":
			frappe.throw(_("M-Pesa (Daraja) only supports KES"))
		if self.is_default:
			frappe.db.set_value(
				"Payment Provider Account",
				{"provider": self.provider, "name": ("!=", self.name), "is_default": 1},
				"is_default", 0,
			)

	def on_update(self):
		if any(self.has_value_changed(f) for f in CREDENTIAL_FIELDS):
			from payment_integration.payment_integration.providers import clear_token_cache

			clear_token_cache(self.name)

	def on_trash(self):
		if frappe.db.exists("Payment Transaction", {"provider_account": self.name}):
			frappe.throw(_("This account has payment history. Disable it instead of deleting."))

	@frappe.whitelist()
	def test_connection(self):
		"""Fetch an access token with the saved credentials."""
		frappe.only_for(("System Manager", "Payments Manager"))
		from payment_integration.payment_integration.providers import get_provider

		try:
			get_provider(self).get_access_token(force_refresh=True)
			result = _("OK: credentials accepted by {0} ({1})").format(self.provider, self.environment)
		except Exception as e:
			result = _("Failed: {0}").format(str(e))
		self.db_set({"last_tested_at": now_datetime(), "last_test_result": result})
		return result

	@frappe.whitelist()
	def register_ipn(self):
		"""Pesapal only: register (or re-register) the IPN URL for this account."""
		frappe.only_for(("System Manager", "Payments Manager"))
		if self.provider != "Pesapal":
			frappe.throw(_("IPN registration is only needed for Pesapal"))
		from payment_integration.payment_integration.providers import get_provider

		return get_provider(self).register_ipn(force=True)
