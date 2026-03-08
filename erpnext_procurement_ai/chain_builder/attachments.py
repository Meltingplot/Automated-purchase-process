"""
Frappe File attachment management for the procurement chain.

Uses Frappe's built-in attachment system (File DocType).
Creates File records with attached_to_doctype/attached_to_name,
which makes attachments appear automatically in the sidebar.

Attachment mapping by source type:
- Cart/Order Confirmation → Purchase Order (primary)
- Delivery Note → Purchase Receipt (primary) + PO (secondary)
- Invoice → Purchase Invoice (primary) + PO + Receipt (secondary)
"""

from __future__ import annotations

import logging

import frappe

logger = logging.getLogger(__name__)

# Source type → (primary target DocType, secondary target DocTypes)
# Keys match the DocType Select field options
ATTACHMENT_TARGETS: dict[str, dict] = {
    "Cart": {"primary": "Purchase Order", "secondary": []},
    "Order Confirmation": {"primary": "Purchase Order", "secondary": []},
    "Delivery Note": {
        "primary": "Purchase Receipt",
        "secondary": ["Purchase Order"],
    },
    "Invoice": {
        "primary": "Purchase Invoice",
        "secondary": ["Purchase Order", "Purchase Receipt"],
    },
}

# Map DocType names to keys used in created_docs dict
DOCTYPE_KEY_MAP: dict[str, str] = {
    "Purchase Order": "purchase_order",
    "Purchase Receipt": "purchase_receipt",
    "Purchase Invoice": "purchase_invoice",
}


def attach_source_to_chain(
    source_file_url: str,
    source_type: str,
    created_docs: dict,
    job_name: str,
) -> list[dict]:
    """
    Attach the source document to all relevant ERPNext documents.

    Creates a Frappe File record per target DocType with the same file_url.
    Frappe displays the attachment automatically in each DocType's sidebar.

    Args:
        source_file_url: Frappe file URL of the source document
        source_type: Document type (cart/order_confirmation/delivery_note/invoice)
        created_docs: Dict mapping doc keys to created document names
        job_name: AI Procurement Job name

    Returns:
        List of dicts with doctype, name, and file info
    """
    targets = ATTACHMENT_TARGETS.get(source_type, {})
    if not targets:
        logger.warning(f"No attachment targets for source_type: {source_type}")
        return []

    # Get original file metadata
    original = frappe.get_all(
        "File",
        filters={"file_url": source_file_url},
        fields=["file_name", "is_private"],
        limit=1,
    )

    if not original:
        logger.warning(f"No File record found for URL: {source_file_url}")
        return []

    file_name = original[0]["file_name"]
    results: list[dict] = []

    # Attach to all target DocTypes (primary + secondary)
    all_doctypes = [targets["primary"]] + targets.get("secondary", [])

    for doctype in all_doctypes:
        key = DOCTYPE_KEY_MAP.get(doctype)
        doc_name = created_docs.get(key) if key else None

        if not doc_name:
            continue

        try:
            file_doc = frappe.get_doc(
                {
                    "doctype": "File",
                    "file_url": source_file_url,
                    "file_name": file_name,
                    "attached_to_doctype": doctype,
                    "attached_to_name": doc_name,
                    "is_private": 1,
                }
            )
            file_doc.insert(ignore_permissions=True)

            results.append(
                {"doctype": doctype, "name": doc_name, "file": file_doc.name}
            )
            logger.info(f"Attached {file_name} to {doctype} {doc_name}")
        except Exception as e:
            logger.error(f"Failed to attach to {doctype} {doc_name}: {e}")

    # Also attach to the AI Procurement Job itself
    try:
        frappe.get_doc(
            {
                "doctype": "File",
                "file_url": source_file_url,
                "file_name": file_name,
                "attached_to_doctype": "AI Procurement Job",
                "attached_to_name": job_name,
                "is_private": 1,
            }
        ).insert(ignore_permissions=True)
    except Exception as e:
        logger.error(f"Failed to attach to job {job_name}: {e}")

    return results
