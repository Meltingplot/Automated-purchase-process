"""
Retrospective document chain builder.

Creates the full ERPNext procurement chain from any entry point.
If you upload an invoice, it creates the missing PO and Receipt.
If you upload a delivery note, it creates the missing PO.

All created documents are marked as retrospective and linked
to the AI Procurement Job.
"""

from __future__ import annotations

import logging
import re

from .attachments import attach_source_to_chain
from .purchase_invoice import create_purchase_invoice
from .purchase_order import create_purchase_order
from .purchase_receipt import create_purchase_receipt
from .supplier import ensure_supplier

logger = logging.getLogger(__name__)

# Mapping: source type → which documents need to be created
# Keys match the DocType Select field options
NEEDED_DOCS: dict[str, list[str]] = {
    "Cart": ["Purchase Order"],
    "Order Confirmation": ["Purchase Order"],
    "Delivery Note": ["Purchase Order", "Purchase Receipt"],
    "Invoice": ["Purchase Order", "Purchase Receipt", "Purchase Invoice"],
}


class RetrospectiveChainBuilder:
    """
    Builds the complete procurement document chain.

    Entry point can be any document type. Missing documents
    in the chain are created retrospectively.
    """

    def build_chain(
        self,
        extracted_data: dict,
        source_type: str,
        source_file_url: str,
        settings: dict,
        job_name: str,
    ) -> dict:
        """
        Create the full document chain from extracted data.

        All LLM-sourced data is sanitized at this entry point before
        reaching any database query or document write downstream.

        Args:
            extracted_data: Consensus data from the LLM pipeline
            source_type: Type of the uploaded document
            source_file_url: Frappe File URL of the source document
            settings: Plugin settings
            job_name: AI Procurement Job name

        Returns:
            Dict with links to all created documents + attachments
        """
        # Sanitize all LLM-sourced data before any DB interaction
        extracted_data = sanitize_extracted_data(extracted_data)

        created: dict = {}

        # 1. Ensure supplier exists
        supplier = ensure_supplier(extracted_data)
        created["supplier"] = supplier

        # 2. Determine which docs are needed
        needed = NEEDED_DOCS.get(source_type, [])
        logger.info(
            f"Job {job_name}: Building chain for '{source_type}', "
            f"needed docs: {needed}"
        )

        # 3. Create docs in order
        if "Purchase Order" in needed:
            po_name = create_purchase_order(
                extracted_data=extracted_data,
                supplier=supplier,
                settings=settings,
                job_name=job_name,
            )
            created["purchase_order"] = po_name

        if "Purchase Receipt" in needed:
            pr_name = create_purchase_receipt(
                extracted_data=extracted_data,
                supplier=supplier,
                settings=settings,
                job_name=job_name,
                purchase_order=created.get("purchase_order"),
            )
            created["purchase_receipt"] = pr_name

        if "Purchase Invoice" in needed:
            pi_name = create_purchase_invoice(
                extracted_data=extracted_data,
                supplier=supplier,
                settings=settings,
                job_name=job_name,
                purchase_order=created.get("purchase_order"),
                purchase_receipt=created.get("purchase_receipt"),
            )
            created["purchase_invoice"] = pi_name

        # 4. Attach source document to all created docs
        if source_file_url:
            attachments = attach_source_to_chain(
                source_file_url=source_file_url,
                source_type=source_type,
                created_docs=created,
                job_name=job_name,
            )
            created["attachments"] = attachments

        logger.info(f"Job {job_name}: Chain complete. Created: {list(created.keys())}")
        return created


# ============================================================
# Centralized input sanitization for all LLM-sourced data.
#
# Applied once at the build_chain() entry point so every
# downstream builder (supplier, PO, PR, PI, attachments)
# receives clean data. This is defense-in-depth — frappe's
# ORM parameterizes queries, but the data originates from
# LLM extraction of potentially adversarial documents.
# ============================================================

# Valid document types for the document_type field
_VALID_DOC_TYPES = {"cart", "order_confirmation", "delivery_note", "invoice"}


def _clean_text(value: str, max_len: int = 200) -> str:
    """Strip null bytes, control chars, collapse whitespace, truncate."""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len]


def _clean_date(value) -> str | None:
    """Validate and return a YYYY-MM-DD date string, or None."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", cleaned):
        return cleaned
    # Also accept YYYY-MM-DDTHH:MM:SS (truncate to date)
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T", cleaned)
    if m:
        return m.group(1)
    logger.warning(f"Rejected invalid date format: {cleaned!r}")
    return None


def _clean_numeric(value) -> float | None:
    """Coerce to float or return None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning(f"Rejected non-numeric value: {value!r}")
        return None


def _clean_tax_id(value: str) -> str:
    """Validate tax ID format: country prefix + alphanumeric."""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", "", value).strip()
    if re.match(r"^[A-Z]{2}[A-Za-z0-9]{2,15}$", cleaned):
        return cleaned
    if re.match(r"^\d{5,15}$", cleaned):
        return cleaned
    if cleaned:
        logger.warning(f"Rejected invalid tax_id format: {cleaned!r}")
    return ""


def _clean_email(value: str) -> str:
    """Validate basic email format."""
    if not isinstance(value, str):
        return ""
    cleaned = value.strip().lower()
    if re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", cleaned):
        return cleaned[:254]
    if cleaned:
        logger.warning(f"Rejected invalid email format: {cleaned!r}")
    return ""


def _clean_phone(value: str) -> str:
    """Keep only phone-valid characters."""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[^\d\s\-+/()\.]", "", value).strip()
    return cleaned[:30]


def _clean_code(value: str) -> str:
    """Allow only alphanumeric, hyphens, dots, underscores, spaces."""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[^\w\s.\-]", "", value).strip()
    return cleaned[:140]


def _clean_currency(value: str) -> str:
    """Validate 3-letter currency code."""
    if not isinstance(value, str):
        return "EUR"
    cleaned = value.strip().upper()
    if re.match(r"^[A-Z]{3}$", cleaned):
        return cleaned
    return "EUR"


def sanitize_extracted_data(data: dict) -> dict:
    """
    Sanitize all fields in the LLM consensus data dict.

    Returns a new dict with all values cleaned. Unknown keys are dropped.
    """
    clean: dict = {}

    # Text fields
    clean["supplier_name"] = _clean_text(data.get("supplier_name", ""), max_len=140)
    clean["supplier_address"] = _clean_text(data.get("supplier_address", ""), max_len=500)
    clean["supplier_tax_id"] = _clean_tax_id(data.get("supplier_tax_id", ""))
    clean["supplier_email"] = _clean_email(data.get("supplier_email", ""))
    clean["supplier_phone"] = _clean_phone(data.get("supplier_phone", ""))
    clean["document_number"] = _clean_text(data.get("document_number", ""), max_len=140)
    clean["payment_terms"] = _clean_text(data.get("payment_terms", ""), max_len=500)
    clean["notes"] = _clean_text(data.get("notes", ""), max_len=2000)
    clean["order_reference"] = _clean_text(data.get("order_reference", ""), max_len=140)
    clean["currency"] = _clean_currency(data.get("currency", "EUR"))

    # Document type — validate against known types
    doc_type = str(data.get("document_type", "")).strip().lower()
    clean["document_type"] = doc_type if doc_type in _VALID_DOC_TYPES else "invoice"

    # Date fields
    clean["document_date"] = _clean_date(data.get("document_date"))
    clean["delivery_date"] = _clean_date(data.get("delivery_date"))

    # Numeric fields
    clean["subtotal"] = _clean_numeric(data.get("subtotal"))
    clean["tax_amount"] = _clean_numeric(data.get("tax_amount"))
    clean["total_amount"] = _clean_numeric(data.get("total_amount"))
    clean["shipping_cost"] = _clean_numeric(data.get("shipping_cost"))
    clean["confidence_self_assessment"] = _clean_numeric(
        data.get("confidence_self_assessment")
    )

    # Line items
    raw_items = data.get("items", [])
    if isinstance(raw_items, list):
        clean["items"] = [_sanitize_line_item(item) for item in raw_items]
    else:
        clean["items"] = []

    return clean


def _sanitize_line_item(item: dict) -> dict:
    """Sanitize a single line item dict."""
    if not isinstance(item, dict):
        return {}

    return {
        "position": _clean_numeric(item.get("position")),
        "item_code": _clean_code(item.get("item_code", "")),
        "item_name": _clean_text(item.get("item_name", "Unknown Item"), max_len=140),
        "description": _clean_text(item.get("description", ""), max_len=500),
        "quantity": _clean_numeric(item.get("quantity")) or 1,
        "uom": _clean_text(item.get("uom", "Nos"), max_len=20),
        "unit_price": _clean_numeric(item.get("unit_price")) or 0,
        "total_price": _clean_numeric(item.get("total_price")) or 0,
        "tax_rate": _clean_numeric(item.get("tax_rate")),
        "discount_percent": _clean_numeric(item.get("discount_percent")),
    }
