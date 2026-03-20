"""
Pydantic v2 schemas for structured document extraction.

All LLM providers must produce output conforming to ExtractedDocument.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    """A single line item from a purchase document."""

    position: int | None = None
    item_code: str | None = None
    item_name: str
    description: str | None = None
    quantity: Decimal
    uom: str = "Stk"
    unit_price: Decimal
    total_price: Decimal
    tax_rate: Decimal | None = None
    discount_percent: Decimal | None = None
    item_type: str | None = Field(
        default=None,
        description='Item type: "stock" for physical goods, "service" for services/fees',
    )


class ExtractedDocument(BaseModel):
    """
    Unified extraction schema that ALL LLM providers must populate.

    Covers all purchase document types: cart, order confirmation,
    delivery note, and invoice.
    """

    document_type: str = Field(
        description="cart | order_confirmation | delivery_note | invoice"
    )

    # Supplier information
    supplier_name: str
    supplier_address: str | None = None
    supplier_tax_id: str | None = None
    supplier_email: str | None = None
    supplier_phone: str | None = None

    # Document metadata
    document_number: str | None = None
    document_date: date | None = None
    order_reference: str | None = None
    delivery_date: date | None = None
    payment_terms: str | None = None
    currency: str = "EUR"

    # Line items
    items: list[LineItem]

    # Totals
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    total_amount: Decimal | None = None
    shipping_cost: Decimal | None = None

    # Metadata
    notes: str | None = None
    confidence_self_assessment: float = Field(
        ge=0.0,
        le=1.0,
        description="Self-assessed extraction quality (0.0-1.0)",
    )

    def to_json_schema_str(self) -> str:
        """Return JSON schema as a formatted string for prompts."""
        import json

        return json.dumps(self.model_json_schema(), indent=2)
