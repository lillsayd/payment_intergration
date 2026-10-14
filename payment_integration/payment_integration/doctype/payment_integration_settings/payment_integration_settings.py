# Copyright (c) 2026, Said Omar and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

# Per-gateway fields whose change invalidates a cached gateway access token.
CREDENTIAL_FIELDS = ("environment", "consumer_key", "consumer_secret", "secret_key")


class PaymentIntegrationSettings(Document):
	"""Receipts + every payment gateway, one tab per gateway (see gateways/__init__.py)."""

	def validate(self):
		from payment_integration.payment_integration.gateways import GATEWAYS, get_gateway, settings_for

		for key in GATEWAYS:
			cfg = settings_for(key, doc=self)
			# Saved passwords load as "*****": an empty field means no token yet.
			if not self.get(cfg.field("callback_token")):
				self.set(cfg.field("callback_token"), secrets.token_urlsafe(32))
			url = (cfg.callback_base_url or "").strip().rstrip("/")
			if url:
				if cfg.environment == "Live" and not url.startswith("https://"):
					frappe.throw(_("{0}: live callbacks must use an https:// URL").format(GATEWAYS[key]["title"]))
				self.set(cfg.field("callback_base_url"), url)
			get_gateway(cfg).validate_settings()
		self._validate_receipt_design()

	def _validate_receipt_design(self):
		if not self.receipt_html:
			return
		from common_utils.pdf import render_html
		from payment_integration.payment_integration.services.receipts import sample_context

		try:
			render_html(self.receipt_html, sample_context(self))
		except Exception as e:
			frappe.throw(_("The receipt design has a template error: {0}").format(str(e)))

	def on_update(self):
		from payment_integration.payment_integration.gateways import GATEWAYS, clear_token_cache

		for key, meta in GATEWAYS.items():
			if any(self.meta.has_field(f"{meta['prefix']}_{f}") and self.has_value_changed(f"{meta['prefix']}_{f}")
				   for f in CREDENTIAL_FIELDS):
				clear_token_cache(key)

	@frappe.whitelist()
	def test_connection(self, gateway: str):
		"""Authenticate one gateway with its saved credentials; record the outcome."""
		frappe.only_for(("System Manager", "Payments Manager"))
		from payment_integration.payment_integration.gateways import get_gateway, settings_for

		cfg = settings_for(gateway)
		try:
			get_gateway(cfg).test_connection()
			result, ok = _("OK: credentials accepted ({0} mode)").format(cfg.environment), True
		except Exception as e:
			result, ok = _("Failed: {0}").format(str(e)), False
		cfg.set_values({"last_tested_at": now_datetime(), "last_test_result": result})
		return {"ok": ok, "message": result}
