// One tab per gateway; each gets its own Test button. See gateways/__init__.py.
const PI_GATEWAYS = [
	["Daraja", "M-Pesa"],
	["Pesapal", "Pesapal"],
	["Stripe", "Stripe"],
	["PayPal", "PayPal"],
	["Flutterwave", "Flutterwave"],
	["Paystack", "Paystack"],
];

frappe.ui.form.on("Payment Integration Settings", {
	refresh(frm) {
		PI_GATEWAYS.forEach(([key, label]) => {
			frm.add_custom_button(
				__("Test {0}", [label]),
				() => {
					if (frm.is_dirty()) {
						frappe.msgprint(__("Save first, then test."));
						return;
					}
					frm.call("test_connection", { gateway: key }).then((r) => {
						const res = r.message || {};
						frappe.msgprint({ title: label, message: res.message, indicator: res.ok ? "green" : "red" });
						frm.reload_doc();
					});
				},
				__("Test Connection")
			);
		});
		frm.add_custom_button(__("Preview Receipt"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save first to preview your latest changes."));
				return;
			}
			window.open("/api/method/payment_integration.payment_integration.api.payments.preview_receipt");
		});
	},
});
