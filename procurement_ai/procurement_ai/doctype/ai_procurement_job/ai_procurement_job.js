frappe.ui.form.on("AI Procurement Job", {
    setup: function (frm) {
        // Default company from AI Procurement Settings
        if (frm.is_new() && !frm.doc.company) {
            frappe.db.get_single_value("AI Procurement Settings", "default_company").then(function (value) {
                if (value) {
                    frm.set_value("company", value);
                }
            });
        }
    },
    refresh: function (frm) {
        // Process button for Pending/Error jobs
        if (frm.doc.status === "Pending" || frm.doc.status === "Error") {
            frm.add_custom_button(__("Process Document"), function () {
                frm.call("process_document").then(() => {
                    frm.reload_doc();
                });
            }).addClass("btn-primary");
        }

        // Unified review UI for both Awaiting Review and Needs Review
        if (frm.doc.status === "Awaiting Review") {
            _render_review_ui(frm, { editable: true });

            frm.add_custom_button(
                __("Approve & Create Documents"),
                function () {
                    _collect_and_approve(frm);
                }
            ).addClass("btn-primary");

            frm.add_custom_button(
                __("Pre-create Items"),
                function () {
                    _precreate_items(frm);
                }
            );

            _add_reject_button(frm);
        }

        if (frm.doc.status === "Needs Review") {
            var has_created_docs = frm.doc.created_po || frm.doc.created_receipt || frm.doc.created_invoice;

            if (has_created_docs) {
                _render_review_ui(frm, {
                    editable: true,
                    show_comparison: true,
                    escalation_reason: frm.doc.escalation_reason,
                });

                frm.add_custom_button(
                    __("Mark as Completed"),
                    function () {
                        frappe.confirm(
                            __("Have you verified the created documents and amounts are correct?"),
                            function () {
                                frm.call("mark_completed").then(function () {
                                    frm.reload_doc();
                                });
                            }
                        );
                    }
                ).addClass("btn-primary");

                frm.add_custom_button(
                    __("Re-approve with Changes"),
                    function () {
                        _collect_and_approve(frm);
                    }
                );
            } else {
                _render_review_ui(frm, {
                    editable: true,
                    escalation_reason: frm.doc.escalation_reason,
                });

                frm.add_custom_button(
                    __("Approve & Create Documents"),
                    function () {
                        _collect_and_approve(frm);
                    }
                ).addClass("btn-primary");

                frm.add_custom_button(
                    __("Pre-create Items"),
                    function () {
                        _precreate_items(frm);
                    }
                );
            }

            _add_reject_button(frm);
        }

        // Show progress indicators
        _render_status_badge(frm);

        // Show created documents in dashboard for Completed status only
        if (frm.doc.status === "Completed") {
            _render_created_docs_dashboard(frm);
        }

        // Realtime progress listener (register once per form instance)
        if (!frm._realtime_bound) {
            frm._realtime_bound = true;
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
        }
    },

    before_save: function (frm) {
        // Collect review UI data into JSON fields so standard Save persists them
        if (
            (frm.doc.status === "Awaiting Review" || frm.doc.status === "Needs Review") &&
            frm.fields_dict.review_html &&
            frm.fields_dict.review_html.$wrapper &&
            frm.fields_dict.review_html.$wrapper.find(".review-field").length
        ) {
            var data = _collect_review_data(frm);
            frm.doc.reviewed_data = JSON.stringify(data.reviewed);
            frm.doc.item_mapping = JSON.stringify(data.item_mapping);
            frm.doc.stock_uom_mapping = JSON.stringify(data.stock_uom_mapping);
        }
    },
    source_document: function (frm) {
        if (frm.doc.source_document) {
            frm.set_value("source_document_url", frm.doc.source_document);
        }
    },
});

// =================================================================
// Review UI rendering (unified)
// =================================================================

var _SUPPLIER_FIELDS = [
    { key: "supplier_name", label: "Supplier Name", type: "text" },
    { key: "supplier_address", label: "Supplier Address", type: "text" },
    { key: "supplier_tax_id", label: "Supplier Tax ID", type: "text" },
    { key: "supplier_email", label: "Supplier Email", type: "text" },
    { key: "supplier_phone", label: "Supplier Phone", type: "text" },
];

var _DOCUMENT_FIELDS = [
    { key: "document_number", label: "Document Number", type: "text" },
    { key: "document_date", label: "Document Date", type: "date" },
    { key: "order_reference", label: "Order Reference", type: "text" },
    { key: "delivery_date", label: "Delivery Date", type: "date" },
    { key: "payment_terms", label: "Payment Terms", type: "text" },
    { key: "currency", label: "Currency", type: "text" },
];

var _TOTALS_FIELDS = [
    { key: "subtotal", label: "Subtotal", type: "number" },
    { key: "tax_amount", label: "Tax Amount", type: "number" },
    { key: "shipping_cost", label: "Shipping Cost", type: "number" },
    { key: "total_amount", label: "Total Amount", type: "number" },
];

// All header fields combined (used by _compute_confidence and _collect_review_data)
var _HEADER_FIELDS = _SUPPLIER_FIELDS.concat(_DOCUMENT_FIELDS).concat(_TOTALS_FIELDS);

var _ITEM_FIELDS = [
    { key: "item_code", label: "Supplier Code" },
    { key: "item_name", label: "Item Name" },
    { key: "description", label: "Description" },
];

var _REVIEW_CSS = '<style>' +
    '.review-input {' +
    '  border: none; border-bottom: 1px solid transparent; background: transparent;' +
    '  padding: 4px 8px; width: 100%; font-size: inherit; color: var(--text-color);' +
    '  transition: all 0.15s ease;' +
    '}' +
    '.review-input:hover { border-bottom-color: var(--gray-300); }' +
    '.review-input:focus { border-bottom-color: var(--primary); background: var(--subtle-fg); outline: none; }' +
    '.review-input[type="number"] { text-align: right; }' +
    '.confidence-full    { border-left: 3px solid #38a169; padding-left: 8px; }' +
    '.confidence-partial { border-left: 3px solid #d69e2e; padding-left: 8px; }' +
    '.confidence-low     { border-left: 3px solid #a0aec0; padding-left: 8px; }' +
    '.review-card {' +
    '  border: 1px solid var(--border-color); border-radius: 8px;' +
    '  padding: 16px; margin-bottom: 16px; background: var(--card-bg);' +
    '}' +
    '.review-card h6 {' +
    '  margin: 0 0 12px; font-size: 0.8em; text-transform: uppercase;' +
    '  letter-spacing: 0.05em; color: var(--text-muted);' +
    '}' +
    '.review-totals { text-align: right; padding: 12px 16px; }' +
    '.review-totals .total-row { display: flex; justify-content: flex-end; gap: 16px; align-items: center; margin-bottom: 4px; }' +
    '.review-totals .total-row label { min-width: 100px; text-align: right; color: var(--text-muted); font-size: 0.9em; }' +
    '.review-totals .total-row .total-input { width: 150px; }' +
    '.review-totals .total-separator { border-top: 1px solid var(--border-color); margin: 8px 0; }' +
    '.comparison-panel {' +
    '  border: 1px solid var(--border-color); border-radius: 8px; padding: 16px;' +
    '  margin-top: 16px; background: var(--subtle-fg);' +
    '}' +
    '.comparison-panel h6 { margin: 0 0 12px; font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); }' +
    '.items-table { width: 100%; border-collapse: collapse; }' +
    '.items-table th { font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.03em; color: var(--text-muted); padding: 6px 8px; border-bottom: 2px solid var(--border-color); }' +
    '.items-table td { padding: 6px 8px; border-bottom: 1px solid var(--border-color); vertical-align: top; }' +
    '.items-table .review-input { padding: 2px 4px; }' +
    '.stock-detail { display: none; margin-top: 4px; padding: 6px 8px; background: var(--subtle-fg); border-radius: 4px; }' +
    '.stock-detail.open { display: flex; align-items: center; gap: 4px; }' +
    '.stock-toggle { cursor: pointer; color: var(--text-muted); font-size: 0.8em; user-select: none; }' +
    '.stock-toggle:hover { color: var(--text-color); }' +
    '</style>';

function _add_reject_button(frm) {
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
                    frm.set_value("human_review_notes", "Rejected: " + values.reason);
                    frm.save();
                },
                __("Reject Job"),
                __("Reject")
            );
        }
    );
}

function _render_field_input(f, val, confidence_map) {
    var escaped = frappe.utils.escape_html(String(val === null || val === undefined ? "" : val));
    var input_type = f.type === "number" ? "number" : f.type === "date" ? "date" : "text";
    var step_attr = f.type === "number" ? ' step="any"' : "";
    var conf_class = _confidence_class(confidence_map[f.key]);
    return '<div class="' + conf_class + '" style="margin-bottom:8px;">' +
        '<label style="font-size:0.8em;color:var(--text-muted);margin-bottom:2px;display:block;">' +
        __(f.label) + '</label>' +
        '<input type="' + input_type + '" class="review-input review-field"' +
        ' data-field="' + f.key + '" value="' + escaped + '"' + step_attr + ' />' +
        '</div>';
}

function _render_review_ui(frm, options) {
    var consensus = {};
    try {
        consensus = JSON.parse(frm.doc.reviewed_data || frm.doc.consensus_data || "{}");
    } catch (e) {
        consensus = {};
    }

    var confidence_map = _compute_confidence(frm);
    var html = _REVIEW_CSS;
    html += '<div class="review-ui" style="padding:10px;">';

    // Escalation banner
    if (options.escalation_reason) {
        html +=
            '<div style="margin-bottom:16px;padding:12px 16px;border-radius:6px;' +
            'background:#fff3cd;border:1px solid #ffc107;">' +
            '<strong style="color:#856404;">' + __("Review Required") + '</strong>' +
            '<pre style="margin:8px 0 0;white-space:pre-wrap;color:#856404;font-size:0.9em;">' +
            frappe.utils.escape_html(options.escalation_reason) + '</pre></div>';
    }

    // Two-column card layout: Supplier + Document Info
    html += '<div class="row">';

    // Supplier card
    html += '<div class="col-md-6">';
    html += '<div class="review-card">';
    html += '<h6>' + __("Supplier") + '</h6>';
    html += '<div class="supplier-match-badge" style="margin-bottom:10px;">' +
        '<span class="text-muted" style="font-size:0.85em;">' + __("Checking...") + '</span></div>';
    _SUPPLIER_FIELDS.forEach(function (f) {
        html += _render_field_input(f, consensus[f.key], confidence_map);
    });
    html += '</div></div>';

    // Document info card
    html += '<div class="col-md-6">';
    html += '<div class="review-card">';
    html += '<h6>' + __("Document Info") + '</h6>';
    _DOCUMENT_FIELDS.forEach(function (f) {
        html += _render_field_input(f, consensus[f.key], confidence_map);
    });
    html += '</div></div>';

    html += '</div>'; // end row

    // Line Items
    var items = consensus.items || [];
    html += '<div class="review-card">';
    html += '<h6>' + __("Line Items") + '</h6>';

    if (items.length > 0) {
        html += '<div style="overflow-x:auto;">';
        html += '<table class="items-table">';
        html += '<thead><tr>';
        html += '<th>#</th>';
        _ITEM_FIELDS.forEach(function (f) {
            html += '<th>' + __(f.label) + '</th>';
        });
        html += '<th>' + __("Qty") + '</th>';
        html += '<th>' + __("Rate") + '</th>';
        html += '<th>' + __("Total") + '</th>';
        html += '<th>' + __("Map to Item") + '</th>';
        html += '</tr></thead><tbody>';

        items.forEach(function (item, idx) {
            var qty = parseFloat(item["quantity"]) || 0;
            var rate = parseFloat(item["unit_price"]) || 0;
            var total = parseFloat(item["total_price"]) || (qty * rate);
            var item_uom = item["uom"] || "Nos";

            html += '<tr data-item-idx="' + idx + '">';
            html += '<td>' + (idx + 1) + '</td>';

            // Text fields (code, name, description)
            _ITEM_FIELDS.forEach(function (f) {
                var val = item[f.key];
                if (val === null || val === undefined) val = "";
                html +=
                    '<td><input type="text" class="review-input review-item-field"' +
                    ' data-idx="' + idx + '" data-field="' + f.key + '"' +
                    ' value="' + frappe.utils.escape_html(String(val)) + '" /></td>';
            });

            // Qty column with toggle for stock detail
            html +=
                '<td class="qty-uom-cell" data-idx="' + idx + '"' +
                ' data-line-total="' + total + '" data-invoice-qty="' + qty + '"' +
                ' data-invoice-rate="' + rate + '" style="white-space:nowrap;">' +
                '<div style="display:flex;align-items:center;gap:4px;">' +
                '<input type="number" class="review-input doc-qty" data-idx="' + idx + '"' +
                ' step="any" style="width:70px;" value="' + qty + '" />' +
                '<span class="stock-toggle" data-idx="' + idx + '" title="' + __("Stock details") + '">&#9660;</span>' +
                '</div>' +
                // Collapsible stock detail row
                '<div class="stock-detail" data-idx="' + idx + '">' +
                '<span style="font-size:0.85em;color:var(--text-muted);">' + __("Stock") + ':&nbsp;</span>' +
                '<input type="number" class="review-input stock-qty" data-idx="' + idx + '"' +
                ' step="any" style="width:60px;" value="' + qty + '" />' +
                '<div class="stock-uom-control" data-idx="' + idx + '"' +
                ' data-initial-value="' + frappe.utils.escape_html(String(item_uom)) + '"' +
                ' style="display:inline-block;width:80px;"></div>' +
                '<span class="qty-info" data-idx="' + idx + '"' +
                ' style="font-size:0.8em;color:var(--text-muted);"></span>' +
                '</div>' +
                '<div class="qty-warning" data-idx="' + idx + '"' +
                ' style="display:none;font-size:0.8em;color:#c53030;margin-top:2px;"></div>' +
                '</td>';

            // Rate (auto-calculated, read-only styled)
            html +=
                '<td><span class="doc-rate-label" data-idx="' + idx + '"' +
                ' style="font-size:0.9em;color:var(--text-muted);">' +
                format_currency(rate) + '</span></td>';

            // Total (bold, from extraction)
            html +=
                '<td><span class="line-total" data-idx="' + idx + '"' +
                ' style="font-weight:600;">' +
                format_currency(total) + '</span></td>';

            // Map to Item
            html +=
                '<td>' +
                '<div class="item-match-cell" data-idx="' + idx + '" style="margin-bottom:4px;">' +
                '<span class="text-muted" style="font-size:0.8em;">' + __("Checking...") + '</span></div>' +
                '<div class="item-link-control" data-idx="' + idx + '"></div>' +
                '</td>';
            html += '</tr>';
        });
        html += '</tbody></table></div>';
    } else {
        html += '<p class="text-muted">' + __("No line items extracted") + '</p>';
    }
    html += '</div>'; // end items card

    // Totals section (receipt-style, right-aligned)
    html += '<div class="review-card">';
    html += '<div class="review-totals">';
    _TOTALS_FIELDS.forEach(function (f, i) {
        // Separator before total_amount
        if (f.key === "total_amount") {
            html += '<div class="total-separator"></div>';
        }
        var val = consensus[f.key];
        if (val === null || val === undefined) val = "";
        var conf_class = _confidence_class(confidence_map[f.key]);
        var is_total = f.key === "total_amount";
        html += '<div class="total-row ' + conf_class + '">';
        html += '<label>' + __(f.label) + ':</label>';
        html += '<input type="number" class="review-input review-field total-input' +
            (is_total ? '" style="font-weight:700;' : '"') +
            ' data-field="' + f.key + '" value="' + frappe.utils.escape_html(String(val)) + '"' +
            ' step="any" />';
        html += '</div>';
    });
    html += '</div></div>';

    // Comparison panel (Needs Review with created docs)
    if (options.show_comparison) {
        html += '<div class="comparison-panel">';
        html += '<h6>' + __("Created Documents") + '</h6>';
        html += '<div class="comparison-docs">';
        if (frm.doc.created_supplier) {
            html += '<div style="margin-bottom:6px;"><a href="/app/supplier/' +
                encodeURIComponent(frm.doc.created_supplier) + '">' +
                __("Supplier") + ': ' + frappe.utils.escape_html(frm.doc.created_supplier) + '</a></div>';
        }
        if (frm.doc.created_po) {
            html += '<div class="comparison-doc-row" data-doctype="Purchase Order"' +
                ' data-name="' + frappe.utils.escape_html(frm.doc.created_po) + '" style="margin-bottom:6px;">' +
                '<a href="/app/purchase-order/' + encodeURIComponent(frm.doc.created_po) + '">' +
                __("PO") + ': ' + frappe.utils.escape_html(frm.doc.created_po) + '</a>' +
                ' <span class="doc-grand-total text-muted"></span></div>';
        }
        if (frm.doc.created_receipt) {
            html += '<div class="comparison-doc-row" data-doctype="Purchase Receipt"' +
                ' data-name="' + frappe.utils.escape_html(frm.doc.created_receipt) + '" style="margin-bottom:6px;">' +
                '<a href="/app/purchase-receipt/' + encodeURIComponent(frm.doc.created_receipt) + '">' +
                __("PR") + ': ' + frappe.utils.escape_html(frm.doc.created_receipt) + '</a>' +
                ' <span class="doc-grand-total text-muted"></span></div>';
        }
        if (frm.doc.created_invoice) {
            html += '<div class="comparison-doc-row" data-doctype="Purchase Invoice"' +
                ' data-name="' + frappe.utils.escape_html(frm.doc.created_invoice) + '" style="margin-bottom:6px;">' +
                '<a href="/app/purchase-invoice/' + encodeURIComponent(frm.doc.created_invoice) + '">' +
                __("PI") + ': ' + frappe.utils.escape_html(frm.doc.created_invoice) + '</a>' +
                ' <span class="doc-grand-total text-muted"></span></div>';
        }
        html += '<div class="comparison-delta" style="margin-top:8px;font-size:0.9em;"></div>';
        html += '</div></div>';
    }

    html += '</div>'; // end review-ui

    // Render into review_html wrapper
    var wrapper = frm.fields_dict.review_html.$wrapper;
    wrapper.html(html);

    // Stock detail toggle
    wrapper.find(".stock-toggle").on("click", function () {
        var idx = $(this).data("idx");
        var $detail = wrapper.find('.stock-detail[data-idx="' + idx + '"]');
        $detail.toggleClass("open");
        $(this).html($detail.hasClass("open") ? "&#9650;" : "&#9660;");
    });

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
                change: function () {
                    frm.dirty();
                    var item_code = control.get_value();
                    if (item_code) {
                        frappe.db.get_value("Item", item_code, "stock_uom", function (r) {
                            if (r && r.stock_uom) {
                                _set_stock_uom_readonly(wrapper, idx, r.stock_uom);
                            }
                        });
                    } else {
                        _set_stock_uom_editable(wrapper, idx);
                    }
                },
            },
            parent: $el,
            render_input: true,
        });
        control.refresh();
        $el.data("control", control);
    });

    // Restore saved item_mapping values (so cleared mappings survive form reload)
    var saved_mapping = {};
    try {
        saved_mapping = JSON.parse(frm.doc.item_mapping || "{}");
    } catch (e) {
        saved_mapping = {};
    }
    wrapper.find(".item-link-control").each(function () {
        var $el = $(this);
        var idx = String($el.data("idx"));
        if (idx in saved_mapping && saved_mapping[idx]) {
            var control = $el.data("control");
            if (control) {
                control.set_value(saved_mapping[idx]);
            }
        }
    });

    // Create Frappe Link controls for Stock UOM fields
    wrapper.find(".stock-uom-control").each(function () {
        var $el = $(this);
        var idx = $el.data("idx");
        var initial_val = $el.data("initial-value") || "Nos";
        var control = frappe.ui.form.make_control({
            df: {
                fieldtype: "Link",
                fieldname: "stock_uom_" + idx,
                options: "UOM",
                placeholder: __("Stock UOM"),
                change: function () {
                    frm.dirty();
                },
            },
            parent: $el,
            render_input: true,
        });
        control.refresh();
        control.set_value(initial_val);
        $el.data("control", control);
    });

    // Quantity cell event handlers
    wrapper.find(".doc-qty").on("change", function () {
        frm.dirty();
        var idx = $(this).data("idx");
        _recalc_qty_cell(wrapper, idx);
    });
    wrapper.find(".stock-qty").on("change", function () {
        frm.dirty();
        var idx = $(this).data("idx");
        _recalc_qty_cell(wrapper, idx);
    });

    // Mark form dirty when any review input changes
    wrapper.find(".review-input").on("change", function () {
        frm.dirty();
    });

    // Async: check which supplier/items already exist
    frm.call("check_review_matches").then(function (r) {
        if (!r || !r.message) return;
        var matches = r.message;
        _render_match_badges(wrapper, matches, frm);
    });

    // Async: fetch grand totals for comparison panel
    if (options.show_comparison) {
        wrapper.find(".comparison-doc-row").each(function () {
            var $row = $(this);
            var doctype = $row.data("doctype");
            var name = $row.data("name");
            frappe.db.get_value(doctype, name, "grand_total", function (r) {
                if (r && r.grand_total !== undefined) {
                    $row.find(".doc-grand-total").text("(" + __("Grand Total") + ": " + format_currency(r.grand_total) + ")");
                }
            });
        });
        // Calculate delta after a short delay to let grand_totals load
        setTimeout(function () {
            var extracted_total = parseFloat(consensus.total_amount) || 0;
            var $delta = wrapper.find(".comparison-delta");
            var doc_totals = [];
            wrapper.find(".doc-grand-total").each(function () {
                var text = $(this).text();
                var match = text.match(/([\d.,]+)/);
                if (match) doc_totals.push(parseFloat(match[1].replace(",", "")));
            });
            if (doc_totals.length > 0 && extracted_total > 0) {
                var max_doc = Math.max.apply(null, doc_totals);
                var delta = Math.abs(extracted_total - max_doc);
                var pct = ((delta / extracted_total) * 100).toFixed(2);
                if (delta > 0.01) {
                    $delta.html(
                        '<span style="color:#c53030;">' +
                        __("Delta") + ': ' + format_currency(delta) + ' (' + pct + '%)' +
                        '</span>'
                    );
                } else {
                    $delta.html('<span style="color:#38a169;">' + __("Amounts match") + '</span>');
                }
            }
        }, 1500);
    }
}

function _render_match_badges(wrapper, matches, frm) {
    // Parse saved item_mapping to respect user-cleared mappings
    var saved_mapping = {};
    try {
        saved_mapping = JSON.parse((frm && frm.doc.item_mapping) || "{}");
    } catch (e) {
        saved_mapping = {};
    }

    // Supplier badge
    var $supplier = wrapper.find(".supplier-match-badge");
    if (matches.supplier) {
        $supplier.html(
            '<a href="/app/supplier/' +
                encodeURIComponent(matches.supplier.name) +
                '">' +
                frappe.utils.escape_html(matches.supplier.name) +
                "</a> " +
                '<span class="badge badge-success" style="background:#38a169;color:#fff;">' +
                __("Exists") + " (" + matches.supplier.method + ")</span>"
        );
    } else {
        $supplier.html(
            '<span class="badge badge-info" style="background:#3182ce;color:#fff;">' +
                __("New — will be created") +
                "</span>"
        );
    }

    // Item badges + UOM adjustments
    var items = matches.items || [];
    items.forEach(function (info, idx) {
        var $cell = wrapper.find('.item-match-cell[data-idx="' + idx + '"]');

        // Set resolved UOM on Stock UOM control
        if (info.resolved_uom) {
            var $stock_uom = wrapper.find('.stock-uom-control[data-idx="' + idx + '"]');
            var stock_uom_control = $stock_uom.data("control");
            if (stock_uom_control) {
                stock_uom_control.set_value(info.resolved_uom);
            }
        }

        // Pre-fill the Link control and show status badge
        var $link = wrapper.find('.item-link-control[data-idx="' + idx + '"]');
        var link_control = $link.data("control");
        var idx_str = String(idx);
        var user_cleared = idx_str in saved_mapping && !saved_mapping[idx_str];
        var user_mapped = idx_str in saved_mapping && !!saved_mapping[idx_str];

        if (user_cleared) {
            // User explicitly cleared this item — don't re-populate, show "New" badge
            $cell.html(
                '<span class="badge" style="background:#3182ce;color:#fff;font-size:0.75em;">' +
                    __("New") + "</span>"
            );
            _set_stock_uom_editable(wrapper, idx);
        } else if (user_mapped) {
            // User explicitly selected an item — keep their choice, show "Exists" badge
            $cell.html(
                '<span class="badge" style="background:#38a169;color:#fff;font-size:0.75em;">' +
                    __("Exists") + "</span>"
            );
            if (info.stock_uom) {
                _set_stock_uom_readonly(wrapper, idx, info.stock_uom);
            }
        } else if (info.exists) {
            if (link_control) {
                link_control.set_value(info.item_code);
            }
            $cell.html(
                '<span class="badge" style="background:#38a169;color:#fff;font-size:0.75em;">' +
                    __("Exists") + "</span>"
            );
            if (info.stock_uom) {
                _set_stock_uom_readonly(wrapper, idx, info.stock_uom);
            }
        } else {
            $cell.html(
                '<span class="badge" style="background:#3182ce;color:#fff;font-size:0.75em;">' +
                    __("New") + "</span>"
            );
        }

        // Pre-fill compact quantity cell when UOM adjustment applies
        if (info.uom_adjustment) {
            var adj = info.uom_adjustment;
            var $qty_cell = wrapper.find('.qty-uom-cell[data-idx="' + idx + '"]');
            // doc-qty = suggested PO qty, stock-qty = original invoice qty (pieces)
            $qty_cell.find('.doc-qty[data-idx="' + idx + '"]').val(adj.suggested_doc_qty);
            $qty_cell.find('.stock-qty[data-idx="' + idx + '"]').val(adj.original_qty);
            _recalc_qty_cell(wrapper, idx);
        }
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

function _confidence_class(info) {
    if (!info || info.total === 0) {
        return "confidence-low";
    }
    if (info.agree === info.total) {
        return "confidence-full";
    }
    if (info.agree > 1) {
        return "confidence-partial";
    }
    return "confidence-low";
}

function _collect_review_data(frm) {
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

    // Collect item fields (text inputs from _ITEM_FIELDS + compact quantity cell)
    var items = consensus.items ? consensus.items.slice() : [];
    wrapper.find(".review-item-field").each(function () {
        var $input = $(this);
        var idx = parseInt($input.data("idx"), 10);
        var field_key = $input.data("field");
        if (!items[idx]) items[idx] = {};
        items[idx][field_key] = $input.val();
    });

    // Collect compact quantity/UOM data from each qty-uom-cell
    wrapper.find(".qty-uom-cell").each(function () {
        var $cell = $(this);
        var idx = parseInt($cell.data("idx"), 10);
        if (!items[idx]) items[idx] = {};

        var doc_qty = parseFloat($cell.find('.doc-qty').val()) || 0;
        var stock_qty_val = parseFloat($cell.find('.stock-qty').val()) || 0;
        var line_total = parseFloat($cell.data("line-total")) || 0;
        var $stock_uom_el = $cell.find('.stock-uom-control');
        var stock_uom_ctrl = $stock_uom_el.data("control");
        var resolved_uom = stock_uom_ctrl ? (stock_uom_ctrl.get_value() || "Nos") : "Nos";

        var factor = doc_qty > 0 ? stock_qty_val / doc_qty : 1;
        var is_bulk = factor > 1 && Number.isInteger(factor);

        items[idx].quantity = doc_qty;
        items[idx].unit_price = doc_qty > 0 ? line_total / doc_qty : 0;
        items[idx].total_price = line_total;
        items[idx].uom = is_bulk ? String(Math.round(factor)) : resolved_uom;
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

    // Collect stock UOM mapping (for new item creation)
    var stock_uom_mapping = {};
    wrapper.find(".stock-uom-control").each(function () {
        var $el = $(this);
        var idx = $el.data("idx");
        var control = $el.data("control");
        var val = control ? control.get_value() : null;
        stock_uom_mapping[idx] = val || null;
    });

    return {
        reviewed: reviewed,
        item_mapping: item_mapping,
        stock_uom_mapping: stock_uom_mapping,
    };
}

function _save_review_data(frm, data) {
    frm.set_value("reviewed_data", JSON.stringify(data.reviewed));
    frm.set_value("item_mapping", JSON.stringify(data.item_mapping));
    frm.set_value("stock_uom_mapping", JSON.stringify(data.stock_uom_mapping));
    return frm.save();
}

function _collect_and_approve(frm) {
    var data = _collect_review_data(frm);
    frm.call("approve_and_create", {
        reviewed_data: JSON.stringify(data.reviewed),
        item_mapping: JSON.stringify(data.item_mapping),
        stock_uom_mapping: JSON.stringify(data.stock_uom_mapping),
    }).then(function () {
        frm.reload_doc();
    });
}

function _precreate_items(frm) {
    var data = _collect_review_data(frm);
    _save_review_data(frm, data).then(function () {
        frm.call("precreate_items").then(function (r) {
            if (!r || !r.message) return;
            var results = r.message;
            var wrapper = frm.fields_dict.review_html.$wrapper;
            var created_count = 0;

            results.forEach(function (item) {
                var idx = item.idx;

                // Update Link control with resolved item_code
                var $link = wrapper.find('.item-link-control[data-idx="' + idx + '"]');
                var link_control = $link.data("control");
                if (link_control) {
                    link_control.set_value(item.item_code);
                }

                // Update badge
                var $cell = wrapper.find('.item-match-cell[data-idx="' + idx + '"]');
                if (item.created) {
                    created_count++;
                    $cell.html(
                        '<span class="badge" style="background:#38a169;color:#fff;font-size:0.75em;">' +
                            __("Created") + "</span>"
                    );
                } else {
                    $cell.html(
                        '<span class="badge" style="background:#38a169;color:#fff;font-size:0.75em;">' +
                            __("Exists") + "</span>"
                    );
                }

                // Lock stock UOM (item now exists in ERPNext)
                if (item.stock_uom) {
                    _set_stock_uom_readonly(wrapper, idx, item.stock_uom);
                }
            });

            if (created_count > 0) {
                frappe.show_alert({
                    message: __("{0} item(s) created. You can edit them before approving.", [created_count]),
                    indicator: "green",
                });
            } else {
                frappe.show_alert({
                    message: __("All items already exist."),
                    indicator: "blue",
                });
            }
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

function _render_created_docs_dashboard(frm) {
    var html = '<div class="created-docs-summary">';
    if (frm.doc.created_supplier) {
        html += '<a href="/app/supplier/' + encodeURIComponent(frm.doc.created_supplier) +
            '">' + __("Supplier") + ': ' + frappe.utils.escape_html(frm.doc.created_supplier) + '</a><br>';
    }
    if (frm.doc.created_po) {
        html += '<a href="/app/purchase-order/' + encodeURIComponent(frm.doc.created_po) +
            '">' + __("PO") + ': ' + frappe.utils.escape_html(frm.doc.created_po) + '</a><br>';
    }
    if (frm.doc.created_receipt) {
        html += '<a href="/app/purchase-receipt/' + encodeURIComponent(frm.doc.created_receipt) +
            '">' + __("PR") + ': ' + frappe.utils.escape_html(frm.doc.created_receipt) + '</a><br>';
    }
    if (frm.doc.created_invoice) {
        html += '<a href="/app/purchase-invoice/' + encodeURIComponent(frm.doc.created_invoice) +
            '">' + __("PI") + ': ' + frappe.utils.escape_html(frm.doc.created_invoice) + '</a><br>';
    }
    html += '</div>';

    if (frm.doc.created_supplier || frm.doc.created_po || frm.doc.created_receipt || frm.doc.created_invoice) {
        frm.dashboard.add_section(html, __("Created Documents"));
    }
}

function _set_stock_uom_readonly(wrapper, idx, stock_uom) {
    var $el = wrapper.find('.stock-uom-control[data-idx="' + idx + '"]');
    var control = $el.data("control");
    if (control) {
        control.set_value(stock_uom);
        control.$input.prop("disabled", true);
        control.$input.css("background", "var(--subtle-fg)");
    }
}

function _set_stock_uom_editable(wrapper, idx) {
    var $el = wrapper.find('.stock-uom-control[data-idx="' + idx + '"]');
    var control = $el.data("control");
    if (control) {
        control.$input.prop("disabled", false);
        control.$input.css("background", "");
    }
}

// =================================================================
// Compact quantity cell helpers
// =================================================================

function _recalc_qty_cell(wrapper, idx) {
    var $cell = wrapper.find('.qty-uom-cell[data-idx="' + idx + '"]');
    if (!$cell.length) return;

    var line_total = parseFloat($cell.data("line-total")) || 0;
    var doc_qty = parseFloat($cell.find(".doc-qty").val()) || 0;
    var stock_qty = parseFloat($cell.find(".stock-qty").val()) || 0;

    var rate = doc_qty > 0 ? line_total / doc_qty : 0;
    var factor = doc_qty > 0 ? stock_qty / doc_qty : 1;
    var is_bulk = factor > 1 && Number.isInteger(factor);

    // Update rate label (compact: "× €0.04")
    $cell.find('.doc-rate-label[data-idx="' + idx + '"]').html(
        "&times;&thinsp;" + format_currency(rate)
    );

    // Update total
    $cell.find('.line-total[data-idx="' + idx + '"]').html(
        "=&thinsp;<strong>" + format_currency(line_total) + "</strong>"
    );

    // Update factor info
    var $info = $cell.find('.qty-info[data-idx="' + idx + '"]');
    if (is_bulk) {
        $info.text("(\u00d7" + Math.round(factor) + ")");
    } else {
        $info.text("");
    }

    // Sub-cent validation
    _validate_rate(wrapper, idx, rate, line_total, doc_qty);
}

function _validate_rate(wrapper, idx, rate, total, current_left) {
    var $cell = wrapper.find('.qty-uom-cell[data-idx="' + idx + '"]');
    var $warning = $cell.find('.qty-warning[data-idx="' + idx + '"]');
    var $doc_qty = $cell.find('.doc-qty[data-idx="' + idx + '"]');

    if (!_has_cent_fractions_js(rate)) {
        $warning.hide();
        $doc_qty.css("border-color", "");
        return;
    }

    var total_cents = Math.round(total * 100);
    var suggestions = _find_nearby_divisors(total_cents, current_left, 3);
    if (suggestions.length === 0) {
        $warning.hide();
        return;
    }
    var msg = __("Sub-cent rate") + ". " + __("Try") + ": " +
        suggestions.map(function (s) {
            return s + " (" + format_currency(total / s) + ")";
        }).join(", ");
    $warning.text(msg).show();
    $doc_qty.css("border-color", "#c53030");
}

function _has_cent_fractions_js(rate) {
    // Check if rate has more than 2 decimal places
    return Math.abs(Math.round(rate * 100) / 100 - rate) > 1e-9;
}

function _find_nearby_divisors(total_cents, near, count) {
    // Find 'count' divisors of total_cents closest to 'near'
    if (total_cents <= 0 || !near) return [];
    var divisors = [];
    var limit = Math.min(total_cents, near * 10);
    for (var d = 1; d <= limit; d++) {
        if (total_cents % d === 0) {
            divisors.push(d);
        }
    }
    // Sort by distance from 'near'
    divisors.sort(function (a, b) {
        return Math.abs(a - near) - Math.abs(b - near);
    });
    // Exclude the current value if it's a sub-cent rate
    divisors = divisors.filter(function (d) {
        return d !== near;
    });
    return divisors.slice(0, count);
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
