"""Document preprocessing: PDF to image conversion.

Security: PDF text extraction is intentionally avoided.
All PDFs are rendered to images first, eliminating hidden text
(white-on-white, invisible layers) that could contain prompt injections.
The LLM only ever sees pixel data.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image


def pdf_to_images(
    pdf_bytes: bytes,
    *,
    dpi: int = 200,
    max_pages: int = 20,
) -> list[bytes]:
    """Convert PDF pages to PNG images.

    Args:
        pdf_bytes: Raw PDF file content.
        dpi: Resolution for rendering. 200 is a good balance
             between quality and token cost.
        max_pages: Maximum number of pages to process.

    Returns:
        List of PNG image bytes, one per page.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ImportError(
            "PyMuPDF is required for PDF processing. "
            "Install with: pip install PyMuPDF"
        )

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images: list[bytes] = []

    for page_num in range(min(len(doc), max_pages)):
        page = doc[page_num]
        # Render page to pixmap (image) — this is the security boundary.
        # Hidden text, invisible layers, and metadata are NOT rendered.
        zoom = dpi / 72  # 72 is the default PDF DPI
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)

        # Convert to PNG bytes
        img_bytes = pix.tobytes("png")
        images.append(img_bytes)

    doc.close()
    return images


def image_file_to_png(image_bytes: bytes) -> list[bytes]:
    """Normalize an image file (JPG, BMP, TIFF, etc.) to PNG.

    Returns a single-element list for API consistency with pdf_to_images.
    """
    img = Image.open(io.BytesIO(image_bytes))
    # Convert to RGB if necessary (e.g. CMYK, RGBA)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return [buf.getvalue()]


def prepare_document(
    file_bytes: bytes,
    filename: str,
    *,
    dpi: int = 200,
    max_pages: int = 20,
) -> tuple[list[bytes], str]:
    """Prepare a document for LLM extraction.

    Args:
        file_bytes: Raw file content.
        filename: Original filename (used to detect type).
        dpi: Resolution for PDF rendering.
        max_pages: Maximum pages for PDFs.

    Returns:
        Tuple of (list of PNG image bytes, media type string).
    """
    ext = Path(filename).suffix.lower()

    if ext == ".pdf":
        images = pdf_to_images(file_bytes, dpi=dpi, max_pages=max_pages)
        return images, "image/png"

    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"):
        images = image_file_to_png(file_bytes)
        return images, "image/png"

    raise ValueError(
        f"Unsupported file type '{ext}'. "
        f"Supported: .pdf, .png, .jpg, .jpeg, .bmp, .tiff, .webp"
    )
