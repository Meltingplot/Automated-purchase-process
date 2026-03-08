app_name = "erpnext_procurement_ai"
app_title = "ERPNext Procurement AI"
app_publisher = "Meltingplot GmbH"
app_description = "AI-powered procurement automation for ERPNext"
app_email = "info@meltingplot.com"
app_license = "MIT"
app_icon = "octicon octicon-package"
app_color = "#4F46E5"

required_apps = ["frappe", "erpnext"]

# Scheduler Events
scheduler_events = {
    "all": [
        "erpnext_procurement_ai.procurement_ai.api.ingest.process_pending_jobs"
    ],
}

# DocType JS Customizations
doctype_js = {
    "Purchase Order": "public/js/purchase_order_custom.js",
    "Purchase Receipt": "public/js/purchase_receipt_custom.js",
    "Purchase Invoice": "public/js/purchase_invoice_custom.js",
}

# Custom Fields installed via fixtures
fixtures = [
    {
        "dt": "Custom Field",
        "filters": [["module", "=", "Procurement AI"]],
    }
]

# Workspace setup
website_route_rules = []
