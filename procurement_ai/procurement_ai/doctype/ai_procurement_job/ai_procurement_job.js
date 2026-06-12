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
            frm.doc.supplier_mapping = data.supplier_mapping || null;
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

// Determines which documents get created (Invoice -> PO+PR+PI etc.),
// so the reviewer must be able to correct a wrong classification.
var _DOCUMENT_TYPE_OPTIONS = [
    { value: "cart", label: "Cart" },
    { value: "order_confirmation", label: "Order Confirmation" },
    { value: "delivery_note", label: "Delivery Note" },
    { value: "invoice", label: "Invoice" },
];

var _DOCUMENT_FIELDS = [
    { key: "document_type", label: "Document Type", type: "select", options: _DOCUMENT_TYPE_OPTIONS },
    { key: "document_number", label: "Document Number", type: "text" },
    { key: "document_date", label: "Document Date", type: "date" },
    { key: "order_reference", label: "Order Reference", type: "text" },
    { key: "delivery_date", label: "Delivery Date", type: "date" },
    { key: "payment_terms", label: "Payment Terms", type: "text" },
    { key: "currency", label: "Currency", type: "link", link_doctype: "Currency" },
    { key: "notes", label: "Notes", type: "text" },
];

var _TOTALS_FIELDS = [
    { key: "subtotal", label: "Subtotal", type: "number" },
    { key: "tax_amount", label: "Tax Amount", type: "number" },
    { key: "shipping_cost", label: "Shipping Cost", type: "number" },
    { key: "total_amount", label: "Total Amount", type: "number" },
];

// All header fields combined (used by _compute_confidence and _collect_review_data)
var _HEADER_FIELDS = _SUPPLIER_FIELDS.concat(_DOCUMENT_FIELDS).concat(_TOTALS_FIELDS);

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
    // Fixed layout: name/description share the flexible space, numeric
    // columns stay narrow, inputs fill their cell. Wrapper scrolls if needed.
    '.items-table { width: 100%; border-collapse: collapse; table-layout: fixed; min-width: 720px; }' +
    '.items-table th { font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.02em; color: var(--text-muted); padding: 6px; border-bottom: 2px solid var(--border-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }' +
    '.items-table td { padding: 6px; border-bottom: 1px solid var(--border-color); vertical-align: top; }' +
    // Each item = two rows: the main row keeps the numbers, the text row
    // below carries name + description at full width and the row border.
    '.items-table tr.item-main-row td { border-bottom: none; padding-bottom: 2px; }' +
    '.items-table tr.item-text-row td { border-bottom: 1px solid var(--border-color); padding-top: 0; padding-bottom: 10px; }' +
    '.item-text-row .text-fields { display: flex; gap: 8px; align-items: flex-start; }' +
    '.item-text-row .item-name-input { flex: 2; font-weight: 500; }' +
    // Description wraps and grows with its content (auto-sized textarea)
    '.item-text-row .item-desc-input { flex: 3; color: var(--text-muted); resize: none; overflow: hidden; line-height: 1.4; min-height: 26px; font-family: inherit; }' +
    // Map to Item: badge and Link control share one line
    '.map-cell { display: flex; align-items: center; gap: 6px; }' +
    '.map-cell .item-match-cell { flex-shrink: 0; }' +
    '.map-cell .item-link-control { flex: 1; min-width: 0; }' +
    '.items-table .review-input { padding: 2px 4px; }' +
    '.items-table .line-total { display: block; text-align: right; white-space: nowrap; }' +
    '.stock-summary { cursor: pointer; margin-top: 2px; font-size: 0.85em; color: var(--text-muted); white-space: nowrap; user-select: none; }' +
    '.stock-summary:hover { color: var(--text-color); }' +
    '.stock-summary .stock-edit-icon { font-size: 0.85em; opacity: 0.6; }' +
    // Edit row stacks vertically so it fits the narrow qty column
    '.stock-detail { display: none; margin-top: 2px; }' +
    '.stock-detail.open { display: block; }' +
    '.stock-detail .review-input, .stock-detail .stock-uom-control { display: block; width: 100% !important; margin-top: 2px; }' +
    '.uom-new-hint { display: none; margin-top: 2px; font-size: 0.75em; color: #d69e2e; }' +
    // Link-control dropdowns (awesomplete) in the table: anchor to the right
    // edge of their column and open leftwards over the table, so the list is
    // not clipped by the scrolling table wrapper.
    '.items-table .awesomplete > ul { z-index: 100; }' +
    '.items-table .item-link-control .awesomplete > ul { left: auto !important; right: 0 !important; width: 320px; max-width: 60vw; }' +
    // Theme-aware colors (work in dark mode, fall back to light-theme values)
    '.review-banner-warning { background: var(--bg-yellow, #fff3cd); border: 1px solid var(--yellow-300, #ffc107); color: var(--text-on-yellow, #856404); }' +
    '.review-banner-warning pre { color: inherit; }' +
    '.review-badge { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 0.75em; }' +
    '.review-badge-success { background: var(--bg-green, #38a169); color: var(--text-on-green, #fff); }' +
    '.review-badge-info { background: var(--bg-blue, #3182ce); color: var(--text-on-blue, #fff); }' +
    '.review-warning-text { color: var(--red-500, #c53030); }' +
    '.review-success-text { color: var(--green-600, #38a169); }' +
    '.item-delete { border: none; background: transparent; color: var(--text-muted); cursor: pointer; font-size: 1.1em; line-height: 1; padding: 2px 6px; }' +
    '.item-delete:hover { color: var(--red-500, #c53030); }' +
    '.totals-check { text-align: right; font-size: 0.85em; margin-bottom: 8px; }' +
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
    var conf_class = _confidence_class(confidence_map[f.key]);
    var label_html = '<label style="font-size:0.8em;color:var(--text-muted);margin-bottom:2px;display:block;">' +
        __(f.label) + '</label>';

    // Link fields (e.g. Currency) render as a Frappe Link control placeholder,
    // wired up later in _render_review_ui.
    if (f.type === "link") {
        return '<div class="' + conf_class + '" style="margin-bottom:8px;">' + label_html +
            '<div class="header-link-control" data-field="' + f.key + '"' +
            ' data-doctype="' + f.link_doctype + '"' +
            ' data-initial-value="' + escaped + '"></div></div>';
    }

    if (f.type === "select") {
        var val_str = String(val === null || val === undefined ? "" : val);
        var found = false;
        var options_html = "";
        (f.options || []).forEach(function (o) {
            var sel = o.value === val_str ? " selected" : "";
            if (sel) found = true;
            options_html += '<option value="' + o.value + '"' + sel + '>' + __(o.label) + '</option>';
        });
        if (!found) {
            // Keep an unexpected/empty extracted value visible instead of
            // silently snapping to the first option.
            options_html = '<option value="' + escaped + '" selected>' + escaped + '</option>' + options_html;
        }
        return '<div class="' + conf_class + '" style="margin-bottom:8px;">' + label_html +
            '<select class="review-input review-field" data-field="' + f.key + '">' +
            options_html + '</select></div>';
    }

    var input_type = f.type === "number" ? "number" : f.type === "date" ? "date" : "text";
    var step_attr = f.type === "number" ? ' step="any"' : "";
    return '<div class="' + conf_class + '" style="margin-bottom:8px;">' + label_html +
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
    var doc_currency = consensus.currency || null;
    var html = _REVIEW_CSS;
    html += '<div class="review-ui" style="padding:10px;">';

    // Escalation banner
    if (options.escalation_reason) {
        html +=
            '<div class="review-banner-warning" style="margin-bottom:16px;padding:12px 16px;border-radius:6px;">' +
            '<strong>' + __("Review Required") + '</strong>' +
            '<pre style="margin:8px 0 0;white-space:pre-wrap;background:transparent;font-size:0.9em;">' +
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
    // Explicit "assign existing supplier" link control — overrides fuzzy
    // matching when the extracted name doesn't match an existing supplier.
    html += '<div style="margin-bottom:8px;">' +
        '<label style="font-size:0.8em;color:var(--text-muted);margin-bottom:2px;display:block;">' +
        __("Assign existing supplier") + '</label>' +
        '<div class="supplier-link-control"></div></div>';
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

    html += '<div style="overflow-x:auto;">';
    html += '<table class="items-table">';
    // Column widths (fixed layout): Map to Item takes the remainder; item
    // name + description live in a full-width second row per item.
    html += '<colgroup>' +
        '<col style="width:28px;">' +   // #
        '<col style="width:130px;">' +  // supplier code
        '<col style="width:110px;">' +  // qty + stock summary
        '<col style="width:90px;">' +   // rate
        '<col style="width:55px;">' +   // tax %
        '<col style="width:90px;">' +   // type
        '<col style="width:100px;">' +  // total
        '<col>' +                        // map to item (flex)
        '<col style="width:28px;">' +   // delete
        '</colgroup>';
    html += '<thead><tr>';
    html += '<th>#</th>';
    html += '<th>' + __("Supplier Code") + '</th>';
    html += '<th>' + __("Qty") + '</th>';
    html += '<th>' + __("Rate") + '</th>';
    html += '<th>' + __("Tax %") + '</th>';
    html += '<th>' + __("Type") + '</th>';
    html += '<th>' + __("Total") + '</th>';
    html += '<th>' + __("Map to Item") + '</th>';
    html += '<th></th>';
    html += '</tr></thead><tbody>';

    items.forEach(function (item, idx) {
        html += _item_row_html(item, idx, doc_currency);
    });
    html += '</tbody></table></div>';
    if (items.length === 0) {
        html += '<p class="text-muted no-items-hint" style="margin-top:8px;">' + __("No line items extracted") + '</p>';
    }
    html += '<button type="button" class="btn btn-xs btn-default add-item-row" style="margin-top:8px;">' +
        '+ ' + __("Add Item") + '</button>';
    html += '</div>'; // end items card

    // Totals section (receipt-style, right-aligned)
    html += '<div class="review-card">';
    html += '<div class="review-totals">';
    html += '<div class="totals-check"></div>';
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

    // Delegated events: survive dynamically added/removed rows and don't
    // stack across re-renders (namespaced off() first).
    wrapper.off(".reviewui");
    // Auto-grow description textareas (initially and while typing)
    wrapper.find(".item-desc-input").each(function () {
        _autosize_textarea(this);
    });
    wrapper.on("input.reviewui", ".item-desc-input", function () {
        _autosize_textarea(this);
    });
    // Stock summary click → swap to the edit row (and back)
    wrapper.on("click.reviewui", ".stock-summary", function () {
        var idx = $(this).data("idx");
        var $detail = wrapper.find('.stock-detail[data-idx="' + idx + '"]');
        $detail.toggleClass("open");
    });
    wrapper.on("change.reviewui", ".doc-qty", function () {
        frm.dirty();
        _recalc_qty_cell(wrapper, $(this).data("idx"));
    });
    wrapper.on("change.reviewui", ".stock-qty", function () {
        frm.dirty();
        _recalc_qty_cell(wrapper, $(this).data("idx"));
    });
    wrapper.on("change.reviewui", ".doc-rate", function () {
        frm.dirty();
        _on_rate_change(wrapper, $(this).data("idx"));
    });
    wrapper.on("change.reviewui", ".review-input", function () {
        frm.dirty();
    });
    wrapper.on("change.reviewui", ".total-input", function () {
        _update_totals_check(wrapper);
    });
    wrapper.on("click.reviewui", ".item-delete", function () {
        var $tr = $(this).closest("tr");
        $tr.next("tr.item-text-row").remove();
        $tr.remove();
        frm.dirty();
        _renumber_rows(wrapper);
        _update_totals_check(wrapper);
    });
    wrapper.on("click.reviewui", ".add-item-row", function () {
        _append_item_row(frm, wrapper);
    });

    // Track the next free row index for added rows (must not collide with
    // rendered indices; collection reindexes by DOM order anyway).
    wrapper.data("next_item_idx", items.length);

    // Create Frappe Link controls (Map to Item + Stock UOM) for all rows
    _wire_row_controls(frm, wrapper, wrapper);

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

    // Create the "Assign existing supplier" Link control
    wrapper.find(".supplier-link-control").each(function () {
        var $el = $(this);
        var control = frappe.ui.form.make_control({
            df: {
                fieldtype: "Link",
                fieldname: "supplier_assign",
                options: "Supplier",
                placeholder: __("Auto (from extracted name)"),
                change: function () {
                    frm.dirty();
                },
            },
            parent: $el,
            render_input: true,
        });
        control.refresh();
        $el.data("control", control);
        // Restore a previously assigned supplier
        if (frm.doc.supplier_mapping) {
            control.set_value(frm.doc.supplier_mapping);
        }
    });

    // Create Frappe Link controls for header Link fields (e.g. Currency)
    wrapper.find(".header-link-control").each(function () {
        var $el = $(this);
        var field_key = $el.data("field");
        var link_doctype = $el.data("doctype");
        var initial_val = $el.data("initial-value");
        var control = frappe.ui.form.make_control({
            df: {
                fieldtype: "Link",
                fieldname: "header_" + field_key,
                options: link_doctype,
                change: function () {
                    frm.dirty();
                    // When the document currency changes, re-render line-item
                    // amounts so they show the selected currency's symbol.
                    if (field_key === "currency") {
                        _refresh_currency_display(wrapper);
                    }
                },
            },
            parent: $el,
            render_input: true,
        });
        control.refresh();
        if (initial_val) {
            control.set_value(String(initial_val));
        }
        $el.data("control", control);
    });

    // Initial plausibility line (items sum vs. totals)
    _update_totals_check(wrapper);

    // Async: check which supplier/items already exist. On failure, replace
    // the "Checking..." placeholders instead of leaving them stuck.
    frm.call("check_review_matches").then(
        function (r) {
            if (!r || !r.message) {
                _render_match_check_failed(wrapper);
                return;
            }
            _render_match_badges(wrapper, r.message, frm);
        },
        function () {
            _render_match_check_failed(wrapper);
        }
    );

    // Async: fetch grand totals for comparison panel
    if (options.show_comparison) {
        var review_currency = _review_currency(wrapper) || consensus.currency || null;
        var fetches = [];
        wrapper.find(".comparison-doc-row").each(function () {
            var $row = $(this);
            var doctype = $row.data("doctype");
            var name = $row.data("name");
            // Fetch the doc's own currency too — created docs are booked in the
            // company base currency, which may differ from the extracted currency.
            fetches.push(
                frappe.db.get_value(doctype, name, ["grand_total", "currency"]).then(function (r) {
                    var msg = r && r.message;
                    if (msg && msg.grand_total !== undefined && msg.grand_total !== null) {
                        // Store the raw numeric value for the delta calc — parsing the
                        // formatted text breaks on locale separators (e.g. "456,45").
                        var gt = parseFloat(msg.grand_total) || 0;
                        var doc_cur = msg.currency || review_currency;
                        $row.data("grand-total", gt);
                        $row.data("doc-currency", doc_cur);
                        $row.find(".doc-grand-total").text(
                            "(" + __("Grand Total") + ": " + format_currency(gt, doc_cur) + ")"
                        );
                    }
                })
            );
        });
        // Calculate the delta once all grand totals are loaded (no timing race)
        Promise.all(fetches).then(function () {
            var extracted_total = parseFloat(consensus.total_amount) || 0;
            var $delta = wrapper.find(".comparison-delta");
            var doc_totals = [];
            var doc_currency = review_currency;
            wrapper.find(".comparison-doc-row").each(function () {
                var gt = $(this).data("grand-total");
                if (gt !== undefined && gt !== null) {
                    doc_totals.push(parseFloat(gt));
                    doc_currency = $(this).data("doc-currency") || doc_currency;
                }
            });
            if (doc_totals.length === 0 || extracted_total <= 0) return;
            var max_doc = Math.max.apply(null, doc_totals);

            var renderDelta = function (extracted_in_doc_cur, note) {
                var delta = Math.abs(extracted_in_doc_cur - max_doc);
                var pct = ((delta / extracted_in_doc_cur) * 100).toFixed(2);
                if (delta > 0.01) {
                    $delta.html(
                        '<span class="review-warning-text">' +
                        __("Delta") + ': ' + format_currency(delta, doc_currency) + ' (' + pct + '%)' +
                        (note || '') + '</span>'
                    );
                } else {
                    $delta.html('<span class="review-success-text">' + __("Amounts match") +
                        (note || '') + '</span>');
                }
            };

            // Created docs are booked in the company currency. If that differs
            // from the document's original currency, convert the extracted total
            // with the exchange rate at the document date before comparing —
            // otherwise the "delta" is just the FX difference, not a real mismatch.
            if (doc_currency && review_currency && doc_currency !== review_currency) {
                frappe.call({
                    method: "erpnext.setup.utils.get_exchange_rate",
                    args: {
                        from_currency: review_currency,
                        to_currency: doc_currency,
                        transaction_date: consensus.document_date || frappe.datetime.get_today(),
                        args: "for_buying",
                    },
                    callback: function (res) {
                        var rate = parseFloat(res && res.message) || 0;
                        if (rate > 0) {
                            renderDelta(
                                extracted_total * rate,
                                ' <span class="text-muted">(' +
                                __("converted from {0}", [review_currency]) + ')</span>'
                            );
                        } else {
                            renderDelta(extracted_total);
                        }
                    },
                });
            } else {
                renderDelta(extracted_total);
            }
        });
    }
}

// Render a single line-item row. Used for the initial table and for rows
// added via the "Add Item" button. `idx` is the render index — collection
// reindexes by DOM order, so gaps after deletions are harmless.
function _item_row_html(item, idx, doc_currency) {
    var qty = parseFloat(item["quantity"]) || 0;
    var rate = parseFloat(item["unit_price"]) || 0;
    var total = parseFloat(item["total_price"]) || (qty * rate);
    var item_uom = item["uom"] || "Nos";
    var tax_rate = item["tax_rate"];
    var tax_val = tax_rate === null || tax_rate === undefined ? "" : tax_rate;
    var item_type = item["item_type"] || "";

    var html = '<tr class="item-main-row" data-item-idx="' + idx + '">';
    html += '<td class="row-num">' + (idx + 1) + '</td>';

    // Supplier code (item name + description live in the full-width text row)
    var code_val = item["item_code"];
    if (code_val === null || code_val === undefined) code_val = "";
    html +=
        '<td><input type="text" class="review-input review-item-field"' +
        ' data-idx="' + idx + '" data-field="item_code"' +
        ' value="' + frappe.utils.escape_html(String(code_val)) + '" /></td>';

    // Qty column: doc qty input + compact stock summary (click to edit)
    html +=
        '<td class="qty-uom-cell" data-idx="' + idx + '"' +
        ' data-line-total="' + total + '" data-invoice-qty="' + qty + '"' +
        ' data-invoice-rate="' + rate + '" style="white-space:nowrap;">' +
        '<input type="number" class="review-input doc-qty" data-idx="' + idx + '"' +
        ' step="any" style="width:100%;" value="' + qty + '" />' +
        // Compact stock line: "= 200 Stk ✎" — click swaps to edit inputs
        '<div class="stock-summary" data-idx="' + idx + '"' +
        ' title="' + __("Edit stock quantity / unit") + '">' +
        '<span class="stock-summary-text" data-idx="' + idx + '">= ' +
        qty + " " + frappe.utils.escape_html(String(item_uom)) + '</span>' +
        ' <span class="stock-edit-icon">&#9998;</span>' +
        '</div>' +
        // Edit row (hidden until the summary is clicked)
        '<div class="stock-detail" data-idx="' + idx + '">' +
        '<input type="number" class="review-input stock-qty" data-idx="' + idx + '"' +
        ' step="any" value="' + qty + '" />' +
        '<div class="stock-uom-control" data-idx="' + idx + '"' +
        ' data-initial-value="' + frappe.utils.escape_html(String(item_uom)) + '"' +
        '></div>' +
        '<span class="qty-info" data-idx="' + idx + '"' +
        ' style="font-size:0.8em;color:var(--text-muted);"></span>' +
        '</div>' +
        '<div class="uom-new-hint" data-idx="' + idx + '"></div>' +
        '<div class="qty-warning review-warning-text" data-idx="' + idx + '"' +
        ' style="display:none;font-size:0.8em;margin-top:2px;"></div>' +
        '</td>';

    // Rate (editable unit price; drives line total)
    html +=
        '<td><input type="number" class="review-input doc-rate" data-idx="' + idx + '"' +
        ' step="any" style="width:100%;" value="' + _fmt_rate(rate) + '" /></td>';

    // Tax rate (%) — feeds the Purchase Taxes and Charges rows
    html +=
        '<td><input type="number" class="review-input review-item-tax" data-idx="' + idx + '"' +
        ' step="any" style="width:100%;" value="' + frappe.utils.escape_html(String(tax_val)) + '" /></td>';

    // Item type (stock/service) — controls is_stock_item on new Items
    html +=
        '<td><select class="review-input review-item-type" data-idx="' + idx + '" style="width:100%;">' +
        '<option value=""' + (item_type === "" ? " selected" : "") + '>' + __("Auto") + '</option>' +
        '<option value="stock"' + (item_type === "stock" ? " selected" : "") + '>' + __("Stock") + '</option>' +
        '<option value="service"' + (item_type === "service" ? " selected" : "") + '>' + __("Service") + '</option>' +
        '</select></td>';

    // Total (bold, from extraction)
    html +=
        '<td><span class="line-total" data-idx="' + idx + '"' +
        ' style="font-weight:600;">' +
        format_currency(total, doc_currency) + '</span></td>';

    // Map to Item: badge and Link control side by side on one line
    html +=
        '<td><div class="map-cell">' +
        '<div class="item-match-cell" data-idx="' + idx + '">' +
        '<span class="text-muted" style="font-size:0.8em;">' + __("Checking...") + '</span></div>' +
        '<div class="item-link-control" data-idx="' + idx + '"></div>' +
        '</div></td>';

    // Remove row
    html +=
        '<td><button type="button" class="item-delete" title="' + __("Remove line item") + '">&times;</button></td>';

    html += '</tr>';

    // Second row: item name + description get the full table width
    var name_val = item["item_name"];
    if (name_val === null || name_val === undefined) name_val = "";
    var desc_val = item["description"];
    if (desc_val === null || desc_val === undefined) desc_val = "";
    html += '<tr class="item-text-row" data-item-text-idx="' + idx + '">';
    html += '<td></td>';
    html += '<td colspan="8"><div class="text-fields">' +
        '<input type="text" class="review-input review-item-field item-name-input"' +
        ' data-idx="' + idx + '" data-field="item_name"' +
        ' placeholder="' + __("Item Name") + '" title="' + __("Item Name") + '"' +
        ' value="' + frappe.utils.escape_html(String(name_val)) + '" />' +
        '<textarea rows="1" class="review-input review-item-field item-desc-input"' +
        ' data-idx="' + idx + '" data-field="description"' +
        ' placeholder="' + __("Description") + '" title="' + __("Description") + '"' +
        '>' + frappe.utils.escape_html(String(desc_val)) + '</textarea>' +
        '</div></td>';
    html += '</tr>';
    return html;
}

// Create the Frappe Link controls (Map to Item, Stock UOM) for all rows
// inside $scope that don't have one yet. $scope is the whole wrapper on
// initial render, or a single freshly added row.
function _wire_row_controls(frm, wrapper, $scope) {
    $scope.find(".item-link-control").each(function () {
        var $el = $(this);
        if ($el.data("control")) return;
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

    $scope.find(".stock-uom-control").each(function () {
        var $el = $(this);
        if ($el.data("control")) return;
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
                    _update_stock_summary(wrapper, idx);
                },
            },
            parent: $el,
            render_input: true,
        });
        control.refresh();
        control.set_value(initial_val);
        $el.data("control", control);
    });
}

// Append a blank line-item row (e.g. for a position the LLM missed entirely)
function _append_item_row(frm, wrapper) {
    var idx = wrapper.data("next_item_idx") || 0;
    wrapper.data("next_item_idx", idx + 1);

    var blank = {
        item_code: "",
        item_name: "",
        description: "",
        quantity: 1,
        unit_price: 0,
        total_price: 0,
        uom: "Nos",
    };
    var $tbody = wrapper.find(".items-table tbody");
    $tbody.append(_item_row_html(blank, idx, _review_currency(wrapper)));
    wrapper.find(".no-items-hint").remove();

    var $row = $tbody.find('tr[data-item-idx="' + idx + '"]');
    _wire_row_controls(frm, wrapper, $row);
    // A manually added row is always new until the user maps an existing Item
    $row.find(".item-match-cell").html(
        '<span class="review-badge review-badge-info">' + __("New") + '</span>'
    );
    _renumber_rows(wrapper);
    _update_totals_check(wrapper);
    frm.dirty();
}

// Keep the visible position numbers sequential after add/delete
function _renumber_rows(wrapper) {
    wrapper.find(".items-table tbody tr.item-main-row").each(function (i) {
        $(this).find(".row-num").text(i + 1);
    });
}

// Replace "Checking..." placeholders when the match check errors out
function _render_match_check_failed(wrapper) {
    var failed =
        '<span class="text-muted" style="font-size:0.8em;">' +
        __("Match check failed") + '</span>';
    wrapper.find(".supplier-match-badge").html(failed);
    wrapper.find(".item-match-cell").each(function () {
        $(this).html(failed);
    });
}

// Live plausibility line in the totals card: compare the sum of the line
// items against Subtotal, and Subtotal + Tax + Shipping against Total.
function _update_totals_check(wrapper) {
    var $check = wrapper.find(".totals-check");
    if (!$check.length) return;

    var cur = _review_currency(wrapper);
    var sum = 0;
    wrapper.find(".qty-uom-cell").each(function () {
        sum += parseFloat($(this).data("line-total")) || 0;
    });

    var read = function (key) {
        var v = wrapper.find('.review-field[data-field="' + key + '"]').val();
        return v === "" || v === undefined || v === null ? null : parseFloat(v);
    };
    var subtotal = read("subtotal");
    var tax = read("tax_amount");
    var shipping = read("shipping_cost");
    var total = read("total_amount");

    var parts = [
        '<span class="text-muted">' + __("Items sum") + ': ' + format_currency(sum, cur) + '</span>',
    ];
    if (subtotal !== null) {
        var d1 = sum - subtotal;
        if (Math.abs(d1) > 0.01) {
            parts.push(
                '<span class="review-warning-text">' +
                __("differs from Subtotal by {0}", [format_currency(d1, cur)]) + '</span>'
            );
        } else {
            parts.push('<span class="review-success-text">' + __("matches Subtotal") + '</span>');
        }
    }
    if (total !== null) {
        var base = subtotal !== null ? subtotal : sum;
        var net_total = base + (tax || 0) + (shipping || 0);
        var net_ok = Math.abs(net_total - total) <= 0.02;
        var gross_ok = Math.abs(base - total) <= 0.02;
        if (!net_ok && !gross_ok) {
            parts.push(
                '<span class="review-warning-text">' +
                __("Subtotal + Tax + Shipping differs from Total by {0}",
                    [format_currency(net_total - total, cur)]) + '</span>'
            );
        }
    }
    $check.html(parts.join(" &middot; "));
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
                '<span class="review-badge review-badge-success">' +
                __("Exists") + " (" + matches.supplier.method + ")</span>"
        );
    } else {
        $supplier.html(
            '<span class="review-badge review-badge-info">' +
                __("New — will be created") +
                "</span>"
        );
    }

    // Item badges + UOM adjustments. Use the server-provided row index when
    // available — sanitization can drop rows (shipping/discount), so the
    // array position does not always equal the rendered row index.
    var items = matches.items || [];
    items.forEach(function (info, pos) {
        var idx = info.idx !== undefined && info.idx !== null ? info.idx : pos;
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
                '<span class="review-badge review-badge-info">' +
                    __("New") + "</span>"
            );
            _set_stock_uom_editable(wrapper, idx);
        } else if (user_mapped) {
            // User explicitly selected an item — keep their choice, show "Exists" badge
            $cell.html(
                '<span class="review-badge review-badge-success">' +
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
                '<span class="review-badge review-badge-success">' +
                    __("Exists") + "</span>"
            );
            if (info.stock_uom) {
                _set_stock_uom_readonly(wrapper, idx, info.stock_uom);
            }
        } else {
            $cell.html(
                '<span class="review-badge review-badge-info">' +
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

        // Show a hint when the bulk UOM record will be auto-created
        if (info.uom_will_be_created) {
            wrapper.find('.uom-new-hint[data-idx="' + idx + '"]')
                .text(__("Unit {0} will be created", [info.uom_will_be_created]))
                .css("display", "block");
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
    // Same base the UI was rendered from — otherwise edits saved in
    // reviewed_data (e.g. on re-approve) would silently fall back to the
    // original consensus values for fields kept outside the inputs.
    var consensus = {};
    try {
        consensus = JSON.parse(frm.doc.reviewed_data || frm.doc.consensus_data || "{}");
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

    // Collect header Link fields (e.g. Currency) from their controls
    wrapper.find(".header-link-control").each(function () {
        var $el = $(this);
        var field_key = $el.data("field");
        var control = $el.data("control");
        if (control) {
            reviewed[field_key] = control.get_value() || "";
        }
    });

    // Collect items row by row in DOM order. Rows can be deleted or added in
    // the UI, so the result is reindexed sequentially — item_mapping and
    // stock_uom_mapping use the same fresh indices.
    var base_items = consensus.items || [];
    var items = [];
    var item_mapping = {};
    var stock_uom_mapping = {};

    wrapper.find(".items-table tbody tr[data-item-idx]").each(function () {
        var $row = $(this);
        var orig_idx = parseInt($row.attr("data-item-idx"), 10);
        // Base: the rendered source item (preserves fields without inputs);
        // rows added in the UI have no base and start empty.
        var item = Object.assign({}, base_items[orig_idx] || {});

        // Text fields: supplier code in the main row, name + description in
        // the companion text row below it.
        var $pair = $row.add($row.next("tr.item-text-row"));
        $pair.find(".review-item-field").each(function () {
            item[$(this).data("field")] = $(this).val();
        });

        // Tax rate (%): empty input means "not extracted" (null)
        var tax_val = $row.find(".review-item-tax").val();
        item.tax_rate = tax_val === "" || tax_val === undefined ? null : parseFloat(tax_val);

        // Item type: "" means auto-classification (null)
        item.item_type = $row.find(".review-item-type").val() || null;

        // Compact quantity/UOM cell
        var $cell = $row.find(".qty-uom-cell");
        var doc_qty = parseFloat($cell.find(".doc-qty").val()) || 0;
        var stock_qty_val = parseFloat($cell.find(".stock-qty").val()) || 0;
        var line_total = parseFloat($cell.data("line-total")) || 0;
        var stock_uom_ctrl = $row.find(".stock-uom-control").data("control");
        var resolved_uom = stock_uom_ctrl ? (stock_uom_ctrl.get_value() || "Nos") : "Nos";

        var factor = doc_qty > 0 ? stock_qty_val / doc_qty : 1;
        var is_bulk = factor > 1 && Number.isInteger(factor);

        item.quantity = doc_qty;
        item.unit_price = doc_qty > 0 ? line_total / doc_qty : 0;
        item.total_price = line_total;
        item.uom = is_bulk ? String(Math.round(factor)) : resolved_uom;

        var new_idx = items.length;
        items.push(item);

        var link_ctrl = $row.find(".item-link-control").data("control");
        item_mapping[new_idx] = link_ctrl ? (link_ctrl.get_value() || null) : null;
        stock_uom_mapping[new_idx] = stock_uom_ctrl ? (stock_uom_ctrl.get_value() || null) : null;
    });

    reviewed.items = items;

    // Collect assigned supplier (overrides fuzzy matching when set)
    var supplier_mapping = "";
    var $supplier_link = wrapper.find(".supplier-link-control");
    if ($supplier_link.length) {
        var supplier_ctrl = $supplier_link.data("control");
        if (supplier_ctrl) {
            supplier_mapping = supplier_ctrl.get_value() || "";
        }
    }

    return {
        reviewed: reviewed,
        item_mapping: item_mapping,
        stock_uom_mapping: stock_uom_mapping,
        supplier_mapping: supplier_mapping,
    };
}

function _save_review_data(frm, data) {
    frm.set_value("reviewed_data", JSON.stringify(data.reviewed));
    frm.set_value("item_mapping", JSON.stringify(data.item_mapping));
    frm.set_value("stock_uom_mapping", JSON.stringify(data.stock_uom_mapping));
    frm.set_value("supplier_mapping", data.supplier_mapping || null);
    return frm.save();
}

function _collect_and_approve(frm) {
    if (frm._approve_in_flight) return;

    var data = _collect_review_data(frm);

    // Validate before sending: empty dates block approval (the chain needs
    // them for posting dates and exchange rates), with a clear message
    // naming the affected fields instead of a generic server error.
    var missing_dates = [];
    _DOCUMENT_FIELDS.forEach(function (f) {
        if (f.type === "date" && !data.reviewed[f.key]) {
            missing_dates.push(__(f.label));
        }
    });
    var unnamed_rows = [];
    (data.reviewed.items || []).forEach(function (item, i) {
        if (!String(item.item_name || "").trim()) {
            unnamed_rows.push(i + 1);
        }
    });
    var no_items = !(data.reviewed.items || []).length;
    if (missing_dates.length || unnamed_rows.length || no_items) {
        var msg = "";
        if (missing_dates.length) {
            msg += __("Please fill in the following date field(s) before approving:") +
                "<br><b>" + missing_dates.join(", ") + "</b>";
        }
        if (unnamed_rows.length) {
            msg += (msg ? "<br><br>" : "") +
                __("Line item row(s) {0} have no item name.", [unnamed_rows.join(", ")]);
        }
        if (no_items) {
            msg += (msg ? "<br><br>" : "") +
                __("At least one line item is required.");
        }
        frappe.msgprint({
            title: __("Cannot approve yet"),
            message: msg,
            indicator: "red",
        });
        return;
    }

    // Guard against double-clicks: a second submit would enqueue the
    // chain builder twice before the status flips to Processing.
    frm._approve_in_flight = true;
    frappe.dom.freeze(__("Creating documents..."));

    frm.call("approve_and_create", {
        reviewed_data: JSON.stringify(data.reviewed),
        item_mapping: JSON.stringify(data.item_mapping),
        stock_uom_mapping: JSON.stringify(data.stock_uom_mapping),
        supplier_mapping: data.supplier_mapping || "",
    }).then(
        function () {
            frappe.dom.unfreeze();
            frm._approve_in_flight = false;
            frm.reload_doc();
        },
        function () {
            frappe.dom.unfreeze();
            frm._approve_in_flight = false;
        }
    );
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
                        '<span class="review-badge review-badge-success">' +
                            __("Created") + "</span>"
                    );
                } else {
                    $cell.html(
                        '<span class="review-badge review-badge-success">' +
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
        _update_stock_summary(wrapper, idx);
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

// Strip floating-point noise from a derived rate for display in the input.
function _fmt_rate(val) {
    if (!val) return 0;
    return Math.round(val * 1e6) / 1e6;
}

// The currency currently selected in the review header (falls back to the
// extracted currency, then the system default). Used so line-item amounts are
// formatted with the chosen currency's symbol, not the company default.
function _review_currency(wrapper) {
    var $cur = wrapper.find('.header-link-control[data-field="currency"]');
    if ($cur.length) {
        var ctrl = $cur.data("control");
        if (ctrl && ctrl.get_value()) return ctrl.get_value();
        var iv = $cur.data("initial-value");
        if (iv) return String(iv);
    }
    return null;
}

// Re-render all line-item totals with the currently selected currency symbol.
function _refresh_currency_display(wrapper) {
    var currency = _review_currency(wrapper);
    wrapper.find(".qty-uom-cell").each(function () {
        var idx = $(this).data("idx");
        var line_total = parseFloat($(this).data("line-total")) || 0;
        wrapper.find('.line-total[data-idx="' + idx + '"]').text(
            format_currency(line_total, currency)
        );
    });
    _update_totals_check(wrapper);
}

// User edited the unit price directly: line total = rate × doc qty.
// The line total is the canonical stored value (data-line-total), so update
// it here and let collection derive unit_price/total_price from it.
function _on_rate_change(wrapper, idx) {
    var $cell = wrapper.find('.qty-uom-cell[data-idx="' + idx + '"]');
    if (!$cell.length) return;

    var doc_qty = parseFloat($cell.find(".doc-qty").val()) || 0;
    var rate = parseFloat(wrapper.find('.doc-rate[data-idx="' + idx + '"]').val()) || 0;
    var line_total = rate * doc_qty;

    $cell.data("line-total", line_total);

    wrapper.find('.line-total[data-idx="' + idx + '"]').text(
        format_currency(line_total, _review_currency(wrapper))
    );

    _validate_rate(wrapper, idx, rate, line_total, doc_qty);
    _update_totals_check(wrapper);
}

function _recalc_qty_cell(wrapper, idx) {
    var $cell = wrapper.find('.qty-uom-cell[data-idx="' + idx + '"]');
    if (!$cell.length) return;

    var line_total = parseFloat($cell.data("line-total")) || 0;
    var doc_qty = parseFloat($cell.find(".doc-qty").val()) || 0;
    var stock_qty = parseFloat($cell.find(".stock-qty").val()) || 0;

    var rate = doc_qty > 0 ? line_total / doc_qty : 0;
    var factor = doc_qty > 0 ? stock_qty / doc_qty : 1;
    var is_bulk = factor > 1 && Number.isInteger(factor);

    // Update rate input (derived from fixed line total) — sits in a sibling
    // <td>, so query from the row wrapper, not the qty-uom-cell.
    wrapper.find('.doc-rate[data-idx="' + idx + '"]').val(_fmt_rate(rate));

    // Update total
    wrapper.find('.line-total[data-idx="' + idx + '"]').text(
        format_currency(line_total, _review_currency(wrapper))
    );

    // Update factor info
    var $info = $cell.find('.qty-info[data-idx="' + idx + '"]');
    if (is_bulk) {
        $info.text("(\u00d7" + Math.round(factor) + ")");
    } else {
        $info.text("");
    }

    _update_stock_summary(wrapper, idx);

    // Sub-cent validation
    _validate_rate(wrapper, idx, rate, line_total, doc_qty);
    _update_totals_check(wrapper);
}

// Grow a textarea to fit its content (no inner scrollbar)
function _autosize_textarea(el) {
    el.style.height = "auto";
    el.style.height = el.scrollHeight + 2 + "px";
}

// Refresh the compact "= 200 Stk" stock summary line from the edit inputs.
function _update_stock_summary(wrapper, idx) {
    var $cell = wrapper.find('.qty-uom-cell[data-idx="' + idx + '"]');
    if (!$cell.length) return;

    var stock_qty = parseFloat($cell.find(".stock-qty").val()) || 0;
    var uom_ctrl = $cell.find(".stock-uom-control").data("control");
    var uom = uom_ctrl ? (uom_ctrl.get_value() || "") : "";
    if (!uom) uom = $cell.find(".stock-uom-control").data("initial-value") || "";

    $cell.find('.stock-summary-text[data-idx="' + idx + '"]').text(
        "= " + stock_qty + " " + uom
    );
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
    var currency = _review_currency(wrapper);
    var msg = __("Sub-cent rate") + ". " + __("Try") + ": " +
        suggestions.map(function (s) {
            return s + " (" + format_currency(total / s, currency) + ")";
        }).join(", ");
    $warning.text(msg).show();
    $doc_qty.css("border-color", "var(--red-500, #c53030)");
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
