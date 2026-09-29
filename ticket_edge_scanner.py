#!/usr/bin/env python3
"""
Ticket Edge Build 12.0 — Hunter Mode

Purpose:
- Hunt broad public/official venue and promoter calendars.
- Extract real event-shaped records instead of keyword-only navigation noise.
- Automatically spawn same-domain event-detail sources.
- Score broker-relevant discovery signals without inventing resale economics.
- Keep direct ticketing-page blocks from blinding the discovery pipeline.
- Write run-level coverage telemetry to Supabase.

Zero paid data APIs. Python standard library only.
"""

import os
import re
import json
import hashlib
import urllib.request
import urllib.error
from urllib.parse import urljoin, urlparse, urldefrag, quote
from datetime import datetime, timezone
from html import unescape

SUPABASE_URL = os.environ["SUPABASE_URL"].strip().rstrip("/")
_raw_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

# Accept modern Supabase server secret keys even if a clipboard inserted stray
# whitespace around them. Fall back to a legacy JWT only when supplied.
_secret_match = re.search(r"sb_secret_[A-Za-z0-9_-]+", _raw_key)
if _secret_match:
    SUPABASE_KEY = _secret_match.group(0)
else:
    SUPABASE_KEY = _raw_key.strip()

USER_AGENT = (
    "TicketEdge/12.0 (+personal resale research; public-source event discovery; "
    "respectful low-frequency monitor)"
)

MONTH_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\b",
    re.I,
)
DATE_NUM_RE = re.compile(r"\b(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2})\b")

NOISE_TITLES = {
    "buy", "buy tickets", "get tickets", "tickets", "more info", "info",
    "calendar", "events", "event", "shows", "all shows", "all events",
    "load more", "load more events", "view details", "learn more", "home",
    "skip to content", "accessibility", "sign up", "newsletter", "search",
    "private events", "faq", "faqs", "contact", "menu", "visit",
}

NOISE_PATTERNS = [
    re.compile(r"^skip to content", re.I),
    re.compile(r"accessibility", re.I),
    re.compile(r"cookie preferences", re.I),
    re.compile(r"privacy notice", re.I),
    re.compile(r"terms (?:and|&) conditions", re.I),
    re.compile(r"sign up for", re.I),
    re.compile(r"^facebook$|^instagram$|^youtube$|^tiktok$", re.I),
]

CATALYST_PHRASES = {
    "ADDED_SHOW": ["added show", "second show", "new date", "added date"],
    "PRESALE": [
        "presale", "pre-sale", "artist presale", "venue presale",
        "cardmember", "amex", "american express", "citi", "visa",
    ],
    "REGISTRATION": ["register", "registration", "sign up for access"],
    "GENERAL_ONSALE": ["on sale", "onsale", "tickets on sale", "buy tickets", "get tickets"],
    "VENUE_UPGRADE": ["venue upgrade", "upgraded venue", "moved to", "moved from"],
    "JUST_ANNOUNCED": ["just announced", "newly announced", "coming soon"],
}

BROKER_SIGNAL_PHRASES = {
    "ONE_OFF": ["one night only", "one-night-only", "special show", "exclusive show"],
    "DEBUT": ["debut", "first u.s.", "first us", "u.s. debut", "us debut", "showcase"],
    "FINALE": ["tour finale", "final show", "hometown show", "homecoming"],
    "RELEASE": ["album release", "record release", "ep release", "release show"],
    "INTIMATE": ["intimate", "underplay", "small venue"],
}

SMALL_VENUE_HINTS = [
    "mercury lounge", "troubadour", "7th st entry", "the independent",
    "empty bottle", "schubas", "lincoln hall", "racket", "the sinclair",
    "music hall of williamsburg", "bowery ballroom", "webster hall",
    "the roxy", "el rey theatre", "fonda theatre", "great american music hall",
]

SOURCE_CHILD_LIMIT = 30
MAX_EVENTS_PER_SOURCE = 120
MAX_EXISTING_FPS = 1000


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def api(path, method="GET", payload=None, extra_headers=None):
    headers = {
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "ticket-edge-intelligence/12.0",
    }
    if extra_headers:
        headers.update(extra_headers)

    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase HTTP {exc.code} on {method} {path}: {body}") from exc


def fetch(url):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read(3_000_000).decode("utf-8", errors="replace")
        return response.status, response.geturl(), raw


def strip_tags(html):
    html = re.sub(r"(?is)<script\b[^>]*>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style\b[^>]*>.*?</style>", " ", html)
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = unescape(html)
    return re.sub(r"\s+", " ", html).strip()


def clean_title(value):
    value = unescape(str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n-|•:")
    value = re.sub(r"^(?:tickets? for|buy tickets? for|get tickets? for)\s+", "", value, flags=re.I)
    return value.strip()


def title_is_real(title):
    t = clean_title(title)
    low = t.lower()
    if len(t) < 3 or len(t) > 180:
        return False
    if low in NOISE_TITLES:
        return False
    if any(rx.search(t) for rx in NOISE_PATTERNS):
        return False
    if len(re.findall(r"[A-Za-z0-9]", t)) < 3:
        return False
    # Reject navigation chains that accidentally become one "title".
    if sum(word in low for word in ["accessibility", "privacy", "cookie", "newsletter", "menu"]) >= 1:
        return False
    return True


def normalize_url(base, href):
    if not href:
        return None
    href = unescape(href).strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    full = urljoin(base, href)
    full, _ = urldefrag(full)
    parsed = urlparse(full)
    if parsed.scheme not in ("http", "https"):
        return None
    return full


def same_domain(a, b):
    pa = urlparse(a).netloc.lower().removeprefix("www.")
    pb = urlparse(b).netloc.lower().removeprefix("www.")
    return pa == pb


def stable_hash(*parts):
    raw = "|".join(str(p or "").strip().lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def detect_catalysts(text):
    low = str(text or "").lower()
    types = []
    hits = []
    for ctype, phrases in CATALYST_PHRASES.items():
        matched = [p for p in phrases if p in low]
        if matched:
            types.append(ctype)
            hits.extend(matched)
    priority = ["REGISTRATION", "PRESALE", "ADDED_SHOW", "VENUE_UPGRADE", "JUST_ANNOUNCED", "GENERAL_ONSALE"]
    primary = next((x for x in priority if x in types), types[0] if types else None)
    return primary, sorted(set(hits))


def broker_signals(text):
    low = str(text or "").lower()
    hits = []
    for label, phrases in BROKER_SIGNAL_PHRASES.items():
        if any(p in low for p in phrases):
            hits.append(label)
    return hits


def detect_cards(text):
    low = str(text or "").lower()
    cards = []
    if "american express" in low or "amex" in low:
        cards.append("AMEX")
    if "citi" in low:
        cards.append("CITI")
    if re.search(r"\bvisa\b", low):
        cards.append("VISA")
    return ", ".join(cards) if cards else None


def extract_jsonld_events(html, base_url):
    events = []
    scripts = re.findall(
        r"(?is)<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html,
    )

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return

        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(str(x).lower().endswith("event") for x in types if x):
            location = node.get("location") or {}
            if isinstance(location, list):
                location = location[0] if location else {}
            address = location.get("address") if isinstance(location, dict) else {}
            if isinstance(address, str):
                address = {"streetAddress": address}
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            name = clean_title(node.get("name"))
            url = node.get("url") or (offers.get("url") if isinstance(offers, dict) else None)
            events.append({
                "name": name,
                "event_url": normalize_url(base_url, url) if url else None,
                "start_date": node.get("startDate"),
                "end_date": node.get("endDate"),
                "venue": clean_title(location.get("name")) if isinstance(location, dict) else None,
                "city": clean_title(address.get("addressLocality")) if isinstance(address, dict) else None,
                "state": clean_title(address.get("addressRegion")) if isinstance(address, dict) else None,
                "status_text": str(node.get("eventStatus") or ""),
                "description": strip_tags(str(node.get("description") or ""))[:1000],
                "extraction": "jsonld",
            })

        for key in ("@graph", "itemListElement", "mainEntity", "subjectOf"):
            if key in node:
                walk(node[key])

    for raw in scripts:
        try:
            walk(json.loads(unescape(raw).strip()))
        except Exception:
            continue
    return events


def extract_anchor_events(html, base_url):
    """Fallback when a calendar lacks JSON-LD.

    Capture link text plus a modest HTML window around the link. We intentionally
    require event-like context so generic Buy/Info/navigation links do not become
    candidates on their own.
    """
    out = []
    pattern = re.compile(
        r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>"
    )
    for m in pattern.finditer(html):
        href, anchor_html = m.group(1), m.group(2)
        anchor_text = clean_title(strip_tags(anchor_html))
        event_url = normalize_url(base_url, href)
        if not event_url:
            continue

        start = max(0, m.start() - 650)
        end = min(len(html), m.end() + 650)
        context = strip_tags(html[start:end])
        context_low = context.lower()

        # Candidate title: anchor itself if meaningful; otherwise infer the nearest
        # heading before the link inside this small event card/window.
        title = anchor_text if title_is_real(anchor_text) else ""
        if not title:
            before = html[start:m.start()]
            headings = re.findall(r"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>", before)
            if headings:
                guessed = clean_title(strip_tags(headings[-1]))
                if title_is_real(guessed):
                    title = guessed

        if not title:
            continue

        has_date = bool(MONTH_RE.search(context) or DATE_NUM_RE.search(context))
        has_ticket = bool(re.search(r"\b(?:buy|get) tickets?|presale|coming soon|more info|view details\b", context_low))
        has_event_word = bool(re.search(r"\b(?:doors|show|tour|concert|presents|venue|ages?|all ages)\b", context_low))
        if not (has_date and (has_ticket or has_event_word)):
            continue

        # Best-effort date text. Exact timestamp parsing is intentionally deferred
        # unless structured data supplies it; we do not invent dates.
        date_match = re.search(
            r"(?i)\b(?:mon|tue|wed|thu|fri|sat|sun)?\w*\s*,?\s*"
            r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
            r"\s+\d{1,2}(?:\s*,?\s*20\d{2})?",
            context,
        )

        out.append({
            "name": title,
            "event_url": event_url,
            "start_date": None,
            "end_date": None,
            "venue": None,
            "city": None,
            "state": None,
            "status_text": "",
            "description": context[:1200],
            "date_text": date_match.group(0) if date_match else None,
            "extraction": "anchor_context",
        })
    return out


def dedupe_events(events):
    seen = set()
    out = []
    for e in events:
        title = clean_title(e.get("name"))
        if not title_is_real(title):
            continue
        key = stable_hash(title, e.get("start_date") or e.get("date_text"), e.get("venue"), e.get("event_url"))
        if key in seen:
            continue
        seen.add(key)
        e["name"] = title
        out.append(e)
    return out[:MAX_EVENTS_PER_SOURCE]


def event_fingerprint(event, source):
    # Prefer stable event identity fields. The source region keeps same-name shows
    # in different markets separate when structured location data is absent.
    return stable_hash(
        event.get("name"),
        event.get("venue"),
        event.get("city"),
        event.get("state"),
        event.get("start_date") or event.get("date_text"),
        source.get("source_region"),
    )


def score_event(event, source, is_new):
    text = " ".join([
        event.get("name") or "",
        event.get("description") or "",
        event.get("status_text") or "",
        source.get("source_name") or "",
        source.get("parser_profile") or "",
    ])
    low = text.lower()
    catalyst, catalyst_hits = detect_catalysts(text)
    signals = broker_signals(text)
    cards = detect_cards(text)

    score = 18
    confidence = 50
    reasons = []

    if event.get("start_date") or event.get("date_text"):
        score += 8
        confidence += 10
        reasons.append("event date detected")
    if event.get("venue"):
        score += 7
        confidence += 8
        reasons.append("venue detected")
    if event.get("city"):
        confidence += 6
    if event.get("event_url"):
        confidence += 5
    if event.get("extraction") == "jsonld":
        score += 6
        confidence += 10
        reasons.append("structured Event data")
    if is_new:
        score += 12
        reasons.append("new to Ticket Edge")
    if source.get("parser_profile") == "just_announced" or "just announced" in low:
        score += 18
        reasons.append("just-announced source/signal")
        catalyst = catalyst or "JUST_ANNOUNCED"
        if "just announced" not in catalyst_hits:
            catalyst_hits.append("just announced")
    if "coming soon" in low:
        score += 12
        reasons.append("ticket access coming soon")
        catalyst = catalyst or "JUST_ANNOUNCED"
    if catalyst in ("PRESALE", "REGISTRATION"):
        score += 18
        reasons.append(catalyst.lower().replace("_", " "))
    elif catalyst in ("ADDED_SHOW", "VENUE_UPGRADE"):
        score += 16
        reasons.append(catalyst.lower().replace("_", " "))
    elif catalyst == "GENERAL_ONSALE":
        score += 4

    if cards:
        score += 8
        reasons.append(f"card access: {cards}")
    if signals:
        score += min(18, 7 * len(signals))
        reasons.extend(s.lower().replace("_", " ") for s in signals)

    venue_blob = " ".join([event.get("venue") or "", event.get("description") or "", event.get("name") or ""]).lower()
    if any(v in venue_blob for v in SMALL_VENUE_HINTS):
        score += 12
        reasons.append("small/intimate venue signal")

    # Penalize obvious routine/cancelled/rescheduled items. They may still be kept
    # if a stronger catalyst is present, but they do not float to the top.
    if "cancelled" in low or "canceled" in low:
        score -= 35
        reasons.append("cancelled")
    if "postponed" in low:
        score -= 20
        reasons.append("postponed")
    if "tribute" in low and not any(s in signals for s in ("ONE_OFF", "RELEASE")):
        score -= 12
        reasons.append("routine tribute penalty")

    score = max(0, min(100, int(round(score))))
    confidence = max(0, min(100, int(round(confidence))))
    return {
        "hunter_score": score,
        "confidence": confidence,
        "catalyst_type": catalyst or "DISCOVERY",
        "catalyst_hits": sorted(set(catalyst_hits)),
        "cards": cards,
        "signals": signals,
        "reason": "; ".join(dict.fromkeys(reasons)) or "new public event discovery",
    }


def patch_source(source_id, payload):
    return api(
        f"scanner_sources?id=eq.{source_id}",
        "PATCH",
        payload,
        {"Prefer": "return=minimal"},
    )


def existing_event_fingerprints(owner_user_id):
    path = (
        "scanner_candidates?owner_user_id=eq." + quote(str(owner_user_id), safe="")
        + "&select=event_fingerprint&event_fingerprint=not.is.null"
        + f"&limit={MAX_EXISTING_FPS}"
    )
    rows = api(path) or []
    return {r.get("event_fingerprint") for r in rows if r.get("event_fingerprint")}


def upsert_candidate(payload):
    # Existing schema already guarantees owner_user_id + fingerprint uniqueness.
    return api(
        "scanner_candidates?on_conflict=owner_user_id,fingerprint",
        "POST",
        payload,
        {"Prefer": "resolution=merge-duplicates,return=minimal"},
    )


def source_exists(owner_user_id, url):
    path = (
        "scanner_sources?owner_user_id=eq." + quote(str(owner_user_id), safe="")
        + "&source_url=eq." + quote(url, safe="")
        + "&select=id&limit=1"
    )
    rows = api(path) or []
    return rows[0]["id"] if rows else None


def add_child_source(parent, event):
    url = event.get("event_url")
    if not url or not same_domain(parent["source_url"], url):
        return False
    if source_exists(parent["owner_user_id"], url):
        return False

    payload = {
        "owner_user_id": parent["owner_user_id"],
        "source_name": f"{clean_title(event.get('name'))} — event detail"[:180],
        "source_url": url,
        "source_type": "official",
        "enabled": True,
        "source_priority": max(55, int(parent.get("source_priority") or 70) - 10),
        "source_category": "event_detail",
        "parser_profile": "event_detail",
        "source_region": parent.get("source_region"),
        "source_scope": "event",
        "discovery_enabled": False,
        "parent_source_id": parent["id"],
        "notes": "Build 12 auto-spawned from broad Hunter discovery source.",
    }
    api("scanner_sources", "POST", payload, {"Prefer": "return=minimal"})
    return True


def start_hunter_run(owner_user_id, enabled_count):
    rows = api(
        "hunter_runs",
        "POST",
        {
            "owner_user_id": owner_user_id,
            "status": "running",
            "sources_enabled": enabled_count,
        },
        {"Prefer": "return=representation"},
    )
    return rows[0]["id"] if rows else None


def finish_hunter_run(run_id, stats, status="success", notes=None):
    if not run_id:
        return
    payload = dict(stats)
    payload.update({"finished_at": utcnow(), "status": status, "notes": notes})
    api(
        f"hunter_runs?id=eq.{run_id}",
        "PATCH",
        payload,
        {"Prefer": "return=minimal"},
    )


def scan_discovery_source(source, html, existing_fps, now):
    events = dedupe_events(
        extract_jsonld_events(html, source["source_url"])
        + extract_anchor_events(html, source["source_url"])
    )

    stats = {
        "events_extracted": len(events),
        "events_new": 0,
        "candidates_written": 0,
        "events_rejected": 0,
        "child_sources_added": 0,
    }

    for idx, event in enumerate(events):
        fp = event_fingerprint(event, source)
        is_new = fp not in existing_fps
        if is_new:
            stats["events_new"] += 1

        scored = score_event(event, source, is_new)

        # Require a real-looking event and a meaningful Hunter score. A generic
        # onsale phrase alone can no longer create a 70-confidence candidate.
        if scored["hunter_score"] < 48:
            stats["events_rejected"] += 1
            continue

        candidate_fp = stable_hash(fp, scored["catalyst_type"])
        evidence = " ".join([
            clean_title(event.get("name")),
            event.get("date_text") or str(event.get("start_date") or ""),
            event.get("venue") or "",
            (event.get("description") or "")[:900],
        ]).strip()[:1500]

        payload = {
            "owner_user_id": source["owner_user_id"],
            "source_id": source["id"],
            "title": clean_title(event.get("name")),
            "event_url": event.get("event_url") or source["source_url"],
            "raw_excerpt": evidence,
            "fingerprint": candidate_fp,
            "status": "NEW",
            "catalyst_type": scored["catalyst_type"],
            "catalyst_hits": scored["catalyst_hits"],
            "artist": clean_title(event.get("name")),
            "event_name": clean_title(event.get("name")),
            "venue": event.get("venue"),
            "city": event.get("city"),
            "state": event.get("state"),
            "event_date": event.get("start_date"),
            "access_method": scored["catalyst_type"],
            "card_type": scored["cards"],
            "candidate_confidence": scored["confidence"],
            "event_fingerprint": fp,
            "normalized_status": "REVIEW",
            "lifecycle_state": "UPCOMING" if event.get("start_date") else "UNSCHEDULED",
            "first_seen_at": now,
            "last_seen_at": now,
            "hunter_score": scored["hunter_score"],
            "discovery_reason": scored["reason"],
            "source_evidence": evidence,
            "discovered_via": source.get("source_name"),
            "is_newly_announced": bool(is_new or source.get("parser_profile") == "just_announced"),
            "extracted_json": {
                "scanner_version": "12.0",
                "extraction": event.get("extraction"),
                "date_text": event.get("date_text"),
                "broker_signals": scored["signals"],
                "source_region": source.get("source_region"),
            },
        }
        upsert_candidate(payload)
        stats["candidates_written"] += 1
        existing_fps.add(fp)

        if idx < SOURCE_CHILD_LIMIT and add_child_source(source, event):
            stats["child_sources_added"] += 1

    patch_source(source["id"], {
        "last_discovery_count": len(events),
        "child_source_count": int(source.get("child_source_count") or 0) + stats["child_sources_added"],
        "last_candidate_at": now if stats["candidates_written"] else source.get("last_candidate_at"),
    })
    return stats


def scan_event_detail_source(source, html, text, changed, now):
    if not changed:
        return 0

    events = dedupe_events(extract_jsonld_events(html, source["source_url"]))
    event = events[0] if events else None
    catalyst, hits = detect_catalysts(text)
    if not catalyst or not hits:
        return 0

    if event:
        title = event.get("name")
        evidence = " ".join([title or "", event.get("description") or "", text[:700]])[:1500]
    else:
        title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = clean_title(strip_tags(title_match.group(1))) if title_match else source.get("source_name")
        evidence = text[:1500]

    if not title_is_real(title):
        return 0

    event = event or {"name": title, "venue": None, "city": None, "state": None, "start_date": None}
    fp = event_fingerprint(event, source)
    scored = score_event(event, source, False)
    scored["catalyst_type"] = catalyst
    scored["catalyst_hits"] = hits
    candidate_fp = stable_hash(fp, catalyst)

    upsert_candidate({
        "owner_user_id": source["owner_user_id"],
        "source_id": source["id"],
        "title": title,
        "event_url": source["source_url"],
        "raw_excerpt": evidence,
        "fingerprint": candidate_fp,
        "status": "NEW",
        "catalyst_type": catalyst,
        "catalyst_hits": hits,
        "artist": title,
        "event_name": title,
        "venue": event.get("venue"),
        "city": event.get("city"),
        "state": event.get("state"),
        "event_date": event.get("start_date"),
        "access_method": catalyst,
        "card_type": detect_cards(text),
        "candidate_confidence": scored["confidence"],
        "event_fingerprint": fp,
        "normalized_status": "REVIEW",
        "lifecycle_state": "UPCOMING" if event.get("start_date") else "UNSCHEDULED",
        "first_seen_at": now,
        "last_seen_at": now,
        "hunter_score": max(48, scored["hunter_score"]),
        "discovery_reason": f"event-detail page changed; {catalyst.lower().replace('_',' ')} detected",
        "source_evidence": evidence,
        "discovered_via": source.get("source_name"),
        "is_newly_announced": False,
        "extracted_json": {"scanner_version": "12.0", "extraction": event.get("extraction")},
    })
    patch_source(source["id"], {"last_candidate_at": now})
    return 1


def main():
    sources = api(
        "scanner_sources?enabled=eq.true"
        "&select=*"
        "&order=source_priority.desc"
    ) or []

    print(f"Hunter Mode enabled sources: {len(sources)}")
    if not sources:
        print("No enabled sources. Run Build 12 SQL seed first.")
        return

    owner_user_id = sources[0]["owner_user_id"]
    run_id = start_hunter_run(owner_user_id, len(sources))
    existing_fps = existing_event_fingerprints(owner_user_id)

    totals = {
        "sources_enabled": len(sources),
        "sources_scanned": 0,
        "sources_blocked": 0,
        "events_extracted": 0,
        "events_new": 0,
        "candidates_written": 0,
        "events_rejected": 0,
        "child_sources_added": 0,
    }

    fatal = None
    try:
        for source in sources:
            now = utcnow()
            try:
                status, final_url, html = fetch(source["source_url"])
                text = strip_tags(html)
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                changed = digest != (source.get("last_content_hash") or "")

                totals["sources_scanned"] += 1
                patch_source(source["id"], {
                    "last_checked_at": now,
                    "last_http_status": status,
                    "last_content_hash": digest,
                    "last_change_at": now if changed else source.get("last_change_at"),
                    "last_success_at": now,
                    "updated_at": now,
                    "consecutive_failures": 0,
                    "last_error": None,
                    "blocked": False,
                })

                if source.get("discovery_enabled"):
                    s = scan_discovery_source(source, html, existing_fps, now)
                    for key in ("events_extracted", "events_new", "candidates_written", "events_rejected", "child_sources_added"):
                        totals[key] += s[key]
                    print(
                        f"DISCOVERY {source['source_name']}: "
                        f"events={s['events_extracted']} new={s['events_new']} "
                        f"candidates={s['candidates_written']} rejected={s['events_rejected']} "
                        f"children={s['child_sources_added']}"
                    )
                else:
                    wrote = scan_event_detail_source(source, html, text, changed, now)
                    totals["candidates_written"] += wrote
                    print(f"DETAIL {source['source_name']}: changed={changed} candidates={wrote}")

            except urllib.error.HTTPError as exc:
                totals["sources_blocked"] += 1 if exc.code in (401, 403, 429) else 0
                blocked = exc.code in (401, 403, 429)
                msg = f"HTTP {exc.code}: {exc.reason}"
                print(f"SOURCE ERROR {source.get('source_name')}: {msg}")
                patch_source(source["id"], {
                    "last_checked_at": now,
                    "last_http_status": exc.code,
                    "updated_at": now,
                    "consecutive_failures": int(source.get("consecutive_failures") or 0) + 1,
                    "last_error": msg,
                    "blocked": blocked,
                })
            except Exception as exc:
                msg = str(exc)
                print(f"SOURCE ERROR {source.get('source_name')}: {msg}")
                try:
                    patch_source(source["id"], {
                        "last_checked_at": now,
                        "last_http_status": 0,
                        "updated_at": now,
                        "consecutive_failures": int(source.get("consecutive_failures") or 0) + 1,
                        "last_error": msg[:1000],
                    })
                except Exception:
                    pass

        # Preserve Build 10/11 lifecycle maintenance after Hunter discovery.
        maintenance = api("rpc/ticket_edge_run_maintenance", "POST", {})
        print("maintenance:", maintenance)

    except Exception as exc:
        fatal = str(exc)
        raise
    finally:
        finish_hunter_run(
            run_id,
            totals,
            status="error" if fatal else "success",
            notes=(fatal[:1200] if fatal else "Build 12 Hunter Mode completed. No resale economics were fabricated."),
        )

    print("HUNTER TOTALS:", json.dumps(totals, sort_keys=True))


if __name__ == "__main__":
    main()
