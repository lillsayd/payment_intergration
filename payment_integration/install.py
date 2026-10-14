import frappe


def after_install():
	ensure_roles()


def after_migrate():
	ensure_roles()


def ensure_roles():
	"""Idempotent: role for staff who manage gateways and view transactions."""
	if not frappe.db.exists("Role", "Payments Manager"):
		role = frappe.new_doc("Role")
		role.role_name = "Payments Manager"
		role.desk_access = 1
		role.insert(ignore_permissions=True)
		frappe.db.commit()
