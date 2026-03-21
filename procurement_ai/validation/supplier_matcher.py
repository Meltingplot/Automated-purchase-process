"""
Fuzzy supplier matching against existing ERPNext suppliers.

Searches by tax ID, email, phone, and name to find existing suppliers.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

NAME_MATCH_THRESHOLD = 0.85  # 85% name similarity for a match


@dataclass
class SupplierMatch:
    """Result of a supplier matching attempt."""

    found: bool
    supplier_name: str | None = None
    match_confidence: float = 0.0
    match_method: str = ""  # "tax_id", "email", "phone", "name_exact", "name_fuzzy"


class SupplierMatcher:
    """
    Finds existing suppliers or suggests creating new ones.

    Match priority:
    1. Exact tax ID match (highest confidence)
    2. Email match via Contact (single JOIN query)
    3. Phone match via Contact (single JOIN query, normalized)
    4. Exact supplier name match
    5. Fuzzy name match — narrowed by LIKE on keywords, then SequenceMatcher
    """

    @staticmethod
    def find_match(extracted_data: dict) -> SupplierMatch:
        """
        Search for a matching supplier in ERPNext.

        All inputs are sanitized before use in queries since the data
        originates from LLM extraction of potentially adversarial documents.

        Args:
            extracted_data: Consensus extraction data containing
                supplier_name, supplier_tax_id, supplier_email, supplier_phone

        Returns:
            SupplierMatch with result
        """
        supplier_name = _sanitize_text(extracted_data.get("supplier_name", ""), max_len=140)
        tax_id = _sanitize_tax_id(extracted_data.get("supplier_tax_id", ""))
        email = _sanitize_email(extracted_data.get("supplier_email", ""))
        phone = _sanitize_phone(extracted_data.get("supplier_phone", ""))

        # 1. Exact tax ID match
        if tax_id:
            match = _match_by_tax_id(tax_id)
            if match:
                return match

        # 2. Email match via Contact
        if email:
            match = _match_by_email(email)
            if match:
                return match

        # 3. Phone match via Contact
        if phone:
            match = _match_by_phone(phone)
            if match:
                return match

        # 4. Exact name match
        if supplier_name:
            match = _match_by_name_exact(supplier_name)
            if match:
                return match

        # 5. Fuzzy name match
        if supplier_name:
            match = _match_by_name_fuzzy(supplier_name)
            if match:
                return match

        # No match found
        return SupplierMatch(found=False, match_confidence=0.0, match_method="none")


# ============================================================
# Input sanitization — all LLM-sourced data is cleaned before
# reaching any database query, even though frappe parameterizes.
# ============================================================


def _sanitize_text(value: str, max_len: int = 200) -> str:
    """
    Sanitize a free-text field from LLM output.

    Strips control characters, null bytes, and excessive whitespace.
    Truncates to max_len.
    """
    if not isinstance(value, str):
        return ""
    # Strip null bytes and control chars (keep printable + common unicode)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len]


def _sanitize_tax_id(value: str) -> str:
    """
    Sanitize and validate a tax ID / VAT number.

    Valid formats: country prefix (2 letters) + digits, e.g. DE257336234.
    Rejects anything that doesn't match.
    """
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", "", value).strip()
    # EU VAT format: 2-letter country code + alphanumeric (max 15)
    if re.match(r"^[A-Z]{2}[A-Za-z0-9]{2,15}$", cleaned):
        return cleaned
    # Also accept pure numeric (some systems store without prefix)
    if re.match(r"^\d{5,15}$", cleaned):
        return cleaned
    if cleaned:
        logger.warning(f"Rejected invalid tax_id format: {cleaned!r}")
    return ""


def _sanitize_email(value: str) -> str:
    """
    Sanitize and validate an email address.

    Basic format check — rejects anything that doesn't look like email.
    """
    if not isinstance(value, str):
        return ""
    cleaned = value.strip().lower()
    # Basic email pattern: no spaces, has @ and domain
    if re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", cleaned):
        return cleaned[:254]  # RFC 5321 max length
    if cleaned:
        logger.warning(f"Rejected invalid email format: {cleaned!r}")
    return ""


def _sanitize_phone(value: str) -> str:
    """
    Sanitize a phone number.

    Keeps only digits, spaces, hyphens, slashes, parens, and leading +.
    """
    if not isinstance(value, str):
        return ""
    # Keep only phone-valid characters
    cleaned = re.sub(r"[^\d\s\-+/()\.]", "", value).strip()
    return cleaned[:30]


def _match_by_tax_id(tax_id: str) -> SupplierMatch | None:
    """Match supplier by exact tax ID."""
    import frappe

    matches = frappe.get_all(
        "Supplier",
        filters={"tax_id": tax_id},
        fields=["name"],
        limit=1,
    )
    if matches:
        logger.debug(f"Supplier matched by tax_id={tax_id}: {matches[0]['name']}")
        return SupplierMatch(
            found=True,
            supplier_name=matches[0]["name"],
            match_confidence=1.0,
            match_method="tax_id",
        )
    return None


def _match_by_email(email: str) -> SupplierMatch | None:
    """Match supplier by email via linked Contact (single JOIN query)."""
    import frappe

    results = frappe.db.sql(
        """
        SELECT dl.link_name AS supplier
        FROM `tabDynamic Link` dl
        JOIN `tabContact Email` ce ON ce.parent = dl.parent
        WHERE dl.link_doctype = 'Supplier'
          AND dl.parenttype = 'Contact'
          AND ce.email_id = %(email)s
        LIMIT 1
        """,
        {"email": email},
        as_dict=True,
    )
    if results:
        logger.debug(f"Supplier matched by email={email}: {results[0]['supplier']}")
        return SupplierMatch(
            found=True,
            supplier_name=results[0]["supplier"],
            match_confidence=0.95,
            match_method="email",
        )
    return None


def _match_by_phone(phone: str) -> SupplierMatch | None:
    """
    Match supplier by phone via linked Contact (single JOIN query).

    Normalizes phone numbers to digits-only for comparison since
    formats vary widely (e.g. "03923 - 6100-0" vs "+49 3923 61000").
    """
    import frappe

    normalized = _normalize_phone(phone)
    if len(normalized) < 6:
        return None

    # Get all supplier phone numbers in one query
    results = frappe.db.sql(
        """
        SELECT dl.link_name AS supplier, cp.phone
        FROM `tabDynamic Link` dl
        JOIN `tabContact Phone` cp ON cp.parent = dl.parent
        WHERE dl.link_doctype = 'Supplier'
          AND dl.parenttype = 'Contact'
        """,
        as_dict=True,
    )

    for row in results:
        existing_normalized = _normalize_phone(row["phone"] or "")
        if not existing_normalized:
            continue
        # Match if one contains the other (handles country code differences)
        if normalized in existing_normalized or existing_normalized in normalized:
            logger.debug(
                f"Supplier matched by phone={phone}: {row['supplier']}"
            )
            return SupplierMatch(
                found=True,
                supplier_name=row["supplier"],
                match_confidence=0.9,
                match_method="phone",
            )

    return None


def _match_by_name_exact(supplier_name: str) -> SupplierMatch | None:
    """Match supplier by exact name (case-insensitive)."""
    import frappe

    matches = frappe.get_all(
        "Supplier",
        filters={"supplier_name": supplier_name},
        fields=["name"],
        limit=1,
    )
    if matches:
        logger.debug(
            f"Supplier matched by exact name={supplier_name!r}: {matches[0]['name']}"
        )
        return SupplierMatch(
            found=True,
            supplier_name=matches[0]["name"],
            match_confidence=1.0,
            match_method="name_exact",
        )
    return None


def _match_by_name_fuzzy(supplier_name: str) -> SupplierMatch | None:
    """
    Match supplier by fuzzy name comparison.

    First narrows candidates with SQL LIKE on significant keywords,
    then scores with SequenceMatcher. Only loads matching candidates,
    not the entire supplier list.
    """
    import frappe

    keywords = _extract_name_keywords(supplier_name)
    if not keywords:
        return None

    # Build OR conditions for LIKE search on each keyword
    candidates: dict[str, str] = {}  # name -> supplier_name
    for kw in keywords:
        matches = frappe.get_all(
            "Supplier",
            filters={"supplier_name": ["like", f"%{kw}%"]},
            fields=["name", "supplier_name"],
            limit=20,
        )
        for m in matches:
            candidates[m["name"]] = m["supplier_name"]

    if not candidates:
        return None

    # Score candidates with SequenceMatcher
    target = supplier_name.lower()
    best_name = None
    best_score = 0.0

    for name, s_name in candidates.items():
        score = SequenceMatcher(None, target, s_name.lower()).ratio()
        if score > best_score:
            best_score = score
            best_name = name

    if best_name and best_score >= NAME_MATCH_THRESHOLD:
        logger.debug(
            f"Supplier matched by fuzzy name={supplier_name!r}: "
            f"{best_name} (score={best_score:.1%})"
        )
        return SupplierMatch(
            found=True,
            supplier_name=best_name,
            match_confidence=best_score,
            match_method="name_fuzzy",
        )

    return None


def _normalize_phone(phone: str) -> str:
    """Strip phone number to digits only (drop leading country +/00 prefix)."""
    digits = re.sub(r"[^\d]", "", phone)
    # Normalize German country code: 0049... or 49... -> 0...
    if digits.startswith("0049"):
        digits = "0" + digits[4:]
    elif digits.startswith("49") and len(digits) > 10:
        digits = "0" + digits[2:]
    return digits


def _extract_name_keywords(name: str) -> list[str]:
    """
    Extract significant keywords from a supplier name for LIKE search.

    Filters out legal suffixes (GmbH, AG, etc.) and short words.
    Returns keywords sorted longest-first.
    """
    # Legal form suffixes common in DACH region — not useful for matching
    legal_forms = {
        "gmbh", "ag", "kg", "ohg", "gbr", "ug", "se", "co",
        "inc", "ltd", "llc", "corp", "plc",
        "e.v.", "ev", "mbh", "kgaa",
    }

    words = re.findall(r"[a-zA-Z0-9äöüÄÖÜß]+", name.lower())
    keywords = [
        w for w in words
        if len(w) >= 3 and w not in legal_forms
    ]

    # Deduplicate, longest first
    seen = set()
    unique = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)

    return sorted(unique, key=len, reverse=True)
