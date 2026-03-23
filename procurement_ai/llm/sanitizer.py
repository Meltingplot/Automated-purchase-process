"""
Input sanitization layer (Security Schicht 1).

Multi-stage text cleaning BEFORE any content reaches an LLM.
Protects against prompt injection, homoglyph attacks, and
invisible character manipulation.
"""

from __future__ import annotations

import re
import unicodedata

# Maximum text length sent to LLMs
MAX_TEXT_LENGTH = 50_000

# Known prompt injection patterns (detect and reject)
INJECTION_PATTERNS = [
    r"ignore\s+(previous|above|all)\s+(instructions?|prompts?)",
    r"you\s+are\s+now\s+",
    r"new\s+instructions?:",
    r"system\s*prompt:",
    r"<\s*system\s*>",
    r"```\s*system",
    r"IMPORTANT:\s*override",
    r"disregard\s+(everything|all)",
    r"forget\s+(everything|all|your)",
    r"act\s+as\s+(if|a|an)",
    r"pretend\s+(you|to\s+be)",
    r"jailbreak",
    r"DAN\s+mode",
    r"do\s+anything\s+now",
    r"ignore\s+all\s+previous",
    r"override\s+system",
]

# Invisible Unicode characters to strip
INVISIBLE_CHARS = frozenset(
    [
        "\u200b",  # Zero Width Space
        "\u200c",  # Zero Width Non-Joiner
        "\u200d",  # Zero Width Joiner
        "\u2060",  # Word Joiner
        "\ufeff",  # Zero Width No-Break Space (BOM)
        "\u00ad",  # Soft Hyphen
        "\u200e",  # Left-to-Right Mark
        "\u200f",  # Right-to-Left Mark
        "\u202a",  # Left-to-Right Embedding
        "\u202b",  # Right-to-Left Embedding
        "\u202c",  # Pop Directional Formatting
        "\u202d",  # Left-to-Right Override
        "\u202e",  # Right-to-Left Override
    ]
)


class PromptInjectionError(Exception):
    """Raised when prompt injection patterns are detected in input text."""

    pass


class InputSanitizer:
    """
    Multi-stage input sanitization before text reaches any LLM.

    Pipeline:
    1. Unicode normalization (NFKC) - defeats homoglyph attacks
    2. Invisible character removal - defeats zero-width injection
    3. Injection pattern scanning - rejects document on detection
    4. Length truncation - prevents context overflow
    """

    @classmethod
    def sanitize(cls, text: str) -> tuple[str, list[str]]:
        """
        Sanitize input text for LLM consumption.

        Returns:
            (sanitized_text, list_of_warnings)

        Raises:
            PromptInjectionError: If prompt injection patterns are detected.
        """
        warnings: list[str] = []

        # 1. Unicode normalization (NFKC) - normalizes homoglyphs
        text = unicodedata.normalize("NFKC", text)

        # 2. Remove invisible characters
        text = cls._remove_invisible_chars(text)

        # 3. Scan for injection patterns — reject on detection
        detected = []
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                detected.append(pattern)

        if detected:
            raise PromptInjectionError(
                f"Potential prompt injection detected: {len(detected)} suspicious "
                "pattern(s) found. The document has been rejected for security reasons."
            )

        # 4. Truncate overly long text
        if len(text) > MAX_TEXT_LENGTH:
            warnings.append(
                f"Text truncated from {len(text)} to {MAX_TEXT_LENGTH} chars"
            )
            text = text[:MAX_TEXT_LENGTH]

        return text, warnings

    @staticmethod
    def _remove_invisible_chars(text: str) -> str:
        """Remove zero-width and other invisible Unicode characters."""
        return "".join(c for c in text if c not in INVISIBLE_CHARS)
