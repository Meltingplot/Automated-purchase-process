"""
Retrospective document chain builder.

Creates the full ERPNext procurement chain from any entry point.
If you upload an invoice, it creates the missing PO and Receipt.
If you upload a delivery note, it creates the missing PO.

All created documents are marked as retrospective and linked
to the AI Procurement Job.
"""

from __future__ import annotations

import logging

from .attachments import attach_source_to_chain
from .purchase_invoice import create_purchase_invoice
from .purchase_order import create_purchase_order
from .purchase_receipt import create_purchase_receipt
from .supplier import ensure_supplier

logger = logging.getLogger(__name__)

# Mapping: source type → which documents need to be created
NEEDED_DOCS: dict[str, list[str]] = {
    "cart": ["Purchase Order"],
    "order_confirmation": ["Purchase Order"],
    "delivery_note": ["Purchase Order", "Purchase Receipt"],
    "invoice": ["Purchase Order", "Purchase Receipt", "Purchase Invoice"],
}


class RetrospectiveChainBuilder:
    """
    Builds the complete procurement document chain.

    Entry point can be any document type. Missing documents
    in the chain are created retrospectively.
    """

    def build_chain(
        self,
        extracted_data: dict,
        source_type: str,
        source_file_url: str,
        settings: dict,
        job_name: str,
    ) -> dict:
        """
        Create the full document chain from extracted data.

        Args:
            extracted_data: Consensus data from the LLM pipeline
            source_type: Type of the uploaded document
            source_file_url: Frappe File URL of the source document
            settings: Plugin settings
            job_name: AI Procurement Job name

        Returns:
            Dict with links to all created documents + attachments
        """
        created: dict = {}

        # 1. Ensure supplier exists
        supplier = ensure_supplier(extracted_data)
        created["supplier"] = supplier

        # 2. Determine which docs are needed
        needed = NEEDED_DOCS.get(source_type, [])
        logger.info(
            f"Job {job_name}: Building chain for '{source_type}', "
            f"needed docs: {needed}"
        )

        # 3. Create docs in order
        if "Purchase Order" in needed:
            po_name = create_purchase_order(
                extracted_data=extracted_data,
                supplier=supplier,
                settings=settings,
                job_name=job_name,
            )
            created["purchase_order"] = po_name

        if "Purchase Receipt" in needed:
            pr_name = create_purchase_receipt(
                extracted_data=extracted_data,
                supplier=supplier,
                settings=settings,
                job_name=job_name,
                purchase_order=created.get("purchase_order"),
            )
            created["purchase_receipt"] = pr_name

        if "Purchase Invoice" in needed:
            pi_name = create_purchase_invoice(
                extracted_data=extracted_data,
                supplier=supplier,
                settings=settings,
                job_name=job_name,
                purchase_order=created.get("purchase_order"),
                purchase_receipt=created.get("purchase_receipt"),
            )
            created["purchase_invoice"] = pi_name

        # 4. Attach source document to all created docs
        if source_file_url:
            attachments = attach_source_to_chain(
                source_file_url=source_file_url,
                source_type=source_type,
                created_docs=created,
                job_name=job_name,
            )
            created["attachments"] = attachments

        logger.info(f"Job {job_name}: Chain complete. Created: {list(created.keys())}")
        return created
