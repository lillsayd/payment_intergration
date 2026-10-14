// Copyright (c) 2026, Said Omar and contributors
// For license information, please see license.txt

frappe.ui.form.on("Payment Provider Account", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Test Connection"), () =>
			frm.call("test_connection").then((r) => {
				frappe.msgprint(r.message);
				frm.reload_doc();
			})
		);
		if (frm.doc.provider === "Pesapal") {
			frm.add_custom_button(__("Register IPN"), () =>
				frm.call("register_ipn").then(() => frm.reload_doc())
			);
		}
	},
});
