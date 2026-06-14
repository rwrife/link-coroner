"""URL extractors.

For M1 we ship one pragmatic regex-based extractor that works on every
supported file type. Format-specific extractors (markdown-it, selectolax)
will land in later milestones.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# A deliberately conservative URL regex: http/https only, stop at whitespace
# and common surrounding punctuation. Good enough for M1.
_URL_RE = re.compile(
    r"""https?://[^\s<>"'`\]\)\}]+""",
    re.IGNORECASE,
)

# Trailing punctuation that's almost never part of a URL.
_STRIP_TRAILING = ".,;:!?\u2026"


def extract_urls(text: str, *, suffix: str | None = None) -> Iterable[str]:
    """Yield URLs found in ``text``.

    ``suffix`` is accepted for future format-aware extraction but ignored
    in M1. URLs are yielded in document order (duplicates included — caller
    can dedupe).
    """
    del suffix  # reserved for M2+
    for match in _URL_RE.finditer(text):
        url = match.group(0)
        # Strip trailing punctuation that's typically prose, not URL.
        while url and url[-1] in _STRIP_TRAILING:
            url = url[:-1]
        # Balance parens: drop trailing ')' if there's no matching '(' in url.
        while url.endswith(")") and url.count("(") < url.count(")"):
            url = url[:-1]
        if url:
            yield url
