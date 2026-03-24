"""
PDF parsing with text extraction and OCR fallback.

Uses pdfplumber for text-based PDFs, falls back to OCR
for scanned/image-based PDFs.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from PIL import Image

from .ocr_engine import OCREngine

logger = logging.getLogger(__name__)

# Minimum characters per page to consider it a text-based PDF
MIN_TEXT_CHARS_PER_PAGE = 50


@dataclass
class PDFExtractionResult:
    """Result of PDF text extraction."""

    text: str
    images: list[bytes] = field(default_factory=list)
    page_count: int = 0
    used_ocr: bool = False
    is_native_text: bool = False


class PDFParser:
    """
    Extracts text and images from PDF files.

    Strategy:
    1. Try pdfplumber for native text extraction
    2. If text is sparse (< 50 chars/page), fall back to OCR
    3. Also extract images for vision-capable LLMs
    """

    def __init__(self, ocr_engine: OCREngine | None = None):
        self.ocr_engine = ocr_engine or OCREngine()

    def extract(self, file_path: str) -> PDFExtractionResult:
        """
        Extract text and images from a PDF file.

        Args:
            file_path: Path to the PDF file

        Returns:
            PDFExtractionResult with text, images, and metadata
        """
        try:
            import pdfplumber
        except ImportError:
            raise ImportError(
                "pdfplumber is not installed. Install with: pip install pdfplumber"
            )

        all_text_parts: list[str] = []
        all_images: list[bytes] = []
        used_ocr = False
        native_text_pages = 0
        page_count = 0

        with pdfplumber.open(file_path) as pdf:
            page_count = len(pdf.pages)

            for page_num, page in enumerate(pdf.pages):
                # Try native text extraction first
                page_text = page.extract_text() or ""

                if len(page_text.strip()) >= MIN_TEXT_CHARS_PER_PAGE:
                    native_text_pages += 1
                else:
                    # Sparse text: this is likely a scanned page
                    logger.info(
                        f"Page {page_num + 1}: sparse text ({len(page_text)} chars), "
                        f"falling back to OCR"
                    )
                    ocr_text = self._ocr_page(page)
                    if ocr_text:
                        page_text = ocr_text
                        used_ocr = True

                all_text_parts.append(page_text)

                # Extract page as image for vision LLMs
                page_image = self._page_to_image(page)
                if page_image:
                    all_images.append(page_image)

        # A PDF is "native text" if majority of pages have good text extraction
        # (e-invoices, digital PDFs). Scanned documents will have mostly sparse pages.
        is_native = page_count > 0 and native_text_pages >= (page_count / 2)

        return PDFExtractionResult(
            text="\n\n".join(all_text_parts),
            images=all_images,
            page_count=page_count,
            used_ocr=used_ocr,
            is_native_text=is_native,
        )

    def extract_from_bytes(self, pdf_bytes: bytes) -> PDFExtractionResult:
        """Extract from PDF bytes instead of file path."""
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp.flush()
            tmp_path = tmp.name

        try:
            return self.extract(tmp_path)
        finally:
            os.unlink(tmp_path)

    def _ocr_page(self, page) -> str:
        """Convert a pdfplumber page to image and run OCR."""
        try:
            pil_image = page.to_image(resolution=300).original
            return self.ocr_engine.extract(pil_image)
        except Exception as e:
            logger.warning(f"OCR failed for page: {e}")
            return ""

    def _page_to_image(self, page) -> bytes | None:
        """Convert a pdfplumber page to PNG bytes."""
        try:
            pil_image = page.to_image(resolution=200).original
            buffer = io.BytesIO()
            pil_image.save(buffer, format="PNG")
            return buffer.getvalue()
        except Exception as e:
            logger.warning(f"Page to image conversion failed: {e}")
            return None
