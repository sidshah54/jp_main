#!/usr/bin/env python3
"""
Second-pass keyword filter for the Bundestag Plenarprotokolle corpus.

Reads bundestag_corpus/index.json, extracts text from each PDF, applies the
keyword-group matching logic, and writes the enriched index back to disk.

Requires:  pip install pypdf

Usage:
    python3 filter_bundestag.py [--refilter]

    --refilter   Re-run even on entries already marked keyword_filtered=True.
                 Default: skip entries where keyword_filtered is already True.
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import pypdf
except ImportError:
    sys.exit(
        "pypdf is required.\n"
        "Install it with:  pip install pypdf\n"
    )

# ──────────────────────────────────────────────────────────────────────────────
# PATHS
# ──────────────────────────────────────────────────────────────────────────────

CORPUS_DIR   = Path("bundestag_corpus")
INDEX_FILE   = CORPUS_DIR / "index.json"
FILTER_LOG   = CORPUS_DIR / "filter.log"
FILTER_SUMMARY = CORPUS_DIR / "filter_summary.txt"

# ──────────────────────────────────────────────────────────────────────────────
# KEYWORD GROUPS  (mirrors download_bundestag.py exactly)
# ──────────────────────────────────────────────────────────────────────────────

GROUP_A_SIMPLE = [
    "Lithium", "Lithiumabbau", "Lithiumgewinnung", "Lithiumvorkommen",
    "Batteriemetalle", "Batterierohstoffe",
]
GROUP_A_CONDITIONAL: Dict[str, List[str]] = {
    "Kobalt": ["Batterie", "Rohstoff", "Lieferkette"],
    "Mangan": ["Batterie", "Rohstoff", "Lieferkette"],
    "Nickel": ["Batterie", "Rohstoff", "Lieferkette"],
}
GROUP_A_SPECIAL = ["Oberrheingraben"]

GROUP_B_SIMPLE = [
    "Versorgungssicherheit", "Rohstoffsicherheit",
    "Lieferkettensicherheit", "Lieferkettenabhängigkeit",
    "strategische Autonomie", "strategische Abhängigkeit",
    "Versorgungsengpass", "Versorgungsrisiko",
]
GROUP_B_CONDITIONAL: Dict[str, List[str]] = {
    "Abhängigkeit": ["Rohstoff", "Lieferkette", "China"],
}

GROUP_C_SIMPLE = [
    "kritische Rohstoffe", "heimische Rohstoffe",
    "Rohstoffstrategie", "Rohstoffpolitik",
    "Primärrohstoffe", "Sekundärrohstoffe",
    "EU-Rohstoffgesetz", "Critical Raw Materials Act", "CRMA",
    "Rohstoffwende",
]
GROUP_C_CONDITIONAL: Dict[str, List[str]] = {
    "Kreislaufwirtschaft": ["Rohstoff", "Batterie"],
}

GROUP_D_SIMPLE = [
    "Batteriezellfertigung", "Batterieproduktion",
    "Gigafactory", "Batteriewerk",
]
GROUP_D_CONDITIONAL: Dict[str, List[str]] = {
    "Energiewende":     ["Rohstoff", "Batterie"],
    "Elektromobilität": ["Rohstoff", "Batterie"],
    "Dekarbonisierung": ["Rohstoff"],
}

GROUP_E_SIMPLE = [
    "Ressourcennationalismus", "Wirtschaftssicherheit",
    "Technologiesouveränität", "Industriesouveränität",
]
GROUP_E_CONDITIONAL: Dict[str, List[str]] = {
    "China":        ["Rohstoff", "Abhängigkeit"],
    "geopolitisch": ["Rohstoff"],
}

# ──────────────────────────────────────────────────────────────────────────────
# KEYWORD MATCHING
# ──────────────────────────────────────────────────────────────────────────────

import re as _re


def _ci_find(text: str, term: str) -> Optional[str]:
    """Case-insensitive search; returns matched form or None."""
    m = _re.compile(_re.escape(term), _re.IGNORECASE).search(text)
    return m.group(0) if m else None


def match_keywords(text: str) -> Tuple[List[str], List[str], bool, bool]:
    """
    Run all keyword groups against text.
    Returns (matched_terms, matched_groups, primary_match, oberrheingraben_match).

    Group inclusion logic:
    - A: any A term
    - B: any B term AND (any A or any C)
    - C: any C term AND (any A or any B)
    - D: any D term AND (any A or any B)
    - E: any E term AND (any A or any B)
    """
    matched: Dict[str, List[str]] = {g: [] for g in "ABCDE"}

    def _simple(group: str, terms: List[str]) -> None:
        for term in terms:
            if _ci_find(text, term):
                matched[group].append(term)

    def _conditional(group: str, cond: Dict[str, List[str]]) -> None:
        for primary, co_terms in cond.items():
            if not _ci_find(text, primary):
                continue
            if any(_ci_find(text, co) for co in co_terms):
                matched[group].append(primary)

    _simple("A", GROUP_A_SIMPLE)
    _simple("A", GROUP_A_SPECIAL)
    _conditional("A", GROUP_A_CONDITIONAL)

    _simple("B", GROUP_B_SIMPLE)
    _conditional("B", GROUP_B_CONDITIONAL)

    _simple("C", GROUP_C_SIMPLE)
    _conditional("C", GROUP_C_CONDITIONAL)

    _simple("D", GROUP_D_SIMPLE)
    _conditional("D", GROUP_D_CONDITIONAL)

    _simple("E", GROUP_E_SIMPLE)
    _conditional("E", GROUP_E_CONDITIONAL)

    has_a = bool(matched["A"])
    has_b = bool(matched["B"])
    has_c = bool(matched["C"])

    active: List[str] = []
    if has_a:
        active.append("A")
    if has_b and (has_a or has_c):
        active.append("B")
    if has_c and (has_a or has_b):
        active.append("C")
    if matched["D"] and (has_a or has_b):
        active.append("D")
    if matched["E"] and (has_a or has_b):
        active.append("E")

    all_terms = sorted(set(t for g in active for t in matched[g]))
    oberrheingraben = bool(_ci_find(text, "Oberrheingraben"))
    return all_terms, active, has_a, oberrheingraben


# ──────────────────────────────────────────────────────────────────────────────
# PDF TEXT EXTRACTION
# ──────────────────────────────────────────────────────────────────────────────

def extract_pdf_text(pdf_path: Path) -> str:
    """
    Extract all text from a PDF using pypdf.
    Returns an empty string on failure.
    """
    try:
        reader = pypdf.PdfReader(str(pdf_path))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                pass
        return "\n".join(parts)
    except Exception as exc:
        log.warning("  Text extraction failed for %s: %s", pdf_path.name, exc)
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(FILTER_LOG, encoding="utf-8"),
        ],
    )


log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refilter", action="store_true",
                        help="Re-run keyword filter on all entries, even already-filtered ones.")
    args = parser.parse_args()

    setup_logging()

    if not INDEX_FILE.exists():
        sys.exit(f"index.json not found at {INDEX_FILE}. Run download_bundestag.py first.")

    with open(INDEX_FILE, encoding="utf-8") as fh:
        index: List[Dict] = json.load(fh)

    total = len(index)
    log.info("Loaded %d entries from index.json", total)

    to_process = [
        e for e in index
        if args.refilter or not e.get("keyword_filtered", False)
    ]
    already_done = total - len(to_process)
    log.info("%d entries to filter  (%d already done, use --refilter to redo)",
             len(to_process), already_done)

    if not to_process:
        log.info("Nothing to do.")
        return

    # Build a lookup: id → entry (for in-place update)
    idx_by_id = {e["id"]: e for e in index}

    matched_count = 0
    failed_text = 0
    processed = 0

    for entry in to_process:
        eid = entry["id"]
        filename = entry.get("filename", "")
        pdf_path = CORPUS_DIR / filename if filename else None

        if pdf_path is None or not pdf_path.exists():
            log.warning("PDF not found for %s (expected: %s) – skipping", eid, pdf_path)
            # Mark as attempted so we don't retry missing files indefinitely
            idx_by_id[eid].update({
                "keyword_filtered": True,
                "matched_terms": [],
                "matched_groups": [],
                "primary_match": False,
                "oberrheingraben_match": False,
                "pdf_missing": True,
            })
            continue

        processed += 1
        log.info("[%d/%d] %s  %s", processed, len(to_process), eid, pdf_path.name)

        text = extract_pdf_text(pdf_path)
        if not text.strip():
            log.warning("  No text extracted from %s", pdf_path.name)
            failed_text += 1
            idx_by_id[eid].update({
                "keyword_filtered": True,
                "matched_terms": [],
                "matched_groups": [],
                "primary_match": False,
                "oberrheingraben_match": False,
            })
            continue

        terms, groups, primary, oberrheingraben = match_keywords(text)

        idx_by_id[eid].update({
            "keyword_filtered": True,
            "matched_terms": terms,
            "matched_groups": groups,
            "primary_match": primary,
            "oberrheingraben_match": oberrheingraben,
        })

        if groups:
            matched_count += 1
            log.info("  MATCH  groups=%s  terms=%s", groups, terms)
        else:
            log.info("  no match")

        # Checkpoint: write index every 50 entries so progress is not lost
        if processed % 50 == 0:
            _write_index(index)
            log.info("  [checkpoint] index.json saved (%d/%d done)", processed, len(to_process))

    # Final write
    _write_index(index)
    log.info("Final index.json written.")

    # ── Summary ──────────────────────────────────────────────────────────────
    relevant = [e for e in index if e.get("matched_groups")]
    group_counts = {g: 0 for g in "ABCDE"}
    for e in relevant:
        for g in e.get("matched_groups", []):
            if g in group_counts:
                group_counts[g] += 1

    primary_count   = sum(1 for e in relevant if e.get("primary_match"))
    oberrhein_count = sum(1 for e in relevant if e.get("oberrheingraben_match"))

    lines = [
        "=" * 70,
        "BUNDESTAG CORPUS KEYWORD FILTER SUMMARY",
        f"Run date: {datetime.utcnow().isoformat()}Z",
        "=" * 70,
        f"Total sessions in index:      {total}",
        f"Processed this run:           {processed}",
        f"Sessions with no text:        {failed_text}",
        "",
        f"Sessions matching any group:  {len(relevant)}  ({len(relevant)/total*100:.1f}%)",
        f"  Primary match (Group A):    {primary_count}",
        f"  Oberrheingraben mentions:   {oberrhein_count}",
        "",
        "Matched-group distribution:",
    ]
    for g in "ABCDE":
        lines.append(f"  Group {g}: {group_counts[g]}")

    lines += [
        "",
        "Relevant sessions (id, date, groups, terms):",
    ]
    for e in sorted(relevant, key=lambda x: (x.get("date") or "9999", x["id"])):
        lines.append(
            f"  {e['id']}  {e.get('date','?'):>10}  "
            f"groups={e['matched_groups']}  terms={e['matched_terms']}"
        )

    summary_text = "\n".join(lines)
    print("\n" + summary_text + "\n")
    with open(FILTER_SUMMARY, "w", encoding="utf-8") as fh:
        fh.write(summary_text + "\n")
    log.info("Filter summary written to %s", FILTER_SUMMARY)


def _write_index(index: List[Dict]) -> None:
    index.sort(key=lambda e: (e.get("date") or "9999", e.get("wahlperiode", 0), e.get("session", 0)))
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    tmp.replace(INDEX_FILE)


if __name__ == "__main__":
    main()
