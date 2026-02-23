frappe.ui.form.on("Purchase Order", {
    refresh: function (frm) {
        if (frm.doc.ai_retrospective) {
            frm.dashboard.set_headline(
                '<span class="indicator-pill blue">' +
                    '<span>AI Retrospective</span></span>' +
                    (frm.doc.ai_procurement_job
                        ? ' &mdash; <a href="/app/ai-procurement-job/' +
                          frm.doc.ai_procurement_job +
                          '">Job: ' +
                          frm.doc.ai_procurement_job +
                          "</a>"
                        : "")
            );
        }
    },
});
