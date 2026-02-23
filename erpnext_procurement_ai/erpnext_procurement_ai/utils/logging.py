"""
Structured logging utilities for the procurement AI pipeline.
"""

from __future__ import annotations

import logging

import frappe


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the app prefix."""
    return logging.getLogger(f"erpnext_procurement_ai.{name}")


def log_job_event(job_name: str, event: str, details: str = ""):
    """
    Log a job processing event to both Python logger and Frappe comment.

    Args:
        job_name: AI Procurement Job name
        event: Event description (e.g., "OCR Complete", "LLM Started")
        details: Optional additional details
    """
    logger = get_logger("pipeline")
    logger.info(f"Job {job_name}: {event} - {details}")

    try:
        frappe.get_doc("AI Procurement Job", job_name).add_comment(
            "Comment", f"{event}: {details}" if details else event
        )
    except Exception:
        pass  # Don't fail on comment errors


def log_llm_usage(
    job_name: str,
    provider: str,
    model: str,
    tokens: int,
    duration_ms: int,
):
    """Log LLM token usage for cost tracking."""
    logger = get_logger("llm_usage")
    logger.info(
        f"Job {job_name}: {provider}/{model} - "
        f"{tokens} tokens, {duration_ms}ms"
    )
