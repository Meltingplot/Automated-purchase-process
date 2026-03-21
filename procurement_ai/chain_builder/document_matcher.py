"""
Document matching for the retrospective chain builder.

Finds existing Purchase Orders, Purchase Receipts, and Purchase Invoices
that match extracted data, avoiding duplicate document creation when a
user already has manually-created documents.

Matching hierarchy uses progressively fuzzier strategies with confidence
scores. An ambiguity check prevents false matches when top candidates
are too close in score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import frappe

logger = logging.getLogger(__name__)


@dataclass
class DocumentMatch:
    """Result of a document matching attempt."""

    found: bool
    doc_name: str | None = None
    match_confidence: float = 0.0
    match_method: str = ""
    is_submitted: bool = False
    item_links: dict = field(default_factory=dict)
    # item_links: {extracted_item_index: {"name": row_name, "item_code": item_code}}


# ============================================================
# Purchase Order matching
# ============================================================


def find_matching_purchase_order(
    supplier: str,
    extracted_data: dict,
    settings: dict,
) -> DocumentMatch:
    """
    Find an existing Purchase Order matching the extracted data.

    Priority:
    1. order_reference matches PO name exactly (confidence 1.0)
    2. order_reference matches PO order_confirmation_no (confidence 0.95)
    3. Supplier + item overlap + date proximity (confidence 0.70-0.90)

    All queries exclude cancelled documents (docstatus != 2).
    """
    order_ref = extracted_data.get("order_reference", "").strip()

    # Priority 1: order_reference matches PO name exactly
    if order_ref:
        match = _match_po_by_name(order_ref)
        if match.found:
            return match

    # Priority 2: order_reference matches order_confirmation_no
    if order_ref:
        match = _match_po_by_confirmation_no(order_ref, supplier)
        if match.found:
            return match

    # Priority 3: Supplier + items + date proximity
    match = _match_po_by_items_and_date(supplier, extracted_data, settings)
    if match.found:
        return match

    return DocumentMatch(found=False)


def _match_po_by_name(order_ref: str) -> DocumentMatch:
    """Priority 1: Exact match on PO name."""
    po = frappe.db.get_value(
        "Purchase Order",
        {"name": order_ref, "docstatus": ["!=", 2]},
        ["name", "docstatus"],
        as_dict=True,
    )
    if not po:
        return DocumentMatch(found=False)

    logger.info(f"PO matched by name: {po.name}")
    item_links = _build_item_links_from_doc("Purchase Order", po.name)
    return DocumentMatch(
        found=True,
        doc_name=po.name,
        match_confidence=1.0,
        match_method="po_name_exact",
        is_submitted=po.docstatus == 1,
        item_links=item_links,
    )


def _match_po_by_confirmation_no(order_ref: str, supplier: str) -> DocumentMatch:
    """Priority 2: Match on order_confirmation_no field."""
    filters = {
        "order_confirmation_no": order_ref,
        "docstatus": ["!=", 2],
    }
    if supplier:
        filters["supplier"] = supplier

    po = frappe.db.get_value(
        "Purchase Order",
        filters,
        ["name", "docstatus"],
        as_dict=True,
    )
    if not po:
        return DocumentMatch(found=False)

    logger.info(f"PO matched by order_confirmation_no: {po.name}")
    item_links = _build_item_links_from_doc("Purchase Order", po.name)
    return DocumentMatch(
        found=True,
        doc_name=po.name,
        match_confidence=0.95,
        match_method="po_confirmation_no",
        is_submitted=po.docstatus == 1,
        item_links=item_links,
    )


def _match_po_by_items_and_date(
    supplier: str,
    extracted_data: dict,
    settings: dict,
) -> DocumentMatch:
    """
    Priority 3: Fuzzy match on supplier + item overlap + date proximity.

    Requires item overlap >= 0.7. Boosted by total amount match and date
    proximity. Returns no match if top-2 candidates are within 0.10
    confidence (ambiguous).
    """
    if not supplier:
        return DocumentMatch(found=False)

    doc_date_str = extracted_data.get("document_date")
    date_range_filters = {"supplier": supplier, "docstatus": ["!=", 2]}

    if doc_date_str:
        try:
            doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d")
            date_range_filters["transaction_date"] = [
                "between",
                [
                    (doc_date - timedelta(days=90)).strftime("%Y-%m-%d"),
                    (doc_date + timedelta(days=90)).strftime("%Y-%m-%d"),
                ],
            ]
        except ValueError:
            pass

    candidates = frappe.get_all(
        "Purchase Order",
        filters=date_range_filters,
        fields=["name", "docstatus", "transaction_date", "grand_total"],
        limit=20,
        order_by="transaction_date desc",
    )

    if not candidates:
        return DocumentMatch(found=False)

    # Resolve extracted items to item_codes for comparison
    from .purchase_order import _try_resolve_item

    extracted_items = extracted_data.get("items", [])
    extracted_codes = set()
    for item in extracted_items:
        code = _try_resolve_item(item, settings, supplier)
        if code:
            extracted_codes.add(code)

    if not extracted_codes:
        return DocumentMatch(found=False)

    extracted_total = extracted_data.get("total_amount")

    scored: list[tuple[float, dict]] = []
    for cand in candidates:
        # Get items for this PO
        po_items = frappe.get_all(
            "Purchase Order Item",
            filters={"parent": cand.name},
            fields=["item_code"],
        )
        po_codes = {row["item_code"] for row in po_items}

        if not po_codes:
            continue

        # Item overlap (Jaccard)
        intersection = extracted_codes & po_codes
        union = extracted_codes | po_codes
        overlap = len(intersection) / len(union) if union else 0

        if overlap < 0.7:
            continue

        # Base confidence from item overlap
        confidence = 0.70 + (overlap - 0.7) * (0.15 / 0.3)  # 0.70-0.85

        # Boost for total amount match (within 5%)
        if extracted_total and cand.grand_total:
            try:
                ratio = float(extracted_total) / float(cand.grand_total)
                if 0.95 <= ratio <= 1.05:
                    confidence += 0.03
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        # Boost for date proximity
        if doc_date_str and cand.transaction_date:
            try:
                doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d")
                cand_date = datetime.strptime(
                    str(cand.transaction_date), "%Y-%m-%d"
                )
                days_apart = abs((doc_date - cand_date).days)
                if days_apart <= 7:
                    confidence += 0.02
                elif days_apart <= 30:
                    confidence += 0.01
            except ValueError:
                pass

        confidence = min(confidence, 0.90)
        scored.append((confidence, cand))

    if not scored:
        return DocumentMatch(found=False)

    scored.sort(key=lambda x: x[0], reverse=True)

    # Ambiguity check: if top-2 are within 0.10, don't match
    if len(scored) >= 2 and (scored[0][0] - scored[1][0]) < 0.10:
        logger.info(
            f"PO match ambiguous: top candidates {scored[0][1].name} "
            f"({scored[0][0]:.2f}) and {scored[1][1].name} ({scored[1][0]:.2f})"
        )
        return DocumentMatch(found=False)

    best_confidence, best_cand = scored[0]
    logger.info(
        f"PO matched by items+date: {best_cand.name} "
        f"(confidence: {best_confidence:.2f})"
    )
    item_links = _build_item_links_from_doc("Purchase Order", best_cand.name)
    return DocumentMatch(
        found=True,
        doc_name=best_cand.name,
        match_confidence=best_confidence,
        match_method="items_supplier_date",
        is_submitted=best_cand.docstatus == 1,
        item_links=item_links,
    )


# ============================================================
# Purchase Receipt matching
# ============================================================


def find_matching_purchase_receipt(
    supplier: str,
    extracted_data: dict,
    settings: dict,
    purchase_order: str | None = None,
) -> DocumentMatch:
    """
    Find an existing Purchase Receipt matching the extracted data.

    Priority:
    1. PR linked to matched PO (via PR Item purchase_order) (confidence 0.95)
    2. Supplier + item overlap + date proximity (confidence 0.65-0.85)
    """
    # Priority 1: PR linked to matched PO
    if purchase_order:
        match = _match_pr_by_purchase_order(purchase_order)
        if match.found:
            return match

    # Priority 2: Supplier + items + date
    match = _match_pr_by_items_and_date(supplier, extracted_data, settings)
    if match.found:
        return match

    return DocumentMatch(found=False)


def _match_pr_by_purchase_order(purchase_order: str) -> DocumentMatch:
    """Priority 1: Find PR linked to a specific PO."""
    pr_items = frappe.get_all(
        "Purchase Receipt Item",
        filters={"purchase_order": purchase_order, "docstatus": ["!=", 2]},
        fields=["parent"],
        limit=1,
    )
    if not pr_items:
        return DocumentMatch(found=False)

    pr_name = pr_items[0]["parent"]
    docstatus = frappe.db.get_value("Purchase Receipt", pr_name, "docstatus")
    if docstatus == 2:
        return DocumentMatch(found=False)

    logger.info(f"PR matched via PO link: {pr_name} (PO: {purchase_order})")
    item_links = _build_item_links_from_doc("Purchase Receipt", pr_name)
    return DocumentMatch(
        found=True,
        doc_name=pr_name,
        match_confidence=0.95,
        match_method="pr_linked_to_po",
        is_submitted=docstatus == 1,
        item_links=item_links,
    )


def _match_pr_by_items_and_date(
    supplier: str,
    extracted_data: dict,
    settings: dict,
) -> DocumentMatch:
    """Priority 2: Fuzzy match on supplier + item overlap + date proximity."""
    if not supplier:
        return DocumentMatch(found=False)

    doc_date_str = extracted_data.get("document_date")
    filters = {"supplier": supplier, "docstatus": ["!=", 2]}

    if doc_date_str:
        try:
            doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d")
            filters["posting_date"] = [
                "between",
                [
                    (doc_date - timedelta(days=90)).strftime("%Y-%m-%d"),
                    (doc_date + timedelta(days=90)).strftime("%Y-%m-%d"),
                ],
            ]
        except ValueError:
            pass

    candidates = frappe.get_all(
        "Purchase Receipt",
        filters=filters,
        fields=["name", "docstatus", "posting_date"],
        limit=20,
        order_by="posting_date desc",
    )

    if not candidates:
        return DocumentMatch(found=False)

    from .purchase_order import _try_resolve_item

    extracted_items = extracted_data.get("items", [])
    extracted_codes = set()
    for item in extracted_items:
        code = _try_resolve_item(item, settings, supplier)
        if code:
            extracted_codes.add(code)

    if not extracted_codes:
        return DocumentMatch(found=False)

    scored: list[tuple[float, dict]] = []
    for cand in candidates:
        pr_items = frappe.get_all(
            "Purchase Receipt Item",
            filters={"parent": cand.name},
            fields=["item_code"],
        )
        pr_codes = {row["item_code"] for row in pr_items}

        if not pr_codes:
            continue

        intersection = extracted_codes & pr_codes
        union = extracted_codes | pr_codes
        overlap = len(intersection) / len(union) if union else 0

        if overlap < 0.7:
            continue

        confidence = 0.65 + (overlap - 0.7) * (0.15 / 0.3)

        if doc_date_str and cand.posting_date:
            try:
                doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d")
                cand_date = datetime.strptime(
                    str(cand.posting_date), "%Y-%m-%d"
                )
                days_apart = abs((doc_date - cand_date).days)
                if days_apart <= 7:
                    confidence += 0.02
                elif days_apart <= 30:
                    confidence += 0.01
            except ValueError:
                pass

        confidence = min(confidence, 0.85)
        scored.append((confidence, cand))

    if not scored:
        return DocumentMatch(found=False)

    scored.sort(key=lambda x: x[0], reverse=True)

    if len(scored) >= 2 and (scored[0][0] - scored[1][0]) < 0.10:
        logger.info(
            f"PR match ambiguous: top candidates {scored[0][1].name} "
            f"({scored[0][0]:.2f}) and {scored[1][1].name} ({scored[1][0]:.2f})"
        )
        return DocumentMatch(found=False)

    best_confidence, best_cand = scored[0]
    logger.info(
        f"PR matched by items+date: {best_cand.name} "
        f"(confidence: {best_confidence:.2f})"
    )
    item_links = _build_item_links_from_doc("Purchase Receipt", best_cand.name)
    return DocumentMatch(
        found=True,
        doc_name=best_cand.name,
        match_confidence=best_confidence,
        match_method="pr_items_supplier_date",
        is_submitted=best_cand.docstatus == 1,
        item_links=item_links,
    )


# ============================================================
# Purchase Invoice matching
# ============================================================


def find_matching_purchase_invoice(
    supplier: str,
    extracted_data: dict,
    settings: dict,
    purchase_order: str | None = None,
    purchase_receipt: str | None = None,
) -> DocumentMatch:
    """
    Find an existing Purchase Invoice matching the extracted data.

    Priority:
    1. document_number matches PI bill_no + same supplier (confidence 1.0)
    2. PI linked to matched PO (via PI Item purchase_order) (confidence 0.90)
    3. Supplier + total amount + date proximity (confidence 0.60-0.80)
    """
    doc_number = extracted_data.get("document_number", "").strip()

    # Priority 1: bill_no match
    if doc_number:
        match = _match_pi_by_bill_no(doc_number, supplier)
        if match.found:
            return match

    # Priority 2: PI linked to matched PO
    if purchase_order:
        match = _match_pi_by_purchase_order(purchase_order)
        if match.found:
            return match

    # Priority 3: Supplier + total + date
    match = _match_pi_by_amount_and_date(supplier, extracted_data)
    if match.found:
        return match

    return DocumentMatch(found=False)


def _match_pi_by_bill_no(doc_number: str, supplier: str) -> DocumentMatch:
    """Priority 1: Match PI by bill_no + supplier."""
    filters = {
        "bill_no": doc_number,
        "docstatus": ["!=", 2],
    }
    if supplier:
        filters["supplier"] = supplier

    pi = frappe.db.get_value(
        "Purchase Invoice",
        filters,
        ["name", "docstatus"],
        as_dict=True,
    )
    if not pi:
        return DocumentMatch(found=False)

    logger.info(f"PI matched by bill_no: {pi.name} (bill_no: {doc_number})")
    item_links = _build_item_links_from_doc("Purchase Invoice", pi.name)
    return DocumentMatch(
        found=True,
        doc_name=pi.name,
        match_confidence=1.0,
        match_method="bill_no",
        is_submitted=pi.docstatus == 1,
        item_links=item_links,
    )


def _match_pi_by_purchase_order(purchase_order: str) -> DocumentMatch:
    """Priority 2: Find PI linked to a specific PO."""
    pi_items = frappe.get_all(
        "Purchase Invoice Item",
        filters={"purchase_order": purchase_order, "docstatus": ["!=", 2]},
        fields=["parent"],
        limit=1,
    )
    if not pi_items:
        return DocumentMatch(found=False)

    pi_name = pi_items[0]["parent"]
    docstatus = frappe.db.get_value("Purchase Invoice", pi_name, "docstatus")
    if docstatus == 2:
        return DocumentMatch(found=False)

    logger.info(f"PI matched via PO link: {pi_name} (PO: {purchase_order})")
    item_links = _build_item_links_from_doc("Purchase Invoice", pi_name)
    return DocumentMatch(
        found=True,
        doc_name=pi_name,
        match_confidence=0.90,
        match_method="pi_linked_to_po",
        is_submitted=docstatus == 1,
        item_links=item_links,
    )


def _match_pi_by_amount_and_date(
    supplier: str,
    extracted_data: dict,
) -> DocumentMatch:
    """Priority 3: Match by supplier + total amount + date proximity."""
    if not supplier:
        return DocumentMatch(found=False)

    extracted_total = extracted_data.get("total_amount")
    if not extracted_total:
        return DocumentMatch(found=False)

    try:
        extracted_total = float(extracted_total)
    except (TypeError, ValueError):
        return DocumentMatch(found=False)

    doc_date_str = extracted_data.get("document_date")
    filters = {"supplier": supplier, "docstatus": ["!=", 2]}

    if doc_date_str:
        try:
            doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d")
            filters["posting_date"] = [
                "between",
                [
                    (doc_date - timedelta(days=90)).strftime("%Y-%m-%d"),
                    (doc_date + timedelta(days=90)).strftime("%Y-%m-%d"),
                ],
            ]
        except ValueError:
            pass

    candidates = frappe.get_all(
        "Purchase Invoice",
        filters=filters,
        fields=["name", "docstatus", "posting_date", "grand_total"],
        limit=20,
        order_by="posting_date desc",
    )

    if not candidates:
        return DocumentMatch(found=False)

    scored: list[tuple[float, dict]] = []
    for cand in candidates:
        if not cand.grand_total:
            continue

        try:
            ratio = extracted_total / float(cand.grand_total)
        except (TypeError, ValueError, ZeroDivisionError):
            continue

        # Amount must be within 5%
        if not (0.95 <= ratio <= 1.05):
            continue

        confidence = 0.60

        # Boost for exact amount match
        if 0.99 <= ratio <= 1.01:
            confidence += 0.10
        else:
            confidence += 0.05

        # Boost for date proximity
        if doc_date_str and cand.posting_date:
            try:
                doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d")
                cand_date = datetime.strptime(
                    str(cand.posting_date), "%Y-%m-%d"
                )
                days_apart = abs((doc_date - cand_date).days)
                if days_apart <= 7:
                    confidence += 0.05
                elif days_apart <= 30:
                    confidence += 0.02
            except ValueError:
                pass

        confidence = min(confidence, 0.80)
        scored.append((confidence, cand))

    if not scored:
        return DocumentMatch(found=False)

    scored.sort(key=lambda x: x[0], reverse=True)

    if len(scored) >= 2 and (scored[0][0] - scored[1][0]) < 0.10:
        logger.info(
            f"PI match ambiguous: top candidates {scored[0][1].name} "
            f"({scored[0][0]:.2f}) and {scored[1][1].name} ({scored[1][0]:.2f})"
        )
        return DocumentMatch(found=False)

    best_confidence, best_cand = scored[0]
    logger.info(
        f"PI matched by amount+date: {best_cand.name} "
        f"(confidence: {best_confidence:.2f})"
    )
    item_links = _build_item_links_from_doc("Purchase Invoice", best_cand.name)
    return DocumentMatch(
        found=True,
        doc_name=best_cand.name,
        match_confidence=best_confidence,
        match_method="pi_amount_date",
        is_submitted=best_cand.docstatus == 1,
        item_links=item_links,
    )


# ============================================================
# Item link building
# ============================================================

# Maps DocType to its Item child table DocType
_ITEM_DOCTYPE_MAP = {
    "Purchase Order": "Purchase Order Item",
    "Purchase Receipt": "Purchase Receipt Item",
    "Purchase Invoice": "Purchase Invoice Item",
}


def _build_item_links_from_doc(doctype: str, doc_name: str) -> dict:
    """
    Build item_links dict from an existing document's items.

    Returns: {item_code: {"name": row_name, "item_code": item_code}}
    Keyed by item_code for easy lookup during downstream doc creation.
    """
    child_doctype = _ITEM_DOCTYPE_MAP.get(doctype)
    if not child_doctype:
        return {}

    items = frappe.get_all(
        child_doctype,
        filters={"parent": doc_name},
        fields=["name", "item_code"],
    )

    links = {}
    for row in items:
        links[row["item_code"]] = {
            "name": row["name"],
            "item_code": row["item_code"],
        }

    return links


def build_item_links(
    matched_doc: str,
    matched_doctype: str,
    extracted_items: list[dict],
    settings: dict,
    supplier: str = "",
) -> dict:
    """
    Map extracted items to existing document item rows.

    Uses item_code match first, then name keyword overlap, then qty+rate
    tiebreaker. Greedy assignment (each doc item used at most once).

    Returns: {extracted_item_index: {"name": row_name, "item_code": item_code}}
    """
    child_doctype = _ITEM_DOCTYPE_MAP.get(matched_doctype)
    if not child_doctype:
        return {}

    doc_items = frappe.get_all(
        child_doctype,
        filters={"parent": matched_doc},
        fields=["name", "item_code", "item_name", "qty", "rate"],
    )

    if not doc_items:
        return {}

    from .purchase_order import _extract_keywords, _try_resolve_item

    # Resolve extracted items to item_codes
    resolved_codes = []
    for item in extracted_items:
        code = _try_resolve_item(item, settings, supplier)
        resolved_codes.append(code)

    used_doc_rows: set[str] = set()
    links: dict[int, dict] = {}

    for idx, ext_item in enumerate(extracted_items):
        best_row = None
        best_score = -1

        ext_code = resolved_codes[idx]
        ext_name = ext_item.get("item_name", "")
        ext_desc = ext_item.get("description", "")
        ext_qty = float(ext_item.get("quantity", 0))
        ext_rate = float(ext_item.get("unit_price", 0))

        for doc_row in doc_items:
            if doc_row["name"] in used_doc_rows:
                continue

            score = 0

            # item_code match (strongest signal)
            if ext_code and doc_row["item_code"] == ext_code:
                score += 10

            # Name keyword overlap
            ext_keywords = _extract_keywords(ext_name, ext_desc)
            doc_text = (doc_row.get("item_name") or "").lower()
            kw_matches = sum(1 for kw in ext_keywords if kw in doc_text)
            score += kw_matches

            # qty+rate match tiebreaker
            if ext_qty and doc_row.get("qty") and ext_qty == float(doc_row["qty"]):
                score += 0.5
            if ext_rate and doc_row.get("rate") and abs(ext_rate - float(doc_row["rate"])) < 0.01:
                score += 0.5

            if score > best_score:
                best_score = score
                best_row = doc_row

        if best_row and best_score > 0:
            used_doc_rows.add(best_row["name"])
            links[idx] = {
                "name": best_row["name"],
                "item_code": best_row["item_code"],
            }

    return links
