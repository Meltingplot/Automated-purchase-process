"""
API endpoints for document ingestion and processing.

Main entry points:
- process(): Upload and queue a document for processing
- run_extraction_pipeline(): Background job that runs the full pipeline
- process_pending_jobs(): Scheduler hook for pending jobs
"""

from __future__ import annotations

import json
import logging
import os
import traceback

import frappe

logger = logging.getLogger(__name__)

# All DocTypes the pipeline may create — checked at entry points
_REQUIRED_CREATE_DOCTYPES = [
    "AI Procurement Job",
    "Supplier",
    "Purchase Order",
    "Purchase Receipt",
    "Purchase Invoice",
    "Item",
]


def _check_creation_permissions(user=None):
    """Check that the user has 'create' permission on all DocTypes the pipeline may create.

    Raises frappe.PermissionError if any permission is missing.
    """
    missing = []
    for dt in _REQUIRED_CREATE_DOCTYPES:
        if not frappe.has_permission(dt, ptype="create", user=user):
            missing.append(dt)
    if missing:
        frappe.throw(
            f"You do not have permission to create: {', '.join(missing)}. "
            "All document creation permissions are required to use AI Procurement.",
            frappe.PermissionError,
        )


@frappe.whitelist()
def process(source_type: str = "Auto-Detect"):
    """
    API endpoint: Upload and queue a document for processing.

    Called as: POST /api/method/procurement_ai.procurement_ai.api.ingest.process

    Expects a file upload in the request. Creates an AI Procurement Job
    and enqueues background processing.
    """
    _check_creation_permissions()

    # Get uploaded file
    uploaded_file = frappe.request.files.get("file")
    if not uploaded_file:
        frappe.throw("No file uploaded")

    filename = uploaded_file.filename
    file_content = uploaded_file.read()

    # Validate file
    from ...utils.security import validate_upload

    is_valid, error = validate_upload(file_content, filename)
    if not is_valid:
        frappe.throw(f"File validation failed: {error}")

    # Save file to Frappe
    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": filename,
            "content": file_content,
            "is_private": 1,
        }
    )
    file_doc.save(ignore_permissions=True)

    # Create AI Procurement Job
    job = frappe.get_doc(
        {
            "doctype": "AI Procurement Job",
            "status": "Pending",
            "source_document": file_doc.file_url,
            "source_document_url": file_doc.file_url,
            "source_type": source_type,
        }
    )
    job.insert(ignore_permissions=True)

    # Check if auto-processing is enabled
    settings = frappe.get_single("AI Procurement Settings")
    if settings.enable_auto_processing:
        job.status = "Processing"
        job.save()

        frappe.enqueue(
            "procurement_ai.procurement_ai.api.ingest.run_extraction_pipeline",
            queue="long",
            timeout=600,
            procurement_job_name=job.name,
        )

    frappe.response["message"] = {
        "job_name": job.name,
        "status": job.status,
        "file_url": file_doc.file_url,
    }


def run_extraction_pipeline(procurement_job_name: str):
    """
    Background job: Run the full extraction pipeline for a job.

    This is enqueued via frappe.enqueue() and runs asynchronously.
    """
    job_name = procurement_job_name
    try:
        job = frappe.get_doc("AI Procurement Job", job_name)
        job.status = "Processing"
        job.save()
        frappe.db.commit()

        # Get settings
        settings_doc = frappe.get_single("AI Procurement Settings")
        settings = settings_doc.get_settings_dict()

        # Step 1: Extract text from document
        raw_text, images = _extract_document(job, settings)
        # Strip null bytes / control chars from OCR text before storing
        import re as _re

        job.raw_text_ocr = _re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw_text)
        job.save()
        frappe.db.commit()

        # Publish realtime update
        frappe.publish_realtime(
            "ai_procurement_progress",
            {"job": job_name, "stage": "ocr_complete"},
            doctype="AI Procurement Job",
            docname=job_name,
            after_commit=True,
        )

        # Step 2: Run LangGraph pipeline
        from ...llm.graph import build_extraction_graph

        graph = build_extraction_graph(settings)

        initial_state = {
            "raw_text": raw_text,
            "document_images": images,
            "source_type_hint": job.source_type or "Auto-Detect",
            "source_file_url": job.source_document_url or job.source_document,
            "job_name": job_name,
            "ocr_result": None,
            "llm_results": [],
            "consensus": None,
            "confidence": 0.0,
            "needs_escalation": False,
            "escalation_reasons": [],
            "validated_data": None,
            "settings": settings,
        }

        result = graph.invoke(initial_state)

        # Publish realtime update
        frappe.publish_realtime(
            "ai_procurement_progress",
            {"job": job_name, "stage": "extraction_complete"},
            doctype="AI Procurement Job",
            docname=job_name,
            after_commit=True,
        )

        # Step 3: Save extraction results to child table
        _save_extraction_results(job, result)

        # Step 4: Check if escalation is needed
        if result.get("needs_escalation"):
            job.status = "Needs Review"
            # Sanitize escalation reasons (pipeline-sourced, may contain LLM text)
            reasons = result.get("escalation_reasons", [])
            job.escalation_reason = "\n".join(
                str(r)[:500] for r in reasons if isinstance(r, str)
            )
            job.confidence_score = float(result.get("confidence", 0.0) or 0.0)
            consensus = result.get("consensus") or {}
            job.consensus_data = json.dumps(consensus)
            job.detected_type = _resolve_detected_type(
                result.get("source_type_hint", ""), consensus,
            )
            job.save()
            frappe.db.commit()

            # Create escalation log
            _create_escalation(job, result)

            frappe.publish_realtime(
                "ai_procurement_progress",
                {"job": job_name, "stage": "needs_review"},
                doctype="AI Procurement Job",
                docname=job_name,
            )
            return

        # Step 4b: Pause for human review if enabled or single-provider
        consensus_data = result.get("validated_data") or result.get("consensus") or {}
        source_type = result.get("source_type_hint", "Invoice")

        # Force review when only one LLM produced valid results (no consensus)
        valid_llm_count = sum(
            1 for r in result.get("llm_results", [])
            if r.get("extracted_data") is not None
        )
        force_review = valid_llm_count < 2

        if settings.get("require_document_review") or force_review:
            job.status = "Awaiting Review"
            job.confidence_score = float(result.get("confidence", 0.0) or 0.0)
            job.consensus_data = json.dumps(consensus_data)
            job.detected_type = _resolve_detected_type(source_type, consensus_data)
            job.save()
            frappe.db.commit()

            frappe.publish_realtime(
                "ai_procurement_progress",
                {"job": job_name, "stage": "awaiting_review"},
                doctype="AI Procurement Job",
                docname=job_name,
            )
            return

        # Step 5: Build document chain
        source_file_url = job.source_document_url or job.source_document

        from ...chain_builder.retrospective import RetrospectiveChainBuilder

        builder = RetrospectiveChainBuilder()
        created = builder.build_chain(
            extracted_data=consensus_data,
            source_type=source_type,
            source_file_url=source_file_url,
            settings=settings,
            job_name=job_name,
        )

        # Step 6: Update job with results
        _complete_job(job, result, consensus_data, source_type, created)

        logger.info(f"Job {job_name}: Pipeline completed successfully")

    except Exception as e:
        logger.error(f"Job {job_name}: Pipeline error: {e}\n{traceback.format_exc()}")

        _safe_error_msg = (
            "An error occurred during document processing. "
            "Check server logs or contact your administrator."
        )

        try:
            job = frappe.get_doc("AI Procurement Job", job_name)
            job.status = "Error"
            job.escalation_reason = _safe_error_msg
            job.save()
            frappe.db.commit()
        except Exception:
            pass

        frappe.publish_realtime(
            "ai_procurement_progress",
            {"job": job_name, "stage": "error", "error": _safe_error_msg},
            doctype="AI Procurement Job",
            docname=job_name,
            after_commit=True,
        )


def _complete_job(job, pipeline_result: dict | None, consensus_data: dict, source_type: str, created: dict):
    """Update job with created docs, verify amounts, and set final status.

    Auto-completes if amounts are within the configured tolerance.
    Stays in 'Needs Review' if amounts differ beyond tolerance.
    """
    job_name = job.name
    confidence = 0.0
    if pipeline_result:
        confidence = float(pipeline_result.get("confidence", 0.0) or 0.0)
    elif job.confidence_score:
        confidence = float(job.confidence_score)

    job.confidence_score = confidence
    job.consensus_data = json.dumps(consensus_data)
    job.detected_type = _resolve_detected_type(source_type, consensus_data)
    job.created_supplier = created.get("supplier")
    job.created_po = created.get("purchase_order")
    job.created_receipt = created.get("purchase_receipt")
    job.created_invoice = created.get("purchase_invoice")

    # Verify amounts against created documents
    settings_doc = frappe.get_single("AI Procurement Settings")
    tolerance = float(settings_doc.amount_tolerance or 0.05)
    has_mismatch, verification = _verify_amounts(consensus_data, created, tolerance)

    if has_mismatch:
        job.status = "Needs Review"
        job.escalation_reason = verification
        stage = "needs_review"
    else:
        job.status = "Completed"
        stage = "completed"

    job.save()
    frappe.db.commit()

    if verification:
        job.add_comment("Comment", verification)
        frappe.db.commit()

    # Auto-resolve open escalations for this job
    open_escalations = frappe.get_all(
        "AI Escalation Log",
        filters={"procurement_job": job_name, "status": "Open"},
        fields=["name"],
    )
    for esc in open_escalations:
        frappe.db.set_value(
            "AI Escalation Log", esc["name"],
            {"status": "Resolved", "resolution_action": "Approved as-is"},
        )

    frappe.publish_realtime(
        "ai_procurement_progress",
        {"job": job_name, "stage": stage},
        doctype="AI Procurement Job",
        docname=job_name,
    )


def run_chain_from_review(procurement_job_name: str):
    """
    Background job: Build document chain from reviewed data.

    Called after user approves the review form. Uses reviewed_data
    (falls back to consensus_data) and item_mapping from the job.
    """
    job_name = procurement_job_name
    try:
        job = frappe.get_doc("AI Procurement Job", job_name)

        # Get settings
        settings_doc = frappe.get_single("AI Procurement Settings")
        settings = settings_doc.get_settings_dict()

        # Use reviewed data if available, otherwise fall back to consensus
        if job.reviewed_data:
            consensus_data = json.loads(job.reviewed_data)
        else:
            consensus_data = json.loads(job.consensus_data or "{}")

        # Validate consensus data against the extraction schema
        from ...llm.schemas import ExtractedDocument
        from pydantic import ValidationError as PydanticValidationError

        try:
            ExtractedDocument.model_validate(consensus_data)
        except PydanticValidationError as e:
            logger.error(f"Job {job_name}: data failed schema validation: {e}")
            raise ValueError(
                "Extracted data does not conform to the expected schema. "
                "Please re-process the document."
            )

        # Parse item_mapping (JSON: {"0": "ITEM-001", "1": null, ...})
        item_mapping = None
        if job.item_mapping:
            raw_mapping = json.loads(job.item_mapping)
            # Convert string keys to int, skip null/empty values
            item_mapping = {
                int(k): v for k, v in raw_mapping.items()
                if v
            }
            if not item_mapping:
                item_mapping = None

        # Parse stock_uom_mapping (JSON: {"0": "mm", "1": null, ...})
        stock_uom_mapping = None
        if job.stock_uom_mapping:
            raw_suom = json.loads(job.stock_uom_mapping)
            stock_uom_mapping = {
                int(k): v for k, v in raw_suom.items()
                if v
            }
            if not stock_uom_mapping:
                stock_uom_mapping = None

        source_type = job.detected_type or "Invoice"
        source_file_url = job.source_document_url or job.source_document

        from ...chain_builder.retrospective import RetrospectiveChainBuilder

        builder = RetrospectiveChainBuilder()
        created = builder.build_chain(
            extracted_data=consensus_data,
            source_type=source_type,
            source_file_url=source_file_url,
            settings=settings,
            job_name=job_name,
            item_mapping=item_mapping,
            stock_uom_mapping=stock_uom_mapping,
        )

        _complete_job(job, None, consensus_data, source_type, created)
        logger.info(f"Job {job_name}: Chain from review completed successfully")

    except Exception as e:
        logger.error(f"Job {job_name}: Chain from review error: {e}\n{traceback.format_exc()}")

        _safe_error_msg = (
            "An error occurred during document creation. "
            "Check server logs or contact your administrator."
        )

        try:
            job = frappe.get_doc("AI Procurement Job", job_name)
            job.status = "Error"
            job.escalation_reason = _safe_error_msg
            job.save()
            frappe.db.commit()
        except Exception:
            pass

        frappe.publish_realtime(
            "ai_procurement_progress",
            {"job": job_name, "stage": "error", "error": _safe_error_msg},
            doctype="AI Procurement Job",
            docname=job_name,
            after_commit=True,
        )


def process_pending_jobs():
    """
    Scheduler hook: Process all pending jobs.

    Called by frappe scheduler (configured in hooks.py).
    """
    settings = frappe.get_single("AI Procurement Settings")
    if not settings.enable_auto_processing:
        return

    pending_jobs = frappe.get_all(
        "AI Procurement Job",
        filters={"status": "Pending"},
        fields=["name"],
        order_by="creation asc",
        limit=10,
    )

    for job_data in pending_jobs:
        frappe.enqueue(
            "procurement_ai.procurement_ai.api.ingest.run_extraction_pipeline",
            queue="long",
            timeout=600,
            procurement_job_name=job_data["name"],
        )


def _extract_document(job, settings: dict) -> tuple[str, list[bytes]]:
    """Extract text and images from the uploaded document."""
    file_url = job.source_document_url or job.source_document
    if not file_url:
        frappe.throw("No source document attached to job")

    # Get file path
    file_path = frappe.get_site_path("private", "files", os.path.basename(file_url))
    if not os.path.exists(file_path):
        # Try public files
        file_path = frappe.get_site_path("public", "files", os.path.basename(file_url))

    if not os.path.exists(file_path):
        frappe.throw(f"File not found: {file_url}")

    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        from ...extraction.ocr_engine import OCREngine
        from ...extraction.pdf_parser import PDFParser

        engine = OCREngine(engine_name=settings.get("ocr_engine", "Tesseract"))
        parser = PDFParser(ocr_engine=engine)
        result = parser.extract(file_path)
        return result.text, result.images

    elif ext in (".png", ".jpg", ".jpeg", ".tiff", ".tif"):
        from PIL import Image

        from ...extraction.ocr_engine import OCREngine

        engine = OCREngine(engine_name=settings.get("ocr_engine", "Tesseract"))
        img = Image.open(file_path)
        text = engine.extract(img)

        import io

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return text, [buffer.getvalue()]

    else:
        frappe.throw(f"Unsupported file type: {ext}")


def _save_extraction_results(job, pipeline_result: dict):
    """Save LLM extraction results to the job's child table."""
    llm_results = pipeline_result.get("llm_results", [])

    for result in llm_results:
        job.append(
            "extraction_results",
            {
                "llm_provider": result.get("provider", "unknown"),
                "model_version": result.get("model_version", ""),
                "extracted_data": json.dumps(
                    result.get("extracted_data")
                    or {"errors": result.get("errors", [])}
                ),
                "confidence": result.get("confidence", 0.0),
                "processing_time_ms": result.get("processing_time_ms", 0),
                "token_count": result.get("token_count", 0),
            },
        )

    job.save()
    frappe.db.commit()


def _create_escalation(job, pipeline_result: dict):
    """Create an AI Escalation Log entry."""
    reasons = pipeline_result.get("escalation_reasons", [])
    # Sanitize reason strings (may contain LLM output fragments)
    clean_reasons = [str(r)[:500] for r in reasons if isinstance(r, str)]

    escalation = frappe.get_doc(
        {
            "doctype": "AI Escalation Log",
            "procurement_job": job.name,
            "escalation_type": _determine_escalation_type(clean_reasons),
            "status": "Open",
            "reason": "\n".join(clean_reasons)[:2000],
            "disputed_fields": json.dumps(
                pipeline_result.get("disputed_fields", {})
            ),
        }
    )
    escalation.insert(ignore_permissions=True)

    # Send notification email
    settings = frappe.get_single("AI Procurement Settings")
    if settings.escalation_email:
        try:
            frappe.sendmail(
                recipients=[settings.escalation_email],
                subject=f"AI Procurement Escalation: {job.name}",
                message=(
                    f"Job {job.name} requires manual review.\n\n"
                    f"Reasons:\n"
                    + "\n".join(f"- {r}" for r in clean_reasons)
                ),
            )
        except Exception as e:
            logger.error(f"Failed to send escalation email: {e}")


_VALID_SOURCE_TYPES = {"", "Cart", "Order Confirmation", "Delivery Note", "Invoice"}

# Extraction LLMs return lowercase keys; map to Frappe Select values
_EXTRACTION_TYPE_MAP = {
    "cart": "Cart",
    "order_confirmation": "Order Confirmation",
    "delivery_note": "Delivery Note",
    "invoice": "Invoice",
}


def _validate_source_type(value: str) -> str:
    """Validate source type against DocType Select options."""
    if isinstance(value, str) and value in _VALID_SOURCE_TYPES:
        return value
    return ""


def _resolve_detected_type(
    classifier_hint: str, consensus_data: dict | None = None,
) -> str:
    """Determine the best detected_type from classifier + consensus.

    The extraction LLMs (via consensus ``document_type``) see the full
    document content and are often more accurate than the lightweight
    classifier.  When the consensus has a valid ``document_type``, prefer
    it over the classifier hint.
    """
    # Prefer consensus document_type (multiple LLMs agreeing)
    if consensus_data:
        consensus_type = consensus_data.get("document_type", "")
        mapped = _EXTRACTION_TYPE_MAP.get(consensus_type, "")
        if mapped:
            return mapped

    return _validate_source_type(classifier_hint)


def _determine_escalation_type(reasons: list[str]) -> str:
    """Determine escalation type from reason strings."""
    reason_text = " ".join(reasons).lower()

    if "amount" in reason_text or "total" in reason_text or "mismatch" in reason_text:
        return "Amount Mismatch"
    elif "supplier" in reason_text:
        return "Supplier Unclear"
    elif "ocr" in reason_text:
        return "OCR Mismatch"
    elif "injection" in reason_text:
        return "Processing Error"
    elif "confidence" in reason_text or "disputed" in reason_text:
        return "Low Confidence"
    elif "field" in reason_text:
        return "Field Dispute"

    return "Low Confidence"


def _verify_amounts(
    consensus_data: dict, created: dict, tolerance: float = 0.05,
) -> tuple[bool, str | None]:
    """Compare extracted amounts against created ERPNext document totals.

    Args:
        consensus_data: Extracted/reviewed data with total_amount
        created: Dict with created document names
        tolerance: Max allowed difference in document currency

    Returns:
        (has_mismatch, verification_comment) tuple.
        has_mismatch is True if any document total exceeds the tolerance.
    """
    extracted_total = consensus_data.get("total_amount")
    if not extracted_total:
        return False, None

    try:
        extracted_total = float(extracted_total)
    except (TypeError, ValueError):
        return False, None

    lines = []
    lines.append(f"**Amount Verification** (extracted total: {extracted_total:.2f}, tolerance: {tolerance:.2f})")

    has_mismatch = False

    # All three docs have the same taxes (shipping + VAT) → compare grand_total
    for doctype, key in [
        ("Purchase Order", "purchase_order"),
        ("Purchase Receipt", "purchase_receipt"),
        ("Purchase Invoice", "purchase_invoice"),
    ]:
        doc_name = created.get(key)
        if not doc_name:
            continue

        grand_total = frappe.db.get_value(doctype, doc_name, "grand_total")
        if grand_total is None:
            continue

        grand_total = float(grand_total)
        diff = abs(grand_total - extracted_total)
        pct = (diff / extracted_total * 100) if extracted_total else 0

        if diff <= tolerance:
            lines.append(f"- {doctype} {doc_name}: {grand_total:.2f} ✓")
        else:
            has_mismatch = True
            lines.append(
                f"- {doctype} {doc_name}: {grand_total:.2f} "
                f"(diff: {diff:.2f}, {pct:.1f}%) ⚠"
            )

    if len(lines) <= 1:
        return False, None

    if has_mismatch:
        lines.insert(0, "⚠ **Amount mismatch detected** — please review the created documents.")

    return has_mismatch, "\n".join(lines)
