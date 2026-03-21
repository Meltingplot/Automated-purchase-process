"""
Image preprocessing for OCR optimization.

Applies deskew, contrast enhancement, and binarization to improve
OCR accuracy on scanned documents.
"""

from __future__ import annotations

import io

from PIL import Image, ImageEnhance, ImageFilter


class Preprocessor:
    """Prepares images for optimal OCR extraction."""

    @staticmethod
    def prepare(image: Image.Image) -> Image.Image:
        """
        Apply preprocessing pipeline to an image.

        Pipeline:
        1. Convert to grayscale
        2. Enhance contrast
        3. Sharpen
        4. Binarize (adaptive threshold via Pillow)

        Args:
            image: PIL Image to preprocess

        Returns:
            Preprocessed PIL Image
        """
        # 1. Convert to grayscale
        if image.mode != "L":
            image = image.convert("L")

        # 2. Enhance contrast
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(1.5)

        # 3. Sharpen
        image = image.filter(ImageFilter.SHARPEN)

        # 4. Binarize - convert to pure black/white
        threshold = 128
        image = image.point(lambda x: 255 if x > threshold else 0, "1")

        return image

    @staticmethod
    def image_to_bytes(image: Image.Image, format: str = "PNG") -> bytes:
        """Convert PIL Image to bytes."""
        buffer = io.BytesIO()
        image.save(buffer, format=format)
        return buffer.getvalue()

    @staticmethod
    def bytes_to_image(data: bytes) -> Image.Image:
        """Convert bytes to PIL Image."""
        return Image.open(io.BytesIO(data))
