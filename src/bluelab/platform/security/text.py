"""Plain-text normalization shared by untrusted content inputs (SEC-020/022)."""

from __future__ import annotations

import unicodedata


def safe_plain_text(value: str) -> str:
    """Normalize Unicode and remove control/format characters, including bidi overrides."""
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(
        character
        for character in normalized
        if character in "\n\t" or not unicodedata.category(character).startswith("C")
    )
