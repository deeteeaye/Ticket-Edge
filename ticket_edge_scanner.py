import os
import re
import json
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

CATALYSTS = {
    "ADDED_SHOW": [
        "added show",
        "second show",
        "new date",
    ],
    "PRESALE": [
        "presale",
        "pre-sale",
        "artist presale",
        "venue presale",
        "live nation presale",
        "vip package presale",
        "amex",
        "american express",
        "citi",
        "visa",
    ],
    "GENERAL_ONSALE": [
        "on sale",
        "onsale",
        "tickets on sale",
        "general sale",
    ],
    "REGISTRATION": [
        "register",
        "registration",
    ],
    "VENUE_UPGRADE": [
        "venue upgrade",
        "upgraded venue",
    ],
}


def api(path, method="GET", payload=None, extra_headers=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"

    headers = {
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "ticket-edge-intelligence/11.0",
    }

    if extra_headers:
        headers.update(extra_headers)

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            if not body:
                return None
            return json.loads(body)

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase HTTP {exc.code} on {method} {path}: {body}"
        ) from exc


def fetch(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "TicketEdge/11.0 public-event-discovery "
                "(contact: repository owner)"
            )
        },
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        return response.status, response.read().decode(
            "utf-8",
            errors="replace",
        )


def textify(html):
    html = re.sub(
        r"<script\b[^>]*>.*?</script>",
        " ",
        html,
        flags=re.I | re.S,
    )
    html = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        html,
        flags=re.I | re.S,
    )
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;", " ", html, flags=re.I)
    html = re.sub(r"&amp;", "&", html, flags=re.I)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def detect_catalysts(text):
    lower = text.lower()
    hits = []
    types = []

    for catalyst_type, phrases in CATALYSTS.items():
        matched = [phrase for phrase in phrases if phrase in lower]

        if matched:
            types.append(catalyst_type)
            hits.extend(matched)

    primary = types[0] if types else None

    return primary, sorted(set(hits))


def extract_title(text):
    patterns = [
        r"([A-Z][A-Za-z0-9 '&.\-]{2,80})\s+(?:Tickets|Presale|Concert)",
        r"(?:Tickets for|See)\s+([A-Z][A-Za-z0-9 '&.\-]{2,80})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            return match.group(1).strip()

    return None


def detect_card_type(text):
    lower = text.lower()
    cards = []

    if "american express" in lower or "amex" in lower:
        cards.append("AMEX")

    if "citi" in lower:
        cards.append("CITI")

    if "visa" in lower:
        cards.append("VISA")

    return ", ".join(cards) if cards else None


def metadata(text, source):
    catalyst_type, catalyst_hits = detect_catalysts(text)
    event_name = extract_title(text)

    confidence = 0

    if event_name:
        confidence += 25

    if catalyst_hits:
        confidence += 35

    if source.get("parser_profile") == "ticketmaster_event":
        confidence += 20

    if source.get("source_category") in (
        "ticketing_event",
        "venue",
    ):
        confidence += 10

    confidence = min(confidence, 100)

    return {
        "catalyst_type": catalyst_type,
        "catalyst_hits": catalyst_hits,
        "artist": event_name,
        "event_name": event_name,
        "venue": None,
        "city": None,
        "state": None,
        "schedule_text": None,
        "card_type": detect_card_type(text),
        "access_method": catalyst_type,
        "candidate_confidence": confidence,
    }


def patch_source(source_id, payload):
    return api(
        f"scanner_sources?id=eq.{source_id}",
        "PATCH",
        payload,
        {"Prefer": "return=minimal"},
    )


def upsert_candidate(payload):
    return api(
        "scanner_candidates"
        "?on_conflict=owner_user_id,fingerprint",
        "POST",
        payload,
        {
            "Prefer":
                "resolution=merge-duplicates,return=representation"
        },
    )


def run_maintenance():
    # RPC endpoints live under /rest/v1/rpc/<function>.
    return api(
        "rpc/ticket_edge_run_maintenance",
        "POST",
        {},
    )


def main():
    sources = api(
        "scanner_sources"
        "?enabled=eq.true"
        "&select=*"
        "&order=source_priority.desc"
    )

    print(f"enabled sources: {len(sources or [])}")

    for source in sources or []:
        now = datetime.now(timezone.utc).isoformat()

        try:
            status, raw = fetch(source["source_url"])
            text = textify(raw)

            digest = hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest()

            changed = (
                digest != source.get("last_content_hash")
            )

            info = metadata(text, source)

            patch = {
                "last_checked_at": now,
                "last_http_status": status,
                "last_content_hash": digest,
                "updated_at": now,
                "consecutive_failures": 0,
                "last_error": None,
            }

            if changed:
                patch["last_change_at"] = now

            patch_source(source["id"], patch)

            if changed and info["catalyst_hits"]:
                event_fp_source = "|".join(
                    [
                        (info.get("event_name") or "").lower(),
                        (info.get("venue") or "").lower(),
                        (info.get("city") or "").lower(),
                        (info.get("state") or "").lower(),
                    ]
                )

                event_fingerprint = hashlib.sha256(
                    event_fp_source.encode("utf-8")
                ).hexdigest()

                fingerprint = hashlib.sha256(
                    (
                        f"{source['id']}|"
                        f"{digest}|"
                        f"{event_fingerprint}"
                    ).encode("utf-8")
                ).hexdigest()

                candidate = {
                    "owner_user_id": source["owner_user_id"],
                    "source_id": source["id"],
                    "title": (
                        info["event_name"]
                        or f"{source['source_name']}: catalyst"
                    ),
                    "event_url": source["source_url"],
                    "raw_excerpt": text[:1500],
                    "fingerprint": fingerprint,
                    "status": "new",
                    "catalyst_type": info["catalyst_type"],
                    "catalyst_hits": info["catalyst_hits"],
                    "artist": info["artist"],
                    "event_name": info["event_name"],
                    "venue": info["venue"],
                    "city": info["city"],
                    "state": info["state"],
                    "access_method": info["access_method"],
                    "card_type": info["card_type"],
                    "candidate_confidence":
                        info["candidate_confidence"],
                    "event_fingerprint": event_fingerprint,
                    "normalized_status": "REVIEW",
                    "lifecycle_state": "UNSCHEDULED",
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "extracted_json": {
                        "schedule_text":
                            info["schedule_text"],
                        "scanner_version": "11.0.1",
                    },
                }

                upsert_candidate(candidate)

                print(
                    "candidate:",
                    source["source_name"],
                    "|",
                    info["catalyst_type"],
                    "| confidence=",
                    info["candidate_confidence"],
                )

            else:
                print(
                    "checked:",
                    source["source_name"],
                    "changed=",
                    changed,
                    "hits=",
                    len(info["catalyst_hits"]),
                )

        except Exception as exc:
            print(
                "ERROR:",
                source.get("source_name"),
                str(exc),
            )

            try:
                patch_source(
                    source["id"],
                    {
                        "last_checked_at": now,
                        "last_http_status": 0,
                        "updated_at": now,
                        "consecutive_failures":
                            int(
                                source.get(
                                    "consecutive_failures",
                                    0,
                                )
                                or 0
                            )
                            + 1,
                        "last_error": str(exc)[:1000],
                    },
                )
            except Exception as patch_exc:
                print(
                    "SOURCE ERROR PATCH FAILED:",
                    str(patch_exc),
                )

    maintenance = run_maintenance()
    print("maintenance:", maintenance)


if __name__ == "__main__":
    main()
