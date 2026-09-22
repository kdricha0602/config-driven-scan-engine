"""True-float provider: stockanalysis.com statistics pages (free, no key).

stockanalysis.com embeds a JSON data blob on each /stocks/{t}/statistics/ page
with id:"float" (exact share count in `hover`), plus short and ownership stats.
Falls back to the SEC shares-outstanding proxy when the page is unavailable.
"""
from __future__ import annotations

import re
import time
import urllib.request

from security import clean_ticker, urlopen_guarded

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def _fetch(url: str, timeout: int = 25) -> str:
    time.sleep(0.7)  # be polite to a free source
    req = urllib.request.Request(url, headers=_UA)
    with urlopen_guarded(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _parse_num(s: str) -> float | None:
    """'4,457,220' or '4.46M' -> float."""
    s = (s or "").strip().replace(",", "")
    if not s or s in ("-", "N/A", "?"):
        return None
    m = re.fullmatch(r"([\d.]+)([BMK])", s)
    if m:
        mult = {"B": 1e9, "M": 1e6, "K": 1e3}[m.group(2)]
        return float(m.group(1)) * mult
    try:
        return float(s)
    except ValueError:
        return None


def float_stats(ticker: str) -> dict:
    """Return true-float stats for a ticker.

    {"ticker","float_shares","float_display","short_pct_float",
     "short_ratio","inst_own_pct","source"}  — values None when unavailable.
    """
    ticker = clean_ticker(ticker)
    out = {"ticker": ticker.upper(), "float_shares": None,
           "float_display": None, "short_pct_float": None,
           "short_ratio": None, "inst_own_pct": None,
           "source": "stockanalysis.com"}
    try:
        html = _fetch(f"https://stockanalysis.com/stocks/{ticker.lower()}/statistics/")
    except Exception as e:  # noqa: BLE001 - free source, degrade gracefully
        out["error"] = str(e)[:120]
        return out
    blob = {}
    for m in re.finditer(
            r'\{id:"(\w+)",title:"([^"]+)",value:"([^"]+)",hover:"([^"]*)"', html):
        blob[m.group(1)] = (m.group(3), m.group(4))
    if "float" in blob:
        val, hov = blob["float"]
        out["float_shares"] = _parse_num(hov) or _parse_num(val)
        out["float_display"] = val
    if "shortFloat" in blob:
        v = blob["shortFloat"][0].rstrip("%")
        try:
            out["short_pct_float"] = float(v)
        except ValueError:
            pass
    if "shortRatio" in blob:
        try:
            out["short_ratio"] = float(blob["shortRatio"][0])
        except ValueError:
            pass
    if "sharesInstitutions" in blob:
        v = blob["sharesInstitutions"][0].rstrip("%")
        try:
            out["inst_own_pct"] = float(v)
        except ValueError:
            pass
    # shares outstanding shown in the page header table (not in blob)
    m = re.search(r"Shares Outstanding[^$]*?([\d.,]+[BMK])", html)
    if m:
        out["shares_outstanding"] = _parse_num(m.group(1))
    return out
