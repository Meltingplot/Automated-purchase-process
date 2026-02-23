"""
Field-level validation rules for extracted document data.

Validates individual fields for format, plausibility, and completeness.
"""

from __future__ import annotations

import re
from datetime import date, datetime


class FieldValidator:
    """Validates extracted field values against business rules."""

    # German/EU tax ID patterns
    TAX_ID_PATTERNS = [
        r"^DE\d{9}$",  # German VAT ID
        r"^ATU\d{8}$",  # Austrian VAT ID
        r"^CHE-\d{3}\.\d{3}\.\d{3}$",  # Swiss UID
        r"^[A-Z]{2}\d{2,12}$",  # Generic EU VAT
    ]

    @classmethod
    def validate(cls, data: dict) -> tuple[dict, list[str]]:
        """
        Validate all fields in extracted data.

        Returns:
            (cleaned_data, list_of_warnings)
        """
        warnings: list[str] = []

        # Validate supplier name
        if data.get("supplier_name"):
            name = data["supplier_name"].strip()
            if len(name) < 2:
                warnings.append(f"Supplier name too short: '{name}'")
            data["supplier_name"] = name

        # Validate tax ID format
        if data.get("supplier_tax_id"):
            tax_id = data["supplier_tax_id"].strip().replace(" ", "")
            if not cls._validate_tax_id(tax_id):
                warnings.append(f"Unrecognized tax ID format: '{tax_id}'")
            data["supplier_tax_id"] = tax_id

        # Validate email
        if data.get("supplier_email"):
            email = data["supplier_email"].strip()
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                warnings.append(f"Invalid email format: '{email}'")

        # Validate dates
        for date_field in ["document_date", "delivery_date"]:
            if data.get(date_field):
                parsed = cls._parse_date(data[date_field])
                if parsed:
                    data[date_field] = parsed
                else:
                    warnings.append(
                        f"Could not parse date for {date_field}: '{data[date_field]}'"
                    )

        # Validate document type
        valid_types = {"cart", "order_confirmation", "delivery_note", "invoice"}
        if data.get("document_type") and data["document_type"] not in valid_types:
            warnings.append(
                f"Invalid document_type: '{data['document_type']}'"
            )

        # Validate currency
        valid_currencies = {"EUR", "USD", "GBP", "CHF"}
        if data.get("currency") and data["currency"] not in valid_currencies:
            warnings.append(f"Unusual currency: '{data['currency']}'")

        return data, warnings

    @classmethod
    def _validate_tax_id(cls, tax_id: str) -> bool:
        """Check if tax ID matches any known pattern."""
        return any(re.match(p, tax_id) for p in cls.TAX_ID_PATTERNS)

    @staticmethod
    def _parse_date(value) -> str | None:
        """Try to parse various date formats into ISO format."""
        if isinstance(value, (date, datetime)):
            return value.isoformat()[:10]

        if isinstance(value, str):
            for fmt in [
                "%Y-%m-%d",
                "%d.%m.%Y",
                "%d/%m/%Y",
                "%m/%d/%Y",
                "%d-%m-%Y",
                "%Y%m%d",
            ]:
                try:
                    return datetime.strptime(value.strip(), fmt).date().isoformat()
                except ValueError:
                    continue

        return None
