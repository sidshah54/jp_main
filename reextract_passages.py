#!/usr/bin/env python3
"""
Re-extract all passage_text values in passages_final.csv from the source PDFs
using precise column-aware extraction.

PRIMARY LOCATION METHOD: keyword-based.
Each passage was identified because it contains specific keywords.  We find
those keywords in the column-correctly ordered page text, then extract from
the nearest preceding speaker marker to the nearest following speaker marker.
Speaker name matching is only used as a secondary fallback.

PDF layout:
  - Header strip: y=0 to y=55 (excluded)
  - Two-column body: left = x[0, width/2], right = x[width/2, width], y[55, height]

Usage:
    python reextract_passages.py \
        --csv /path/to/passages_final.csv \
        --corpus /path/to/bundestag_corpus \
        --index /path/to/bundestag_corpus/index.json

Optional:
    --debug    Print page text when extraction fails (for diagnosis)
"""

import argparse
import ast
import csv
import json
import os
import re
import shutil
import sys

try:
    import pdfplumber
except ImportError:
    sys.exit("ERROR: pdfplumber is not installed. Run: pip install pdfplumber")

# ---------------------------------------------------------------------------
# All known keywords (used as fallback when no per-row keyword column exists)
# ---------------------------------------------------------------------------
ALL_KEYWORDS = [
    # Group A
    "Lithium", "Lithiumabbau", "Lithiumgewinnung", "Lithiumvorkommen",
    "Batteriemetalle", "Batterierohstoffe", "Oberrheingraben",
    "Kobalt", "Mangan", "Nickel",
    # Group B
    "Versorgungssicherheit", "Rohstoffsicherheit",
    "Lieferkettensicherheit", "Lieferkettenabhängigkeit",
    "strategische Autonomie", "strategische Abhängigkeit",
    "Versorgungsengpass", "Versorgungsrisiko", "Abhängigkeit",
    # Group C
    "kritische Rohstoffe", "heimische Rohstoffe",
    "Rohstoffstrategie", "Rohstoffpolitik",
    "Primärrohstoffe", "Sekundärrohstoffe",
    "EU-Rohstoffgesetz", "Critical Raw Materials Act", "CRMA",
    "Rohstoffwende", "Kreislaufwirtschaft",
    # Group D
    "Batteriezellfertigung", "Batterieproduktion",
    "Gigafactory", "Batteriewerk",
    "Energiewende", "Elektromobilität", "Dekarbonisierung",
    # Group E
    "Ressourcennationalismus", "Wirtschaftssicherheit",
    "Technologiesouveränität", "Industriesouveränität",
    "China", "geopolitisch",
]
# Sort longest first so more specific multi-word terms are tried before shorter ones
ALL_KEYWORDS.sort(key=len, reverse=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEADER_Y = 55
MAX_EXTRA_PAGES = 3
TERMINAL_CHARS = {'.', '?', '!', ')'}

# Speaker line: "Firstname Lastname (FRAKTION):"
SPEAKER_LINE_RE = re.compile(
    r'(?m)^((?:(?:Dr|Prof|Prof\. Dr|Drs|Dipl|Dr\.-Ing|PD Dr|Prof\.)\.\s+)?'
    r'[A-ZÄÖÜ][^\n(]+\([A-ZÄÖÜ][^\n)]*\)\s*:)',
    re.UNICODE
)

HEADER_TEXT_RE = re.compile(
    r'Deutscher Bundestag\s*[–\-]\s*\d+\.\s*Wahlperiode[^\n]*'
)
QUADRANT_RE        = re.compile(r'(?m)^\([ABCD]\)\s*$')
QUADRANT_INLINE_RE = re.compile(r'(?m)(?<!\w)\([ABCD]\)(?!\w)')
HYPHEN_RE          = re.compile(r'(\w)-\n(\w)')
TITLE_PREFIX_RE    = re.compile(
    r'^(?:(?:Dr|Prof|Prof\. Dr|Drs|Dipl|Dr\.-Ing|PD Dr|Prof\.)\.\s+)+'
)

# ---------------------------------------------------------------------------
# Keyword parsing
# ---------------------------------------------------------------------------

def parse_keywords(raw: str) -> list:
    """
    Parse the keywords/matched_terms cell from the CSV.
    Handles JSON arrays, Python list literals, and comma-separated strings.
    Returns a list of non-empty strings sorted longest-first.
    """
    if not raw or not raw.strip():
        return []
    raw = raw.strip()

    # Try JSON array
    if raw.startswith('['):
        try:
            result = json.loads(raw)
            if isinstance(result, list):
                terms = [str(t).strip() for t in result if str(t).strip()]
                terms.sort(key=len, reverse=True)
                return terms
        except json.JSONDecodeError:
            pass
        # Try Python literal
        try:
            result = ast.literal_eval(raw)
            if isinstance(result, list):
                terms = [str(t).strip() for t in result if str(t).strip()]
                terms.sort(key=len, reverse=True)
                return terms
        except Exception:
            pass

    # Comma-separated
    terms = [t.strip() for t in raw.split(',') if t.strip()]
    terms.sort(key=len, reverse=True)
    return terms


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def extract_page_text(page) -> str:
    """Column-aware extraction: left column then right column, skip header strip."""
    w, h = page.width, page.height
    left  = page.crop((0,   HEADER_Y, w / 2, h)).extract_text() or ''
    right = page.crop((w/2, HEADER_Y, w,     h)).extract_text() or ''
    return left + '\n' + right


def postprocess(text: str) -> str:
    """Rejoin hyphen breaks, strip quadrant markers and running headers, normalise whitespace."""
    text = HYPHEN_RE.sub(r'\1\2', text)
    text = QUADRANT_RE.sub('', text)
    text = QUADRANT_INLINE_RE.sub('', text)
    text = HEADER_TEXT_RE.sub('', text)

    lines = [re.sub(r'  +', ' ', line).strip() for line in text.split('\n')]
    out, prev_blank = [], False
    for line in lines:
        if line == '':
            if not prev_blank:
                out.append('')
            prev_blank = True
        else:
            out.append(line)
            prev_blank = False
    return '\n'.join(out).strip()


def ends_with_terminal(text: str) -> bool:
    s = text.rstrip()
    return bool(s) and s[-1] in TERMINAL_CHARS


# ---------------------------------------------------------------------------
# Speaker-block extraction from a page text given a known anchor position
# ---------------------------------------------------------------------------

def extract_block_at(page_text: str, anchor_pos: int) -> str:
    """
    Given an anchor character position (e.g. keyword hit), return the full
    speaker block that contains it: from the nearest preceding speaker marker
    to the nearest following speaker marker (or end of text).
    """
    # Collect all speaker marker positions
    speaker_positions = [(m.start(), m.end()) for m in SPEAKER_LINE_RE.finditer(page_text)]

    block_start = 0
    block_end   = len(page_text)

    for i, (s_start, _) in enumerate(speaker_positions):
        if s_start <= anchor_pos:
            block_start = s_start
            block_end   = speaker_positions[i + 1][0] if i + 1 < len(speaker_positions) else len(page_text)
        else:
            # First speaker marker after anchor
            block_end = s_start
            break

    return page_text[block_start:block_end]


# ---------------------------------------------------------------------------
# Location strategies
# ---------------------------------------------------------------------------

def locate_by_keywords(page_text: str, keywords: list) -> int:
    """
    Return the anchor position of the first keyword hit, or -1.
    Tries keywords sorted longest-first to prefer specific multi-word terms.
    """
    for kw in sorted(keywords, key=len, reverse=True):
        m = re.search(re.escape(kw), page_text, re.IGNORECASE)
        if m:
            return m.start()
    return -1


def _fix_camelcase(name: str) -> str:
    return re.sub(r'([a-zäöü])([A-ZÄÖÜ])', r'\1 \2', name)


def speaker_variants(speaker: str) -> list:
    """Return ordered list of name variants to try (handles compound, CamelCase, titles)."""
    if not speaker or speaker.upper() == 'UNKNOWN':
        return []
    if ' and ' in speaker:
        result = []
        for p in [p.strip() for p in speaker.split(' and ')]:
            result.extend(speaker_variants(p))
        return result

    variants, seen = [], set()

    def _add(v):
        v = v.strip()
        if v and v not in seen:
            seen.add(v); variants.append(v)

    original = speaker.strip()
    _add(original)
    fixed = _fix_camelcase(original)
    _add(fixed)
    for candidate in list(variants):
        _add(TITLE_PREFIX_RE.sub('', candidate).strip())
    base = TITLE_PREFIX_RE.sub('', fixed).strip()
    last = base.split()[-1] if base else ''
    if len(last) > 2:
        _add(last)
    return variants


def locate_by_speaker(page_text: str, variants: list) -> int:
    """Return offset of the first matching speaker line, or -1."""
    for v in variants:
        if not v:
            continue
        pat = re.compile(
            r'(?m)^' + re.escape(v) + r'[^\n]*\([A-ZÄÖÜ][^\n)]*\)\s*:',
            re.IGNORECASE | re.UNICODE
        )
        m = pat.search(page_text)
        if m:
            return m.start()
        if ' ' not in v and len(v) > 2:
            pat2 = re.compile(
                r'(?m)^[^\n]*\b' + re.escape(v) + r'\b[^\n]*\([A-ZÄÖÜ][^\n)]*\)\s*:',
                re.IGNORECASE | re.UNICODE
            )
            m2 = pat2.search(page_text)
            if m2:
                return m2.start()
    return -1


_STOPWORDS = {
    'dass', 'aber', 'auch', 'oder', 'eine', 'einen', 'einem', 'einer',
    'nicht', 'wird', 'haben', 'sind', 'kann', 'muss', 'mehr', 'noch',
    'sehr', 'über', 'sich', 'sein', 'beim', 'dies', 'damit', 'doch',
    'dann', 'jetzt', 'beim', 'nach', 'dieses', 'diesen',
}

def locate_by_fingerprint(page_text: str, original_text: str) -> int:
    """
    Locate passage by matching distinctive 3-word windows from the original
    passage_text (body only, after stripping the speaker line).
    Returns an anchor offset, or -1.
    """
    if not original_text:
        return -1
    body = SPEAKER_LINE_RE.sub('', original_text, count=1).strip()
    if not body:
        return -1
    words = [w for w in re.findall(r'\b[A-Za-zÄÖÜäöüß]{4,}\b', body)
             if w.lower() not in _STOPWORDS]
    if len(words) < 3:
        return -1
    for i in range(min(len(words) - 2, 10)):
        phrase = r'\s+'.join(re.escape(w) for w in words[i:i + 3])
        m = re.search(phrase, page_text, re.IGNORECASE)
        if m:
            return m.start()
    return -1


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_passage(pdf_path: str, page_no: int,
                    keywords: list, speaker_variants_list: list,
                    original_text: str = '',
                    debug: bool = False) -> tuple:
    """
    Extract the passage from the PDF using three location strategies in order:
      1. Keyword-based  (primary — most reliable)
      2. Speaker-name   (fallback)
      3. Text fingerprint from original passage_text (last resort)

    Returns (passage_text, flag, method_used)
    flag: '' = success, 'check' = needs review, 'speaker_not_found', 'pdf_not_found'
    """
    if not os.path.isfile(pdf_path):
        return ('', 'pdf_not_found', 'pdf_not_found')

    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception:
        return ('', 'pdf_not_found', 'pdf_not_found')

    with pdf:
        total_pages = len(pdf.pages)
        zero_idx = page_no - 1

        if zero_idx < 0 or zero_idx >= total_pages:
            return ('', 'pdf_not_found', 'pdf_not_found')

        page_text = extract_page_text(pdf.pages[zero_idx])

        # ── Strategy 1: keyword ──────────────────────────────────────────
        anchor = locate_by_keywords(page_text, keywords)
        method = 'keyword'

        # ── Strategy 2: speaker name ─────────────────────────────────────
        if anchor < 0:
            anchor = locate_by_speaker(page_text, speaker_variants_list)
            method = 'speaker'

        # ── Strategy 3: text fingerprint ────────────────────────────────
        if anchor < 0:
            anchor = locate_by_fingerprint(page_text, original_text)
            method = 'fingerprint'

        if anchor < 0:
            if debug:
                print(f"\n  [DEBUG] Page {page_no} text (first 1500 chars):\n{page_text[:1500]}\n")
            return ('', 'speaker_not_found', 'none')

        # ── Extract the speaker block containing the anchor ───────────────
        passage = extract_block_at(page_text, anchor)
        passage = postprocess(passage)

        # ── Continuation pages ────────────────────────────────────────────
        current_idx = zero_idx
        for _ in range(MAX_EXTRA_PAGES):
            if ends_with_terminal(passage):
                break
            current_idx += 1
            if current_idx >= total_pages:
                break
            cont_text = extract_page_text(pdf.pages[current_idx])
            next_speaker = next((m.start() for m in SPEAKER_LINE_RE.finditer(cont_text)), -1)
            snippet = cont_text[:next_speaker] if next_speaker >= 0 else cont_text
            snippet = postprocess(snippet)
            if snippet:
                passage = passage.rstrip('\n') + '\n' + snippet
            if next_speaker >= 0:
                break

        # Fingerprint / speaker-name extractions need manual boundary check
        if method in ('fingerprint', 'speaker'):
            flag = 'check'
        else:
            flag = '' if ends_with_terminal(passage) else 'check'

        return (passage, flag, method)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Re-extract passage_text from Bundestag PDFs (keyword-first).'
    )
    parser.add_argument('--csv',    required=True)
    parser.add_argument('--corpus', required=True)
    parser.add_argument('--index',  required=True)
    parser.add_argument('--debug',  action='store_true')
    args = parser.parse_args()

    csv_path, corpus_dir, index_path = args.csv, args.corpus, args.index

    for path, label in [(csv_path,'CSV'), (corpus_dir,'corpus dir'), (index_path,'index.json')]:
        if not os.path.exists(path):
            sys.exit(f"ERROR: {label} not found: {path}")

    print("Loading index.json...")
    with open(index_path, encoding='utf-8') as f:
        pdf_index = {e['id']: e['filename'] for e in json.load(f)}
    print(f"  {len(pdf_index)} protocol entries.\n")

    backup_path = csv_path.replace('.csv', '_backup_preextract.csv')
    if not os.path.exists(backup_path):
        shutil.copy2(csv_path, backup_path)
        print(f"Backup written to: {backup_path}\n")
    else:
        print(f"Backup already exists: {backup_path}\n")

    # Load backup for original passage_text (fingerprint fallback)
    backup_by_id = {}
    with open(backup_path, encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            pid = row.get('passage_id') or row.get('id', '')
            backup_by_id[pid] = row

    with open(csv_path, encoding='utf-8', newline='') as f:
        reader      = csv.DictReader(f)
        fieldnames  = list(reader.fieldnames or [])
        rows        = list(reader)

    if not rows:
        sys.exit("ERROR: CSV is empty.")

    if 'extraction_flag' not in fieldnames:
        fieldnames.append('extraction_flag')

    # Detect keyword column (try common names)
    kw_col = next(
        (c for c in fieldnames if c.lower() in ('matched_terms', 'keywords', 'keyword', 'terms')),
        None
    )
    if kw_col:
        print(f"Using '{kw_col}' column for per-row keywords.\n")
    else:
        print("No keyword column found — will use full keyword list for all rows.\n")

    total = len(rows)
    counts = {'success': 0, 'check': 0, 'error': 0}
    method_counts = {'keyword': 0, 'speaker': 0, 'fingerprint': 0, 'none': 0}

    print(f"Processing {total} rows...\n")

    for i, row in enumerate(rows):
        row_num     = i + 1
        passage_id  = row.get('passage_id') or row.get('id') or f'row_{row_num}'
        speaker     = (row.get('speaker') or '').strip()
        protocol_no = (row.get('protocol_no') or '').strip()

        try:
            page_no = int(row.get('page_no', 1))
        except (ValueError, TypeError):
            page_no = 1

        # Keywords for this row
        if kw_col:
            keywords = parse_keywords(row.get(kw_col, ''))
        else:
            keywords = []
        if not keywords:
            keywords = ALL_KEYWORDS  # fall back to full list

        # PDF path
        filename = pdf_index.get(protocol_no)
        pdf_path = os.path.join(corpus_dir, filename) if filename else \
                   os.path.join(corpus_dir, f"{protocol_no}.pdf")

        # Speaker name variants (secondary fallback only)
        variants = speaker_variants(speaker)

        # Original text for fingerprint fallback
        orig_text = backup_by_id.get(str(passage_id), {}).get('passage_text', '')

        # Extract
        passage_text, flag, method = extract_passage(
            pdf_path, page_no, keywords, variants,
            original_text=orig_text, debug=args.debug
        )

        method_counts[method] += 1

        if flag in ('pdf_not_found', 'speaker_not_found'):
            outcome = f'ERROR: {flag}'
            counts['error'] += 1
        elif flag == 'check':
            outcome = f'check [{method}]'
            counts['check'] += 1
            row['passage_text'] = passage_text
        else:
            outcome = f'success [{method}]'
            counts['success'] += 1
            row['passage_text'] = passage_text

        row['extraction_flag'] = flag

        spk_display = (speaker[:38] + '…') if len(speaker) > 40 else speaker
        print(f"[{row_num}/{total}] id={passage_id} speaker={spk_display} -> {outcome}")

    # Write output
    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

    print("\n" + "=" * 70)
    print("=== SUMMARY ===")
    print("=" * 70)
    print(f"  Total rows:          {total}")
    print(f"  Successful:          {counts['success']}")
    print(f"  Check (review):      {counts['check']}")
    print(f"  Errors:              {counts['error']}")
    print(f"\n  Method breakdown:")
    print(f"    keyword:           {method_counts['keyword']}")
    print(f"    speaker name:      {method_counts['speaker']}")
    print(f"    fingerprint:       {method_counts['fingerprint']}")
    print(f"    not found:         {method_counts['none']}")
    print(f"\nOutput: {csv_path}")
    print(f"Backup: {backup_path}")


if __name__ == '__main__':
    main()
