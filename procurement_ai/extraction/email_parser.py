"""
Email parsing for attachment extraction.

Extracts PDF and image attachments from email messages
for processing through the extraction pipeline.
"""

from __future__ import annotations

import email
import logging
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage

logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/tiff",
}


@dataclass
class EmailAttachment:
    """An extracted email attachment."""

    filename: str
    content_type: str
    data: bytes


class EmailParser:
    """Extracts purchase document attachments from emails."""

    @staticmethod
    def extract_attachments(email_content: str | bytes) -> list[EmailAttachment]:
        """
        Parse an email and extract relevant attachments.

        Args:
            email_content: Raw email content (string or bytes)

        Returns:
            List of EmailAttachment objects with allowed content types
        """
        if isinstance(email_content, str):
            msg = email.message_from_string(email_content, policy=policy.default)
        else:
            msg = email.message_from_bytes(email_content, policy=policy.default)

        attachments: list[EmailAttachment] = []

        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            # Skip non-attachment parts
            if "attachment" not in content_disposition and "inline" not in content_disposition:
                continue

            if content_type not in ALLOWED_CONTENT_TYPES:
                logger.debug(f"Skipping attachment with type: {content_type}")
                continue

            filename = part.get_filename() or f"attachment.{content_type.split('/')[-1]}"
            payload = part.get_payload(decode=True)

            if payload:
                attachments.append(
                    EmailAttachment(
                        filename=filename,
                        content_type=content_type,
                        data=payload,
                    )
                )
                logger.info(f"Extracted attachment: {filename} ({content_type})")

        return attachments

    @staticmethod
    def get_email_metadata(email_content: str | bytes) -> dict:
        """Extract email metadata (subject, from, date)."""
        if isinstance(email_content, str):
            msg = email.message_from_string(email_content, policy=policy.default)
        else:
            msg = email.message_from_bytes(email_content, policy=policy.default)

        return {
            "subject": msg.get("Subject", ""),
            "from": msg.get("From", ""),
            "to": msg.get("To", ""),
            "date": msg.get("Date", ""),
        }
