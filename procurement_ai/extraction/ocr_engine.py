"""
OCR engine wrapper supporting Tesseract and EasyOCR.

Provides a unified interface for text extraction from images,
configurable via AI Procurement Settings.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .preprocessor import Preprocessor

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger(__name__)


class OCREngine:
    """
    Unified OCR interface for Tesseract and EasyOCR.

    Usage:
        engine = OCREngine(engine_name="Tesseract")
        text = engine.extract(image)
    """

    def __init__(self, engine_name: str = "Tesseract"):
        self.engine_name = engine_name
        self._easyocr_reader = None

    def extract(self, image: "Image.Image", preprocess: bool = True) -> str:
        """
        Extract text from an image using the configured OCR engine.

        Args:
            image: PIL Image to extract text from
            preprocess: Whether to apply preprocessing (default: True)

        Returns:
            Extracted text string
        """
        if preprocess:
            image = Preprocessor.prepare(image)

        if self.engine_name == "Tesseract":
            return self._extract_tesseract(image)
        elif self.engine_name == "EasyOCR":
            return self._extract_easyocr(image)
        else:
            raise ValueError(f"Unknown OCR engine: {self.engine_name}")

    def _extract_tesseract(self, image: "Image.Image") -> str:
        """Extract text using Tesseract."""
        try:
            import pytesseract

            text = pytesseract.image_to_string(image, lang="deu+eng")
            return text.strip()
        except ImportError:
            raise ImportError(
                "pytesseract is not installed. Install with: pip install pytesseract"
            )
        except Exception as e:
            logger.error(f"Tesseract OCR failed: {e}")
            raise

    def _extract_easyocr(self, image: "Image.Image") -> str:
        """Extract text using EasyOCR."""
        try:
            import easyocr
            import numpy as np

            if self._easyocr_reader is None:
                self._easyocr_reader = easyocr.Reader(
                    ["de", "en"], gpu=False, verbose=False
                )

            # EasyOCR expects numpy array
            img_array = np.array(image)
            results = self._easyocr_reader.readtext(img_array, detail=0)
            return "\n".join(results).strip()
        except ImportError:
            raise ImportError(
                "easyocr is not installed. Install with: pip install easyocr"
            )
        except Exception as e:
            logger.error(f"EasyOCR failed: {e}")
            raise
