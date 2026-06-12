"""
Retrospective document chain builder.

Creates the full ERPNext procurement chain from any entry point.
If you upload an invoice, it creates the missing PO and Receipt.
If you upload a delivery note, it creates the missing PO.

Uses "find before create" logic: attempts to match existing documents
before creating new ones, avoiding duplicates when a user already has
manually-created documents.

All created documents are marked as retrospective and linked
to the AI Procurement Job.
"""

from __future__ import annotations

import logging
import re

from .attachments import attach_source_to_chain
from .document_matcher import (
    build_item_links,
    find_matching_purchase_invoice,
    find_matching_purchase_order,
    find_matching_purchase_receipt,
)
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
    in the chain are found (if existing) or created retrospectively.
    """

    def build_chain(
        self,
        extracted_data: dict,
        source_type: str,
        source_file_url: str,
        settings: dict,
        job_name: str,
        item_mapping: dict | None = None,
        stock_uom_mapping: dict | None = None,
        supplier_mapping: str | None = None,
    ) -> dict:
        """
        Find or create the full document chain from extracted data.

        Uses "find before create" logic: for each document in the chain,
        first attempts to match an existing document. Only creates a new
        one if no match is found.

        All LLM-sourced data is sanitized at this entry point before
        reaching any database query or document write downstream.

        Args:
            extracted_data: Consensus data from the LLM pipeline
            source_type: Type of the uploaded document
            source_file_url: Frappe File URL of the source document
            settings: Plugin settings
            job_name: AI Procurement Job name
            item_mapping: Optional user-reviewed mapping of item index to
                          ERPNext Item code. Skips _resolve_item for mapped items.
            supplier_mapping: Optional Supplier name explicitly assigned by the
                          user. Bypasses fuzzy matching/creation when set.

        Returns:
            Dict with links to all created/matched documents + attachments.
            Includes *_matched flags to distinguish matched vs created docs.
        """
        # Sanitize all LLM-sourced data before any DB interaction
        extracted_data = sanitize_extracted_data(extracted_data)

        # Book everything in the company base currency: convert a foreign-currency
        # document up-front (rate at document date) so the whole chain — and the
        # matching below — runs in base currency. Happens only after approval,
        # never in the review UI.
        extracted_data = _convert_to_company_currency(extracted_data, settings)

        result: dict = {}

        # 1. Ensure supplier exists
        supplier = ensure_supplier(extracted_data, forced_supplier=supplier_mapping)
        result["supplier"] = supplier

        # 2. Determine which docs are needed
        needed = NEEDED_DOCS.get(source_type, [])
        logger.info(
            f"Job {job_name}: Building chain for '{source_type}', "
            f"needed docs: {needed}"
        )

        # Track item links for downstream documents
        po_item_links: dict | None = None
        pr_item_links: dict | None = None

        # Track which docs were newly created (for attachment logic)
        created_doc_keys: set[str] = set()

        # 3. Find or create docs in order
        if "Purchase Order" in needed:
            po_match = find_matching_purchase_order(
                supplier=supplier,
                extracted_data=extracted_data,
                settings=settings,
            )
            if po_match.found:
                result["purchase_order"] = po_match.doc_name
                result["purchase_order_matched"] = True
                result["purchase_order_match_method"] = po_match.match_method
                result["purchase_order_match_confidence"] = po_match.match_confidence
                po_item_links = po_match.item_links
                logger.info(
                    f"Job {job_name}: Matched existing PO {po_match.doc_name} "
                    f"via {po_match.match_method} "
                    f"(confidence: {po_match.match_confidence:.2f})"
                )
            else:
                po_name = create_purchase_order(
                    extracted_data=extracted_data,
                    supplier=supplier,
                    settings=settings,
                    job_name=job_name,
                    item_mapping=item_mapping,
                    stock_uom_mapping=stock_uom_mapping,
                )
                result["purchase_order"] = po_name
                result["purchase_order_matched"] = False
                created_doc_keys.add("purchase_order")
                # Build item links from the newly created PO
                po_item_links = build_item_links(
                    matched_doc=po_name,
                    matched_doctype="Purchase Order",
                    extracted_items=extracted_data.get("items", []),
                    settings=settings,
                    supplier=supplier,
                )

        if "Purchase Receipt" in needed:
            pr_match = find_matching_purchase_receipt(
                supplier=supplier,
                extracted_data=extracted_data,
                settings=settings,
                purchase_order=result.get("purchase_order"),
            )
            if pr_match.found:
                result["purchase_receipt"] = pr_match.doc_name
                result["purchase_receipt_matched"] = True
                result["purchase_receipt_match_method"] = pr_match.match_method
                result["purchase_receipt_match_confidence"] = pr_match.match_confidence
                pr_item_links = pr_match.item_links
                logger.info(
                    f"Job {job_name}: Matched existing PR {pr_match.doc_name} "
                    f"via {pr_match.match_method} "
                    f"(confidence: {pr_match.match_confidence:.2f})"
                )
            else:
                pr_name = create_purchase_receipt(
                    extracted_data=extracted_data,
                    supplier=supplier,
                    settings=settings,
                    job_name=job_name,
                    purchase_order=result.get("purchase_order"),
                    po_item_links=po_item_links,
                    item_mapping=item_mapping,
                    stock_uom_mapping=stock_uom_mapping,
                )
                result["purchase_receipt"] = pr_name
                result["purchase_receipt_matched"] = False
                created_doc_keys.add("purchase_receipt")
                # Build item links from the newly created PR
                pr_item_links = build_item_links(
                    matched_doc=pr_name,
                    matched_doctype="Purchase Receipt",
                    extracted_items=extracted_data.get("items", []),
                    settings=settings,
                    supplier=supplier,
                )

        if "Purchase Invoice" in needed:
            pi_match = find_matching_purchase_invoice(
                supplier=supplier,
                extracted_data=extracted_data,
                settings=settings,
                purchase_order=result.get("purchase_order"),
                purchase_receipt=result.get("purchase_receipt"),
            )
            if pi_match.found:
                result["purchase_invoice"] = pi_match.doc_name
                result["purchase_invoice_matched"] = True
                result["purchase_invoice_match_method"] = pi_match.match_method
                result["purchase_invoice_match_confidence"] = pi_match.match_confidence
                logger.info(
                    f"Job {job_name}: Matched existing PI {pi_match.doc_name} "
                    f"via {pi_match.match_method} "
                    f"(confidence: {pi_match.match_confidence:.2f})"
                )
            else:
                pi_name = create_purchase_invoice(
                    extracted_data=extracted_data,
                    supplier=supplier,
                    settings=settings,
                    job_name=job_name,
                    purchase_order=result.get("purchase_order"),
                    purchase_receipt=result.get("purchase_receipt"),
                    po_item_links=po_item_links,
                    pr_item_links=pr_item_links,
                    item_mapping=item_mapping,
                    stock_uom_mapping=stock_uom_mapping,
                )
                result["purchase_invoice"] = pi_name
                result["purchase_invoice_matched"] = False
                created_doc_keys.add("purchase_invoice")

        # 4. Attach source document only to newly created docs
        if source_file_url and created_doc_keys:
            # Filter created_docs to only include newly created documents
            created_docs_for_attach = {
                "supplier": supplier,
            }
            for key in created_doc_keys:
                created_docs_for_attach[key] = result[key]

            attachments = attach_source_to_chain(
                source_file_url=source_file_url,
                source_type=source_type,
                created_docs=created_docs_for_attach,
                job_name=job_name,
            )
            result["attachments"] = attachments

        # Audit note on newly created docs when the chain was currency-converted
        currency_note = extracted_data.get("_currency_note")
        if currency_note and created_doc_keys:
            import frappe

            _DOCTYPE_BY_KEY = {
                "purchase_order": "Purchase Order",
                "purchase_receipt": "Purchase Receipt",
                "purchase_invoice": "Purchase Invoice",
            }
            for key in created_doc_keys:
                doctype = _DOCTYPE_BY_KEY.get(key)
                if not doctype:
                    continue
                try:
                    frappe.get_doc(doctype, result[key]).add_comment("Comment", currency_note)
                except Exception:  # noqa: BLE001 — audit note is best-effort
                    logger.warning(f"Could not add currency note to {result[key]}")

        matched_docs = [
            k for k in ("purchase_order", "purchase_receipt", "purchase_invoice")
            if result.get(f"{k}_matched")
        ]
        created_docs = list(created_doc_keys)
        logger.info(
            f"Job {job_name}: Chain complete. "
            f"Matched: {matched_docs}, Created: {created_docs}"
        )
        return result


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
    """Keep only phone-valid characters.

    Frappe rejects '/' in phone fields, so replace with space.
    """
    if not isinstance(value, str):
        return ""
    value = value.replace("/", " ")
    cleaned = re.sub(r"[^\d\s\-+()\.]", "", value).strip()
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

    # Line items. Stamp each with its original index so user-provided
    # mappings (item_mapping / stock_uom_mapping, keyed by review-UI row
    # index) stay aligned even after shipping/discount/surcharge rows are
    # removed below.
    raw_items = data.get("items", [])
    if isinstance(raw_items, list):
        clean["items"] = [
            {**_sanitize_line_item(item), "_orig_idx": i}
            for i, item in enumerate(raw_items)
        ]
    else:
        clean["items"] = []

    # Remove shipping line items when shipping_cost is already a separate field,
    # to avoid double-counting (once as item row, once as tax charge).
    if clean["shipping_cost"] and clean["shipping_cost"] > 0:
        clean["items"] = [
            item for item in clean["items"] if not _is_shipping_item(item)
        ]

    # Extract discount line items (Rabatt/Skonto/Vorkasse) — these reduce
    # the document total, not individual item values or warehouse stock value.
    # Applied as document-level discount_amount in the chain builders.
    discount_items = [item for item in clean["items"] if _is_discount_item(item)]
    if discount_items:
        discount_total = sum(abs(item.get("total_price", 0)) for item in discount_items)
        clean["discount_amount"] = discount_total
        clean["items"] = [
            item for item in clean["items"] if not _is_discount_item(item)
        ]
    else:
        clean["discount_amount"] = None

    # Extract surcharge line items (Mindermengenaufschlag, Kleinmengenzuschlag)
    # — these increase item cost proportionally, applied as "Actual" tax charge.
    surcharge_items = [item for item in clean["items"] if _is_surcharge_item(item)]
    if surcharge_items:
        surcharge_total = sum(abs(item.get("total_price", 0)) for item in surcharge_items)
        clean["surcharge_amount"] = surcharge_total
        clean["items"] = [
            item for item in clean["items"] if not _is_surcharge_item(item)
        ]
    else:
        clean["surcharge_amount"] = None

    # Resolve package UOMs (VPE/Pack/Karton/...) into numeric bulk UOMs so the
    # stock booking reflects the real piece count (1 VPE à 1000 → UOM "1000").
    for item in clean["items"]:
        _apply_package_uom(item)

    return clean


# Package-type transaction UOMs (Verpackungseinheiten). These carry no piece
# count by themselves — the content is stated in the item name/description
# ("1000 Stück") or extracted by the LLM as pack_size.
_PACKAGE_UOM_ALIASES = {
    "vpe", "ve", "pack", "pack.", "paket", "pkg", "package",
    "karton", "box", "gebinde", "set", "satz",
}

# "1000 Stück", "(100 Stück)", "100 Stk.", "à 50", "50 Schrauben", "200 pcs"
_PACK_SIZE_RE = re.compile(
    r"(?:à|je)\s*(\d{1,6})\b"
    r"|(\d{1,6})\s*(?:stück|stck|stk\.?|st\.|pcs|pieces|teile|schrauben|muttern|scheiben)\b",
    re.IGNORECASE,
)


def _parse_pack_size(item: dict) -> float | None:
    """Infer the package content from item name/description text.

    Fallback for extractions without an explicit pack_size field, e.g.
    "Linsenkopfschrauben ... - 1000 Stück" → 1000.
    """
    text = f"{item.get('item_name') or ''} {item.get('description') or ''}"
    match = _PACK_SIZE_RE.search(text)
    if not match:
        return None
    value = float(match.group(1) or match.group(2))
    return value if value > 0 else None


def _apply_package_uom(item: dict) -> None:
    """Convert a package UOM (VPE/Pack/...) into a numeric bulk UOM in place.

    A line of "1 VPE à 7.10" containing 1000 pieces becomes uom "1000" with
    unchanged qty/price. The chain builders' existing numeric-UOM machinery
    (`_ensure_numeric_uom_setup`) then creates the UOM + conversion factor,
    so ERPNext books qty × pack_size pieces into stock while the line keeps
    the per-package price. Without a known pack size the UOM is left as-is
    (resolves to the piece UOM downstream, the previous behaviour).
    """
    uom = (item.get("uom") or "").strip().lower()
    if uom in _PACKAGE_UOM_ALIASES:
        # Package UOM: trust explicit pack_size, else parse it from the text.
        pack_size = item.get("pack_size") or _parse_pack_size(item)
    elif item.get("pack_size"):
        # Piece/other UOM with an explicit LLM pack_size (unit column said
        # "Stk" but the description said "1000 Stück pro VPE"). Never text-
        # parse here — "100 Schrauben" with qty 100 would multiply to 10000.
        # Guard: pack_size == quantity means the LLM echoed the piece count.
        pack_size = item["pack_size"]
        if pack_size == item.get("quantity"):
            return
    else:
        return

    if not pack_size or pack_size <= 1 or pack_size != int(pack_size):
        return

    item["pack_size"] = float(pack_size)
    item["uom"] = str(int(pack_size))


def _convert_to_company_currency(data: dict, settings: dict) -> dict:
    """Convert all monetary amounts to the company base currency.

    The system books everything in the company currency (no multi-currency
    sub-ledgers). When a document is issued in a foreign currency, the amounts
    are converted up-front using the exchange rate **at the document date**
    (ERPNext's ``Currency Exchange`` interface), so every downstream document
    (PO/PR/PI) is created in the base currency and booked against the standard
    base-currency accounts — sidestepping ERPNext's party-account-currency
    validation entirely (which otherwise rejects the *first* foreign-currency
    invoice of a supplier that has no base-currency ledger history yet).

    Runs at chain-build time (after the user approves), never in the review UI,
    so the reviewer keeps comparing the original-currency amounts against the
    source document.

    Mutates and returns ``data``. When a conversion happens, records the
    original currency/total, the applied rate, and a ready-made audit note
    under ``_original_currency`` / ``_original_total`` / ``_conversion_rate`` /
    ``_currency_note`` for a comment on the created documents.
    """
    import frappe
    from frappe.utils import flt, today

    currency = (data.get("currency") or "").strip()
    if not currency:
        return data

    company = settings.get("default_company")
    company_currency = (
        frappe.get_cached_value("Company", company, "default_currency")
        if company else None
    )
    if not company_currency or currency == company_currency:
        return data  # already base currency — nothing to convert

    posting_date = data.get("document_date") or today()

    from erpnext.setup.utils import get_exchange_rate

    rate = get_exchange_rate(currency, company_currency, posting_date, args="for_buying")
    if not rate or flt(rate) <= 0:
        frappe.throw(
            f"No exchange rate {currency}→{company_currency} found for "
            f"{posting_date}. Please add a Currency Exchange record for that date "
            f"(Accounting → Currency Exchange) and re-run the job."
        )
    rate = flt(rate)

    def _conv(value):
        v = _clean_numeric(value)
        return round(v * rate, 2) if v is not None else value

    original_total = data.get("total_amount")

    # Absolute monetary fields. Percentages (tax_rate, discount_percent) and
    # confidence stay untouched — a rate applies to the converted net amount.
    for field in (
        "subtotal", "tax_amount", "total_amount",
        "shipping_cost", "discount_amount", "surcharge_amount",
    ):
        if data.get(field) is not None:
            data[field] = _conv(data[field])

    for item in data.get("items", []):
        for field in ("unit_price", "total_price"):
            if item.get(field) is not None:
                item[field] = _conv(item[field])

    data["currency"] = company_currency
    data["_original_currency"] = currency
    data["_original_total"] = original_total
    data["_conversion_rate"] = rate
    data["_currency_note"] = (
        f"Original document: {original_total} {currency} — converted to "
        f"{company_currency} at exchange rate {rate} (document date {posting_date})."
    )

    logger.info(
        f"Converted document {currency}→{company_currency} at rate {rate} "
        f"(document date {posting_date})"
    )
    return data


_SHIPPING_KEYWORDS = {
    "versand", "versandkosten", "shipping", "freight", "fracht",
    "transport", "porto", "lieferkosten", "delivery cost", "postage",
    "spedition", "paketversand", "logistik",
    # Carrier names (item name often is just "DHL Paket Deutschland" etc.)
    "dhl", "dpd", "ups", "fedex", "hermes", "gls", "tnt",
    "paket deutschland", "paketdienst",
}


def _is_shipping_item(item: dict) -> bool:
    """Detect if a line item represents shipping/freight costs."""
    name = (item.get("item_name") or "").lower()
    return any(kw in name for kw in _SHIPPING_KEYWORDS)


_DISCOUNT_KEYWORDS = {
    "rabatt", "skonto", "vorkasserabatt", "nachlass", "discount",
    "preisnachlass", "gutschrift", "abzug",
}


def _is_discount_item(item: dict) -> bool:
    """Detect if a line item represents a discount/rebate.

    Matches by keyword AND negative total_price (common for discount rows).
    """
    name = (item.get("item_name") or "").lower()
    has_keyword = any(kw in name for kw in _DISCOUNT_KEYWORDS)
    has_negative_total = (item.get("total_price") or 0) < 0
    return has_keyword and has_negative_total


_SURCHARGE_KEYWORDS = {
    "mindermengenaufschlag", "mindermengenzuschlag",
    "kleinmengenaufschlag", "kleinmengenzuschlag",
    "small order surcharge", "minimum quantity surcharge",
    "mindestmengenaufschlag", "mindestmengenzuschlag",
    "zuschlag", "aufschlag",
}


def _is_surcharge_item(item: dict) -> bool:
    """Detect if a line item represents a small-quantity surcharge."""
    name = (item.get("item_name") or "").lower()
    return any(kw in name for kw in _SURCHARGE_KEYWORDS)


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
        "uom": _clean_text(item.get("uom", ""), max_len=20),
        "pack_size": _clean_numeric(item.get("pack_size")),
        "unit_price": _clean_numeric(item.get("unit_price")) or 0,
        "total_price": _clean_numeric(item.get("total_price")) or 0,
        "tax_rate": _clean_numeric(item.get("tax_rate")),
        "discount_percent": _clean_numeric(item.get("discount_percent")),
        "item_type": item.get("item_type") if item.get("item_type") in ("stock", "service") else None,
    }
