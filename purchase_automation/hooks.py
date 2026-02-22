app_name = "purchase_automation"
app_title = "Purchase Automation"
app_publisher = "Meltingplot"
app_description = "Automated purchase process for ERPNext with dual-model LLM document extraction"
app_email = "info@meltingplot.net"
app_license = "MIT"
app_version = "0.1.0"

# Apps
required_apps = ["frappe", "erpnext"]

# Document Events
doc_events = {
    "Purchase Document": {
        "after_insert": "purchase_automation.orchestrator.workflow.on_document_uploaded",
    }
}

# Scheduled Tasks
scheduler_events = {
    "hourly_long": [
        "purchase_automation.orchestrator.workflow.retry_failed_extractions",
    ],
}

# Fixtures
fixtures = []
