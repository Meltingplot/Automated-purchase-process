frappe.pages["procurement-ai-dashboard"].on_page_load = function (wrapper) {
    var page = frappe.ui.make_app_page({
        parent: wrapper,
        title: __("Procurement AI Dashboard"),
        single_column: true,
    });

    page.main.html(
        '<div class="procurement-ai-dashboard">' +
            '<div class="stats-row row"></div>' +
            '<div class="row">' +
            '<div class="col-md-7"><div class="recent-jobs"></div></div>' +
            '<div class="col-md-5"><div class="escalation-queue"></div></div>' +
            "</div>" +
            "</div>"
    );

    page.set_primary_action(__("New Job"), function () {
        frappe.new_doc("AI Procurement Job");
    });

    page.set_secondary_action(__("Refresh"), function () {
        load_dashboard(page);
    });

    load_dashboard(page);
};

function load_dashboard(page) {
    frappe.call({
        method: "erpnext_procurement_ai.procurement_ai.api.status.get_dashboard_stats",
        callback: function (r) {
            if (r.message) {
                render_stats(page, r.message);
                render_recent_jobs(page, r.message.recent_jobs);
                render_escalations(page, r.message.open_escalations);
            }
        },
    });
}

function render_stats(page, data) {
    var counts = data.status_counts || {};
    var html = "";
    var status_config = [
        { key: "Pending", color: "orange", icon: "clock" },
        { key: "Processing", color: "blue", icon: "refresh" },
        { key: "Needs Review", color: "yellow", icon: "alert-circle" },
        { key: "Completed", color: "green", icon: "check" },
        { key: "Error", color: "red", icon: "x" },
    ];

    status_config.forEach(function (s) {
        html +=
            '<div class="col-sm">' +
            '<div class="stat-card" style="padding:15px;margin:5px;border-radius:8px;' +
            "background:var(--subtle-fg);text-align:center;\">" +
            '<div class="stat-value" style="font-size:2em;font-weight:bold;">' +
            (counts[s.key] || 0) +
            "</div>" +
            '<div class="stat-label text-muted">' +
            __(s.key) +
            "</div>" +
            "</div>" +
            "</div>";
    });

    page.main.find(".stats-row").html(html);
}

function render_recent_jobs(page, jobs) {
    var html =
        '<h5 style="margin:20px 0 10px;">' + __("Recent Jobs") + "</h5>";

    if (!jobs || jobs.length === 0) {
        html += '<p class="text-muted">' + __("No jobs yet") + "</p>";
    } else {
        html += '<div class="frappe-list">';
        jobs.forEach(function (job) {
            var color_map = {
                Pending: "orange",
                Processing: "blue",
                "Needs Review": "yellow",
                Completed: "green",
                Error: "red",
            };
            var color = color_map[job.status] || "grey";
            var confidence = job.confidence_score
                ? (job.confidence_score * 100).toFixed(0) + "%"
                : "-";

            html +=
                '<div class="list-row" style="padding:8px 12px;border-bottom:1px solid var(--border-color);">' +
                '<a href="/app/ai-procurement-job/' + job.name + '">' +
                '<span class="indicator-pill ' + color + ' filterable ellipsis">' +
                '<span class="ellipsis">' + __(job.status) + "</span></span> " +
                "<strong>" + job.name + "</strong>" +
                " &mdash; " + (job.detected_type || job.source_type || "") +
                ' <span class="text-muted pull-right">' +
                confidence + " | " + frappe.datetime.prettyDate(job.creation) +
                "</span></a></div>";
        });
        html += "</div>";
    }

    page.main.find(".recent-jobs").html(html);
}

function render_escalations(page, escalations) {
    var html =
        '<h5 style="margin:20px 0 10px;">' +
        __("Open Escalations") +
        "</h5>";

    if (!escalations || escalations.length === 0) {
        html +=
            '<p class="text-muted">' + __("No open escalations") + "</p>";
    } else {
        escalations.forEach(function (esc) {
            html +=
                '<div class="escalation-card" style="padding:10px;margin:5px 0;' +
                'border-radius:6px;background:var(--subtle-fg);">' +
                '<a href="/app/ai-procurement-job/' + esc.procurement_job + '">' +
                "<strong>" + esc.procurement_job + "</strong></a>" +
                '<span class="badge badge-warning pull-right">' +
                esc.escalation_type + "</span>" +
                '<div class="text-muted" style="margin-top:4px;font-size:0.9em;">' +
                (esc.reason || "").substring(0, 100) +
                "</div></div>";
        });
    }

    page.main.find(".escalation-queue").html(html);
}
