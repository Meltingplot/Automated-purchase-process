app_name = "procurement_ai"
app_title = "Procurement AI"
app_publisher = "Meltingplot GmbH"
app_description = "AI-powered procurement automation for ERPNext"
app_email = "info@meltingplot.net"
app_license = "MIT"
app_icon = "octicon octicon-package"
app_color = "#4F46E5"

required_apps = ["frappe", "erpnext"]

# Scheduler Events
scheduler_events = {
    "all": [
        "procurement_ai.procurement_ai.api.ingest.process_pending_jobs"
    ],
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
