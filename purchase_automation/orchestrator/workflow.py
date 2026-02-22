"""Purchase document workflow orchestrator.

Coordinates the full lifecycle of a purchase document:
1. Document upload triggers background extraction
2. Dual-model extraction runs asynchronously
3. Results are compared and validated
4. ERPNext documents are created based on document type
5. Human review is triggered when needed

This module contains Frappe-specific code (doc events, background jobs).
All LLM interaction is delegated to the extraction module.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import frappe
from frappe import _

from purchase_automation.extraction.comparator import ComparisonLevel
from purchase_automation.extraction.extractor import extract_document
from purchase_automation.extraction.schemas import DocumentType, ExtractedDocument
from purchase_automation.llm.registry import get_provider
from purchase_automation.matching.supplier_matcher import match_supplier
from purchase_automation.matching.item_matcher import match_item

logger = logging.getLogger(__name__)


def on_document_uploaded(doc, method=None):
    """Hook: triggered after a Purchase Document is inserted.

    Enqueues the extraction as a background job so the upload
    returns immediately to the user.
    """
    if doc.status != "Uploaded":
        return

    frappe.enqueue(
        "purchase_automation.orchestrator.workflow.process_document",
        queue="long",
        timeout=300,
        document_name=doc.name,
    )

    frappe.msgprint(
        _("Document uploaded. Extraction is running in the background."),
        alert=True,
    )


def process_document(document_name: str) -> None:
    """Background job: run full extraction pipeline on a Purchase Document.

    This is the main entry point for processing. It:
    1. Reads the uploaded file
    2. Runs dual-model extraction
    3. Stores results on the Purchase Document
    4. Creates ERPNext documents if auto-accepted
    5. Sets status for review if needed
    """
    doc = frappe.get_doc("Purchase Document", document_name)

    try:
        doc.status = "Processing"
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        # Get file content
        file_url = doc.source_file
        if not file_url:
            raise ValueError("No source file attached")

        file_doc = frappe.get_doc("File", {"file_url": file_url})
        file_bytes = file_doc.get_content()
        filename = file_doc.file_name or "document.pdf"

        # Get LLM providers
        provider_a = get_provider("primary")
        provider_b = get_provider("secondary")

        # Get thresholds from settings
        settings = frappe.get_doc("Purchase Automation Settings")
        auto_accept_threshold = float(settings.auto_accept_threshold or 0.95)
        review_threshold = float(settings.review_threshold or 0.70)

        # Run dual-model extraction
        result = extract_document(
            file_bytes=file_bytes,
            filename=filename,
            provider_a=provider_a,
            provider_b=provider_b,
            auto_accept_threshold=auto_accept_threshold,
            review_threshold=review_threshold,
        )

        # Store extraction results
        doc.extraction_a_result = json.dumps(
            result.parsed_a.model_dump(mode="json") if result.parsed_a else None,
            ensure_ascii=False,
        )
        doc.extraction_b_result = json.dumps(
            result.parsed_b.model_dump(mode="json") if result.parsed_b else None,
            ensure_ascii=False,
        )

        if result.comparison:
            doc.comparison_score = result.comparison.overall_score
            doc.comparison_details = json.dumps(
                result.comparison.to_dict(), ensure_ascii=False
            )
            doc.merged_result = json.dumps(
                result.comparison.merged_result, ensure_ascii=False
            ) if result.comparison.merged_result else None

        # Log extraction details for audit
        _create_extraction_log(doc, result)

        # Store processing time
        doc.processing_time_seconds = (
            result.response_a.duration_seconds + result.response_b.duration_seconds
        )

        # Determine next steps based on comparison level
        if not result.success:
            doc.status = "Error"
            doc.error_log = _format_errors(result)

        elif result.level == ComparisonLevel.AUTO_ACCEPT:
            doc.status = "Extracted"
            # Auto-create ERPNext documents
            _create_erpnext_documents(doc, result.parsed_a)

        elif result.level == ComparisonLevel.AUTO_RESOLVE:
            doc.status = "Extracted"
            _create_erpnext_documents(doc, result.parsed_a)

        elif result.level == ComparisonLevel.REVIEW:
            doc.status = "Review"

        elif result.level == ComparisonLevel.REJECT:
            doc.status = "Review"
            doc.review_notes = _(
                "Dual-model extraction produced conflicting results. "
                "Manual review required."
            )

        doc.save(ignore_permissions=True)
        frappe.db.commit()

    except Exception as e:
        logger.exception("Error processing document %s", document_name)
        doc.reload()
        doc.status = "Error"
        doc.error_log = f"{type(e).__name__}: {e}"
        doc.save(ignore_permissions=True)
        frappe.db.commit()


def retry_failed_extractions():
    """Scheduled job: retry documents stuck in Error status.

    Only retries documents that failed within the last 24 hours
    and have been retried fewer than 3 times.
    """
    failed_docs = frappe.get_all(
        "Purchase Document",
        filters={
            "status": "Error",
            "retry_count": ("<", 3),
        },
        fields=["name"],
        limit_page_length=10,
    )

    for doc_ref in failed_docs:
        doc = frappe.get_doc("Purchase Document", doc_ref["name"])
        doc.retry_count = (doc.retry_count or 0) + 1
        doc.status = "Uploaded"
        doc.error_log = None
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        # Re-trigger processing
        on_document_uploaded(doc)


def _create_erpnext_documents(
    purchase_doc: object,
    extracted: ExtractedDocument,
) -> None:
    """Create ERPNext purchase documents from validated extraction.

    The document type determines which ERPNext documents are created:
    - Shopping Cart → Purchase Order (Draft)
    - Order Confirmation → Purchase Order (Submitted)
    - Delivery Note → Purchase Order + Purchase Receipt
    - Purchase Invoice → Purchase Order + Purchase Invoice
    """
    # Match supplier
    supplier_match = match_supplier(
        extracted.supplier_name,
        extracted.supplier_tax_id,
    )

    if not supplier_match.matched:
        purchase_doc.status = "Review"
        purchase_doc.review_notes = _(
            "Supplier '{0}' not found in master data. "
            "Please assign manually."
        ).format(extracted.supplier_name)
        return

    supplier_name = supplier_match.supplier_name

    # Match items
    matched_items = []
    has_unmatched = False

    for line_item in extracted.line_items:
        item_match = match_item(
            item_description=line_item.item_description,
            supplier_item_code=line_item.item_code_supplier,
            supplier_name=supplier_name,
        )

        matched_items.append({
            "extracted": line_item,
            "match": item_match,
        })

        if not item_match.matched:
            has_unmatched = True

    if has_unmatched:
        purchase_doc.status = "Review"
        unmatched = [
            mi["extracted"].item_description
            for mi in matched_items
            if not mi["match"].matched
        ]
        purchase_doc.review_notes = _(
            "Items not found in master data: {0}. "
            "Please assign manually."
        ).format(", ".join(unmatched))
        return

    # Create Purchase Order
    schedule_date = (
        extracted.delivery_date
        or extracted.document_date
        or date.today()
    )

    po = frappe.new_doc("Purchase Order")
    po.supplier = supplier_name
    po.transaction_date = str(extracted.document_date or date.today())
    po.schedule_date = str(schedule_date)

    if extracted.document_number:
        po.supplier_reference = extracted.document_number

    for mi in matched_items:
        line = mi["extracted"]
        item = mi["match"]
        po.append("items", {
            "item_code": item.item_code,
            "item_name": item.item_name,
            "qty": line.quantity,
            "rate": line.unit_price or 0,
            "schedule_date": str(schedule_date),
        })

    po.insert(ignore_permissions=True)
    purchase_doc.linked_purchase_order = po.name

    # Submit PO for order confirmations
    if extracted.document_type in (
        DocumentType.ORDER_CONFIRMATION,
        DocumentType.DELIVERY_NOTE,
        DocumentType.PURCHASE_INVOICE,
    ):
        po.submit()

    # Create Purchase Receipt for delivery notes
    if extracted.document_type == DocumentType.DELIVERY_NOTE:
        _create_purchase_receipt(purchase_doc, po, extracted)

    # Create Purchase Invoice for invoices
    if extracted.document_type == DocumentType.PURCHASE_INVOICE:
        _create_purchase_invoice(purchase_doc, po, extracted)

    purchase_doc.status = "Approved"


def _create_purchase_receipt(purchase_doc, po, extracted: ExtractedDocument):
    """Create a Purchase Receipt from a submitted Purchase Order."""
    pr = frappe.new_doc("Purchase Receipt")
    pr.supplier = po.supplier
    pr.posting_date = str(extracted.document_date or date.today())

    if extracted.document_number:
        pr.supplier_delivery_note = extracted.document_number

    for po_item in po.items:
        pr.append("items", {
            "item_code": po_item.item_code,
            "item_name": po_item.item_name,
            "qty": po_item.qty,
            "rate": po_item.rate,
            "purchase_order": po.name,
            "purchase_order_item": po_item.name,
            "warehouse": frappe.db.get_single_value(
                "Stock Settings", "default_warehouse"
            ) or "",
        })

    pr.insert(ignore_permissions=True)
    pr.submit()
    purchase_doc.linked_purchase_receipt = pr.name


def _create_purchase_invoice(purchase_doc, po, extracted: ExtractedDocument):
    """Create a Purchase Invoice from a submitted Purchase Order."""
    pi = frappe.new_doc("Purchase Invoice")
    pi.supplier = po.supplier
    pi.posting_date = str(extracted.document_date or date.today())

    if extracted.due_date:
        pi.due_date = str(extracted.due_date)

    if extracted.document_number:
        pi.bill_no = extracted.document_number
        pi.bill_date = str(extracted.document_date or date.today())

    for po_item in po.items:
        pi.append("items", {
            "item_code": po_item.item_code,
            "item_name": po_item.item_name,
            "qty": po_item.qty,
            "rate": po_item.rate,
            "purchase_order": po.name,
            "po_detail": po_item.name,
        })

    pi.insert(ignore_permissions=True)
    purchase_doc.linked_purchase_invoice = pi.name


def _create_extraction_log(purchase_doc, result) -> None:
    """Create LLM Extraction Log entries for audit trail."""
    for label, response, parse_error in [
        ("primary", result.response_a, result.parse_error_a),
        ("secondary", result.response_b, result.parse_error_b),
    ]:
        log = frappe.new_doc("LLM Extraction Log")
        log.purchase_document = purchase_doc.name
        log.provider = response.provider_name
        log.model = response.model_name
        log.role = label
        log.prompt_tokens = response.prompt_tokens
        log.completion_tokens = response.completion_tokens
        log.duration_seconds = round(response.duration_seconds, 2)
        log.raw_response = response.raw_text[:10000] if response.raw_text else ""
        log.error = response.error or parse_error or ""
        log.insert(ignore_permissions=True)


def _format_errors(result) -> str:
    """Format extraction errors for the error_log field."""
    errors = []
    if result.response_a.error:
        errors.append(f"Model A ({result.response_a.provider_name}): {result.response_a.error}")
    if result.parse_error_a:
        errors.append(f"Model A parse: {result.parse_error_a}")
    if result.response_b.error:
        errors.append(f"Model B ({result.response_b.provider_name}): {result.response_b.error}")
    if result.parse_error_b:
        errors.append(f"Model B parse: {result.parse_error_b}")
    return "\n".join(errors)
