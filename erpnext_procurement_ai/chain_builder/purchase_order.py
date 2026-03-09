"""
Purchase Order creation from extracted data.
"""

from __future__ import annotations

import logging
import math
import re

import frappe
from frappe.utils import today

logger = logging.getLogger(__name__)


# ============================================================
# Input sanitization — LLM-sourced data is cleaned before use
# in any database query or document creation.
# ============================================================


def _sanitize_text(value: str, max_len: int = 200) -> str:
    """
    Sanitize a free-text field from LLM output.

    Strips null bytes, control characters, and excessive whitespace.
    Truncates to max_len.
    """
    if not isinstance(value, str):
        return ""
    # Strip null bytes and control chars (keep printable + common unicode)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len]


def _sanitize_code(value: str) -> str:
    """
    Sanitize an item_code / supplier_part_no from LLM output.

    Allows only alphanumeric, hyphens, dots, underscores, spaces.
    """
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[^\w\s.\-]", "", value).strip()
    return cleaned[:140]


def create_purchase_order(
    extracted_data: dict,
    supplier: str,
    settings: dict,
    job_name: str,
    item_mapping: dict | None = None,
) -> str:
    """
    Create a Purchase Order from extracted document data.

    Args:
        extracted_data: Consensus extraction data
        supplier: Supplier document name
        settings: Plugin settings dict
        job_name: AI Procurement Job name

    Returns:
        Purchase Order name
    """
    items = _build_items(extracted_data, settings, supplier, item_mapping=item_mapping)
    if not items:
        frappe.throw("Cannot create Purchase Order without line items")

    # Retrospective documents must not be dated later than the source document
    doc_date = extracted_data.get("document_date") or today()
    schedule = extracted_data.get("delivery_date") or doc_date

    po_data = {
        "doctype": "Purchase Order",
        "supplier": supplier,
        "company": settings.get("default_company"),
        "transaction_date": doc_date,
        "schedule_date": schedule,
        "ai_retrospective": 1,
        "ai_procurement_job": job_name,
        "items": items,
    }

    # Set invoice currency — ERPNext auto-populates conversion_rate
    if extracted_data.get("currency"):
        po_data["currency"] = extracted_data["currency"]

    # Store order_reference as order_confirmation_no for future matching
    order_ref = extracted_data.get("order_reference", "").strip()
    if order_ref:
        po_data["order_confirmation_no"] = order_ref

    # Add tax charges from extracted data
    taxes = _build_taxes(extracted_data, settings)
    if taxes:
        po_data["taxes"] = taxes

    po = frappe.get_doc(po_data)
    po.insert(ignore_permissions=True)
    po.add_comment(
        "Comment",
        f"Retrospectively created from {extracted_data.get('document_type', 'unknown')} "
        f"by AI Procurement (Job: {job_name})",
    )

    if settings.get("auto_submit_documents"):
        po.submit()

    logger.info(f"Created Purchase Order: {po.name}")
    return po.name


def _build_items(
    extracted_data: dict,
    settings: dict,
    supplier: str = "",
    item_mapping: dict | None = None,
) -> list[dict]:
    """Build PO items list from extracted line items."""
    items = []
    doc_date = extracted_data.get("document_date") or today()
    schedule_date = extracted_data.get("delivery_date") or doc_date

    for idx, item in enumerate(extracted_data.get("items", [])):
        mapped_code = item_mapping.get(idx) if item_mapping else None
        item_code = mapped_code if mapped_code else _resolve_item(item, settings, supplier)
        qty = float(item.get("quantity", 1))
        rate = float(item.get("unit_price", 0))
        uom = _resolve_uom(item.get("uom", "Nos"))

        # Adjust for sub-cent unit prices (e.g. 1000 resistors at 0.0002 EUR)
        qty, rate, uom = _adjust_bulk_uom(
            qty, rate, uom, item_code=item_code, currency=extracted_data.get("currency"),
        )

        items.append(
            {
                "item_code": item_code,
                "item_name": item.get("item_name", "Unknown Item"),
                "qty": qty,
                "rate": rate,
                "uom": uom,
                "schedule_date": schedule_date,
            }
        )

    return items


def _build_taxes(extracted_data: dict, settings: dict) -> list[dict]:
    """Build Purchase Taxes and Charges from extracted tax info."""
    # Collect tax rates from items
    tax_rates = set()
    for item in extracted_data.get("items", []):
        rate = item.get("tax_rate")
        if rate is not None:
            try:
                tax_rates.add(float(rate))
            except (TypeError, ValueError):
                pass

    if not tax_rates:
        return []

    company = settings.get("default_company")
    tax_account = _get_tax_account(company)
    if not tax_account:
        return []

    taxes = []
    for rate in sorted(tax_rates):
        if rate <= 0:
            continue
        taxes.append(
            {
                "charge_type": "On Net Total",
                "account_head": tax_account,
                "rate": rate,
                "description": f"VAT {rate:.1f}%",
            }
        )

    return taxes


def _get_tax_account(company: str) -> str | None:
    """Find the tax account for the company."""
    if not company:
        return None

    # Check if there's a default Purchase Taxes and Charges Template
    # and get its account
    default_template = frappe.db.get_value(
        "Purchase Taxes and Charges Template",
        {"company": company, "is_default": 1},
        "name",
    )
    if default_template:
        accounts = frappe.get_all(
            "Purchase Taxes and Charges",
            filters={"parent": default_template},
            fields=["account_head"],
            limit=1,
        )
        if accounts:
            return accounts[0]["account_head"]

    # Fall back to first tax account for the company
    accounts = frappe.get_all(
        "Account",
        filters={
            "account_type": "Tax",
            "is_group": 0,
            "company": company,
        },
        fields=["name"],
        limit=1,
        order_by="creation asc",
    )
    if accounts:
        return accounts[0]["name"]

    logger.warning(f"No tax account found for company {company!r}")
    return None


# Common German UOM aliases → ERPNext standard UOM names
_UOM_MAP = {
    "stk": "Nos",
    "stück": "Nos",
    "stk.": "Nos",
    "pcs": "Nos",
    "pc": "Nos",
    "ea": "Nos",
    "kg": "Kg",
    "g": "Gram",
    "l": "Liter",
    "m": "Meter",
    "km": "Km",
}


def _resolve_uom(uom: str) -> str:
    """Map LLM-extracted UOM to a valid ERPNext UOM."""
    if not uom:
        return "Nos"

    # Check if UOM exists in ERPNext as-is
    if frappe.db.exists("UOM", uom):
        return uom

    # Try mapping
    mapped = _UOM_MAP.get(uom.lower().strip())
    if mapped and frappe.db.exists("UOM", mapped):
        return mapped

    return "Nos"


def _is_numeric_uom(uom_name: str) -> bool:
    """Check if a UOM name is purely numeric (e.g. '10', '100', '1000').

    Only numeric UOM names represent "per-N-pieces" packaging.
    Named UOMs like 'Box', 'Tray', 'Pallet' have item-specific
    meanings and must not be auto-applied.
    """
    try:
        return float(uom_name) > 0
    except (TypeError, ValueError):
        return False


def _get_currency_precision(currency: str | None) -> int:
    """Return the number of decimal places for a currency.

    Reads ``smallest_currency_fraction_value`` from the Currency doctype.
    E.g. 0.01 → 2 (EUR, USD), 0.1 → 1, 1 → 0 (JPY).  Defaults to 2.
    """
    if not currency:
        return 2
    smallest = frappe.db.get_value(
        "Currency", currency, "smallest_currency_fraction_value", cache=True,
    )
    if not smallest or float(smallest) <= 0:
        return 2
    return max(0, int(round(-math.log10(float(smallest)))))


def _has_cent_fractions(rate: float, currency: str | None = None) -> bool:
    """Return True if the rate has more decimal places than the currency allows.

    Uses ``smallest_currency_fraction_value`` from the Currency doctype.
    E.g. for EUR (precision 2): 0.065 → True, 0.07 → False, 11.23 → False.
    For a currency with precision 1: 0.07 → True, 0.1 → False.
    """
    precision = _get_currency_precision(currency)
    return abs(round(rate, precision) - rate) > 1e-9


def _adjust_bulk_uom(
    qty: float, rate: float, uom: str, item_code: str | None = None,
    currency: str | None = None, dry_run: bool = False,
) -> tuple[float, float, str]:
    """
    Adjust qty/rate/uom when the per-piece price has sub-cent fractions.

    Precision is checked against the document currency using
    ``smallest_currency_fraction_value`` from the Currency doctype.
    ERPNext handles conversion to company currency automatically via
    ``conversion_rate`` on the document.

    ERPNext requires UOM conversions to be registered on the specific Item's
    ``uoms`` child table. So we check in order:

    1. Item-specific conversions (``UOM Conversion Detail`` child of Item)
    2. Global ``UOM Conversion Factor`` table (fallback)

    If a suitable bulk UOM is found:
      new_qty  = qty / factor
      new_rate = rate * factor
      new_uom  = bulk_uom

    If the conversion came from the global table and the item exists, the
    conversion is registered on the item so ERPNext accepts it on transactions.

    Only applies to piece-type UOMs (Nos) where bulk packaging makes sense.
    Only considers numeric UOM names (10, 25, 50, 100, 1000, ...) — named
    UOMs like 'Box' or 'Tray' are skipped as they have item-specific meanings.
    """
    if not _has_cent_fractions(rate, currency) or rate <= 0 or qty <= 0:
        return qty, rate, uom

    # Only adjust piece-type UOMs — bulk packaging doesn't apply to kg/m/etc.
    _PIECE_UOMS = {"Nos", "Stk", "Stück", "pcs"}
    if uom not in _PIECE_UOMS:
        return qty, rate, uom

    # Build list of equivalent UOM names to check conversion factors against
    # (user may have set up "Stk" -> "10" instead of "Nos" -> "10")
    uom_aliases = {uom}
    for alias, target in _UOM_MAP.items():
        if target == "Nos":
            # Add the alias if it exists as a UOM in ERPNext
            uom_aliases.add(alias.capitalize())
            uom_aliases.add(alias)
    uom_aliases.update(_PIECE_UOMS)

    # Step 1: Check item-specific UOM conversions (only numeric UOM names)
    if item_code and frappe.db.exists("Item", item_code):
        item_uoms = frappe.get_all(
            "UOM Conversion Detail",
            filters={"parent": item_code, "parenttype": "Item", "conversion_factor": [">", 1]},
            fields=["uom", "conversion_factor"],
            order_by="conversion_factor asc",
        )
        for row in item_uoms:
            if not _is_numeric_uom(row["uom"]):
                continue
            factor = float(row["conversion_factor"])
            adjusted_rate = rate * factor
            if not _has_cent_fractions(adjusted_rate, currency) and qty / factor >= 1:
                logger.info(
                    f"Bulk UOM adjustment (item {item_code}): {qty} x {rate} {uom} "
                    f"-> {qty / factor} x {adjusted_rate} {row['uom']} "
                    f"(factor {factor})"
                )
                return qty / factor, adjusted_rate, row["uom"]

    # Step 2a: Check global UOM Conversion Factor table
    bulk_uoms = frappe.get_all(
        "UOM Conversion Factor",
        filters={"to_uom": ["in", list(uom_aliases)], "value": [">", 1]},
        fields=["from_uom", "value"],
        order_by="value asc",
    )
    for row in bulk_uoms:
        if not _is_numeric_uom(row["from_uom"]):
            continue
        factor = float(row["value"])
        adjusted_rate = rate * factor
        if not _has_cent_fractions(adjusted_rate, currency) and qty / factor >= 1:
            if not dry_run and item_code:
                _ensure_item_uom(item_code, row["from_uom"], factor)
            logger.info(
                f"Bulk UOM adjustment (global): {qty} x {rate} {uom} "
                f"-> {qty / factor} x {adjusted_rate} {row['from_uom']} "
                f"(factor {factor})"
            )
            return qty / factor, adjusted_rate, row["from_uom"]

    # Step 2b: Scan for numeric UOMs without conversion factors yet
    # (e.g. UOM "100" exists but no UOM Conversion Factor "100" -> "Nos")
    known = {row["from_uom"] for row in bulk_uoms}
    numeric_uoms = frappe.get_all("UOM", fields=["name"])
    candidates = []
    for row in numeric_uoms:
        name = row["name"]
        if name in known or name == uom or not _is_numeric_uom(name):
            continue
        factor = float(name)
        if factor > 1:
            candidates.append((name, factor))
    candidates.sort(key=lambda x: x[1])

    for uom_name, factor in candidates:
        adjusted_rate = rate * factor
        if not _has_cent_fractions(adjusted_rate, currency) and qty / factor >= 1:
            if not dry_run:
                _ensure_uom_conversion_factor(uom_name, uom, factor)
                if item_code:
                    _ensure_item_uom(item_code, uom_name, factor)
            logger.info(
                f"Bulk UOM adjustment (new numeric UOM): {qty} x {rate} {uom} "
                f"-> {qty / factor} x {adjusted_rate} {uom_name} "
                f"(factor {factor}, conversion factor created)"
            )
            return qty / factor, adjusted_rate, uom_name

    return qty, rate, uom


def _ensure_uom_conversion_factor(from_uom: str, to_uom: str, value: float):
    """Create a global UOM Conversion Factor record if it doesn't exist."""
    existing = frappe.get_all(
        "UOM Conversion Factor",
        filters={"from_uom": from_uom, "to_uom": to_uom},
        limit=1,
    )
    if existing:
        return

    doc = frappe.get_doc({
        "doctype": "UOM Conversion Factor",
        "from_uom": from_uom,
        "to_uom": to_uom,
        "value": value,
    })
    doc.insert(ignore_permissions=True)
    logger.info(f"Created UOM Conversion Factor: 1 {from_uom} = {value} {to_uom}")


def _ensure_item_uom(item_code: str, uom: str, conversion_factor: float):
    """Register a UOM conversion on an Item if not already present."""
    if not frappe.db.exists("Item", item_code):
        return

    existing = frappe.get_all(
        "UOM Conversion Detail",
        filters={"parent": item_code, "parenttype": "Item", "uom": uom},
        fields=["name"],
        limit=1,
    )
    if existing:
        return

    item_doc = frappe.get_doc("Item", item_code)
    item_doc.append("uoms", {"uom": uom, "conversion_factor": conversion_factor})
    item_doc.save(ignore_permissions=True)
    logger.info(
        f"Registered UOM '{uom}' (factor {conversion_factor}) on Item {item_code}"
    )


def _get_default_item_group() -> str:
    """Get the default or first available Item Group."""
    # Try Stock Settings default
    default = frappe.db.get_single_value("Stock Settings", "item_group")
    if default:
        return default

    # Fall back to first non-group Item Group
    groups = frappe.get_all(
        "Item Group",
        filters={"is_group": 0},
        fields=["name"],
        order_by="lft asc",
        limit=1,
    )
    if groups:
        return groups[0]["name"]

    # Last resort: first Item Group of any kind
    groups = frappe.get_all(
        "Item Group",
        fields=["name"],
        order_by="lft asc",
        limit=1,
    )
    if groups:
        return groups[0]["name"]

    frappe.throw(
        "No Item Group found. Please create at least one Item Group "
        "or set a default in Stock Settings."
    )


def _try_resolve_item(item: dict, settings: dict, supplier: str = "") -> str | None:
    """
    Try to find an existing ERPNext Item matching the extracted item.

    Steps 1-3 of _resolve_item without creating a new Item.
    Returns None if no match found. Safe for use during document matching
    (no side effects).
    """
    extracted_code = _sanitize_code(item.get("item_code", ""))
    extracted_name = _sanitize_text(item.get("item_name", "Unknown Item"), max_len=140)
    extracted_desc = _sanitize_text(item.get("description", ""), max_len=500)

    # Step 1: delivered_by_supplier + supplier + supplier_part_no match
    if supplier and extracted_code:
        match = _match_by_supplier_part_no(supplier, extracted_code)
        if match:
            return match

    # Step 2: delivered_by_supplier + item_code match + text overlap
    if extracted_code:
        match = _match_by_code_and_text(
            extracted_code, extracted_name, extracted_desc
        )
        if match:
            return match

    # Step 3: Text match on item_name or description
    match = _match_by_text(extracted_name, extracted_desc, supplier, extracted_code)
    if match:
        return match

    return None


def _resolve_item(item: dict, settings: dict, supplier: str = "") -> str:
    """
    Find or create an ERPNext Item matching the extracted item.

    All inputs are sanitized before use in queries since the data
    originates from LLM extraction of potentially adversarial documents.

    Matching hierarchy:
    1. Item with delivered_by_supplier + Item Supplier row matching
       supplier AND supplier_part_no
    2. Item with delivered_by_supplier + item_code match + text overlap
       in item_name or description
    3. Any Item where item_name or description has text overlap
    4. Create new Item
    """
    match = _try_resolve_item(item, settings, supplier)
    if match:
        logger.info(f"Item resolved to existing: {match}")
        return match

    # Step 4: Create new item
    return _create_item(item, supplier, settings)


def _match_by_supplier_part_no(supplier: str, supplier_part_no: str) -> str | None:
    """
    Step 1: Find Item with delivered_by_supplier=1 that has an Item Supplier
    row matching the supplier and supplier_part_no.
    """
    matches = frappe.get_all(
        "Item Supplier",
        filters={
            "supplier": supplier,
            "supplier_part_no": supplier_part_no,
        },
        fields=["parent"],
        limit=5,
    )

    for m in matches:
        is_drop_ship = frappe.db.get_value(
            "Item", m["parent"], "delivered_by_supplier"
        )
        if is_drop_ship:
            return m["parent"]

    # Also accept non-drop-ship if supplier + part_no match exactly
    if matches:
        return matches[0]["parent"]

    return None


def _match_by_code_and_text(
    item_code: str, item_name: str, description: str
) -> str | None:
    """
    Step 2: Find Item with delivered_by_supplier=1 where item_code matches
    AND at least one keyword from item_name/description overlaps.
    """
    # Search by item_code (ERPNext Item.name = item_code)
    candidates = frappe.get_all(
        "Item",
        filters={
            "name": item_code,
            "delivered_by_supplier": 1,
        },
        fields=["name", "item_name", "description"],
        limit=5,
    )

    if not candidates:
        return None

    keywords = _extract_keywords(item_name, description)
    if not keywords:
        # No keywords to match, but code matched exactly
        return candidates[0]["name"]

    for c in candidates:
        candidate_text = f"{c.get('item_name', '')} {c.get('description', '')}".lower()
        if any(kw in candidate_text for kw in keywords):
            return c["name"]

    return None


def _match_by_text(
    item_name: str,
    description: str,
    supplier: str = "",
    extracted_code: str = "",
) -> str | None:
    """
    Step 3: Find any Item where item_name or description has text overlap
    with the extracted item_name or description.

    If the extracted item has a supplier_part_no, reject candidates that
    already have a *different* supplier_part_no for the same supplier
    (they are different products with similar names).
    """
    keywords = _extract_keywords(item_name, description)
    if not keywords:
        return None

    # Try each keyword as a LIKE search on item_name, pick best match
    for kw in keywords:
        # Search item_name first (more specific)
        matches = frappe.get_all(
            "Item",
            filters={"item_name": ["like", f"%{kw}%"]},
            fields=["name", "item_name", "description"],
            limit=10,
        )

        if not matches:
            continue

        # Score candidates by how many keywords they contain
        best_match = None
        best_score = 0
        for m in matches:
            # Reject if candidate has a different supplier_part_no for same supplier
            if supplier and extracted_code:
                if _has_conflicting_supplier_part(m["name"], supplier, extracted_code):
                    continue

            candidate_text = (
                f"{m.get('item_name', '')} {m.get('description', '')}"
            ).lower()
            score = sum(1 for k in keywords if k in candidate_text)
            if score > best_score:
                best_score = score
                best_match = m["name"]

        if best_match and best_score >= 2:
            return best_match

    return None


def _has_conflicting_supplier_part(
    item_name: str, supplier: str, extracted_code: str
) -> bool:
    """
    Check if an Item already has a supplier_part_no for this supplier
    that differs from extracted_code. If so, it's a different product.
    """
    existing = frappe.get_all(
        "Item Supplier",
        filters={"parent": item_name, "supplier": supplier},
        fields=["supplier_part_no"],
        limit=1,
    )
    if not existing:
        return False
    return existing[0].get("supplier_part_no", "") != extracted_code


def _extract_keywords(item_name: str, description: str) -> list[str]:
    """
    Extract meaningful keywords from item name and description.

    Filters out short words (<3 chars) and common German/English stopwords.
    Returns lowercase keywords sorted by length (longest first, more specific).
    """
    import re

    stopwords = {
        "für", "und", "mit", "von", "des", "den", "dem", "der", "die", "das",
        "ein", "eine", "the", "and", "for", "with", "from",
    }

    text = f"{item_name or ''} {description or ''}"
    words = re.findall(r"[a-zA-Z0-9äöüÄÖÜß]+", text.lower())
    keywords = [
        w for w in words if len(w) >= 3 and w not in stopwords
    ]

    # Deduplicate, sort longest first (more specific = better match)
    seen = set()
    unique = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)

    return sorted(unique, key=len, reverse=True)


def _create_item(item: dict, supplier: str, settings: dict) -> str:
    """Create a new ERPNext Item and optionally link it to the supplier."""
    item_name = _sanitize_text(item.get("item_name", "Unknown Item"), max_len=140)
    item_desc = _sanitize_text(item.get("description", item_name), max_len=500)
    item_code = _sanitize_code(item.get("item_code", ""))
    uom = _resolve_uom(item.get("uom", "Nos"))

    # item_code is mandatory in ERPNext. Generate a unique SKU to avoid
    # collisions — neither the supplier's part number nor item_name are
    # safe as item_code. The supplier's code is stored as supplier_part_no.
    generated_sku = f"SKU-{frappe.generate_hash(length=8).upper()}"

    new_item = frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": generated_sku,
            "item_name": item_name,
            "item_group": _get_default_item_group(),
            "stock_uom": uom,
            "is_stock_item": 0,
            "delivered_by_supplier": 1 if supplier else 0,
            "description": item_desc,
        }
    )

    # Add supplier link with supplier_part_no if available
    if supplier:
        supplier_row = {"supplier": supplier}
        if item_code:
            supplier_row["supplier_part_no"] = item_code
        new_item.append("supplier_items", supplier_row)

    new_item.insert(ignore_permissions=True)
    new_item.add_comment(
        "Comment",
        "Automatically created by AI Procurement Plugin",
    )

    logger.info(f"Created new Item: {new_item.name} (supplier: {supplier})")
    return new_item.name
