"""
File upload security validation.

Validates uploaded files against allowed MIME types and magic bytes
to prevent malicious file uploads.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Allowed file types (MIME type → file extensions)
ALLOWED_TYPES: dict[str, list[str]] = {
    "application/pdf": [".pdf"],
    "image/png": [".png"],
    "image/jpeg": [".jpg", ".jpeg"],
    "image/tiff": [".tif", ".tiff"],
}

# Magic bytes for file type validation
MAGIC_BYTES: dict[str, list[bytes]] = {
    "application/pdf": [b"%PDF"],
    "image/png": [b"\x89PNG"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/tiff": [b"II\x2a\x00", b"MM\x00\x2a"],
}

# Maximum file size (20 MB)
MAX_FILE_SIZE = 20 * 1024 * 1024


def validate_upload(file_content: bytes, filename: str) -> tuple[bool, str]:
    """
    Validate an uploaded file.

    Checks:
    1. File size within limit
    2. File extension is allowed
    3. Magic bytes match expected type

    Returns:
        (is_valid, error_message)
    """
    # 1. Size check
    if len(file_content) > MAX_FILE_SIZE:
        return False, f"File too large: {len(file_content)} bytes (max {MAX_FILE_SIZE})"

    if len(file_content) == 0:
        return False, "Empty file"

    # 2. Extension check
    ext = _get_extension(filename)
    allowed_ext = {e for exts in ALLOWED_TYPES.values() for e in exts}
    if ext not in allowed_ext:
        return False, f"File type '{ext}' not allowed. Allowed: {sorted(allowed_ext)}"

    # 3. Magic bytes check
    for mime_type, extensions in ALLOWED_TYPES.items():
        if ext in extensions:
            if not _check_magic_bytes(file_content, mime_type):
                return (
                    False,
                    f"File content does not match expected type for '{ext}'",
                )
            break

    return True, ""


def _get_extension(filename: str) -> str:
    """Extract lowercase file extension."""
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()


def _check_magic_bytes(content: bytes, mime_type: str) -> bool:
    """Check if file content starts with expected magic bytes."""
    expected = MAGIC_BYTES.get(mime_type, [])
    if not expected:
        return True  # No magic bytes check for this type

    return any(content.startswith(magic) for magic in expected)
