"""Provider registry. Each adapter turns one gateway's API into the common
PaymentProvider interface (see base.py). Adding a gateway = one module here +
an entry in PROVIDERS + its fields on Payment Provider Account."""

import frappe

from payment_integration.payment_integration.providers.base import (  # noqa: F401
	PaymentProvider,
	ProviderError,
	ProviderResult,
)


def _registry() -> dict:
	from payment_integration.payment_integration.providers.daraja import DarajaProvider
	from payment_integration.payment_integration.providers.pesapal import PesapalProvider

	return {"Daraja": DarajaProvider, "Pesapal": PesapalProvider}


def get_provider(account) -> PaymentProvider:
	"""Adapter for a Payment Provider Account (doc or name)."""
	if isinstance(account, str):
		account = frappe.get_doc("Payment Provider Account", account)
	cls = _registry().get(account.provider)
	if not cls:
		frappe.throw(f"Unsupported payment provider: {account.provider}")
	return cls(account)


def token_cache_key(account_name: str) -> str:
	return f"payment_integration:token:{account_name}"


def clear_token_cache(account_name: str):
	frappe.cache.delete_value(token_cache_key(account_name))
