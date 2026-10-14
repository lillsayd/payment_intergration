"""Gateway registry.

All gateway settings live on one Single DocType, Payment Integration Settings,
with one tab per gateway. A gateway's fields share a prefix (daraja_*,
pesapal_*, stripe_*); adapters see them unprefixed through GatewayConfig.

Adding a gateway:
  1. add a tab of `<prefix>_*` fields to Payment Integration Settings
     (copy an existing tab: enabled, environment, display_label, currency,
     sort_order, callback_* and last_test_* are expected by every gateway),
  2. write an adapter in gateways/<name>.py (see base.PaymentGateway),
  3. add one entry to GATEWAYS below.
Checkout, the admin "Payment Gateways" page, callbacks and receipts pick it up.
"""

import frappe

from payment_integration.payment_integration.gateways.base import (  # noqa: F401
	PaymentGateway,
	ProviderError,
	ProviderResult,
)

SETTINGS = "Payment Integration Settings"

# key -> metadata. `flow`: stk_push (prompt on phone) or redirect (hosted page).
GATEWAYS = {
	"Daraja": {
		"prefix": "daraja",
		"title": "M-Pesa (Safaricom Daraja)",
		"description": "STK Push: the payer approves on their phone with their M-Pesa PIN.",
		"flow": "stk_push",
		"requires_phone": True,
		"icon": "smartphone",
		"adapter": "payment_integration.payment_integration.gateways.daraja.DarajaGateway",
		"docs": "https://developer.safaricom.co.ke/",
	},
	"Pesapal": {
		"prefix": "pesapal",
		"title": "Pesapal",
		"description": "Hosted checkout for cards, M-Pesa, Airtel Money and bank payments.",
		"flow": "redirect",
		"requires_phone": False,
		"icon": "credit-card",
		"adapter": "payment_integration.payment_integration.gateways.pesapal.PesapalGateway",
		"docs": "https://developer.pesapal.com/",
	},
	"Stripe": {
		"prefix": "stripe",
		"title": "Stripe",
		"description": "Stripe Checkout for international cards, Apple Pay and Google Pay.",
		"flow": "redirect",
		"requires_phone": False,
		"icon": "globe",
		"adapter": "payment_integration.payment_integration.gateways.stripe.StripeGateway",
		"docs": "https://docs.stripe.com/payments/checkout",
	},
	"PayPal": {
		"prefix": "paypal",
		"title": "PayPal",
		"description": "PayPal Checkout: PayPal balance, cards and Pay Later (no KES — use USD etc.).",
		"flow": "redirect",
		"requires_phone": False,
		"icon": "wallet",
		"adapter": "payment_integration.payment_integration.gateways.paypal.PayPalGateway",
		"docs": "https://developer.paypal.com/docs/checkout/",
	},
	"Flutterwave": {
		"prefix": "flutterwave",
		"title": "Flutterwave",
		"description": "Cards, M-Pesa, Airtel Money and mobile money across Africa.",
		"flow": "redirect",
		"requires_phone": False,
		"icon": "zap",
		"adapter": "payment_integration.payment_integration.gateways.flutterwave.FlutterwaveGateway",
		"docs": "https://developer.flutterwave.com/docs",
	},
	"Paystack": {
		"prefix": "paystack",
		"title": "Paystack",
		"description": "Cards, M-Pesa (Kenya), mobile money and bank in Kenya, Nigeria, Ghana and South Africa.",
		"flow": "redirect",
		"requires_phone": False,
		"icon": "layers",
		"adapter": "payment_integration.payment_integration.gateways.paystack.PaystackGateway",
		"docs": "https://paystack.com/docs/payments/accept-payments/",
	},
}


class _PrefixedMeta:
	def __init__(self, meta, prefix):
		self._meta, self._prefix = meta, prefix

	def get_label(self, fieldname):
		return self._meta.get_label(f"{self._prefix}_{fieldname}")


class GatewayConfig:
	"""One gateway's slice of Payment Integration Settings, unprefixed:
	cfg.consumer_key reads doc.daraja_consumer_key."""

	def __init__(self, doc, key: str):
		self.doc = doc
		self.gateway = key
		self.prefix = GATEWAYS[key]["prefix"]
		self.doctype = doc.doctype
		self.meta = _PrefixedMeta(doc.meta, self.prefix)

	def field(self, name: str) -> str:
		return f"{self.prefix}_{name}"

	def __getattr__(self, name):
		if name.startswith("_") or name in ("doc", "gateway", "prefix", "doctype", "meta"):
			raise AttributeError(name)
		return self.doc.get(self.field(name))

	def get(self, name, default=None):
		value = self.doc.get(self.field(name))
		return default if value is None else value

	def get_password(self, name, raise_exception=False):
		return self.doc.get_password(self.field(name), raise_exception=raise_exception)

	def set_values(self, values: dict):
		"""Persist system-managed values (IPN id, test results) without a full save."""
		prefixed = {self.field(k): v for k, v in values.items()}
		frappe.db.set_single_value(SETTINGS, prefixed)
		for k, v in prefixed.items():
			self.doc.set(k, v)


def gateway_meta(key: str) -> dict:
	meta = GATEWAYS.get(key)
	if not meta:
		frappe.throw(f"Unknown payment gateway: {key}")
	return meta


def settings_doc():
	"""Fresh (uncached) Payment Integration Settings."""
	return frappe.get_doc(SETTINGS, SETTINGS)


def settings_for(key: str, doc=None) -> GatewayConfig:
	gateway_meta(key)
	return GatewayConfig(doc or settings_doc(), key)


def get_gateway(key_or_config) -> PaymentGateway:
	"""Adapter for a gateway key or its GatewayConfig."""
	config = settings_for(key_or_config) if isinstance(key_or_config, str) else key_or_config
	return frappe.get_attr(gateway_meta(config.gateway)["adapter"])(config)


def is_enabled(key: str) -> bool:
	return bool(int(frappe.db.get_single_value(SETTINGS, f"{gateway_meta(key)['prefix']}_enabled") or 0))


def token_cache_key(key: str) -> str:
	return f"payment_integration:token:{key}"


def clear_token_cache(key: str):
	frappe.cache.delete_value(token_cache_key(key))
