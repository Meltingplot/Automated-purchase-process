frappe.ui.form.on("Purchase Document", {
    refresh: function (frm) {
        // Reprocess button
        if (frm.doc.status === "Error" || frm.doc.status === "Review") {
            frm.add_custom_button(__("Reprocess"), function () {
                frm.call({
                    method: "reprocess",
                    doc: frm.doc,
                    callback: function () {
                        frm.reload_doc();
                    },
                });
            });
        }

        // Approve button
        if (frm.doc.status === "Review" || frm.doc.status === "Extracted") {
            frm.add_custom_button(
                __("Approve & Create Documents"),
                function () {
                    frappe.confirm(
                        __(
                            "This will create Purchase Order and related documents in ERPNext. Continue?"
                        ),
                        function () {
                            frm.call({
                                method: "approve_and_create",
                                doc: frm.doc,
                                callback: function () {
                                    frm.reload_doc();
                                },
                            });
                        }
                    );
                },
                __("Actions")
            );
        }

        // Status indicator
        if (frm.doc.comparison_score) {
            var score = frm.doc.comparison_score;
            var indicator = "green";
            if (score < 0.7) {
                indicator = "red";
            } else if (score < 0.95) {
                indicator = "orange";
            }
            frm.dashboard.add_indicator(
                __("Dual-Model Agreement: {0}%", [
                    (score * 100).toFixed(1),
                ]),
                indicator
            );
        }
    },
});
