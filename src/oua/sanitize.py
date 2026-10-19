"""Data is not instruction: detect text in the data that tries to steer the agent. It is logged, never obeyed."""
from __future__ import annotations

import re

_PATTERNS = [
    ("ignore_previous_rules", re.compile(r"ignore\s+(all\s+|your\s+|any\s+)?(previous|prior|above)\s+(rules|instructions)", re.I)),
    ("system_note", re.compile(r"system\s+(note|prompt|message|instruction)", re.I)),
    ("addressed_to_ai", re.compile(r"\b(for|to)\s+(the\s+)?(ai|llm|assistant)\s*(agents?|models?)?\b", re.I)),
    ("broadcast_message", re.compile(r"\b(send|message|notify|whatsapp|push)\b.{0,60}\b(every|all)\s+(the\s+)?users?\b", re.I)),
    ("bypass_consent", re.compile(r"(opted\s*out|without\s+consent|regardless\s+of\s+consent)", re.I)),
]


def scan_text(text):
    """Return the names of injection patterns found in `text` (empty list when the text is clean)."""
    if not text:
        return []
    return [name for name, rx in _PATTERNS if rx.search(text)]
