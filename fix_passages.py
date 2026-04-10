#!/usr/bin/env python3
from __future__ import annotations
"""
fix_passages.py — Fix passages_final.csv extracted from Bundestag PDFs.

Fixes two problems:
  1. Cross-page speech truncation: re-extracts text from source PDFs for
     passages that end mid-sentence.
  2. Umlaut corruption: repairs garbled German characters (ä, ö, ü, ß, etc.)
     caused by PDF encoding errors.

Usage:
    python3 fix_passages.py [--csv PATH] [--corpus PATH] [--dry-run]

Defaults:
    --csv     passages_final.csv
    --corpus  bundestag_corpus
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

try:
    import pandas as pd
except ImportError:
    sys.exit("pandas is required: pip install pandas")

try:
    import pdfplumber
    _PDF_BACKEND = "pdfplumber"
except:  # noqa: E722 — catch pyo3 panics and other non-Exception failures
    pdfplumber = None  # type: ignore[assignment]
    _PDF_BACKEND = "none"

_pypdf = None
_pypdfium2 = None

if pdfplumber is None:
    try:
        import pypdf as _pypdf  # type: ignore[assignment]
        _PDF_BACKEND = "pypdf"
    except:  # noqa: E722
        _pypdf = None

if pdfplumber is None and _pypdf is None:
    try:
        import pypdfium2 as _pypdfium2  # type: ignore[assignment]
        _PDF_BACKEND = "pypdfium2"
    except ImportError:
        sys.exit(
            "No PDF library available. Install one of:\n"
            "  pip install pdfplumber\n"
            "  pip install pypdf\n"
            "  pip install pypdfium2\n"
        )


# ---------------------------------------------------------------------------
# UMLAUT REPAIR
# ---------------------------------------------------------------------------

# Encoding-corruption patterns → correct German character.
# Order matters: longer/more-specific patterns come first.
UMLAUT_PATTERNS = [
    # UTF-8 bytes misread as Latin-1 / Windows-1252
    (r"Ã¤",   "ä"),
    (r"Ã¶",   "ö"),
    (r"Ã¼",   "ü"),
    (r"Ã„",   "Ä"),
    (r"Ã–",   "Ö"),
    (r"Ãœ",   "Ü"),
    (r"ÃŸ",   "ß"),
    (r"Ã\x9f","ß"),
    # Mojibake with raw bytes sometimes seen from pypdf/pdfminer
    (r"\xe4",  "ä"),
    (r"\xf6",  "ö"),
    (r"\xfc",  "ü"),
    (r"\xc4",  "Ä"),
    (r"\xd6",  "Ö"),
    (r"\xdc",  "Ü"),
    (r"\xdf",  "ß"),
    # Single-question-mark substitutions inside clearly German words.
    # These are handled by a context-aware word-level pass below.
    # Generic smart-quote / dash corruption (UTF-8 bytes read as Latin-1)
    # U+201C " : E2 80 9C → â€œ
    ("\xe2\x80\x9c",  "\u201c"),
    # U+201D " : E2 80 9D → â€
    ("\xe2\x80\x9d",  "\u201d"),
    # U+2018 ' : E2 80 98 → â€˜
    ("\xe2\x80\x98",  "\u2018"),
    # U+2019 ' : E2 80 99 → â€™
    ("\xe2\x80\x99",  "\u2019"),
    # U+2013 – : E2 80 93 → â€"
    ("\xe2\x80\x93",  "\u2013"),
    # U+2014 — : E2 80 94 → â€"
    ("\xe2\x80\x94",  "\u2014"),
]

# Common German words containing umlauts — used to fix lone-? replacements.
# Mapping from corrupted form (ä→a, ö→o, ü→u, ß→ss) to correct form.
# We keep a representative set; the word-level heuristic handles unknown words.
KNOWN_UMLAUT_WORDS: dict[str, str] = {
    # ä words
    "Beitrage":      "Beiträge",
    "Abhangigkeit":  "Abhängigkeit",
    "Abhangigkeiten":"Abhängigkeiten",
    "Lander":        "Länder",
    "Möglichkeiten": "Möglichkeiten",
    "Vortrage":      "Vorträge",
    "Erklarung":     "Erklärung",
    "Erklarungen":   "Erklärungen",
    "Gesprache":     "Gespräche",
    "Gesprach":      "Gespräch",
    "Zusammenhange": "Zusammenhänge",
    "starker":       "stärker",
    "starke":        "stärke",
    "Qualitat":      "Qualität",
    "Kapazitat":     "Kapazität",
    "Sicherheit":    "Sicherheit",
    "Nachhaltigkeit":"Nachhaltigkeit",
    "Marktplatze":   "Marktplätze",
    "Geschafte":     "Geschäfte",
    "Ruckgang":      "Rückgang",
    "Ruckkehr":      "Rückkehr",
    # ö words
    "Rohstoffe":     "Rohstoffe",
    "Borse":         "Börse",
    "Wirtschaft":    "Wirtschaft",
    "Angebote":      "Angebote",
    "Konnen":        "Können",
    "konnen":        "können",
    "mochte":        "möchte",
    "Möglichkeit":   "Möglichkeit",
    # ü words
    "Bundnis":       "Bündnis",
    "Zukunft":       "Zukunft",
    "Burgerinnen":   "Bürgerinnen",
    "Burger":        "Bürger",
    "Funf":          "Fünf",
    "funf":          "fünf",
    "Ruck":          "Rück",
    "uber":          "über",
    "Uber":          "Über",
    "Dunger":        "Dünger",
    "kunstliche":    "künstliche",
    # ß words
    "Strasse":       "Straße",
    "grosse":        "große",
    "Masse":         "Maße",
    "muss":          "muss",   # keep as-is (ß→ss is acceptable in new orthography)
    "Fluss":         "Fluss",
    "Prozess":       "Prozess",
}


def repair_encoding_patterns(text: str) -> tuple[str, int]:
    """Apply byte-level encoding fix patterns. Returns (fixed_text, count)."""
    count = 0
    for pattern, replacement in UMLAUT_PATTERNS:
        new_text, n = re.subn(pattern, replacement, text)
        count += n
        text = new_text
    return text, count


def repair_question_mark_umlauts(text: str) -> tuple[str, int]:
    """
    Heuristically fix lone '?' characters that replaced German umlauts.

    Strategy: look for known German words with a ? in an umlaut position.
    Only fix clear cases; leave ambiguous ? intact.
    """
    count = 0

    # Build regex to find words containing '?'
    # A "word" here is a run of word-chars plus '?'
    def try_fix_word(m: re.Match) -> str:
        nonlocal count
        word = m.group(0)
        # Try each umlaut substitution
        candidates = []
        for bad, good in [("?", "ä"), ("?", "ö"), ("?", "ü"), ("?", "Ä"), ("?", "Ö"), ("?", "Ü"), ("?", "ß")]:
            candidate = word.replace(bad, good, 1)
            if candidate in KNOWN_UMLAUT_WORDS.values():
                candidates.append(candidate)
        # Also check our explicit dictionary
        if word in KNOWN_UMLAUT_WORDS:
            candidates.append(KNOWN_UMLAUT_WORDS[word])
        if len(candidates) == 1:
            count += 1
            return candidates[0]
        return word

    text = re.sub(r"\b\w*\?\w*\b", try_fix_word, text)
    return text, count


def fix_mac_roman_corruption(text: str) -> tuple[str, int]:
    """
    Fix UTF-8 text that was incorrectly decoded as Mac Roman.

    In Mac Roman, byte 0xC3 (the UTF-8 lead byte for all German umlauts)
    maps to U+221A (√). So each umlaut appears as √ followed by another
    corrupted character, e.g.:
        ä (0xC3 0xA4) → √§
        ö (0xC3 0xB6) → √∂
        ü (0xC3 0xBC) → √º

    3-byte UTF-8 sequences (dashes, quotes) have 0xE2 as lead byte, which
    maps to U+201A (‚) in Mac Roman, e.g.:
        – (en dash,  0xE2 0x80 0x93) → ‚Äì
        — (em dash,  0xE2 0x80 0x94) → ‚Äî
        • (bullet,   0xE2 0x80 0xA2) → ‚Ä¢

    Fix: re-encode each corrupted sequence as Mac Roman bytes, then decode
    as UTF-8.
    """
    # Mac Roman chars that are lead bytes of multi-byte UTF-8 sequences
    LEAD_2 = "\u221a"   # √  = MacRoman 0xC3 (lead byte of 2-byte UTF-8)
    LEAD_3 = "\u201a"   # ‚  = MacRoman 0xE2 (lead byte of 3-byte UTF-8)

    count = 0
    result: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        # Try 3-byte sequence first (‚XX)
        if c == LEAD_3 and i + 2 < len(text):
            triple = text[i : i + 3]
            try:
                fixed = triple.encode("mac_roman").decode("utf-8")
                result.append(fixed)
                count += 1
                i += 3
                continue
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        # Try 2-byte sequence (√X)
        if c == LEAD_2 and i + 1 < len(text):
            pair = text[i : i + 2]
            try:
                fixed = pair.encode("mac_roman").decode("utf-8")
                result.append(fixed)
                count += 1
                i += 2
                continue
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        result.append(c)
        i += 1
    return "".join(result), count


def fix_umlauts(text: str) -> tuple[str, int]:
    """Run all umlaut repair passes. Returns (fixed_text, total_fixes)."""
    text, n1 = fix_mac_roman_corruption(text)
    text, n2 = repair_encoding_patterns(text)
    text, n3 = repair_question_mark_umlauts(text)
    return text, n1 + n2 + n3


# ---------------------------------------------------------------------------
# TRUNCATION DETECTION
# ---------------------------------------------------------------------------

# Sentence-ending punctuation (German uses . ! ? … and closing quotes)
_SENTENCE_END_RE = re.compile(r'[.!?…"\u201d\u2019]\s*$')

# Common mid-sentence indicators
_MID_SENTENCE_RE = re.compile(
    r'(,|;|:|\bund\b|\boder\b|\bdass\b|\bdie\b|\bder\b|\bdes\b|\bdem\b|\bden\b'
    r'|\bein\b|\beine\b|\beinen\b|\bwir\b|\bich\b|\bsie\b|\bdies\b)\s*$',
    re.IGNORECASE
)

# Very short final "word" suggests cut-off (e.g. ends with a preposition)
_SHORT_FINAL_WORD_RE = re.compile(r'\b\w{1,3}\s*$')


def is_truncated(text: str) -> bool:
    """
    Return True if the passage appears to end mid-sentence.
    Heuristics:
      - does NOT end with sentence-closing punctuation
      - ends with a conjunction, comma, colon, or very short word
    """
    if not text or not text.strip():
        return False
    t = text.strip()
    if _SENTENCE_END_RE.search(t):
        return False
    # If it ends with a lower-case word (not a proper noun) it's likely cut off
    if _MID_SENTENCE_RE.search(t):
        return True
    if _SHORT_FINAL_WORD_RE.search(t):
        return True
    # If last char is a regular letter (not punctuation) treat as truncated
    if t[-1].isalpha():
        return True
    return False


# ---------------------------------------------------------------------------
# PDF LOOKUP & RE-EXTRACTION
# ---------------------------------------------------------------------------

def build_pdf_index(corpus_dir: Path) -> dict[str, Path]:
    """
    Read bundestag_corpus/index.json and build a lookup:
        protocol_no (e.g. "18_244") → pdf_path
    """
    index_file = corpus_dir / "index.json"
    if not index_file.exists():
        return {}
    with open(index_file, encoding="utf-8") as fh:
        entries = json.load(fh)
    lookup: dict[str, Path] = {}
    for e in entries:
        eid = e.get("id", "")
        filename = e.get("filename", "")
        if eid and filename:
            lookup[eid] = corpus_dir / filename
    return lookup


def find_speaker_block(pages_text: list[str], snippet: str, window: int = 3) -> Optional[str]:
    """
    Given a list of per-page text strings and a starting snippet from the
    passage, find the full speech block that contains the snippet, spanning
    up to `window` additional pages.

    Returns the extracted extended text, or None if not found.
    """
    # Use first 80 chars of snippet to locate starting page
    probe = snippet.strip()[:80]
    start_page = None
    for i, page_text in enumerate(pages_text):
        if probe in page_text:
            start_page = i
            break
    if start_page is None:
        # Try a shorter probe
        probe = snippet.strip()[:40]
        for i, page_text in enumerate(pages_text):
            if probe in page_text:
                start_page = i
                break
    if start_page is None:
        return None

    # Collect text from start_page through start_page+window
    end_page = min(start_page + window, len(pages_text) - 1)
    combined = "\n".join(pages_text[start_page: end_page + 1])

    # Find where the snippet starts in the combined text
    pos = combined.find(probe)
    if pos == -1:
        return None

    # Extract from snippet start to the next speaker-change marker.
    # Bundestag transcripts use "Vorname Nachname (Fraktion):" for speaker turns.
    # We look for a new speaker header after the snippet.
    after = combined[pos:]

    # Pattern: "WORD WORD (WORD):" — a new speaker introduction
    speaker_change = re.search(
        r'\n[A-ZÄÖÜ][a-zäöüß]+ (?:[A-ZÄÖÜ][a-zäöüß]+ )*\([A-ZÄÖÜ/\w ]+\):',
        after[len(probe):]  # skip past current speaker header if present
    )
    if speaker_change:
        # Cut off at next speaker
        cut = len(probe) + speaker_change.start()
        return after[:cut].strip()

    # Also stop at "Vizepräsident/Präsident" chair interventions
    chair_change = re.search(
        r'\n(?:Vizepräsident|Präsident|Präsidentin|Vizepräsidentin)[^\n]*:\n',
        after[len(probe):]
    )
    if chair_change:
        cut = len(probe) + chair_change.start()
        return after[:cut].strip()

    return after.strip()


def extract_pages_text(pdf_path: Path) -> list[str]:
    """Return list of per-page text strings.

    Tries pdfplumber first (best layout fidelity), then pypdf, then pypdfium2.
    """
    pages: list[str] = []

    if pdfplumber is not None:
        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                for page in pdf.pages:
                    pages.append(page.extract_text() or "")
            return pages
        except Exception as exc:
            print(f"  [WARN] pdfplumber failed for {pdf_path.name}: {exc}", file=sys.stderr)

    if _pypdf is not None:
        try:
            reader = _pypdf.PdfReader(str(pdf_path))
            for page in reader.pages:
                pages.append(page.extract_text() or "")
            return pages
        except Exception as exc:
            print(f"  [WARN] pypdf failed for {pdf_path.name}: {exc}", file=sys.stderr)

    if _pypdfium2 is not None:
        try:
            doc = _pypdfium2.PdfDocument(str(pdf_path))
            for page in doc:
                textpage = page.get_textpage()
                pages.append(textpage.get_text_range() or "")
            return pages
        except Exception as exc:
            print(f"  [WARN] pypdfium2 failed for {pdf_path.name}: {exc}", file=sys.stderr)

    return pages


def extend_passage(
    row,
    pdf_index: dict,
    corpus_dir: Path,
    verbose: bool = False,
) -> tuple[str, bool, bool]:
    """
    Try to extend a truncated passage by re-reading the source PDF.

    Returns (new_text, was_extended, uncertain).
    """
    text = str(row["passage_text"])
    protocol_no = str(row.get("protocol_no", "")).strip()

    if not protocol_no:
        return text, False, True

    pdf_path = pdf_index.get(protocol_no)
    if pdf_path is None or not pdf_path.exists():
        if verbose:
            print(f"  [WARN] PDF not found for protocol_no={protocol_no}", file=sys.stderr)
        return text, False, True

    pages = extract_pages_text(pdf_path)
    if not pages:
        return text, False, True

    extended = find_speaker_block(pages, text, window=3)
    if extended and len(extended) > len(text) + 20:
        return extended, True, False

    return text, False, False


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", default="passages_final.csv",
                        help="Path to passages_final.csv (default: passages_final.csv)")
    parser.add_argument("--corpus", default="bundestag_corpus",
                        help="Path to bundestag_corpus directory (default: bundestag_corpus)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be changed without writing files")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-row progress")
    args = parser.parse_args()

    csv_path    = Path(args.csv)
    corpus_dir  = Path(args.corpus)
    backup_path = csv_path.with_name(csv_path.stem + "_backup.csv")

    # ── Load CSV ─────────────────────────────────────────────────────────────
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")

    df = pd.read_csv(csv_path, dtype=str)
    print(f"Loaded {len(df)} rows from {csv_path}")

    if "passage_text" not in df.columns:
        sys.exit("Column 'passage_text' not found in CSV.")

    # ── Backup ───────────────────────────────────────────────────────────────
    if not args.dry_run:
        shutil.copy2(csv_path, backup_path)
        print(f"Backup written to {backup_path}")

    # ── Build PDF index ───────────────────────────────────────────────────────
    pdf_index = build_pdf_index(corpus_dir)
    print(f"PDF index: {len(pdf_index)} entries")

    # ── Process rows ──────────────────────────────────────────────────────────
    extended_count   = 0
    umlaut_count     = 0
    uncertain_rows   = []

    for i, row in df.iterrows():
        original_text = str(row["passage_text"]) if pd.notna(row["passage_text"]) else ""
        text = original_text

        # --- Umlaut repair ---
        text, n_umlaut = fix_umlauts(text)
        if n_umlaut > 0:
            umlaut_count += 1
            if args.verbose:
                print(f"  Row {i}: fixed {n_umlaut} umlaut(s)")

        # --- Truncation check & extension ---
        if is_truncated(text):
            new_text, was_extended, uncertain = extend_passage(
                row, pdf_index, corpus_dir, verbose=args.verbose
            )
            if was_extended:
                text = new_text
                extended_count += 1
                if args.verbose:
                    pid = row.get("passage_id", i)
                    print(f"  Row {i} (id={pid}): extended from "
                          f"{len(original_text)} → {len(text)} chars")
            elif uncertain:
                pid = row.get("passage_id", i)
                uncertain_rows.append({"row": i, "passage_id": pid,
                                       "reason": "PDF not found or unlocatable"})

        df.at[i, "passage_text"] = text

    # ── Write output ──────────────────────────────────────────────────────────
    if not args.dry_run:
        df.to_csv(csv_path, index=False, encoding="utf-8")
        print(f"Saved corrected CSV to {csv_path}")
    else:
        print("[dry-run] No files written.")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total rows processed:         {len(df)}")
    print(f"Passages extended (truncation): {extended_count}")
    print(f"Passages with umlaut fixes:     {umlaut_count}")
    print(f"Uncertain / unresolved rows:    {len(uncertain_rows)}")
    if uncertain_rows:
        print()
        print("Uncertain rows (truncated but PDF not found / unlocatable):")
        for r in uncertain_rows[:50]:
            print(f"  row {r['row']}  passage_id={r['passage_id']}  — {r['reason']}")
        if len(uncertain_rows) > 50:
            print(f"  ... and {len(uncertain_rows) - 50} more")
    print("=" * 60)


if __name__ == "__main__":
    main()
