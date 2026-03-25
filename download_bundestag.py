#!/usr/bin/env python3
"""
Download Bundestag Plenarprotokolle PDFs (January 1, 2017 – December 31, 2025)
for a longitudinal corpus on German parliamentary discourse around domestic
lithium extraction, applying securitization theory framing analysis.

Primary data source: Bundestag DIP REST API (search.dip.bundestag.de)
Fallback: URL enumeration via dserver.bundestag.de + bundestag.de scraping
"""

import os
import sys
import json
import re
import time
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

API_KEY        = "rgsaY4U.oZRQKUHdJhF9qguHMkwCGn4BO7Ns8bh3GQ"
DIP_API_BASE   = "https://search.dip.bundestag.de/api/v1"
DSERVER_BASE   = "https://dserver.bundestag.de/btp"
BT_PROTO_URL   = "https://www.bundestag.de/protokolle"

CORPUS_DIR     = Path("bundestag_corpus")
INDEX_FILE     = CORPUS_DIR / "index.json"
FAILED_LOG     = CORPUS_DIR / "failed_downloads.log"
SUMMARY_FILE   = CORPUS_DIR / "download_summary.txt"

START_DATE     = date(2017, 1, 1)
END_DATE       = date(2025, 12, 31)
RATE_LIMIT     = 1.1          # seconds between requests
MAX_RETRIES    = 3
CORPUS_LIMIT   = 600          # pause and warn if this is exceeded

# Wahlperiode session ranges to enumerate in fallback mode.
#
# IMPORTANT: The actual dserver.bundestag.de URL format is
#   btp/{WP}/{WP}{SESSION:03d}.pdf   (3-digit zero-padded session number)
# NOT the 5-digit format shown in the task description.
# Verified empirically: btp/20/20001.pdf ✓  btp/19/19001.pdf ✓  btp/18/18001.pdf ✓
#
# WP 18: started Oct 2013, ended ~Sep 2017; sessions in Jan 2017 ≈ #211–245
#   → scan only 205–245 to avoid downloading 2014–2016 sessions
# WP 19: Oct 2017 – Oct 2021; all 245 sessions are within our target range
# WP 20: Dec 2021 – Dec 2025; sessions 1–219 confirmed; scan to 225 for safety
WP_SCAN_RANGES = {
    18: (205, 246),   # scan sessions 205–245 → covers Jan–Oct 2017
    19: (1,   246),   # scan sessions 1–245  → Oct 2017 – Oct 2021
    20: (1,   226),   # scan sessions 1–225  → Dec 2021 – Dec 2025
}

# Year estimation for WP18/19 sessions (Last-Modified dates are batch-update
# dates from 2020, not actual session dates, so we use known session calendars).
#
# WP 18 (Oct 2013 – Sep 2017): all sessions in scan range (205–245) are 2017.
# WP 19 approximate year breakpoints (based on known parliamentary calendar):
#   Sessions  1–  4  → 2017  (4 sessions, Oct–Dec 2017)
#   Sessions  5– 69  → 2018  (~65 sessions)
#   Sessions 70–139  → 2019  (~70 sessions)
#   Sessions140–208  → 2020  (~69 sessions)
#   Sessions209–245  → 2021  (~37 sessions, Jan–Oct 2021)
# WP 20: year taken from Last-Modified header (accurate within a few weeks,
#         since WP20 files were uploaded individually after each session).
WP19_YEAR_BREAKPOINTS = [
    (1,    4,   2017),
    (5,    69,  2018),
    (70,   139, 2019),
    (140,  208, 2020),
    (209,  245, 2021),
]

# ──────────────────────────────────────────────────────────────────────────────
# KEYWORD GROUPS
# Each entry is either a plain string (simple match) or a tuple
# (primary_term, [co-occurring_terms]) meaning the primary must co-occur
# with at least one of the listed terms in the same document.
# ──────────────────────────────────────────────────────────────────────────────

GROUP_A_SIMPLE = [
    "Lithium", "Lithiumabbau", "Lithiumgewinnung", "Lithiumvorkommen",
    "Batteriemetalle", "Batterierohstoffe",
]
GROUP_A_CONDITIONAL = {
    # term → must co-occur with at least one of
    "Kobalt": ["Batterie", "Rohstoff", "Lieferkette"],
    "Mangan": ["Batterie", "Rohstoff", "Lieferkette"],
    "Nickel": ["Batterie", "Rohstoff", "Lieferkette"],
}
GROUP_A_SPECIAL = ["Oberrheingraben"]      # also sets oberrheingraben_match

GROUP_B_SIMPLE = [
    "Versorgungssicherheit", "Rohstoffsicherheit",
    "Lieferkettensicherheit", "Lieferkettenabhängigkeit",
    "strategische Autonomie", "strategische Abhängigkeit",
    "Versorgungsengpass", "Versorgungsrisiko",
]
GROUP_B_CONDITIONAL = {
    "Abhängigkeit": ["Rohstoff", "Lieferkette", "China"],
}

GROUP_C_SIMPLE = [
    "kritische Rohstoffe", "heimische Rohstoffe",
    "Rohstoffstrategie", "Rohstoffpolitik",
    "Primärrohstoffe", "Sekundärrohstoffe",
    "EU-Rohstoffgesetz", "Critical Raw Materials Act", "CRMA",
    "Rohstoffwende",
]
GROUP_C_CONDITIONAL = {
    "Kreislaufwirtschaft": ["Rohstoff", "Batterie"],
}

GROUP_D_SIMPLE = [
    "Batteriezellfertigung", "Batterieproduktion",
    "Gigafactory", "Batteriewerk",
]
GROUP_D_CONDITIONAL = {
    "Energiewende":    ["Rohstoff", "Batterie"],
    "Elektromobilität": ["Rohstoff", "Batterie"],
    "Dekarbonisierung": ["Rohstoff"],
}

GROUP_E_SIMPLE = [
    "Ressourcennationalismus", "Wirtschaftssicherheit",
    "Technologiesouveränität", "Industriesouveränität",
]
GROUP_E_CONDITIONAL = {
    "China":        ["Rohstoff", "Abhängigkeit"],
    "geopolitisch": ["Rohstoff"],
}

# Group-B, C, D, E terms require co-occurrence with Group A or another group
# per the spec. We capture this by searching all groups and then applying
# the combined-group logic in filter_by_groups().

KEYWORD_GROUPS: Dict[str, dict] = {
    "A": {"simple": GROUP_A_SIMPLE, "conditional": GROUP_A_CONDITIONAL, "special": GROUP_A_SPECIAL},
    "B": {"simple": GROUP_B_SIMPLE, "conditional": GROUP_B_CONDITIONAL},
    "C": {"simple": GROUP_C_SIMPLE, "conditional": GROUP_C_CONDITIONAL},
    "D": {"simple": GROUP_D_SIMPLE, "conditional": GROUP_D_CONDITIONAL},
    "E": {"simple": GROUP_E_SIMPLE, "conditional": GROUP_E_CONDITIONAL},
}

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING SETUP
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(CORPUS_DIR / "download.log", encoding="utf-8"),
        ],
    )

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# HTTP SESSION (with retry)
# ──────────────────────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    s.headers.update({"User-Agent": "BundestagCorpusBot/1.0 (academic research)"})
    return s

SESSION = make_session()
_last_request = 0.0

def polite_get(url: str, **kwargs) -> requests.Response:
    """Rate-limited GET."""
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < RATE_LIMIT:
        time.sleep(RATE_LIMIT - elapsed)
    resp = SESSION.get(url, timeout=30, **kwargs)
    _last_request = time.monotonic()
    return resp

def polite_head(url: str, **kwargs) -> requests.Response:
    """Rate-limited HEAD."""
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < RATE_LIMIT:
        time.sleep(RATE_LIMIT - elapsed)
    resp = SESSION.head(url, timeout=15, **kwargs)
    _last_request = time.monotonic()
    return resp

# ──────────────────────────────────────────────────────────────────────────────
# DIP API  (primary data source)
# ──────────────────────────────────────────────────────────────────────────────

def dip_api_available() -> bool:
    """Quick probe to see if the DIP API accepts our key."""
    try:
        r = polite_get(
            f"{DIP_API_BASE}/plenarprotokoll",
            params={"apikey": API_KEY, "format": "json", "rows": 1},
        )
        if r.status_code == 200:
            data = r.json()
            return "documents" in data or "numFound" in data
        log.warning("DIP API probe returned HTTP %s – will try fallback.", r.status_code)
        return False
    except Exception as exc:
        log.warning("DIP API probe failed (%s) – will use fallback.", exc)
        return False


def dip_fetch_all_protocols() -> List[Dict]:
    """
    Retrieve ALL plenarprotokoll records in the target date range via the DIP API.
    Returns list of dicts with at minimum: wahlperiode, sitzungsnummer, datum, id.
    """
    records: List[Dict] = []
    cursor = "*"
    page = 0
    while True:
        params = {
            "apikey": API_KEY,
            "format": "json",
            "rows": 100,
            "cursor": cursor,
            "f.datum.start": START_DATE.isoformat(),
            "f.datum.end":   END_DATE.isoformat(),
        }
        try:
            r = polite_get(f"{DIP_API_BASE}/plenarprotokoll", params=params)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.error("DIP API fetch error on page %d: %s", page, exc)
            break

        docs = data.get("documents", [])
        if not docs:
            break

        records.extend(docs)
        log.info("  DIP API page %d: fetched %d records (total so far: %d)",
                 page, len(docs), len(records))

        next_cursor = data.get("cursor", {}).get("next")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        page += 1

    log.info("DIP API: retrieved %d plenarprotokoll records total.", len(records))
    return records


def dip_search_keyword(term: str) -> List[str]:
    """
    Search DIP API for sessions containing a specific term.
    Returns list of document IDs (e.g. '19-045').
    """
    ids: List[str] = []
    cursor = "*"
    while True:
        params = {
            "apikey": API_KEY,
            "format": "json",
            "rows": 100,
            "cursor": cursor,
            "q": term,
            "f.datum.start": START_DATE.isoformat(),
            "f.datum.end":   END_DATE.isoformat(),
        }
        try:
            r = polite_get(f"{DIP_API_BASE}/plenarprotokoll", params=params)
            if r.status_code != 200:
                break
            data = r.json()
        except Exception:
            break

        for doc in data.get("documents", []):
            ids.append(doc.get("id", ""))

        nc = data.get("cursor", {}).get("next")
        if not nc or nc == cursor:
            break
        cursor = nc

    return ids


# ──────────────────────────────────────────────────────────────────────────────
# FALLBACK: enumerate sessions directly
# ──────────────────────────────────────────────────────────────────────────────

def pdf_url(wp: int, session: int) -> str:
    # Actual Bundestag dserver format: {WP}{SESSION:03d}.pdf
    # e.g. WP19 s100 → btp/19/19100.pdf   WP20 s1 → btp/20/20001.pdf
    return f"{DSERVER_BASE}/{wp}/{wp}{session:03d}.pdf"


def estimate_year(wp: int, session: int, lm_date: Optional[date] = None) -> Optional[int]:
    """
    Estimate the calendar year of a plenary session.

    WP 18 scan range (205–245) is all 2017 by construction.
    WP 19 uses hardcoded breakpoints (Last-Modified is a batch date, unusable).
    WP 20 uses the Last-Modified header year (files uploaded per session).
    """
    if wp == 18:
        return 2017
    if wp == 19:
        for lo, hi, yr in WP19_YEAR_BREAKPOINTS:
            if lo <= session <= hi:
                return yr
        return None
    if wp == 20:
        if lm_date is not None:
            return lm_date.year
        return None
    return None


def probe_session(wp: int, session: int) -> Tuple[bool, Optional[int], Optional[date]]:
    """
    Check whether a session PDF exists.
    Returns (exists, estimated_year, lm_date_or_None).
    For WP20, lm_date is the Last-Modified value (reasonable proxy for session date).
    """
    url = pdf_url(wp, session)
    try:
        r = polite_head(url, allow_redirects=True)
        if r.status_code != 200:
            return False, None, None
        lm_date = None
        lm_hdr = r.headers.get("Last-Modified") or r.headers.get("last-modified")
        if lm_hdr:
            try:
                lm_date = datetime.strptime(lm_hdr, "%a, %d %b %Y %H:%M:%S %Z").date()
            except ValueError:
                pass
        year = estimate_year(wp, session, lm_date)
        return True, year, lm_date
    except Exception as exc:
        log.debug("Head %s failed: %s", url, exc)
        return False, None, None


def enumerate_sessions() -> List[Dict]:
    """
    Scan all candidate WP/session combinations, probe for existence,
    and return metadata dicts for sessions in our date range.
    """
    found: List[Dict] = []
    for wp, (lo, hi) in WP_SCAN_RANGES.items():
        log.info("Scanning WP %d sessions %d–%d …", wp, lo, hi - 1)
        consecutive_misses = 0
        wp_found_count = 0
        for sess in range(lo, hi):
            exists, year, lm_date = probe_session(wp, sess)
            if not exists:
                consecutive_misses += 1
                # Keep trying until we've found at least one session;
                # once sessions are found, stop after 10 consecutive misses.
                if consecutive_misses >= 10 and wp_found_count > 0:
                    log.info("  WP %d: 10 consecutive misses after session %d – stopping scan.",
                             wp, sess)
                    break
                continue
            consecutive_misses = 0
            wp_found_count += 1

            # For WP20, use Last-Modified as approximate session date.
            # For WP18/19, date is unknown (batch-updated server; use year estimate only).
            if wp == 20 and lm_date:
                date_str = lm_date.isoformat()
            else:
                date_str = None   # date unknown; year estimated from session number

            rec = {
                "wahlperiode": wp,
                "session": sess,
                "date": date_str,
                "year": year,
                "id": f"{wp}_{sess:03d}",
                "url": pdf_url(wp, sess),
                # Keyword fields – populated later if API search is available
                "matched_terms": [],
                "matched_groups": [],
                "primary_match": False,
                "oberrheingraben_match": False,
                "keyword_filtered": False,   # marks that filtering was NOT applied
            }
            found.append(rec)
            log.info("  WP %d session %03d  year=%-4s  [found]",
                     wp, sess, str(year) if year else "?")

    return found


# ──────────────────────────────────────────────────────────────────────────────
# KEYWORD MATCHING  (against API-provided text/metadata)
# ──────────────────────────────────────────────────────────────────────────────

def _ci_find(text: str, term: str) -> Optional[str]:
    """Case-insensitive search. Returns the matched form if found, else None."""
    pattern = re.compile(re.escape(term), re.IGNORECASE)
    m = pattern.search(text)
    return m.group(0) if m else None


def match_keywords(text: str) -> Tuple[List[str], List[str], bool, bool]:
    """
    Run all keyword groups against text.
    Returns (matched_terms, matched_groups, primary_match, oberrheingraben_match).

    Group inclusion logic (per spec):
    - A: include if any A term appears
    - B: include if any B term appears AND (any A or any C term also appears)
    - C: include if any C term appears AND (any A or any B term also appears)
    - D: include if any D term appears AND (any A or any B term also appears)
    - E: include if any E term appears AND (any A or any B term also appears)
    """
    matched: Dict[str, List[str]] = {g: [] for g in "ABCDE"}

    def _check_simple(group: str, terms: List[str]) -> None:
        for term in terms:
            found = _ci_find(text, term)
            if found:
                matched[group].append(found)

    def _check_conditional(group: str, cond: Dict[str, List[str]]) -> None:
        for primary, co_terms in cond.items():
            if not _ci_find(text, primary):
                continue
            for co in co_terms:
                if _ci_find(text, co):
                    matched[group].append(primary)
                    break

    # A
    _check_simple("A", GROUP_A_SIMPLE)
    _check_simple("A", GROUP_A_SPECIAL)
    _check_conditional("A", GROUP_A_CONDITIONAL)

    # B
    _check_simple("B", GROUP_B_SIMPLE)
    _check_conditional("B", GROUP_B_CONDITIONAL)

    # C
    _check_simple("C", GROUP_C_SIMPLE)
    _check_conditional("C", GROUP_C_CONDITIONAL)

    # D
    _check_simple("D", GROUP_D_SIMPLE)
    _check_conditional("D", GROUP_D_CONDITIONAL)

    # E
    _check_simple("E", GROUP_E_SIMPLE)
    _check_conditional("E", GROUP_E_CONDITIONAL)

    # Apply group-level cross-reference logic
    has_a = bool(matched["A"])
    has_b = bool(matched["B"])
    has_c = bool(matched["C"])

    active_groups: List[str] = []
    if has_a:
        active_groups.append("A")
    if has_b and (has_a or has_c):
        active_groups.append("B")
    if has_c and (has_a or has_b):
        active_groups.append("C")
    if matched["D"] and (has_a or has_b):
        active_groups.append("D")
    if matched["E"] and (has_a or has_b):
        active_groups.append("E")

    all_terms: List[str] = []
    for g in active_groups:
        all_terms.extend(matched[g])
    all_terms = sorted(set(all_terms))

    primary_match = has_a
    oberrheingraben = any(
        _ci_find(text, "Oberrheingraben") for _ in [1]
    )

    return all_terms, active_groups, primary_match, bool(oberrheingraben)


def is_relevant(matched_terms: List[str], matched_groups: List[str]) -> bool:
    """A session is included if it has any matched group."""
    return len(matched_groups) > 0


# ──────────────────────────────────────────────────────────────────────────────
# PDF DOWNLOAD
# ──────────────────────────────────────────────────────────────────────────────

def download_pdf(
    url: str,
    dest: Path,
    wp: int,
    session: int,
) -> bool:
    """
    Download a PDF to dest. Retries up to MAX_RETRIES times.
    Returns True on success.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 10_000:
        log.info("  Already exists: %s", dest.name)
        return True

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            elapsed = time.monotonic() - _last_request
            if elapsed < RATE_LIMIT:
                time.sleep(RATE_LIMIT - elapsed)
            r = SESSION.get(url, timeout=120, stream=True)
            globals()["_last_request"] = time.monotonic()

            if r.status_code == 404:
                log.warning("  404 – not found: %s", url)
                return False
            r.raise_for_status()

            with open(dest, "wb") as fh:
                for chunk in r.iter_content(chunk_size=65536):
                    fh.write(chunk)

            size_kb = dest.stat().st_size // 1024
            log.info("  Downloaded %s  (%d KB)", dest.name, size_kb)
            return True

        except Exception as exc:
            log.warning("  Attempt %d/%d failed for %s: %s",
                        attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    return False


def log_failure(wp: int, session: int, url: str, reason: str) -> None:
    with open(FAILED_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.utcnow().isoformat()}Z  WP{wp} s{session:03d}  {url}  {reason}\n")


# ──────────────────────────────────────────────────────────────────────────────
# SCRAPE FALLBACK  (bundestag.de/protokolle HTML listing)
# ──────────────────────────────────────────────────────────────────────────────

def try_scrape_session_list() -> List[Dict]:
    """
    Attempt to scrape session metadata from bundestag.de.
    Returns a list of partial metadata dicts (wp, session, date, url)
    or an empty list on failure.
    """
    log.info("Attempting to scrape bundestag.de/protokolle …")
    sessions: List[Dict] = []

    try:
        r = polite_get(BT_PROTO_URL)
        html = r.text
    except Exception as exc:
        log.warning("Could not fetch %s: %s", BT_PROTO_URL, exc)
        return sessions

    # Look for direct links to PDFs in the HTML
    # Pattern: href="https://dserver.bundestag.de/btp/NN/NNNNNNN.pdf"
    pdf_pattern = re.compile(
        r'href="(https://dserver\.bundestag\.de/btp/(\d+)/(\d{2})(\d{5})\.pdf)"',
        re.IGNORECASE,
    )
    for m in pdf_pattern.finditer(html):
        url_found, wp_str, wp2_str, sess_str = m.group(1), m.group(2), m.group(3), m.group(4)
        wp = int(wp_str)
        sess = int(sess_str)
        if wp in (18, 19, 20):
            sessions.append({
                "wahlperiode": wp,
                "session": sess,
                "url": url_found,
                "id": f"{wp}_{sess:03d}",
                "date": None,
                "year": None,
            })

    # Also look for date annotations near PDFs
    date_pattern = re.compile(r'(\d{2})\.\s*(\d{2})\.\s*(20\d{2})')

    if sessions:
        log.info("Scraped %d session links from bundestag.de", len(sessions))
    else:
        log.warning("No direct PDF links found in bundestag.de HTML.")

    return sessions


# ──────────────────────────────────────────────────────────────────────────────
# API-BASED KEYWORD SEARCH PATH
# ──────────────────────────────────────────────────────────────────────────────

def build_relevant_session_set_via_api(all_records: List[Dict]) -> List[Dict]:
    """
    Given all DIP API records in the date range, enrich each record with
    keyword matches against available metadata text and filter to relevant only.
    """
    relevant: List[Dict] = []
    for rec in all_records:
        # Build a searchable text blob from API metadata fields
        text_parts = [
            rec.get("titel", ""),
            rec.get("abstract", ""),
            " ".join(rec.get("dokumentart", [])),
            # Some records include Vorgänge (agenda items)
            " ".join(
                v.get("titel", "") for v in rec.get("vorgaenge", [])
                if isinstance(v, dict)
            ),
        ]
        full_text = " ".join(filter(None, text_parts))

        terms, groups, primary, oberrheingraben = match_keywords(full_text)
        if not is_relevant(terms, groups):
            continue

        # Parse date
        datum_str = rec.get("datum") or rec.get("date") or ""
        sdate = None
        if datum_str:
            for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                try:
                    sdate = datetime.strptime(datum_str[:10], fmt).date()
                    break
                except ValueError:
                    pass

        wp = int(rec.get("wahlperiode", 0))
        sess_raw = rec.get("sitzungsnummer") or rec.get("nummer") or 0
        try:
            sess = int(sess_raw)
        except (ValueError, TypeError):
            sess = 0

        doc_id = f"{wp}_{sess:03d}"
        year = sdate.year if sdate else None

        relevant.append({
            "id": doc_id,
            "wahlperiode": wp,
            "session": sess,
            "date": sdate.isoformat() if sdate else None,
            "year": year,
            "url": pdf_url(wp, sess),
            "matched_terms": terms,
            "matched_groups": groups,
            "primary_match": primary,
            "oberrheingraben_match": oberrheingraben,
            "keyword_filtered": True,
        })

    log.info("API path: %d / %d sessions are relevant.", len(relevant), len(all_records))
    return relevant


# ──────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATION
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 70)
    log.info("Bundestag Plenarprotokolle Downloader")
    log.info("Corpus range: %s to %s", START_DATE, END_DATE)
    log.info("Output dir: %s", CORPUS_DIR.resolve())
    log.info("=" * 70)

    # ── Step 1: Determine session list ──────────────────────────────────────
    api_ok = dip_api_available()

    if api_ok:
        log.info("DIP API is available – fetching full protocol list via API.")
        all_records = dip_fetch_all_protocols()
        candidates = build_relevant_session_set_via_api(all_records)
        log.info("Keyword-filtered candidates: %d", len(candidates))
    else:
        log.warning("DIP API unavailable – activating fallback enumeration.")
        log.warning(
            "NOTE: Keyword filtering cannot be applied without API text access. "
            "All sessions in the date range will be downloaded. "
            "Manual keyword filtering on the PDFs will be required."
        )

        # Try scraping first (may yield a partial list quickly)
        scraped = try_scrape_session_list()

        # Always run enumeration to ensure completeness
        log.info("Running full session enumeration (HEAD probing) …")
        enumerated = enumerate_sessions()

        # Merge: prefer enumerated (has date probed via Last-Modified)
        seen_ids: set = set()
        candidates = []
        for rec in enumerated + scraped:
            rid = f"{rec['wahlperiode']}_{rec['session']:03d}"
            if rid not in seen_ids:
                seen_ids.add(rid)
                if "matched_terms" not in rec:
                    rec.update({
                        "matched_terms": [],
                        "matched_groups": [],
                        "primary_match": False,
                        "oberrheingraben_match": False,
                        "keyword_filtered": False,
                    })
                candidates.append(rec)

    # Safety check
    if len(candidates) > CORPUS_LIMIT:
        log.error(
            "SAFETY STOP: %d candidates exceed the limit of %d. "
            "The relevance filter may be too loose (or API filtering was bypassed). "
            "Please inspect the candidate list before proceeding.",
            len(candidates), CORPUS_LIMIT,
        )
        print("\n⚠  STOPPING: candidate count (%d) exceeds limit (%d)." % (len(candidates), CORPUS_LIMIT))
        print("   Check bundestag_corpus/download.log for details.")
        sys.exit(1)

    log.info("Proceeding to download %d PDFs.", len(candidates))

    # ── Step 2: Download PDFs ───────────────────────────────────────────────
    index_entries: List[Dict] = []
    year_counts: Dict[int, int] = {}
    group_counts: Dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0}
    failed: List[Dict] = []
    downloaded = 0
    skipped = 0

    for rec in candidates:
        wp      = rec["wahlperiode"]
        sess    = rec["session"]
        sdate   = rec.get("date")
        year    = rec.get("year")
        url     = rec.get("url") or pdf_url(wp, sess)
        doc_id  = rec.get("id") or f"{wp}_{sess:03d}"

        # If year is unknown, put in an 'unknown' subfolder
        year_str = str(year) if year else "unknown"
        filename = f"{wp}_{sess:03d}_{sdate or 'unknown'}.pdf"
        dest = CORPUS_DIR / year_str / filename

        success = download_pdf(url, dest, wp, sess)

        if not success:
            log_failure(wp, sess, url, "download failed after retries")
            failed.append(rec)
            skipped += 1
            continue

        downloaded += 1
        if year:
            year_counts[year] = year_counts.get(year, 0) + 1

        for g in rec.get("matched_groups", []):
            if g in group_counts:
                group_counts[g] += 1

        entry = {
            "id": doc_id,
            "wahlperiode": wp,
            "session": sess,
            "date": sdate,
            "year": year,
            "filename": f"{year_str}/{filename}",
            "matched_terms": rec.get("matched_terms", []),
            "matched_groups": rec.get("matched_groups", []),
            "primary_match": rec.get("primary_match", False),
            "oberrheingraben_match": rec.get("oberrheingraben_match", False),
            "keyword_filtered": rec.get("keyword_filtered", False),
        }
        index_entries.append(entry)

    # ── Step 3: Write index.json ────────────────────────────────────────────
    index_entries.sort(key=lambda e: (e["date"] or "9999", e["wahlperiode"], e["session"]))
    with open(INDEX_FILE, "w", encoding="utf-8") as fh:
        json.dump(index_entries, fh, ensure_ascii=False, indent=2)
    log.info("Wrote index.json with %d entries.", len(index_entries))

    # ── Step 4: Summary ─────────────────────────────────────────────────────
    summary_lines = [
        "=" * 70,
        "BUNDESTAG CORPUS DOWNLOAD SUMMARY",
        f"Run date: {datetime.utcnow().isoformat()}Z",
        "=" * 70,
        f"Total sessions checked (candidates):   {len(candidates)}",
        f"Successfully downloaded:               {downloaded}",
        f"Failed downloads:                      {skipped}",
        "",
        "Per-year breakdown:",
    ]
    for y in sorted(year_counts):
        summary_lines.append(f"  {y}: {year_counts[y]} PDF(s)")

    summary_lines += [
        "",
        "Matched group distribution (sessions with each group):",
    ]
    for g in "ABCDE":
        summary_lines.append(f"  Group {g}: {group_counts[g]}")

    if failed:
        summary_lines += ["", "Failed downloads:"]
        for f_rec in failed:
            summary_lines.append(
                f"  WP{f_rec['wahlperiode']} s{f_rec['session']:03d}  {f_rec.get('url', '')}"
            )

    if not api_ok:
        summary_lines += [
            "",
            "⚠  WARNING: DIP API was unavailable. Keyword filtering was NOT applied.",
            "   All sessions in the date range were downloaded.",
            "   matched_terms and matched_groups are empty for all entries.",
            "   A second-pass keyword filter should be run after PDF text extraction.",
        ]

    summary_text = "\n".join(summary_lines)
    print("\n" + summary_text + "\n")
    with open(SUMMARY_FILE, "w", encoding="utf-8") as fh:
        fh.write(summary_text + "\n")
    log.info("Summary written to %s", SUMMARY_FILE)


if __name__ == "__main__":
    main()
