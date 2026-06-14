"""
Purchase Order creation from extracted data.
"""

from __future__ import annotations

import logging
import math
import re

import frappe
from frappe import _
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
        frappe.throw(_("Cannot create Purchase Order without line items"))

    # Retrospective documents must not be dated later than the source document
    doc_date = extracted_data.get("document_date") or today()
    schedule = extracted_data.get("delivery_date") or doc_date
    # ERPNext requires schedule_date >= transaction_date
    if schedule < doc_date:
        schedule = doc_date

    po_data = {
        "doctype": "Purchase Order",
        "supplier": supplier,
        "company": settings.get("default_company"),
        "transaction_date": doc_date,
        "schedule_date": schedule,
        "ai_procurement_job": job_name,
        "items": items,
    }

    # Set transaction currency + conversion rate (book foreign docs in base currency)
    _apply_document_currency(po_data, extracted_data, settings, doc_date)

    # Store order_reference as order_confirmation_no for future matching
    order_ref = extracted_data.get("order_reference", "").strip()
    if order_ref:
        po_data["order_confirmation_no"] = order_ref

    # VAT: select the template/tax_category via the Tax Rule; ERPNext computes
    # the per-item rate from the Item Tax Templates on insert.
    _apply_tax_template(po_data, supplier, settings.get("default_company"), doc_date)

    # Apply document-level discount (Rabatt/Skonto extracted from line items)
    if extracted_data.get("discount_amount"):
        po_data["apply_discount_on"] = "Net Total"
        po_data["discount_amount"] = extracted_data["discount_amount"]

    po = frappe.get_doc(po_data)
    po.insert(ignore_permissions=True)
    # Add shipping/surcharge charges after insert (see _finalize_charges).
    _finalize_charges(po, extracted_data, settings)
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
    # ERPNext requires schedule_date >= transaction_date
    if schedule_date < doc_date:
        schedule_date = doc_date

    for idx, item in enumerate(extracted_data.get("items", [])):
        # Mappings are keyed by the review-UI row index; sanitization may have
        # removed rows (shipping/discount), so use the stamped original index.
        map_idx = item.get("_orig_idx", idx)
        mapped_code = item_mapping.get(map_idx) if item_mapping else None
        # A key present with None value means user explicitly cleared the mapping
        # → force creation of a new item (skip fuzzy matching).
        user_cleared = item_mapping is not None and map_idx in item_mapping and item_mapping[map_idx] is None
        stock_uom = stock_uom_mapping.get(map_idx) if stock_uom_mapping else None
        if mapped_code:
            item_code = mapped_code
            # User-mapped item — ensure supplier link exists on the item
            extracted_code = _sanitize_code(item.get("item_code", ""))
            _ensure_supplier_link(item_code, supplier, extracted_code)
        elif user_cleared:
            item_code = _create_item(item, supplier, settings, stock_uom=stock_uom)
        else:
            item_code = _resolve_item(item, settings, supplier, stock_uom=stock_uom)
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


def _apply_tax_template(
    doc_data: dict, supplier: str, company: str, posting_date: str
) -> None:
    """Set ``tax_category`` + the Tax-Rule-resolved Purchase Taxes and Charges
    Template on the document data.

    VAT is **not** built by us anymore.  Each Item carries a (generic) Item Tax
    Template at the Item-master level; ERPNext resolves it per line during
    validation (``item_tax_rate``) and ``set_taxes_and_charges`` adds one tax
    row per VAT account (``set_by_item_tax_template``).  ``calculate_taxes_and_
    totals`` then computes the real per-item tax — the summary tax row therefore
    legitimately shows ``rate = 0`` while ``tax_amount`` is correct.

    All we do here is drive the selection: set ``tax_category`` (from the
    supplier) so the Tax Rule + any tax-category-specific Item Tax Templates
    resolve, and record the template the Tax Rule picks on ``taxes_and_charges``.
    """
    tax_category = frappe.db.get_value("Supplier", supplier, "tax_category")
    if tax_category:
        doc_data["tax_category"] = tax_category

    try:
        from erpnext.accounts.party import set_taxes

        supplier_group = frappe.db.get_value("Supplier", supplier, "supplier_group")
        template = set_taxes(
            supplier,
            "Supplier",
            posting_date,
            company,
            supplier_group=supplier_group,
            tax_category=tax_category,
        )
    except Exception:
        # No Tax Rule / erpnext unavailable — VAT still comes from the per-item
        # Item Tax Templates, so this is non-fatal.
        logger.warning(
            "Could not resolve tax template via Tax Rule for supplier %r",
            supplier,
            exc_info=True,
        )
        template = None

    if template:
        doc_data["taxes_and_charges"] = template


def _build_charge_rows(extracted_data: dict, settings: dict) -> list[dict]:
    """Build the document-level *charge* rows we add ourselves: shipping
    (Versandkosten) and surcharge (Mindermengenaufschlag).

    These are **Bezugsnebenkosten** — ``Actual`` rows categorised as
    ``Valuation and Total`` so they feed stock valuation (landed cost), not just
    the document total.  VAT rows are added by ERPNext from the Item Tax
    Templates, so they are intentionally absent here.
    """
    company = settings.get("default_company")
    charges: list[dict] = []

    for amount_key, description in (
        ("shipping_cost", "Shipping / Versandkosten"),
        ("surcharge_amount", "Mindermengenaufschlag"),
    ):
        try:
            amount = float(extracted_data.get(amount_key) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            continue
        account = _get_shipping_account(company)
        if not account:
            continue
        charges.append(
            {
                "charge_type": "Actual",
                "account_head": account,
                "tax_amount": amount,
                "description": description,
                "add_deduct_tax": "Add",
                "category": "Valuation and Total",
            }
        )

    return charges


def _finalize_charges(doc, extracted_data: dict, settings: dict) -> None:
    """Append our shipping/surcharge charge rows to an *already inserted* doc.

    This must run **after** ``insert()``: ERPNext only auto-populates the
    per-item VAT rows when the taxes table is still empty (``set_taxes_and_
    charges`` early-returns otherwise).  So we let it add the VAT rows on insert,
    then append the Bezugsnebenkosten and re-save to recompute the totals.
    """
    charges = _build_charge_rows(extracted_data, settings)
    if not charges:
        return
    for row in charges:
        doc.append("taxes", row)
    doc.save(ignore_permissions=True)


def _get_shipping_account(company: str) -> str | None:
    """Find an expense account suitable for shipping/freight charges."""
    if not company:
        return None

    # Try to find a shipping/freight specific account
    for keyword in ("bezugsnebenkosten", "bezugsnk", "delivery costs", "shipping", "freight", "versand", "fracht", "transport"):
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


def _apply_document_currency(
    doc_data: dict, extracted_data: dict, settings: dict, posting_date: str,
) -> None:
    """Set the transaction currency and, for foreign-currency documents, the
    conversion rate to the company's base currency.

    The document keeps its own currency (e.g. a USD-only invoice stays USD),
    but ERPNext books the General Ledger in the company base currency (e.g.
    EUR) via ``conversion_rate``. For base-currency documents nothing extra is
    needed (conversion_rate stays 1).
    """
    currency = (extracted_data.get("currency") or "").strip()
    if not currency:
        return
    doc_data["currency"] = currency

    company = settings.get("default_company")
    company_currency = (
        frappe.get_cached_value("Company", company, "default_currency")
        if company else None
    )
    if not company_currency or currency == company_currency:
        return  # base-currency document — conversion_rate stays 1

    # Foreign currency: fetch the rate to the base currency for the posting date
    try:
        from erpnext.setup.utils import get_exchange_rate

        rate = get_exchange_rate(
            currency, company_currency, posting_date, args="for_buying",
        )
    except Exception as e:  # noqa: BLE001 — rate lookup is best-effort
        logger.warning(
            f"Could not fetch exchange rate {currency}->{company_currency}: {e}"
        )
        rate = 0

    if rate and flt(rate) > 0:
        doc_data["conversion_rate"] = flt(rate)
        logger.info(
            f"Foreign-currency document {currency}: booking in {company_currency} "
            f"at conversion_rate {flt(rate)}"
        )
    else:
        logger.warning(
            f"No exchange rate {currency}->{company_currency} for {posting_date}. "
            "Add a Currency Exchange record or set the rate manually on the document."
        )


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
        "category": _get_uom_category(),
        "from_uom": from_uom,
        "to_uom": to_uom,
        "value": value,
    })
    doc.insert(ignore_permissions=True)
    logger.info(f"Created UOM Conversion Factor: 1 {from_uom} = {value} {to_uom}")


# Cache for the resolved UOM category name
_uom_category_cache: str | None = None


def _get_uom_category() -> str:
    """Return a UOM Category for bulk piece-count conversions.

    Looks up the first existing UOM Category, falling back to creating
    a 'Unit' category if none exist.
    """
    global _uom_category_cache
    if _uom_category_cache is not None:
        return _uom_category_cache

    # Prefer 'Anzahl' if it exists
    if frappe.db.exists("UOM Category", "Anzahl"):
        _uom_category_cache = "Anzahl"
        return "Anzahl"

    # Fall back to first available category
    categories = frappe.get_all("UOM Category", fields=["name"], limit=1)
    if categories:
        _uom_category_cache = categories[0]["name"]
        return _uom_category_cache

    # None exist — create 'Anzahl'
    frappe.get_doc({"doctype": "UOM Category", "category_name": "Anzahl"}).insert(
        ignore_permissions=True
    )
    _uom_category_cache = "Anzahl"
    return "Anzahl"


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
        _("No Item Group found. Please create at least one Item Group "
          "or set a default in Stock Settings.")
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
        extracted_code = _sanitize_code(item.get("item_code", ""))
        _ensure_supplier_link(match, supplier, extracted_code)
        return match

    # Step 4: Create new item
    return _create_item(item, supplier, settings, stock_uom=stock_uom)


def _ensure_supplier_link(item_code: str, supplier: str, supplier_part_no: str) -> None:
    """Ensure the Item has an Item Supplier row for the given supplier.

    If the supplier is already linked, update the ``supplier_part_no`` when
    the existing row has no part number but the extracted data provides one.
    If the supplier is not linked at all, append a new ``supplier_items`` row.

    This is intentionally a no-op when *supplier* is empty (no supplier
    context) or when *item_code* is empty (nothing to link to).
    """
    if not supplier or not item_code:
        return

    # Check existing supplier rows on the Item
    existing_rows = frappe.get_all(
        "Item Supplier",
        filters={"parent": item_code, "supplier": supplier},
        fields=["name", "supplier_part_no"],
        limit=1,
    )

    if existing_rows:
        row = existing_rows[0]
        # Update supplier_part_no if the existing row is blank and we have one
        if supplier_part_no and not row.get("supplier_part_no"):
            frappe.db.set_value(
                "Item Supplier", row["name"], "supplier_part_no", supplier_part_no
            )
            logger.info(
                f"Updated supplier_part_no={supplier_part_no!r} on Item {item_code!r} "
                f"for supplier {supplier!r}"
            )
        return

    # Supplier not linked yet — append a new row
    item_doc = frappe.get_doc("Item", item_code)
    supplier_row = {"supplier": supplier}
    if supplier_part_no:
        supplier_row["supplier_part_no"] = supplier_part_no
    item_doc.append("supplier_items", supplier_row)
    item_doc.save(ignore_permissions=True)
    logger.info(
        f"Added supplier link on Item {item_code!r}: supplier={supplier!r}, "
        f"supplier_part_no={supplier_part_no!r}"
    )


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
    # Use user-specified stock UOM if provided, otherwise default to the
    # transaction UOM — except for numeric bulk UOMs ("1000"): stock is always
    # kept in pieces, the numeric UOM only scales the transaction line.
    if stock_uom:
        effective_stock_uom = _resolve_uom(stock_uom)
    elif _is_numeric_uom(uom):
        effective_stock_uom = _get_piece_uom()
    else:
        effective_stock_uom = uom

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
