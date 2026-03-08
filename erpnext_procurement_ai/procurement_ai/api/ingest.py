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


@frappe.whitelist()
def process(source_type: str = "Auto-Detect"):
    """
    API endpoint: Upload and queue a document for processing.

    Called as: POST /api/method/erpnext_procurement_ai.procurement_ai.api.ingest.process

    Expects a file upload in the request. Creates an AI Procurement Job
    and enqueues background processing.
    """
    if not frappe.has_permission("AI Procurement Job", ptype="create"):
        frappe.throw(
            "You do not have permission to create AI Procurement Jobs",
            frappe.PermissionError,
        )

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
            "erpnext_procurement_ai.procurement_ai.api.ingest.run_extraction_pipeline",
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
        job.raw_text_ocr = raw_text
        job.save()
        frappe.db.commit()

        # Publish realtime update
        frappe.publish_realtime(
            "ai_procurement_progress",
            {"job": job_name, "stage": "ocr_complete"},
            doctype="AI Procurement Job",
            docname=job_name,
        )

        # Step 2: Run LangGraph pipeline
        from ...llm.graph import build_extraction_graph

        graph = build_extraction_graph(settings)

        # Map source_type display value to internal key
        source_type_map = {
            "Auto-Detect": "Auto-Detect",
            "Cart": "Cart",
            "Order Confirmation": "Order Confirmation",
            "Delivery Note": "Delivery Note",
            "Invoice": "Invoice",
        }

        initial_state = {
            "raw_text": raw_text,
            "document_images": images,
            "source_type_hint": source_type_map.get(
                job.source_type, job.source_type
            ),
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
        )

        # Step 3: Save extraction results to child table
        _save_extraction_results(job, result)

        # Step 4: Check if escalation is needed
        if result.get("needs_escalation"):
            job.status = "Needs Review"
            job.escalation_reason = "\n".join(
                result.get("escalation_reasons", [])
            )
            job.confidence_score = result.get("confidence", 0.0)
            job.consensus_data = json.dumps(result.get("consensus") or {})
            job.detected_type = result.get("source_type_hint", "")
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

        # Step 5: Build document chain
        consensus_data = result.get("validated_data") or result.get("consensus") or {}
        source_type = result.get("source_type_hint", "invoice")
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
        job.status = "Completed"
        job.confidence_score = result.get("confidence", 0.0)
        job.consensus_data = json.dumps(consensus_data)
        job.detected_type = source_type
        job.created_supplier = created.get("supplier")
        job.created_po = created.get("purchase_order")
        job.created_receipt = created.get("purchase_receipt")
        job.created_invoice = created.get("purchase_invoice")
        job.save()
        frappe.db.commit()

        frappe.publish_realtime(
            "ai_procurement_progress",
            {"job": job_name, "stage": "completed"},
            doctype="AI Procurement Job",
            docname=job_name,
        )

        logger.info(f"Job {job_name}: Pipeline completed successfully")

    except Exception as e:
        logger.error(f"Job {job_name}: Pipeline error: {e}\n{traceback.format_exc()}")

        try:
            job = frappe.get_doc("AI Procurement Job", job_name)
            job.status = "Error"
            job.escalation_reason = str(e)
            job.save()
            frappe.db.commit()
        except Exception:
            pass

        frappe.publish_realtime(
            "ai_procurement_progress",
            {"job": job_name, "stage": "error", "error": str(e)},
            doctype="AI Procurement Job",
            docname=job_name,
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
            "erpnext_procurement_ai.procurement_ai.api.ingest.run_extraction_pipeline",
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
                "extracted_data": json.dumps(result.get("extracted_data") or {}),
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
    disputed = pipeline_result.get("consensus", {})

    escalation = frappe.get_doc(
        {
            "doctype": "AI Escalation Log",
            "procurement_job": job.name,
            "escalation_type": _determine_escalation_type(reasons),
            "status": "Open",
            "reason": "\n".join(reasons),
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
                    f"Reasons:\n" + "\n".join(f"- {r}" for r in reasons)
                ),
            )
        except Exception as e:
            logger.error(f"Failed to send escalation email: {e}")


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
