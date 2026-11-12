"""Fail if anything that looks like a credential is tracked in git.

Lightweight, dependency-free complement to the gitleaks step in CI; also handy
as a pre-commit hook:  python scripts/check_secrets.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "Postgres URL with password": re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:/\s'\"]+:(?!<|\*|\$|password|p@|pw@|tp@)[^@\s'\"]{6,}@"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Slack webhook": re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"),
    "Supabase service key (JWT)": re.compile(r"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\.[A-Za-z0-9_\-]{40,}"),
}
FORBIDDEN_FILES = {".env", ".streamlit/secrets.toml"}
SKIP_SUFFIXES = {".png", ".jpg", ".gif", ".ico", ".parquet", ".db", ".gz"}


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout  # noqa: S603, S607
        return [Path(p) for p in out.splitlines() if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [p for p in Path(".").rglob("*") if p.is_file() and ".git" not in p.parts]


def main() -> int:
    problems = []
    for path in tracked_files():
        if path.as_posix() in FORBIDDEN_FILES:
            problems.append(f"{path}: secret file must not be committed")
            continue
        if path.suffix in SKIP_SUFFIXES or not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pat in PATTERNS.items():
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{path}:{line}: possible {name}")
    if problems:
        print("Secret scan FAILED:\n  " + "\n  ".join(problems))
        return 1
    print("Secret scan passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
