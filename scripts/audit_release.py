from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path


FORBIDDEN_NAMES = (
    re.compile(r"(^|/)\.env($|\.)", re.IGNORECASE),
    re.compile(r"\.session(?:-journal)?$", re.IGNORECASE),
    re.compile(r"\.(?:pem|p12|pfx|key)$", re.IGNORECASE),
    re.compile(r"(^|/)(?:memory|messages?)\.(?:db|sqlite|sqlite3)$", re.IGNORECASE),
)

SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\b(?:RAILWAY_TOKEN|RAILWAY_API_TOKEN)\s*=\s*\S+", re.IGNORECASE),
    re.compile(
        rb"^[ \t]*TG_SESSION_STRING[ \t]*=[ \t]*"
        rb"(?!$|your[-_ ]|<|placeholder)\S+",
        re.MULTILINE,
    ),
    re.compile(
        rb"^[ \t]*(?:TG_API_HASH|AI_API_KEY|ACCOUNT_ENCRYPTION_KEY|"
        rb"DASHBOARD_PASSWORD|OPENAI_MEDIA_API_KEY|AZURE_SPEECH_KEY)"
        rb"[ \t]*=[ \t]*"
        rb"(?!$|your[-_ ]|<|placeholder|please[-_ ])\S+",
        re.MULTILINE,
    ),
)

TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".txt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".example",
    "",
}

IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}


def forbidden_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if normalized.lower().endswith(".env.example"):
        return False
    return any(pattern.search(normalized) for pattern in FORBIDDEN_NAMES)


def inspect_bytes(name: str, data: bytes) -> list[str]:
    if Path(name).suffix.lower() not in TEXT_SUFFIXES:
        return []
    return [
        f"{name}: matched secret pattern {index}"
        for index, pattern in enumerate(SECRET_PATTERNS, start=1)
        if pattern.search(data)
    ]


def audit_directory(root: Path) -> list[str]:
    findings: list[str] = []
    for path in root.rglob("*"):
        relative_path = path.relative_to(root)
        if not path.is_file() or any(
            part in IGNORED_DIRECTORIES for part in relative_path.parts
        ):
            continue
        relative = relative_path.as_posix()
        if forbidden_name(relative):
            findings.append(f"{relative}: forbidden sensitive filename")
            continue
        findings.extend(inspect_bytes(relative, path.read_bytes()))
    return findings


def audit_zip(path: Path) -> list[str]:
    findings: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if forbidden_name(info.filename):
                findings.append(f"{info.filename}: forbidden sensitive filename")
                continue
            findings.extend(inspect_bytes(info.filename, archive.read(info)))
    return findings


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings = audit_zip(target) if target.suffix.lower() == ".zip" else audit_directory(target)
    if findings:
        print("Release audit failed:")
        print("\n".join(f"- {finding}" for finding in findings))
        raise SystemExit(1)
    print(f"Release audit passed: {target}")


if __name__ == "__main__":
    main()
