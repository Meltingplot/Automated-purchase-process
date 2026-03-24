"""
API endpoints for job status queries.
"""

from __future__ import annotations

import frappe
from frappe import _


@frappe.whitelist()
def get_job_status(job_name: str) -> dict:
    """
    Get the current status of an AI Procurement Job.

    Returns status, confidence, and links to created documents.
    """
    if not frappe.has_permission("AI Procurement Job", ptype="read"):
        frappe.throw(
            _("You do not have permission to view AI Procurement Jobs"),
            frappe.PermissionError,
        )

    job = frappe.get_doc("AI Procurement Job", job_name)

    return {
        "name": job.name,
        "status": job.status,
        "source_type": job.source_type,
        "detected_type": job.detected_type,
        "confidence_score": job.confidence_score,
        "created_supplier": job.created_supplier,
        "created_po": job.created_po,
        "created_receipt": job.created_receipt,
        "created_invoice": job.created_invoice,
        "escalation_reason": job.escalation_reason,
    }


@frappe.whitelist()
def get_dashboard_stats() -> dict:
    """
    Get statistics for the procurement AI dashboard.

    Returns counts by status, recent jobs, and escalation queue.
    """
    if not frappe.has_permission("AI Procurement Job", ptype="read"):
        frappe.throw(
            _("You do not have permission to view AI Procurement Jobs"),
            frappe.PermissionError,
        )

    # Status counts
    status_counts = {}
    for status in ["Pending", "Processing", "Awaiting Review", "Needs Review", "Completed", "Error"]:
        count = frappe.db.count(
            "AI Procurement Job", filters={"status": status}
        )
        status_counts[status] = count

    # Recent jobs
    recent_jobs = frappe.get_all(
        "AI Procurement Job",
        fields=[
            "name",
            "status",
            "source_type",
            "detected_type",
            "confidence_score",
            "creation",
        ],
        order_by="creation desc",
        limit=20,
    )

    # Open escalations
    open_escalations = frappe.get_all(
        "AI Escalation Log",
        filters={"status": ["in", ["Open", "In Review"]]},
        fields=[
            "name",
            "procurement_job",
            "escalation_type",
            "status",
            "reason",
            "creation",
        ],
        order_by="creation desc",
        limit=10,
    )

    return {
        "status_counts": status_counts,
        "recent_jobs": recent_jobs,
        "open_escalations": open_escalations,
        "total_jobs": sum(status_counts.values()),
    }
