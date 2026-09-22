#!/usr/bin/env python3
"""Pre-deploy guard: scan the engine tree for secrets and dangerous calls.

Runs before any engine deploy or worker restart. Stdlib only, zero cost.
Exit 0 = clean, exit 1 = findings (printed with file:line).

Checks (charter IV.38 — the guard scans code changes before deploy):
  1. Secret shapes: API keys, tokens, private keys — in code OR in values
     assigned to key/secret/token/password variables.
  2. Dangerous calls: eval/exec/__import__/compile, os.system/popen,
     subprocess with shell=True, pickle/marshal deserialization,
     yaml.load without SafeLoader.
  3. Raw credential-ish literals: 32+ char alphanumeric strings assigned to
     suspicious variable names.

Skips: __pycache__, .pyc files, and this file's own pattern literals
(the _SECRET_RES list below is allowlisted by construction).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# (regex, label)
_SECRET_RES = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "openai-like key"),
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "groq-like key"),
    (re.compile(r"nvapi-[A-Za-z0-9_\\-]{20,}"), "nvidia-like key"),
    (re.compile(r"AIza[0-9A-Za-z_\\-]{20,}"), "google-like key"),
    (re.compile(r"xai-[A-Za-z0-9]{20,}"), "xai-like key"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
     "private key"),
]
# variable = "long random-looking literal"
_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|bearer)\b"
    r"\s*[:=]\s*['\"]([A-Za-z0-9_\\-]{16,})['\"]")

_DANGEROUS_RES = [
    (re.compile(r"(?<![A-Za-z0-9_.])eval\s*\("), "eval()"),
    (re.compile(r"(?<![A-Za-z0-9_.])exec\s*\("), "exec()"),
    (re.compile(r"__import__\s*\("), "__import__()"),
    (re.compile(r"(?<![A-Za-z0-9_.])compile\s*\("), "compile()"),
    (re.compile(r"os\.system\s*\("), "os.system()"),
    (re.compile(r"os\.popen\s*\("), "os.popen()"),
    (re.compile(r"subprocess\.\w+\(.*shell\s*=\s*True"), "subprocess shell=True"),
    (re.compile(r"pickle\.loads?\s*\("), "pickle deserialization"),
    (re.compile(r"marshal\.loads?\s*\("), "marshal deserialization"),
    (re.compile(r"yaml\.load\s*\("), "yaml.load (use safe_load)"),
]

_SKIP_DIRS = {"__pycache__", ".git", "node_modules"}
_SKIP_FILES = {"guard_check.py"}  # this file's own pattern literals


def scan_file(path: Path) -> list[str]:
    findings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for rx, label in _SECRET_RES:
            if rx.search(line):
                findings.append(f"{path}:{i}: secret pattern ({label})")
        m = _ASSIGN_RE.search(line)
        if m and "example" not in line.lower() and "placeholder" not in line.lower():
            findings.append(f"{path}:{i}: credential-shaped assignment "
                            f"to '{m.group(1)}'")
        for rx, label in _DANGEROUS_RES:
            if rx.search(line):
                findings.append(f"{path}:{i}: dangerous call ({label})")
    return findings


def main() -> int:
    findings: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if path.name in _SKIP_FILES:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        findings.extend(scan_file(path))
    # also sweep YAML configs for pasted secrets
    for path in sorted(ROOT.parent.rglob("*.yaml")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        findings.extend(scan_file(path))
    if findings:
        print("GUARD FAIL — findings:")
        for f in findings:
            print(" ", f)
        return 1
    print("GUARD PASS — no secrets or dangerous calls found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
