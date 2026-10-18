"""Permission + navigation for the payments admin UI, registered into the
common_utils registries (same mechanism as pilates_pro.lms_domains)."""

PERM_MANAGE_GATEWAYS = "manage_payment_gateways"

NAV_ITEMS = [
	{
		# Everyone who pays (learners, applicants) sees their own payments and receipts.
		"key": "my-payments",
		"label": "My payments",
		"href": "/my-payments",
		"icon": "my-payments",
		"section": "main",
	},
	{
		"key": "payment-gateways",
		"label": "Payment Gateways",
		"href": "/settings/payment-gateways",
		"icon": "payment-gateways",
		"permission": PERM_MANAGE_GATEWAYS,
		"section": "manage",
	},
]

_registered = False


def ensure_registered():
	"""auth_hooks entry: never let a navigation nicety break authentication."""
	try:
		register()
	except Exception:
		pass


def register():
	global _registered
	if _registered:
		return
	_registered = True
	from common_utils.api.navigation import register_navigation_domain
	from common_utils.api.permissions import register_permission_domain

	register_permission_domain(
		domain="payments",
		permissions=[PERM_MANAGE_GATEWAYS],
		role_permissions={"Payments Manager": [PERM_MANAGE_GATEWAYS]},
		roles=["Payments Manager"],
	)
	register_navigation_domain(domain="payments", items=NAV_ITEMS)
