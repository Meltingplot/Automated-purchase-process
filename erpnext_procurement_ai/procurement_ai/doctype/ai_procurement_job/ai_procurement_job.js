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

        // Awaiting Review: render review form + approve/reject buttons
        if (frm.doc.status === "Awaiting Review") {
            _render_review_form(frm);

            frm.add_custom_button(
                __("Approve & Create Documents"),
                function () {
                    _collect_and_approve(frm);
                }
            ).addClass("btn-primary");

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
                }
            );
        }

        // Approve button for Needs Review jobs (escalation)
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
                if (
                    data.stage === "completed" ||
                    data.stage === "error" ||
                    data.stage === "needs_review" ||
                    data.stage === "awaiting_review"
                ) {
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

// =================================================================
// Review form rendering
// =================================================================

var _HEADER_FIELDS = [
    { key: "supplier_name", label: "Supplier Name", type: "text" },
    { key: "supplier_address", label: "Supplier Address", type: "text" },
    { key: "supplier_tax_id", label: "Supplier Tax ID", type: "text" },
    { key: "supplier_email", label: "Supplier Email", type: "text" },
    { key: "supplier_phone", label: "Supplier Phone", type: "text" },
    { key: "document_number", label: "Document Number", type: "text" },
    { key: "document_date", label: "Document Date", type: "date" },
    { key: "order_reference", label: "Order Reference", type: "text" },
    { key: "delivery_date", label: "Delivery Date", type: "date" },
    { key: "payment_terms", label: "Payment Terms", type: "text" },
    { key: "currency", label: "Currency", type: "text" },
    { key: "subtotal", label: "Subtotal", type: "number" },
    { key: "tax_amount", label: "Tax Amount", type: "number" },
    { key: "total_amount", label: "Total Amount", type: "number" },
    { key: "shipping_cost", label: "Shipping Cost", type: "number" },
];

var _ITEM_FIELDS = [
    { key: "item_code", label: "Supplier Code" },
    { key: "item_name", label: "Item Name" },
    { key: "description", label: "Description" },
    { key: "quantity", label: "Qty" },
    { key: "unit_price", label: "Rate" },
    { key: "uom", label: "UOM" },
];

function _render_review_form(frm) {
    var consensus = {};
    try {
        consensus = JSON.parse(frm.doc.consensus_data || "{}");
    } catch (e) {
        consensus = {};
    }

    var confidence_map = _compute_confidence(frm);

    var html = '<div class="review-form" style="padding:10px;">';

    // Header fields
    html += '<h5 style="margin-bottom:12px;">' + __("Document Fields") + "</h5>";
    html += '<table class="table table-bordered table-sm">';
    html += "<thead><tr><th>" + __("Field") + "</th><th>" + __("Value") + "</th>";
    html += "<th style='width:80px;'>" + __("Agreement") + "</th></tr></thead><tbody>";

    _HEADER_FIELDS.forEach(function (f) {
        var val = consensus[f.key];
        if (val === null || val === undefined) val = "";
        var input_type = f.type === "number" ? "number" : f.type === "date" ? "date" : "text";
        var step_attr = f.type === "number" ? ' step="any"' : "";
        var badge = _confidence_badge(confidence_map[f.key]);

        html += "<tr>";
        html += "<td><strong>" + __(f.label) + "</strong></td>";
        html +=
            '<td><input type="' + input_type + '" class="form-control input-sm review-field"' +
            ' data-field="' + f.key + '" value="' + frappe.utils.escape_html(String(val)) + '"' +
            step_attr + " /></td>";
        html += "<td>" + badge + "</td>";
        html += "</tr>";
    });
    html += "</tbody></table>";

    // Items table
    var items = consensus.items || [];
    html +=
        '<h5 style="margin-top:20px;margin-bottom:12px;">' +
        __("Line Items") +
        "</h5>";

    if (items.length > 0) {
        html += '<div style="overflow-x:auto;">';
        html += '<table class="table table-bordered table-sm">';
        html += "<thead><tr><th>#</th>";
        _ITEM_FIELDS.forEach(function (f) {
            html += "<th>" + __(f.label) + "</th>";
        });
        html += "<th>" + __("Map to Item") + "</th></tr></thead><tbody>";

        items.forEach(function (item, idx) {
            html += '<tr data-item-idx="' + idx + '">';
            html += "<td>" + (idx + 1) + "</td>";
            _ITEM_FIELDS.forEach(function (f) {
                var val = item[f.key];
                if (val === null || val === undefined) val = "";
                var input_type =
                    f.key === "quantity" || f.key === "unit_price"
                        ? "number"
                        : "text";
                var step_attr = input_type === "number" ? ' step="any"' : "";
                html +=
                    '<td><input type="' + input_type + '"' +
                    ' class="form-control input-xs review-item-field"' +
                    ' data-idx="' + idx + '" data-field="' + f.key + '"' +
                    ' value="' + frappe.utils.escape_html(String(val)) + '"' +
                    step_attr + " /></td>";
            });
            html +=
                '<td><div class="item-link-control" data-idx="' + idx + '"></div></td>';
            html += "</tr>";
        });
        html += "</tbody></table></div>";
    } else {
        html += '<p class="text-muted">' + __("No line items extracted") + "</p>";
    }

    html += "</div>";

    // Render into review_html wrapper
    var wrapper = frm.fields_dict.review_html.$wrapper;
    wrapper.html(html);

    // Create Frappe Link controls for "Map to Item"
    wrapper.find(".item-link-control").each(function () {
        var $el = $(this);
        var idx = $el.data("idx");
        var control = frappe.ui.form.make_control({
            df: {
                fieldtype: "Link",
                fieldname: "item_map_" + idx,
                options: "Item",
                placeholder: __("Auto-resolve"),
            },
            parent: $el,
            render_input: true,
        });
        control.refresh();
        $el.data("control", control);
    });
}

function _compute_confidence(frm) {
    // Parse each extraction result and compute per-field agreement
    var results = (frm.doc.extraction_results || []).map(function (row) {
        try {
            return JSON.parse(row.extracted_data || "{}");
        } catch (e) {
            return {};
        }
    });

    var total = results.length;
    if (total === 0) return {};

    var confidence_map = {};

    _HEADER_FIELDS.forEach(function (f) {
        var values = results
            .map(function (r) {
                var v = r[f.key];
                return v === null || v === undefined ? "" : String(v).trim();
            })
            .filter(function (v) {
                return v !== "";
            });

        if (values.length === 0) {
            confidence_map[f.key] = { agree: 0, total: total };
            return;
        }

        // Count occurrences of the most common value
        var counts = {};
        values.forEach(function (v) {
            counts[v] = (counts[v] || 0) + 1;
        });
        var max_count = Math.max.apply(null, Object.values(counts));
        confidence_map[f.key] = { agree: max_count, total: total };
    });

    return confidence_map;
}

function _confidence_badge(info) {
    if (!info || info.total === 0) {
        return '<span class="text-muted">-</span>';
    }
    if (info.agree === info.total) {
        return (
            '<span class="badge badge-success" style="background:#38a169;color:#fff;">' +
            info.agree + "/" + info.total + "</span>"
        );
    }
    return (
        '<span class="badge badge-warning" style="background:#d69e2e;color:#fff;">' +
        info.agree + "/" + info.total + "</span>"
    );
}

function _collect_and_approve(frm) {
    var wrapper = frm.fields_dict.review_html.$wrapper;
    var consensus = {};
    try {
        consensus = JSON.parse(frm.doc.consensus_data || "{}");
    } catch (e) {
        consensus = {};
    }

    // Collect header fields
    var reviewed = Object.assign({}, consensus);
    wrapper.find(".review-field").each(function () {
        var $input = $(this);
        var field_key = $input.data("field");
        var val = $input.val();

        // Preserve numeric types
        var field_def = _HEADER_FIELDS.find(function (f) {
            return f.key === field_key;
        });
        if (field_def && field_def.type === "number") {
            reviewed[field_key] = val ? parseFloat(val) : null;
        } else {
            reviewed[field_key] = val;
        }
    });

    // Collect item fields
    var items = consensus.items ? consensus.items.slice() : [];
    wrapper.find(".review-item-field").each(function () {
        var $input = $(this);
        var idx = parseInt($input.data("idx"), 10);
        var field_key = $input.data("field");
        var val = $input.val();

        if (!items[idx]) items[idx] = {};
        if (field_key === "quantity" || field_key === "unit_price") {
            items[idx][field_key] = val ? parseFloat(val) : null;
        } else {
            items[idx][field_key] = val;
        }
    });
    reviewed.items = items;

    // Collect item mapping
    var item_mapping = {};
    wrapper.find(".item-link-control").each(function () {
        var $el = $(this);
        var idx = $el.data("idx");
        var control = $el.data("control");
        var val = control ? control.get_value() : null;
        item_mapping[idx] = val || null;
    });

    // Save reviewed data and item mapping to the doc, then call approve
    frm.set_value("reviewed_data", JSON.stringify(reviewed));
    frm.set_value("item_mapping", JSON.stringify(item_mapping));
    frm.save().then(function () {
        frm.call("approve_and_create").then(function () {
            frm.reload_doc();
        });
    });
}

// =================================================================
// Status badge & created docs
// =================================================================

function _render_status_badge(frm) {
    var color_map = {
        Pending: "orange",
        Processing: "blue",
        "Awaiting Review": "cyan",
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
        awaiting_review: "Extraction complete, awaiting review...",
        needs_review: "Manual review required",
        completed: "Processing complete!",
        error: "An error occurred during processing",
    };
    var msg = stages[stage] || stage;
    frappe.show_alert({ message: msg, indicator: stage === "error" ? "red" : "blue" });
}
