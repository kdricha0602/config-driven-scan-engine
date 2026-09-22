"""News + catalyst detection. Free, no key.

- Google News RSS per ticker (headlines, source, date).
- 8-K item codes from EDGAR filing indexes (1.01/2.01/7.01/8.01 ...).
- Deterministic catalyst classifier built from the desk's own valid/reject
  catalyst lists — no LLM needed for the first pass; interpret_catalyst can
  refine later.
"""

from __future__ import annotations

import re
import time
import html
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .cache import get as cache_get, put as cache_put
from security import clean_ticker, safe_xml_parse, urlopen_guarded

_NEWS_TTL = 3600
_LAST = [0.0]

# Material catalysts (from the desk's momentum spec)
_MATERIAL = [
    "fda approval", "fda approved", "fda approves", "fda clearance", "fda grants",
    "breakthrough designation", "phase 1", "phase 2", "phase 3",
    "clinical trial", "trial results", "trial data", "topline data",
    "contract", "awarded", "purchase order", "backlog", "contract win",
    "wins contract",
    "partnership", "collaboration", "license agreement", "licensing deal",
    "earnings beat", "beats estimates", "raised guidance", "raises guidance",
    "lifts outlook", "record revenue", "record earnings",
    "acquisition", "to acquire", "being acquired", "merger",
    "takeover", "buyout", "patent", "court ruling", "settlement",
    "design win", "major customer", "strategic investment",
    "buyback", "share repurchase", "repurchase program", "repurchase plan",
    "uplisting", "uplist", "joins russell", "russell 2000",
]
# Rejections (from the desk's momentum spec)
_REJECT = [
    "reverse split", "reverse-split", "delisting", "delist",
    "deficiency notice", "non-compliance", "bid price deficiency",
    "public offering", "secondary offering", "follow-on offering",
    "at-the-market", "atm offering", "shelf registration",
    "convertible notes", "private placement", "registered direct",
    "warrant", "going concern", "bankruptcy", "chapter 11",
    "paid promotion", "stock promotion",
    "blockchain", "crypto", "token", "token2049", "digital asset treasury",
    "conference", "to present", "fireside chat", "webinar",
]


def _fetch(url: str, timeout: int = 25) -> bytes:
    gap = time.time() - _LAST[0]
    if gap < 0.4:
        time.sleep(0.4 - gap)
    req = urllib.request.Request(
        url, headers={"User-Agent":
                      "QuantKernal scan-engine/1.0 "
                      "(contact: quantkernal-scan-engine@example.com)"})
    with urlopen_guarded(req, timeout=timeout) as resp:
        raw = resp.read()
    _LAST[0] = time.time()
    return raw


def ticker_news(ticker: str, company_name: str = "",
                days: int = 7, max_items: int = 20,
                as_of: datetime | None = None) -> list[dict]:
    """Recent headlines mentioning the ticker/company. `as_of` anchors
    recency (scan-as-of support); items dated after it are dropped."""
    ticker = clean_ticker(ticker)
    as_of = as_of or datetime.now(timezone.utc)
    key = f"news:rss:{ticker}:{days}"
    cached = cache_get(key, _NEWS_TTL)
    items = cached if cached is not None else None
    if items is None:
        q = f'"{ticker}" stock'
        if company_name:
            q += f' OR "{company_name}"'
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en")
        try:
            raw = _fetch(url)
            root = safe_xml_parse(raw)
            items = []
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                src = (it.findtext("source") or "").strip()
                pub = (it.findtext("pubDate") or "").strip()
                link = (it.findtext("link") or "").strip()
                try:
                    dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")
                    dt = dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                items.append({"title": title, "source": src,
                              "published": dt.isoformat(), "url": link})
            cache_put(key, items)
        except Exception:
            items = []
    cutoff = as_of - timedelta(days=days)
    out = []
    for it in items:  # type: ignore[union-attr]
        try:
            dt = datetime.fromisoformat(it["published"])
        except (KeyError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if cutoff <= dt <= as_of:
            out.append({**it, "age_days": (as_of - dt).total_seconds() / 86400})
    return out[:max_items]


def _relevant(items: list[dict], ticker: str, company_name: str) -> list[dict]:
    """Keep headlines that actually mention the company (not ticker soup)."""
    t = ticker.upper()
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", company_name.lower()).split()
             if len(w) > 2 and w not in {"inc", "corp", "the", "and", "co"}]
    keep = []
    for it in items:
        title = it["title"]
        up = title.upper()
        if t in re.split(r"[^A-Z]", up):
            keep.append(it)
            continue
        low = title.lower()
        if words and sum(1 for w in words[:4] if w in low) >= 2:
            keep.append(it)
    return keep


# A dilution event that was SCRAPPED is a bullish catalyst (overhang removed),
# not a rejection — check negation before the plain reject list.
_NEGATED = ["scrapped", "withdrawn", "cancelled", "canceled", "terminated",
            "called off", "drops plan", "abandons plan", "abandoned"]
_DILUTION = ["offering", "share sale", "secondary", "at-the-market",
             "atm offering", "shelf", "convertible", "private placement",
             "registered direct", "warrant"]


def classify_catalyst(items: list[dict]) -> dict:
    """Deterministic verdict from the desk's catalyst lists.

    Headlines are judged newest-first; the first decisive headline wins, so
    fresh news outranks stale news. Returns verdict: material | rejected |
    none, plus the deciding headline.
    """
    for it in items:
        # 6-K exhibits carry a classify_text (headline + body) so promos
        # that name the conference/token in the body still get caught
        low = (it.get("classify_text") or it["title"]).lower()
        base = {"headline": it["title"], "source": it["source"],
                "age_days": it.get("age_days")}
        if (any(k in low for k in _NEGATED)
                and any(k in low for k in _DILUTION)):
            return {**base, "verdict": "material",
                    "reason": "dilution overhang removed (offering scrapped/withdrawn)"}
        if any(k in low for k in _REJECT):
            return {**base, "verdict": "rejected",
                    "reason": "headline matches rejection list"}
        if any(k in low for k in _MATERIAL):
            return {**base, "verdict": "material",
                    "reason": "headline matches material-catalyst list"}
    return {"verdict": "none", "headline": "", "source": "",
            "age_days": None, "reason": "no catalyst headline found"}


# 8-K item codes: the filing index JSON carries them -------------------------

_MATERIAL_ITEMS = {"1.01", "1.02", "2.01", "2.03", "5.02", "7.01", "8.01"}


def eight_k_items(ticker: str, days: int = 7,
                  as_of: datetime | None = None) -> list[dict]:
    """Recent 8-Ks with their item codes (from EDGAR filing index JSON).

    Item 1.01/2.01/7.01/8.01-class filings are the regulatory footprint of
    the same catalysts the news classifier looks for.
    """
    from . import sec_edgar
    ticker = clean_ticker(ticker)
    as_of = as_of or datetime.now(timezone.utc)
    cutoff = (as_of - timedelta(days=days)).date().isoformat()
    asof_d = as_of.date().isoformat()
    try:
        cik = sec_edgar.ticker_to_cik(ticker)
        sub = sec_edgar.submissions(cik)
    except Exception:
        return []
    recent = (sub.get("filings", {}) or {}).get("recent", {}) or {}
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    out = []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        d = dates[i] if i < len(dates) else ""
        if not (cutoff <= d <= asof_d):
            continue
        acc = accs[i].replace("-", "") if i < len(accs) else ""
        items: list[str] = []
        if acc:
            try:
                idx = sec_edgar._fetch(
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                    f"{acc}-index.json")
                raw_items = str(idx.get("items", "") or "")
                items = [p.strip() for p in re.split(r"[,\s]+", raw_items)
                         if p.strip()]
            except Exception:
                pass
        out.append({"filing_date": d, "items": items,
                    "material": any(it in _MATERIAL_ITEMS for it in items)})
    return out


def _filing_docs(cik: str, acc: str) -> list[str]:
    """Document names in a filing; tries index.json, then the classic
    index.html (SEC's JSON index 404s intermittently for recent filings)."""
    from . import sec_edgar
    base = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}")
    try:
        idx = sec_edgar._fetch(f"{base}-index.json")
        items = (idx.get("directory", {}) or {}).get("item", []) or []
        return [x.get("name", "") for x in items if x.get("name")]
    except Exception:
        pass
    try:
        html = _fetch(f"{base}-index.html").decode("utf-8", "ignore")
        return re.findall(r'href="([^"]+\.htm[l]?)"', html, re.I)
    except Exception:
        pass
    try:  # plain directory listing (Apache autoindex)
        html = _fetch(f"{base}/").decode("utf-8", "ignore")
        # file rows use absolute /Archives/edgar/data/... paths; the
        # site's own nav links never contain "/Archives/edgar/data/"
        return re.findall(r'href="(/Archives/edgar/data/[^"]+\.html?)"',
                          html, re.I)
    except Exception:
        return []


def six_k_exhibits(ticker: str, days: int = 7,
                   as_of: datetime | None = None) -> list[dict]:
    """6-K press-release exhibits (EX-99.1) for foreign private issuers.

    For foreign filers the 6-K is where material news lands — the 8-K
    footprint above misses it entirely. Fetches the exhibit text and
    shapes it like a headline so the same classifier judges it.
    """
    from . import sec_edgar
    ticker = clean_ticker(ticker)
    as_of = as_of or datetime.now(timezone.utc)
    cutoff = (as_of - timedelta(days=days)).date().isoformat()
    asof_d = as_of.date().isoformat()
    try:
        cik = sec_edgar.ticker_to_cik(ticker)
        sub = sec_edgar.submissions(cik)
    except Exception:
        return []
    recent = (sub.get("filings", {}) or {}).get("recent", {}) or {}
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    out = []
    for i, form in enumerate(forms):
        if form != "6-K":
            continue
        d = dates[i] if i < len(dates) else ""
        if not (cutoff <= d <= asof_d):
            continue
        acc = accs[i].replace("-", "") if i < len(accs) else ""
        if not acc:
            continue
        try:
            names = _filing_docs(cik, acc)
            ex = next((n.split("/")[-1] for n in names
                       if re.search(r"ex[-_ ]?99", n, re.I)), None)
            if not ex:
                continue
            raw = _fetch(
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{acc}/{ex}").decode("utf-8", "ignore")
            # keep block structure so the press-release headline survives
            t = re.sub(r"</?(p|div|br|tr|h[1-6]|li|table|font)[^>]*>",
                       "\n", raw, flags=re.I)
            t = html.unescape(re.sub(r"<[^>]+>", " ", t))
            lines = [re.sub(r"\s+", " ", ln).strip() for ln in t.split("\n")]
            lines = [ln for ln in lines if len(ln) > 25]
            title = ""
            for i, ln in enumerate(lines):
                if "PRESS RELEASE" in ln.upper():
                    cands = [c for c in lines[i + 1:i + 10]
                             if len(c) > 30
                             and "exhibit 99" not in c.lower()
                             and "forward-looking" not in c.lower()
                             and "press release" not in c.lower()]
                    # longest line = the actual headline, not the dateline
                    title = max(cands, key=len) if cands else ""
                    break
            title = title or (lines[0] if lines else "")
            body = " ".join(lines[1:6])
            age = (as_of - datetime.fromisoformat(d)
                   .replace(tzinfo=timezone.utc)).total_seconds() / 86400
            out.append({"title": title, "source": "SEC 6-K EX-99.1",
                        "published": d, "age_days": age,
                        "excerpt": body[:600],
                        # classify on headline + body: promos name the
                        # conference/token in the body, not the headline
                        "classify_text": f"{title} {body[:400]}"})
        except Exception:
            continue
    return out


def catalyst_report(ticker: str, company_name: str = "", days: int = 7,
                    as_of: datetime | None = None) -> dict:
    """Combined catalyst verdict: news headlines + 8-K item footprint
    + 6-K press-release exhibits (foreign filers).

    The company's own filing outranks headlines: any rejected verdict
    (news or 6-K exhibit) fails the gate even if another headline
    looked material.
    """
    ticker = clean_ticker(ticker)
    news = _relevant(ticker_news(ticker, company_name, days, as_of=as_of),
                     ticker, company_name)
    verdict = classify_catalyst(news)
    eight_ks = eight_k_items(ticker, days, as_of=as_of)
    six_ks = six_k_exhibits(ticker, days, as_of=as_of)
    six_verdict = classify_catalyst(six_ks)
    material_8k = next((k for k in eight_ks if k["material"]), None)
    rejected = (verdict["verdict"] == "rejected"
                or six_verdict["verdict"] == "rejected")
    material = (verdict["verdict"] == "material"
                or six_verdict["verdict"] == "material"
                or bool(material_8k))
    return {"ticker": ticker.upper(),
            "news_verdict": verdict,
            "six_k_verdict": six_verdict,
            "headlines": news[:5],
            "eight_ks": eight_ks,
            "six_ks": [{"title": s["title"], "published": s["published"],
                        "verdict": six_verdict["verdict"]
                        if s["title"] == six_verdict.get("headline") else ""}
                       for s in six_ks],
            "corroborated": bool(material_8k) or material,
            "passes": (not rejected) and material}
