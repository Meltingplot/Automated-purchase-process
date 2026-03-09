"""
Purchase Order creation from extracted data.
"""

from __future__ import annotations

import logging
import math
import re

import frappe
from frappe.utils import flt, round_based_on_smallest_currency_fraction, today

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
    stock_uom_mapping: dict | None = None,
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
    items = _build_items(extracted_data, settings, supplier, item_mapping=item_mapping, stock_uom_mapping=stock_uom_mapping)
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

    # Add tax charges (shipping + VAT) — same as PI so amounts match
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
    stock_uom_mapping: dict | None = None,
) -> list[dict]:
    """Build PO items list from extracted line items."""
    items = []
    doc_date = extracted_data.get("document_date") or today()
    schedule_date = extracted_data.get("delivery_date") or doc_date

    for idx, item in enumerate(extracted_data.get("items", [])):
        mapped_code = item_mapping.get(idx) if item_mapping else None
        stock_uom = stock_uom_mapping.get(idx) if stock_uom_mapping else None
        item_code = mapped_code if mapped_code else _resolve_item(item, settings, supplier, stock_uom=stock_uom)
        qty = float(item.get("quantity", 1) or 1)
        rate = _true_unit_price(item, qty)
        uom_raw = item.get("uom") or ""
        _ensure_numeric_uom_setup(uom_raw)
        uom = _resolve_uom(uom_raw)

        # Adjust for sub-cent unit prices (e.g. 1000 resistors at 0.0002 EUR)
        qty, rate, uom = _adjust_bulk_uom(
            qty, rate, uom, item_code=item_code, currency=extracted_data.get("currency"),
        )
        _ensure_numeric_uom_setup(uom, item_code)

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


def _build_shipping_charges(extracted_data: dict, settings: dict) -> list[dict]:
    """Build shipping-only charges for PO/PR (no VAT)."""
    shipping = extracted_data.get("shipping_cost")
    if not shipping:
        return []
    try:
        shipping_amount = float(shipping)
    except (TypeError, ValueError):
        return []
    if shipping_amount <= 0:
        return []

    company = settings.get("default_company")
    shipping_account = _get_shipping_account(company)
    if not shipping_account:
        return []

    return [
        {
            "charge_type": "Actual",
            "account_head": shipping_account,
            "tax_amount": shipping_amount,
            "description": "Shipping / Versandkosten",
            "add_deduct_tax": "Add",
        }
    ]


def _build_taxes(extracted_data: dict, settings: dict) -> list[dict]:
    """Build Purchase Taxes and Charges (shipping + VAT)."""
    company = settings.get("default_company")
    taxes = []

    # Shipping first (Actual amount), so VAT can reference it
    shipping = extracted_data.get("shipping_cost")
    has_shipping = False
    if shipping:
        try:
            shipping_amount = float(shipping)
        except (TypeError, ValueError):
            shipping_amount = 0
        if shipping_amount > 0:
            shipping_account = _get_shipping_account(company)
            if shipping_account:
                taxes.append(
                    {
                        "charge_type": "Actual",
                        "account_head": shipping_account,
                        "tax_amount": shipping_amount,
                        "description": "Shipping / Versandkosten",
                        "add_deduct_tax": "Add",
                    }
                )
                has_shipping = True

    # Collect tax rates from items
    tax_rates = set()
    for item in extracted_data.get("items", []):
        rate = item.get("tax_rate")
        if rate is not None:
            try:
                tax_rates.add(float(rate))
            except (TypeError, ValueError):
                pass

    # VAT rows — "On Previous Row Total" if shipping exists, else "On Net Total"
    if tax_rates:
        tax_account = _get_tax_account(company)
        if tax_account:
            for rate in sorted(tax_rates):
                if rate <= 0:
                    continue
                tax_row = {
                    "account_head": tax_account,
                    "rate": rate,
                    "description": f"VAT {rate:.1f}%",
                }
                if has_shipping:
                    tax_row["charge_type"] = "On Previous Row Total"
                    tax_row["row_id"] = len(taxes)  # references the shipping row
                else:
                    tax_row["charge_type"] = "On Net Total"
                taxes.append(tax_row)

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


def _get_shipping_account(company: str) -> str | None:
    """Find an expense account suitable for shipping/freight charges."""
    if not company:
        return None

    # Try to find a shipping/freight specific account
    for keyword in ("shipping", "freight", "versand", "fracht", "transport"):
        accounts = frappe.get_all(
            "Account",
            filters={
                "company": company,
                "is_group": 0,
                "name": ["like", f"%{keyword}%"],
            },
            fields=["name"],
            limit=1,
        )
        if accounts:
            return accounts[0]["name"]

    # Fall back to the company's default expense account
    default = frappe.db.get_value("Company", company, "default_expense_account")
    if default:
        return default

    logger.warning(f"No shipping account found for company {company!r}")
    return None


# Candidate names for the "piece" UOM, checked in order.
# ERPNext international uses "Nos"; German installations typically have "Stk".
_PIECE_UOM_CANDIDATES = ("Nos", "Stk", "Stück", "Stk.")

# Cache for the resolved piece UOM name (populated on first call)
_piece_uom_cache: str | None = None


def _get_piece_uom() -> str:
    """Return the piece UOM that exists in this ERPNext instance.

    Checks common piece UOM names in order of preference and returns the
    first one found.  If none exist, creates "Nos" as a safe default.
    """
    global _piece_uom_cache
    if _piece_uom_cache is not None:
        return _piece_uom_cache

    for candidate in _PIECE_UOM_CANDIDATES:
        if frappe.db.exists("UOM", candidate):
            _piece_uom_cache = candidate
            return candidate

    # No piece UOM found at all — create the ERPNext standard one
    _ensure_uom_exists("Nos")
    _piece_uom_cache = "Nos"
    return "Nos"


# Common German UOM aliases → ERPNext standard UOM names.
# Piece-unit aliases use a sentinel that gets resolved dynamically
# via _get_piece_uom() in _resolve_uom().
_PIECE_UOM_ALIASES = {"stk", "stück", "stk.", "pcs", "pc", "ea", "nos"}

_UOM_MAP = {
    "kg": "Kg",
    "g": "Gram",
    "l": "Liter",
    "m": "Meter",
    "km": "Km",
}


def _true_unit_price(item: dict, qty: float) -> float:
    """Derive the true unit price from the line total when available.

    Invoices often round the printed unit price (e.g. 0.029) but calculate
    the line total from a more precise value (5.88 / 200 = 0.0294).
    In German tax law, the line total (Positionspreis) is authoritative
    when stated, so we back-calculate from total_price / quantity.
    """
    total_price = item.get("total_price")
    unit_price = float(item.get("unit_price", 0) or 0)

    if total_price and qty > 0:
        try:
            total_price = float(total_price)
            if total_price > 0:
                return total_price / qty
        except (TypeError, ValueError):
            pass

    return unit_price


def _resolve_uom(uom: str) -> str:
    """Map LLM-extracted UOM to a valid ERPNext UOM.

    Checks piece-unit aliases first (stk/Stück/pcs/ea/Nos → system piece UOM),
    then the static alias map, then checks for an exact match in the DB.
    Falls back to the system piece UOM if nothing matches.
    """
    if not uom:
        return _get_piece_uom()

    normalised = uom.lower().strip()

    # Piece-unit aliases → system piece UOM (Nos or Stk depending on install)
    if normalised in _PIECE_UOM_ALIASES:
        return _get_piece_uom()

    # Static alias map (kg, g, l, m, km)
    mapped = _UOM_MAP.get(normalised)
    if mapped and frappe.db.exists("UOM", mapped):
        return mapped

    # Check if UOM exists in ERPNext as-is (exact or case-insensitive match)
    # Use get_value to retrieve the actual stored name (correct case)
    stored = frappe.db.get_value("UOM", uom, "name")
    if stored:
        return stored

    return _get_piece_uom()


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

    Uses a two-step approach:
    1. Check item's existing numeric UOM conversions (avoid duplicates).
    2. Try powers of 10 (10→100→1000→10000), then fall back to qty itself
       (giving qty=1). Auto-creates UOM + conversion factor as needed.

    Only applies to piece-type UOMs (Nos) where bulk packaging makes sense.
    """
    if not _has_cent_fractions(rate, currency) or rate <= 0 or qty <= 0:
        return qty, rate, uom

    # Only adjust piece-type UOMs — bulk packaging doesn't apply to kg/m/etc.
    _PIECE_UOMS = {"nos", "stk", "stück", "pcs"}
    if uom.lower() not in _PIECE_UOMS:
        return qty, rate, uom

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
                return flt(qty / factor, 6), round_based_on_smallest_currency_fraction(adjusted_rate, currency or ""), row["uom"]

    # Step 2: Compute optimal bulk factor and create UOM + conversions
    factor = _compute_bulk_factor(qty, rate, currency)
    if factor is not None:
        uom_name = str(int(factor)) if factor == int(factor) else str(factor)
        adjusted_rate = rate * factor
        if not dry_run:
            _ensure_uom_exists(uom_name)
            _ensure_uom_conversion_factor(uom_name, uom, factor)
            if item_code:
                _ensure_item_uom(item_code, uom_name, factor)
        logger.info(
            f"Bulk UOM adjustment: {qty} x {rate} {uom} "
            f"-> {qty / factor} x {adjusted_rate} {uom_name} "
            f"(factor {factor})"
        )
        return flt(qty / factor, 6), round_based_on_smallest_currency_fraction(adjusted_rate, currency or ""), uom_name

    return qty, rate, uom


def _compute_bulk_factor(qty: float, rate: float, currency: str | None) -> float | None:
    """Find the smallest power-of-10 factor that eliminates sub-cent fractions.

    Tries 10, 100, 1000, 10000 in order. If none work and the quantity
    itself produces a representable rate (giving qty=1), uses that instead.
    Returns None if no suitable factor is found.
    """
    for factor in (10, 100, 1000, 10000):
        if not _has_cent_fractions(rate * factor, currency) and qty / factor >= 1:
            return float(factor)
    if qty > 1 and not _has_cent_fractions(rate * qty, currency):
        return qty
    return None


def _ensure_uom_exists(uom_name: str):
    """Create a UOM record if it doesn't exist."""
    if frappe.db.exists("UOM", uom_name):
        return
    doc = frappe.get_doc({
        "doctype": "UOM",
        "uom_name": uom_name,
    })
    doc.insert(ignore_permissions=True)
    logger.info(f"Created UOM: {uom_name}")


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
    """Register a UOM conversion on an Item if not already present.

    Uses direct child-table insert instead of full Item save to avoid
    deadlocks from Item hooks and validations.
    """
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

    # Get current max idx for ordering
    max_idx = frappe.db.sql(
        """SELECT IFNULL(MAX(idx), 0) FROM `tabUOM Conversion Detail`
           WHERE parent = %s AND parenttype = 'Item'""",
        item_code,
    )[0][0]

    doc = frappe.get_doc({
        "doctype": "UOM Conversion Detail",
        "parent": item_code,
        "parenttype": "Item",
        "parentfield": "uoms",
        "idx": max_idx + 1,
        "uom": uom,
        "conversion_factor": conversion_factor,
    })
    doc.db_insert()
    logger.info(
        f"Registered UOM '{uom}' (factor {conversion_factor}) on Item {item_code}"
    )


def _ensure_numeric_uom_setup(uom_raw: str, item_code: str | None = None):
    """Ensure a numeric UOM exists with proper conversion factors.

    When the review UI sets uom to e.g. "10", the UOM record, global
    conversion factor (10 → piece UOM), and Item UOM Conversion Detail
    must all exist before ERPNext's insert() runs set_missing_values().
    """
    if not _is_numeric_uom(uom_raw):
        return
    piece_uom = _get_piece_uom()
    factor = float(uom_raw)
    _ensure_uom_exists(uom_raw)
    _ensure_uom_conversion_factor(uom_raw, piece_uom, factor)
    if item_code and frappe.db.exists("Item", item_code):
        _ensure_item_uom(item_code, uom_raw, factor)


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


def _resolve_item(item: dict, settings: dict, supplier: str = "", stock_uom: str | None = None) -> str:
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
    return _create_item(item, supplier, settings, stock_uom=stock_uom)


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
        candidate_text = _normalize_dimensions(
            f"{c.get('item_name', '')} {c.get('description', '')}"
        ).lower()
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

            candidate_text = _normalize_dimensions(
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


def _normalize_dimensions(text: str) -> str:
    """Normalize dimension expressions so '6mm' and '6 mm' match identically.

    Collapses optional whitespace between a number and a unit suffix
    (mm, cm, m, km, kg, g, ml, l) into the no-space form, e.g.
    "4 mm x 6 mm" → "4mm x 6mm".
    """
    import re

    return re.sub(
        r"(\d)\s+("
        r"mm²|cm²|km²|m²|mm³|cm³|m³|"  # area / volume (longest first)
        r"µm|nm|mm|cm|dm|km|m|"  # length
        r"mg|kg|g|t|"  # mass
        r"µl|ml|cl|dl|hl|l|"  # volume
        r"kPa|MPa|Pa|mbar|bar|"  # pressure
        r"kHz|MHz|GHz|Hz|"  # frequency
        r"mA|kA|A|"  # current
        r"mV|kV|V|"  # voltage
        r"kWh|MWh|Wh|"  # energy
        r"µW|mW|kW|MW|GW|W|"  # power
        r"µF|nF|pF|F|"  # capacitance
        r"mH|µH|H|"  # inductance
        r"kN|MN|Nm|N|"  # force / torque
        r"µs|ms|ns|s|"  # time
        r"dB|lm|lx|mol|cd|°C|kΩ|MΩ|Ω"  # misc
        r")\b",
        r"\1\2",
        text,
        flags=re.IGNORECASE,
    )


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

    text = _normalize_dimensions(f"{item_name or ''} {description or ''}")
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


def _create_item(item: dict, supplier: str, settings: dict, stock_uom: str | None = None) -> str:
    """Create a new ERPNext Item and optionally link it to the supplier.

    Args:
        stock_uom: Optional stock/warehouse UOM override. When set, the
                   item is created with this as ``stock_uom`` instead of
                   the resolved transaction UOM. ERPNext then handles
                   conversion from the transaction UOM automatically.
    """
    item_name = _sanitize_text(item.get("item_name", "Unknown Item"), max_len=140)
    item_desc = _sanitize_text(item.get("description", item_name), max_len=500)
    item_code = _sanitize_code(item.get("item_code", ""))
    uom = _resolve_uom(item.get("uom") or "")
    # Use user-specified stock UOM if provided, otherwise default to transaction UOM
    effective_stock_uom = _resolve_uom(stock_uom) if stock_uom else uom

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
            "stock_uom": effective_stock_uom,
            "is_stock_item": 0 if item.get("item_type") == "service" else 1,
            "delivered_by_supplier": 1 if supplier else 0,
            "description": item_desc,
        }
    )

    # Add UOM conversion if stock UOM differs from transaction UOM
    if effective_stock_uom != uom:
        if _is_numeric_uom(uom):
            conversion_factor = float(uom)
        else:
            conversion_factor = 1.0
        new_item.append("uoms", {
            "uom": uom,
            "conversion_factor": conversion_factor,
        })

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
