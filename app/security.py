from __future__ import annotations

import re


_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(
        r"(?i)\b(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"
    ),
    re.compile(
        r"(?i)\b(api[_ -]?key|access[_ -]?token|session(?:_string)?)"
        r"(\s*[:=]\s*)['\"]?[^\s,'\";]+"
    ),
    re.compile(r"\b1[A-Za-z0-9_-]{80,}\b"),
)


def safe_error(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        text = exc.__class__.__name__
    text = _SECRET_PATTERNS[0].sub("[redacted]", text)
    text = _SECRET_PATTERNS[1].sub(r"\1[redacted]", text)
    text = _SECRET_PATTERNS[2].sub(r"\1\2[redacted]", text)
    text = _SECRET_PATTERNS[3].sub("[redacted-session]", text)
    return text[:240]
