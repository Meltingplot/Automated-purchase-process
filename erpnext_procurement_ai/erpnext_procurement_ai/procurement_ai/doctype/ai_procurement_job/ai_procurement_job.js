frappe.ui.form.on("AI Procurement Job", {
    refresh: function (frm) {
        // Process button for Pending/Error jobs
        if (frm.doc.status === "Pending" || frm.doc.status === "Error") {
            frm.add_custom_button(__("Process Document"), function () {
                frm.call("process_document").then(() => {
                    frm.reload_doc();
                });
            }).addClass("btn-primary");
        }

        // Approve button for Needs Review jobs
        if (frm.doc.status === "Needs Review") {
            frm.add_custom_button(
                __("Approve & Create"),
                function () {
                    frappe.confirm(
                        __(
                            "This will create the procurement documents from the extracted data. Continue?"
                        ),
                        function () {
                            frm.call({
                                method: "process_document",
                                callback: function () {
                                    frm.reload_doc();
                                },
                            });
                        }
                    );
                },
                __("Review")
            );

            frm.add_custom_button(
                __("Reject"),
                function () {
                    frappe.prompt(
                        {
                            fieldname: "reason",
                            fieldtype: "Small Text",
                            label: "Rejection Reason",
                            reqd: 1,
                        },
                        function (values) {
                            frm.set_value("status", "Error");
                            frm.set_value(
                                "human_review_notes",
                                "Rejected: " + values.reason
                            );
                            frm.save();
                        },
                        __("Reject Job"),
                        __("Reject")
                    );
                },
                __("Review")
            );
        }

        // Show progress indicators
        _render_status_badge(frm);

        // Show created documents links
        _render_created_docs(frm);

        // Realtime progress listener
        frappe.realtime.on("ai_procurement_progress", function (data) {
            if (data.job === frm.doc.name) {
                _update_progress(frm, data.stage);
                if (data.stage === "completed" || data.stage === "error" || data.stage === "needs_review") {
                    frm.reload_doc();
                }
            }
        });
    },

    source_document: function (frm) {
        if (frm.doc.source_document) {
            frm.set_value("source_document_url", frm.doc.source_document);
        }
    },
});

function _render_status_badge(frm) {
    var color_map = {
        Pending: "orange",
        Processing: "blue",
        "Needs Review": "yellow",
        Completed: "green",
        Error: "red",
    };
    var color = color_map[frm.doc.status] || "grey";

    frm.dashboard.set_headline(
        '<span class="indicator whitespace-nowrap ' +
            color +
            '"><span class="hidden-xs">' +
            __(frm.doc.status) +
            "</span></span>" +
            (frm.doc.confidence_score
                ? ' &mdash; Confidence: <strong>' +
                  (frm.doc.confidence_score * 100).toFixed(1) +
                  "%</strong>"
                : "")
    );
}

function _render_created_docs(frm) {
    if (frm.doc.status !== "Completed") return;

    var html = '<div class="created-docs-summary">';
    if (frm.doc.created_supplier) {
        html +=
            '<a href="/app/supplier/' +
            frm.doc.created_supplier +
            '">Supplier: ' +
            frm.doc.created_supplier +
            "</a><br>";
    }
    if (frm.doc.created_po) {
        html +=
            '<a href="/app/purchase-order/' +
            frm.doc.created_po +
            '">PO: ' +
            frm.doc.created_po +
            "</a><br>";
    }
    if (frm.doc.created_receipt) {
        html +=
            '<a href="/app/purchase-receipt/' +
            frm.doc.created_receipt +
            '">Receipt: ' +
            frm.doc.created_receipt +
            "</a><br>";
    }
    if (frm.doc.created_invoice) {
        html +=
            '<a href="/app/purchase-invoice/' +
            frm.doc.created_invoice +
            '">Invoice: ' +
            frm.doc.created_invoice +
            "</a><br>";
    }
    html += "</div>";

    if (
        frm.doc.created_supplier ||
        frm.doc.created_po ||
        frm.doc.created_receipt ||
        frm.doc.created_invoice
    ) {
        frm.dashboard.add_section(html, __("Created Documents"));
    }
}

function _update_progress(frm, stage) {
    var stages = {
        ocr_complete: "OCR extraction complete...",
        extraction_complete: "LLM extraction & consensus complete...",
        needs_review: "Manual review required",
        completed: "Processing complete!",
        error: "An error occurred during processing",
    };
    var msg = stages[stage] || stage;
    frappe.show_alert({ message: msg, indicator: stage === "error" ? "red" : "blue" });
}
