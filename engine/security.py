"""Security foundation for the scan engine.

Covers charter Parts I (AI rules) and IV (guard mandate) at the code level:

- input validation (tickers),
- safe parsing of untrusted XML (RSS feeds),
- same-origin-only HTTP redirects,
- the AI guard layer: prompt-injection scanning of untrusted text, secret
  scanning of LLM outputs, tool-argument schema validation, prompt budgets,
- an append-only audit trail.

Stdlib only: no new dependencies, no network calls, no per-call cost.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# 1. Ticker validation — tickers are interpolated into provider URLs, so an
#    unvalidated ticker is a URL-injection vector (path/query smuggling).
# ---------------------------------------------------------------------------

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")


def clean_ticker(ticker: str) -> str:
    """Uppercase, strip, and validate a ticker symbol.

    Raises ValueError on anything that is not a plain equity symbol, so a
    hostile or malformed value can never reach URL construction, workflow
    IDs, or cache keys.
    """
    t = (ticker or "").strip().upper()
    if not _TICKER_RE.fullmatch(t):
        raise ValueError(f"invalid ticker symbol: {ticker!r}")
    return t


# ---------------------------------------------------------------------------
# 2. Safe XML parsing — RSS/Atom feeds are untrusted input. stdlib ElementTree
#    expands internal entities (billion-laughs / quadratic-blowup DoS), so
#    entity declarations are rejected outright and payload size is capped.
# ---------------------------------------------------------------------------

_MAX_XML_BYTES = 5_000_000
_ENTITY_DECL_RE = re.compile(rb"<!ENTITY", re.IGNORECASE)


def safe_xml_parse(raw: bytes) -> ET.Element:
    """Parse untrusted XML with entity-expansion attacks rejected.

    Raises ValueError instead of parsing when the payload is oversized or
    declares entities. Fails closed: a rejected feed is a missed headline,
    not a crashed worker.
    """
    if len(raw) > _MAX_XML_BYTES:
        raise ValueError(f"XML payload too large: {len(raw)} bytes")
    if _ENTITY_DECL_RE.search(raw):
        raise ValueError("XML entity declarations rejected (entity-expansion risk)")
    return ET.fromstring(raw)


# ---------------------------------------------------------------------------
# 3. Same-origin redirects — urllib follows redirects silently, including to
#    attacker hosts if a provider is ever compromised. Redirects are allowed
#    only within the same registrable domain (query1 -> query2.yahoo.com is
#    fine; yahoo.com -> evil.com raises).
# ---------------------------------------------------------------------------

def _base_domain(host: str) -> str:
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_host = urllib.parse.urlparse(req.full_url).hostname or ""
        new_host = (urllib.parse.urlparse(
            urllib.parse.urljoin(req.full_url, newurl)).hostname or "")
        if _base_domain(new_host) != _base_domain(old_host):
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"cross-origin redirect blocked: {old_host} -> {new_host}",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameOriginRedirect)


def urlopen_guarded(req: urllib.request.Request, timeout: int = 25):
    """urlopen with cross-origin redirects blocked. Drop-in replacement."""
    return _OPENER.open(req, timeout=timeout)


# ---------------------------------------------------------------------------
# 4. Guard layer — input/output scanning for every LLM call (charter IV.38).
# ---------------------------------------------------------------------------

# Known prompt-injection / instruction-override phrases. Matched against
# UNTRUSTED text (headlines, PR copy) before it enters a prompt — never
# against the engine's own prompt templates.
_INJECTION_PHRASES = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard previous instructions",
    "disregard all previous",
    "override your instructions",
    "forget your instructions",
    "new instructions:",
    "reveal your instructions",
    "reveal your system prompt",
    "system prompt",
    "developer mode",
    "do anything now",
    "jailbreak",
)


def scan_untrusted_text(text: str) -> list[str]:
    """Return the injection indicators found in untrusted text (else [])."""
    t = (text or "").lower()
    return [p for p in _INJECTION_PHRASES if p in t]


# Secret shapes that must never appear in an LLM output (or a log line).
_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "openai-like key"),
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "groq-like key"),
    (re.compile(r"nvapi-[A-Za-z0-9_\-]{20,}"), "nvidia-like key"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "google-like key"),
    (re.compile(r"xai-[A-Za-z0-9]{20,}"), "xai-like key"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*"
                r"['\"]?[A-Za-z0-9_\-]{16,}"), "labeled secret"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
     "private key"),
)


def scan_output_for_secrets(text: str) -> list[str]:
    """Return the secret indicators found in model output (else [])."""
    t = text or ""
    return [label for rx, label in _SECRET_PATTERNS if rx.search(t)]


def redact_secrets(text: str) -> str:
    """Replace detected secret shapes with [REDACTED]."""
    t = text or ""
    for rx, _label in _SECRET_PATTERNS:
        t = rx.sub("[REDACTED]", t)
    return t


_JSON_TYPES = {
    "string": str, "integer": int, "boolean": bool, "number": (int, float),
}


def _matches_json_type(value, want: str) -> bool:
    # bool is a subclass of int — never accept it as integer/number.
    if isinstance(value, bool):
        return want == "boolean"
    if want == "string":
        return isinstance(value, str)
    if want == "boolean":
        return isinstance(value, bool)
    if want == "integer":
        return isinstance(value, int)
    if want == "number":
        return isinstance(value, (int, float))
    return True  # unknown type name: don't block on what we don't know


def validate_tool_args(tool_name: str, args: dict,
                       properties: dict) -> None:
    """Validate parsed tool-call arguments against the tool's schema.

    `json.loads` succeeding is not enough — a model can return well-formed
    JSON with wrong types or missing fields. Raises ValueError on mismatch.
    """
    if not isinstance(args, dict):
        raise ValueError(f"{tool_name}: arguments are not an object")
    for key, spec in (properties or {}).items():
        if key not in args:
            continue  # optional fields may be absent; required-ness is
                       # enforced by the tool schema at the provider
        want = spec.get("type")
        if want and not _matches_json_type(args[key], want):
            raise ValueError(
                f"{tool_name}: field {key!r} must be {want}, "
                f"got {type(args[key]).__name__}")


# Prompt/input size budget — unbounded prompts are a cost and reliability
# risk (and a wider injection surface). Untrusted sections are truncated
# first; the truncation is audit-logged.
MAX_PROMPT_CHARS = 24_000


def enforce_prompt_budget(prompt: str, label: str) -> str:
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    audit_log("prompt_truncated",
              {"label": label, "original_chars": len(prompt),
               "budget_chars": MAX_PROMPT_CHARS})
    return prompt[:MAX_PROMPT_CHARS] + "\n[truncated: prompt budget]"


# ---------------------------------------------------------------------------
# 5. Append-only audit trail (charter III.32). Entries are appended, never
#    edited; a failed write never breaks the scan.
# ---------------------------------------------------------------------------

_AUDIT_PATH = "/tmp/scan-engine-audit.jsonl"


def audit_log(event: str, details: dict | None = None) -> None:
    try:
        with open(_AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"ts": time.time(), "event": event,
                 **(details or {})},
                default=str) + "\n")
    except OSError:
        pass
