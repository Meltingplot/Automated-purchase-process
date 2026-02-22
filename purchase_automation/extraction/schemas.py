"""Pydantic models for structured LLM extraction output.

These schemas define the expected output format for document extraction.
They serve as both the JSON schema sent to the LLM and the validation
layer for the response. All fields have strict type constraints to catch
invalid or injected data.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class DocumentType(str, Enum):
    """Recognized purchase document types."""

    SHOPPING_CART = "shopping_cart"
    ORDER_CONFIRMATION = "order_confirmation"
    DELIVERY_NOTE = "delivery_note"
    PURCHASE_INVOICE = "purchase_invoice"


class ExtractedLineItem(BaseModel):
    """A single line item extracted from the document."""

    position: Optional[int] = Field(None, description="Position number if visible")
    item_description: str = Field(
        ..., min_length=1, max_length=500, description="Item description"
    )
    item_code_supplier: Optional[str] = Field(
        None, max_length=100, description="Supplier's item/part number"
    )
    quantity: float = Field(..., gt=0, le=1_000_000, description="Quantity ordered")
    unit: Optional[str] = Field(
        None, max_length=20, description="Unit of measure (pcs, kg, m, ...)"
    )
    unit_price: Optional[float] = Field(
        None, ge=0, le=10_000_000, description="Unit price (net)"
    )
    total_price: Optional[float] = Field(
        None, ge=0, le=100_000_000, description="Line total (net)"
    )
    tax_rate: Optional[float] = Field(
        None, ge=0, le=100, description="Tax rate in percent"
    )

    @field_validator("item_description")
    @classmethod
    def sanitize_description(cls, v: str) -> str:
        """Remove control characters from description."""
        return "".join(ch for ch in v if ch.isprintable() or ch in ("\n", "\t"))


class ExtractedDocument(BaseModel):
    """Complete structured extraction result from a purchase document.

    This is the schema that the LLM must produce. It is intentionally
    flat and simple to minimize extraction errors and maximize
    comparability between two model outputs.
    """

    document_type: DocumentType

    # Supplier information
    supplier_name: str = Field(
        ..., min_length=1, max_length=200, description="Supplier / vendor name"
    )
    supplier_address: Optional[str] = Field(
        None, max_length=500, description="Supplier address"
    )
    supplier_tax_id: Optional[str] = Field(
        None,
        max_length=30,
        pattern=r"^[A-Z]{2}[0-9A-Za-z\s]{2,28}$|^$",
        description="VAT ID (e.g. DE123456789)",
    )

    # Document metadata
    document_number: Optional[str] = Field(
        None, max_length=100, description="Document reference number"
    )
    document_date: Optional[date] = Field(None, description="Document date")
    delivery_date: Optional[date] = Field(
        None, description="Expected/actual delivery date"
    )
    due_date: Optional[date] = Field(
        None, description="Payment due date (invoices only)"
    )

    # Line items
    line_items: list[ExtractedLineItem] = Field(
        ..., min_length=1, max_length=200, description="Extracted line items"
    )

    # Totals
    subtotal: Optional[float] = Field(
        None, ge=0, le=100_000_000, description="Net subtotal"
    )
    tax_amount: Optional[float] = Field(
        None, ge=0, le=100_000_000, description="Total tax amount"
    )
    total_amount: Optional[float] = Field(
        None, ge=0, le=100_000_000, description="Gross total"
    )
    currency: str = Field(
        default="EUR", pattern=r"^[A-Z]{3}$", description="ISO 4217 currency code"
    )

    notes: Optional[str] = Field(
        None, max_length=1000, description="Relevant notes or payment terms"
    )

    @model_validator(mode="after")
    def validate_totals_plausibility(self) -> "ExtractedDocument":
        """Check that totals are plausible if all values are present."""
        if (
            self.subtotal is not None
            and self.tax_amount is not None
            and self.total_amount is not None
        ):
            expected_total = self.subtotal + self.tax_amount
            if abs(expected_total - self.total_amount) > 0.05:
                # Don't reject — flag via notes, let comparator handle it
                if self.notes:
                    self.notes += (
                        f" [PLAUSIBILITY WARNING: subtotal({self.subtotal}) + "
                        f"tax({self.tax_amount}) = {expected_total} != "
                        f"total({self.total_amount})]"
                    )
                else:
                    self.notes = (
                        f"[PLAUSIBILITY WARNING: subtotal({self.subtotal}) + "
                        f"tax({self.tax_amount}) = {expected_total} != "
                        f"total({self.total_amount})]"
                    )
        return self

    @classmethod
    def json_schema_for_llm(cls) -> dict:
        """Return the JSON schema to include in LLM prompts.

        This is a simplified version of the Pydantic schema, optimized
        for LLM comprehension.
        """
        return cls.model_json_schema()
