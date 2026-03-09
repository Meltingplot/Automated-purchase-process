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

        // Needs Review: post-creation verification or escalation
        if (frm.doc.status === "Needs Review") {
            var has_created_docs = frm.doc.created_po || frm.doc.created_receipt || frm.doc.created_invoice;

            if (has_created_docs) {
                // Show review summary and mismatch reason
                _render_needs_review_summary(frm);

                // Post-creation verification — user checks amounts match
                frm.add_custom_button(
                    __("Mark as Completed"),
                    function () {
                        frappe.confirm(
                            __(
                                "Have you verified the created documents and amounts are correct?"
                            ),
                            function () {
                                frm.call("mark_completed").then(function () {
                                    frm.reload_doc();
                                });
                            }
                        );
                    }
                ).addClass("btn-primary");
            } else {
                // Escalation — no docs created yet
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
            }

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
];

// Extra columns after _ITEM_FIELDS: Quantity (compact), Map to Item = 2
var _EXTRA_COLS = 2;

function _render_review_form(frm) {
    var consensus = {};
    try {
        consensus = JSON.parse(frm.doc.consensus_data || "{}");
    } catch (e) {
        consensus = {};
    }

    var confidence_map = _compute_confidence(frm);

    var html = '<div class="review-form" style="padding:10px;">';

    // Supplier match indicator
    html +=
        '<div class="supplier-match-info" style="margin-bottom:14px;padding:10px;' +
        'border-radius:6px;background:var(--subtle-fg);">' +
        '<strong>' + __("Supplier") + ':</strong> ' +
        '<span class="supplier-match-badge">' +
        '<span class="text-muted">' + __("Checking...") + '</span></span>' +
        '</div>';

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
        html +=
            "<th>" + __("Quantity") + "</th>" +
            "<th>" + __("Map to Item") + "</th></tr></thead><tbody>";

        items.forEach(function (item, idx) {
            var qty = parseFloat(item["quantity"]) || 0;
            var rate = parseFloat(item["unit_price"]) || 0;
            var total = parseFloat(item["total_price"]) || (qty * rate);
            var item_uom = item["uom"] || "Nos";

            html += '<tr data-item-idx="' + idx + '">';
            html += "<td>" + (idx + 1) + "</td>";
            _ITEM_FIELDS.forEach(function (f) {
                var val = item[f.key];
                if (val === null || val === undefined) val = "";
                html +=
                    '<td><input type="text"' +
                    ' class="form-control input-xs review-item-field"' +
                    ' data-idx="' + idx + '" data-field="' + f.key + '"' +
                    ' value="' + frappe.utils.escape_html(String(val)) + '" /></td>';
            });
            // Compact Quantity cell
            html +=
                '<td class="qty-uom-cell" data-idx="' + idx + '" style="min-width:340px;"' +
                ' data-line-total="' + total + '" data-invoice-qty="' + qty + '"' +
                ' data-invoice-rate="' + rate + '">' +
                '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">' +
                '<input type="number" class="form-control input-xs doc-qty" data-idx="' + idx + '"' +
                ' step="any" style="width:80px;" value="' + qty + '" />' +
                '<span class="doc-rate-label" data-idx="' + idx + '"' +
                ' style="white-space:nowrap;">' +
                '&times; ' + format_currency(rate) + ' / ' + frappe.utils.escape_html(item_uom) +
                '</span>' +
                '<span style="font-size:1.1em;">&rarr;</span>' +
                '<input type="number" class="form-control input-xs stock-qty" data-idx="' + idx + '"' +
                ' step="any" style="width:80px;" value="' + qty + '" />' +
                '<span>/ </span>' +
                '<div class="stock-uom-control" data-idx="' + idx + '"' +
                ' data-initial-value="' + frappe.utils.escape_html(String(item_uom)) + '"' +
                ' style="display:inline-block;width:80px;"></div>' +
                '<span class="line-total" data-idx="' + idx + '"' +
                ' style="margin-left:auto;white-space:nowrap;">' +
                '= <strong>' + format_currency(total) + '</strong></span>' +
                '</div>' +
                '<div class="qty-warning" data-idx="' + idx + '"' +
                ' style="display:none;font-size:0.8em;color:#c53030;margin-top:4px;"></div>' +
                '<div class="qty-info" data-idx="' + idx + '"' +
                ' style="font-size:0.8em;color:var(--text-muted);margin-top:2px;display:none;"></div>' +
                '</td>';
            // Map to Item (badge + Link control in one cell)
            html +=
                '<td>' +
                '<div class="item-match-cell" data-idx="' + idx + '" style="margin-bottom:4px;">' +
                '<span class="text-muted" style="font-size:0.8em;">' + __("Checking...") + '</span></div>' +
                '<div class="item-link-control" data-idx="' + idx + '"></div>' +
                '</td>';
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
                change: function () {
                    var item_code = control.get_value();
                    if (item_code) {
                        // Fetch stock_uom and lock the warehouse UOM field
                        frappe.db.get_value("Item", item_code, "stock_uom", function (r) {
                            if (r && r.stock_uom) {
                                _set_stock_uom_readonly(wrapper, idx, r.stock_uom);
                            }
                        });
                    } else {
                        // Cleared — unlock the warehouse UOM field
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
        var idx = $(this).data("idx");
        _recalc_qty_cell(wrapper, idx);
    });
    wrapper.find(".stock-qty").on("change", function () {
        var idx = $(this).data("idx");
        _recalc_qty_cell(wrapper, idx);
    });

    // Async: check which supplier/items already exist
    frm.call("check_review_matches").then(function (r) {
        if (!r || !r.message) return;
        var matches = r.message;
        _render_match_badges(wrapper, matches);
    });
}

function _render_match_badges(wrapper, matches) {
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
        if (info.exists) {
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

// =================================================================
// Needs Review summary (post-creation amount mismatch)
// =================================================================

function _render_needs_review_summary(frm) {
    var consensus = {};
    try {
        consensus = JSON.parse(frm.doc.reviewed_data || frm.doc.consensus_data || "{}");
    } catch (e) {
        consensus = {};
    }

    var html = '<div class="needs-review-summary" style="padding:10px;">';

    // Reason banner
    if (frm.doc.escalation_reason) {
        html +=
            '<div style="margin-bottom:16px;padding:12px 16px;border-radius:6px;' +
            'background:#fff3cd;border:1px solid #ffc107;">' +
            '<strong style="color:#856404;">' + __("Review Required") + '</strong>' +
            '<pre style="margin:8px 0 0;white-space:pre-wrap;color:#856404;font-size:0.9em;">' +
            frappe.utils.escape_html(frm.doc.escalation_reason) + '</pre></div>';
    }

    // Read-only header fields
    html += '<h5 style="margin-bottom:12px;">' + __("Extracted Data") + '</h5>';
    html += '<table class="table table-bordered table-sm">';
    html += '<thead><tr><th>' + __("Field") + '</th><th>' + __("Value") + '</th></tr></thead><tbody>';

    _HEADER_FIELDS.forEach(function (f) {
        var val = consensus[f.key];
        if (val === null || val === undefined || val === "") return;
        var display_val = frappe.utils.escape_html(String(val));
        if (f.type === "number" && val) {
            display_val = format_currency(parseFloat(val));
        }
        html += '<tr><td><strong>' + __(f.label) + '</strong></td>';
        html += '<td>' + display_val + '</td></tr>';
    });
    html += '</tbody></table>';

    // Read-only items table
    var items = consensus.items || [];
    if (items.length > 0) {
        html += '<h5 style="margin-top:16px;margin-bottom:12px;">' + __("Line Items") + '</h5>';
        html += '<div style="overflow-x:auto;">';
        html += '<table class="table table-bordered table-sm">';
        html += '<thead><tr><th>#</th>';
        _ITEM_FIELDS.forEach(function (f) {
            html += '<th>' + __(f.label) + '</th>';
        });
        html += '<th>' + __("Qty") + '</th>';
        html += '<th>' + __("Rate") + '</th>';
        html += '<th>' + __("UOM") + '</th>';
        html += '<th>' + __("Total") + '</th>';
        html += '</tr></thead><tbody>';

        items.forEach(function (item, idx) {
            var qty = parseFloat(item["quantity"]) || 0;
            var rate = parseFloat(item["unit_price"]) || 0;
            var total = parseFloat(item["total_price"]) || (qty * rate);
            html += '<tr>';
            html += '<td>' + (idx + 1) + '</td>';
            _ITEM_FIELDS.forEach(function (f) {
                var val = item[f.key];
                if (val === null || val === undefined) val = "";
                html += '<td>' + frappe.utils.escape_html(String(val)) + '</td>';
            });
            html += '<td>' + qty + '</td>';
            html += '<td>' + format_currency(rate) + '</td>';
            html += '<td>' + frappe.utils.escape_html(String(item["uom"] || "")) + '</td>';
            html += '<td>' + format_currency(total) + '</td>';
            html += '</tr>';
        });
        html += '</tbody></table></div>';
    }

    html += '</div>';

    // Render into review_html wrapper
    var wrapper = frm.fields_dict.review_html;
    if (wrapper && wrapper.$wrapper) {
        wrapper.$wrapper.html(html);
    }
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

    // Save reviewed data, item mapping, and stock UOM mapping to the doc
    frm.set_value("reviewed_data", JSON.stringify(reviewed));
    frm.set_value("item_mapping", JSON.stringify(item_mapping));
    frm.set_value("stock_uom_mapping", JSON.stringify(stock_uom_mapping));
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
    if (frm.doc.status !== "Completed" && frm.doc.status !== "Needs Review") return;

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

    // Update rate label
    var uom_label = is_bulk ? String(Math.round(factor)) : "Nos";
    var $stock_uom_el = $cell.find(".stock-uom-control");
    var stock_uom_ctrl = $stock_uom_el.data("control");
    if (stock_uom_ctrl && !is_bulk) {
        uom_label = stock_uom_ctrl.get_value() || "Nos";
    }
    $cell.find('.doc-rate-label[data-idx="' + idx + '"]').html(
        "&times; " + format_currency(rate) + " / " + frappe.utils.escape_html(uom_label)
    );

    // Update total
    $cell.find('.line-total[data-idx="' + idx + '"]').html(
        "= <strong>" + format_currency(line_total) + "</strong>"
    );

    // Info line (factor > 1)
    var $info = $cell.find('.qty-info[data-idx="' + idx + '"]');
    if (is_bulk) {
        $info.text(
            __("1 {0} = {1} {2}", [uom_label, Math.round(factor), "Nos"])
        ).show();
    } else {
        $info.hide();
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
