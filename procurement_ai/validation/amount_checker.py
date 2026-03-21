"""
Arithmetic validation for extracted financial data.

Verifies that line item totals, subtotals, tax amounts, and
grand totals are internally consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


@dataclass
class AmountCheckResult:
    """Result of arithmetic validation."""

    valid: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    calculated_subtotal: Decimal | None = None
    calculated_total: Decimal | None = None


class AmountChecker:
    """Validates arithmetic consistency of extracted financial data."""

    # Tolerance for rounding differences
    TOLERANCE = Decimal("0.05")  # 5 cent

    @classmethod
    def check(cls, data: dict) -> AmountCheckResult:
        """
        Run all arithmetic checks on extracted data.

        Checks:
        1. Line item qty * price = item total
        2. Sum of item totals = subtotal
        3. Subtotal + tax + shipping = total
        """
        result = AmountCheckResult()
        items = data.get("items", [])

        if not items:
            result.warnings.append("No line items to validate")
            return result

        # 1. Check each line item
        item_subtotal = Decimal("0")
        for i, item in enumerate(items):
            cls._check_line_item(item, i, result)
            try:
                item_subtotal += Decimal(str(item.get("total_price", 0)))
            except (InvalidOperation, TypeError):
                pass

        result.calculated_subtotal = item_subtotal

        # 2. Check subtotal vs sum of items
        stated_subtotal = cls._to_decimal(data.get("subtotal"))
        if stated_subtotal is not None:
            diff = abs(item_subtotal - stated_subtotal)
            if diff > cls.TOLERANCE:
                result.errors.append(
                    f"Subtotal mismatch: sum of items={item_subtotal}, "
                    f"stated subtotal={stated_subtotal}, diff={diff}"
                )
                result.valid = False

        # 3. Check grand total
        stated_total = cls._to_decimal(data.get("total_amount"))
        if stated_total is not None:
            calc_total = item_subtotal
            tax = cls._to_decimal(data.get("tax_amount"))
            shipping = cls._to_decimal(data.get("shipping_cost"))

            if tax is not None:
                calc_total += tax
            if shipping is not None:
                calc_total += shipping

            result.calculated_total = calc_total
            diff = abs(calc_total - stated_total)
            if diff > cls.TOLERANCE:
                result.errors.append(
                    f"Total mismatch: calculated={calc_total}, "
                    f"stated={stated_total}, diff={diff}"
                )
                result.valid = False

        return result

    @classmethod
    def _check_line_item(cls, item: dict, index: int, result: AmountCheckResult):
        """Validate a single line item's arithmetic."""
        qty = cls._to_decimal(item.get("quantity"))
        price = cls._to_decimal(item.get("unit_price"))
        total = cls._to_decimal(item.get("total_price"))

        if qty is None or price is None or total is None:
            result.warnings.append(
                f"Line item {index + 1}: missing qty, price, or total"
            )
            return

        expected = qty * price
        discount = cls._to_decimal(item.get("discount_percent"))
        if discount is not None and discount > 0:
            expected = expected * (1 - discount / 100)

        diff = abs(expected - total)
        if diff > cls.TOLERANCE:
            result.warnings.append(
                f"Line item {index + 1} ({item.get('item_name', '?')}): "
                f"qty*price={expected}, total={total}, diff={diff}"
            )

    @staticmethod
    def _to_decimal(value) -> Decimal | None:
        """Convert a value to Decimal, returning None on failure."""
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
