#!/usr/bin/env python3
"""
Re-extract all passage_text values in passages_final.csv from the source PDFs
using precise column-aware extraction.

PDF layout:
  - Page size: 595.276 x 841.89 pts
  - Header strip: y=0 to y=55 (excluded)
  - Two-column body: left = x[0, width/2], right = x[width/2, width], y[55, height]
  - Column split at exactly page.width / 2

Usage:
    python reextract_passages.py \
        --csv /path/to/passages_final.csv \
        --corpus /path/to/bundestag_corpus \
        --index /path/to/bundestag_corpus/index.json

Optional:
    --debug    Print page content when speaker cannot be located (verbose)
"""

import argparse
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
# Constants
# ---------------------------------------------------------------------------
HEADER_Y = 55          # y coordinate below which body text starts
MAX_EXTRA_PAGES = 3    # max continuation pages if passage ends mid-sentence

# Quadrant markers at line boundaries: standalone (A), (B), (C), (D)
QUADRANT_RE = re.compile(r'(?m)^\([ABCD]\)\s*$')
QUADRANT_INLINE_RE = re.compile(r'(?m)(?<!\w)\([ABCD]\)(?!\w)')

# Speaker line: "Firstname Lastname (FRAKTION):" or "Dr. Firstname Lastname (FRAKTION):"
SPEAKER_LINE_RE = re.compile(
    r'(?m)^((?:(?:Dr|Prof|Prof\. Dr|Drs|Dipl|Dr\.-Ing|PD Dr|Prof\.)\.\s+)?'
    r'[A-ZÄÖÜ][^\n(]+\([A-ZÄÖÜ][^\n)]*\)\s*:)',
    re.UNICODE
)

# Running header pattern: "Deutscher Bundestag – NNN. Wahlperiode ..."
HEADER_TEXT_RE = re.compile(
    r'Deutscher Bundestag\s*[–\-]\s*\d+\.\s*Wahlperiode[^\n]*'
)

# Hyphenated line-break: word ending in hyphen/dash followed by newline then rest of word
HYPHEN_RE = re.compile(r'(\w)-\n(\w)')

# Terminal punctuation characters
TERMINAL_CHARS = {'.', '?', '!', ')'}

# Academic title prefixes to strip when trying name variants
TITLE_PREFIX_RE = re.compile(
    r'^(?:(?:Dr|Prof|Prof\. Dr|Drs|Dipl|Dr\.-Ing|PD Dr|Prof\.)\.\s+)+'
)

# ---------------------------------------------------------------------------
# Speaker name normalisation
# ---------------------------------------------------------------------------

def _fix_camelcase(name: str) -> str:
    """Insert a space between a lowercase letter and an uppercase letter.
    Handles 'JuliaVerlinden' -> 'Julia Verlinden'.
    Does not affect existing spaces or title dots.
    """
    return re.sub(r'([a-zäöü])([A-ZÄÖÜ])', r'\1 \2', name)


def speaker_variants(speaker: str) -> list:
    """
    Return a prioritised list of name strings to try matching in the PDF.

    Handles:
      - Compound names joined by ' and ': try each sub-name independently.
      - Missing CamelCase spaces: 'Dr. JuliaVerlinden' -> 'Dr. Julia Verlinden'.
      - Title-stripped forms: 'Reinhard Brandle' alongside 'Dr. Reinhard Brandle'.
      - Last-name-only form as final fallback.
    """
    if not speaker or speaker.upper() == 'UNKNOWN':
        return []

    # Split compound names: "Bernd Westphal and Reinhard Houben"
    if ' and ' in speaker:
        parts = [p.strip() for p in speaker.split(' and ')]
        result = []
        for p in parts:
            result.extend(speaker_variants(p))
        return result

    variants = []
    seen = set()

    def _add(v):
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    original = speaker.strip()
    _add(original)

    # CamelCase fix
    fixed = _fix_camelcase(original)
    _add(fixed)

    # Title-stripped versions of both
    for candidate in list(variants):
        no_title = TITLE_PREFIX_RE.sub('', candidate).strip()
        _add(no_title)

    # Last name only (last whitespace-separated token of the title-stripped form)
    base = TITLE_PREFIX_RE.sub('', fixed).strip()
    last_name = base.split()[-1] if base else ''
    if len(last_name) > 2:
        _add(last_name)

    return variants


def extract_speaker_from_text(text: str) -> str:
    """
    Given an existing (possibly scrambled) passage_text, try to recover the
    speaker's name by finding the first speaker-line pattern in the first 600
    characters and returning just the name part (before the party tag).
    """
    if not text:
        return ''
    m = SPEAKER_LINE_RE.search(text[:600])
    if not m:
        return ''
    line = m.group(1)
    # Strip the trailing (FRAKTION): part
    name = re.sub(r'\s*\([^)]+\)\s*:.*$', '', line).strip()
    return name if name else ''


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def extract_page_text(page) -> str:
    """
    Column-aware extraction: crop at y=HEADER_Y, split at width/2.
    Returns left column text followed by right column text.
    """
    width = page.width
    height = page.height
    left_text = page.crop((0, HEADER_Y, width / 2, height)).extract_text() or ''
    right_text = page.crop((width / 2, HEADER_Y, width, height)).extract_text() or ''
    return left_text + '\n' + right_text


def postprocess(text: str) -> str:
    """
    Clean extracted text:
      1. Rejoin hyphenated line-breaks ('Versor-\\ngung' -> 'Versorgung').
      2. Strip standalone quadrant markers (A)–(D).
      3. Remove running header lines.
      4. Normalise whitespace (collapse spaces, preserve paragraph breaks).
    """
    text = HYPHEN_RE.sub(r'\1\2', text)
    text = QUADRANT_RE.sub('', text)
    text = QUADRANT_INLINE_RE.sub('', text)
    text = HEADER_TEXT_RE.sub('', text)

    lines = [re.sub(r'  +', ' ', line).strip() for line in text.split('\n')]
    result_lines = []
    prev_blank = False
    for line in lines:
        if line == '':
            if not prev_blank:
                result_lines.append('')
            prev_blank = True
        else:
            result_lines.append(line)
            prev_blank = False

    return '\n'.join(result_lines).strip()


def ends_with_terminal(text: str) -> bool:
    """True if text ends with terminal punctuation (ignoring trailing whitespace)."""
    stripped = text.rstrip()
    return bool(stripped) and stripped[-1] in TERMINAL_CHARS


def find_speaker_offset(page_text: str, name_variant: str) -> int:
    """
    Search for a single name variant in page_text.
    Returns the character offset of the matching speaker line, or -1.
    """
    if not name_variant:
        return -1

    # Anchored at line start, followed by optional middle-name tokens,
    # then (FRAKTION):
    pat = re.compile(
        r'(?m)^' + re.escape(name_variant) + r'[^\n]*\([A-ZÄÖÜ][^\n)]*\)\s*:',
        re.IGNORECASE | re.UNICODE
    )
    m = pat.search(page_text)
    if m:
        return m.start()

    # If name_variant is a single token (last name), allow it to appear
    # anywhere on the line (not just at the very start)
    if ' ' not in name_variant and len(name_variant) > 2:
        pat2 = re.compile(
            r'(?m)^[^\n]*\b' + re.escape(name_variant) + r'\b[^\n]*\([A-ZÄÖÜ][^\n)]*\)\s*:',
            re.IGNORECASE | re.UNICODE
        )
        m2 = pat2.search(page_text)
        if m2:
            return m2.start()

    return -1


def find_speaker_offset_all_variants(page_text: str, variants: list) -> int:
    """Try each name variant in order; return the first match offset, or -1."""
    for v in variants:
        offset = find_speaker_offset(page_text, v)
        if offset >= 0:
            return offset
    return -1


def find_next_speaker_offset(text: str, start: int) -> int:
    """Return the offset of the next speaker marker after `start`, or -1."""
    m = SPEAKER_LINE_RE.search(text, start)
    return m.start() if m else -1


def find_passage_by_fingerprint(page_text: str, original_text: str) -> int:
    """
    Locate the passage start in `page_text` using distinctive words from
    `original_text` as a fingerprint.

    Strategy: take the first 8+ meaningful words (>=4 chars) from the body
    of the original passage (after stripping the speaker line), then search
    for any 3-consecutive-word window in the column-correctly ordered page
    text.  Return the start-of-line offset of the match, or -1.

    This allows accurate extraction even when the speaker name is unknown or
    misspelled — we find the right location by content, not by name.
    """
    if not original_text:
        return -1

    # Strip the speaker line at the top of the original text
    body = SPEAKER_LINE_RE.sub('', original_text, count=1).strip()
    if not body:
        return -1

    # Collect meaningful words (letters only, length >= 4, skip stopwords)
    _STOPWORDS = {'dass', 'aber', 'auch', 'oder', 'eine', 'einen', 'einem',
                  'einer', 'nicht', 'wird', 'wird', 'haben', 'sind', 'kann',
                  'wird', 'muss', 'mehr', 'noch', 'sehr', 'über', 'sich',
                  'sein', 'beim', 'dies', 'damit', 'doch', 'dann', 'jetzt'}
    words = [w for w in re.findall(r'\b[A-Za-zÄÖÜäöüß]{4,}\b', body)
             if w.lower() not in _STOPWORDS]

    if len(words) < 3:
        return -1

    # Try sliding windows of 3 words, up to the first 12 words
    for i in range(min(len(words) - 2, 10)):
        phrase_pat = r'\s+'.join(re.escape(w) for w in words[i:i + 3])
        m = re.search(phrase_pat, page_text, re.IGNORECASE)
        if m:
            # Walk back to the nearest line beginning or speaker marker before match
            line_start = page_text.rfind('\n', 0, m.start()) + 1
            # If there's a speaker marker on the same line or just before, use that
            snippet = page_text[max(0, line_start - 200): m.start()]
            sm = None
            for sm in SPEAKER_LINE_RE.finditer(snippet):
                pass  # keep last match
            if sm:
                return line_start - 200 + sm.start()
            return line_start

    return -1


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_passage(pdf_path: str, page_no: int, variants: list,
                    original_text: str = '',
                    debug: bool = False) -> tuple:
    """
    Extract the passage for the given speaker (name variants) from the PDF.

    Args:
        pdf_path:      absolute path to the PDF file.
        page_no:       1-indexed page number.
        variants:      ordered list of name strings to try (from speaker_variants()).
        original_text: existing passage_text from backup (used as fingerprint
                       fallback when speaker name lookup fails).
        debug:         if True, print page content on speaker_not_found.

    Returns:
        (passage_text, flag)
        flag: '' = success, 'check' = incomplete/fingerprint,
              'speaker_not_found', 'pdf_not_found'
    """
    if not os.path.isfile(pdf_path):
        return ('', 'pdf_not_found')

    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception:
        return ('', 'pdf_not_found')

    with pdf:
        total_pages = len(pdf.pages)
        zero_idx = page_no - 1  # pdfplumber is 0-indexed

        if zero_idx < 0 or zero_idx >= total_pages:
            return ('', 'pdf_not_found')

        page = pdf.pages[zero_idx]
        page_text = extract_page_text(page)

        speaker_offset = find_speaker_offset_all_variants(page_text, variants)
        used_fingerprint = False

        if speaker_offset < 0:
            # Fallback: locate passage by content fingerprint from original text
            speaker_offset = find_passage_by_fingerprint(page_text, original_text)
            if speaker_offset >= 0:
                used_fingerprint = True
            else:
                if debug:
                    print(f"\n    [DEBUG] Page {page_no} text (first 1500 chars):\n")
                    print(page_text[:1500])
                    print()
                return ('', 'speaker_not_found')

        # Slice from speaker start to next speaker on same page
        next_offset = find_next_speaker_offset(page_text, speaker_offset + 1)
        if next_offset >= 0:
            passage = page_text[speaker_offset:next_offset]
        else:
            passage = page_text[speaker_offset:]

        passage = postprocess(passage)

        # Continuation pages when passage ends mid-sentence
        current_idx = zero_idx
        for _ in range(MAX_EXTRA_PAGES):
            if ends_with_terminal(passage):
                break
            current_idx += 1
            if current_idx >= total_pages:
                break

            cont_text = extract_page_text(pdf.pages[current_idx])
            first_speaker = find_next_speaker_offset(cont_text, 0)
            continuation = cont_text[:first_speaker] if first_speaker >= 0 else cont_text
            continuation = postprocess(continuation)
            if continuation:
                passage = passage.rstrip('\n') + '\n' + continuation
            if first_speaker >= 0:
                break

        # Fingerprint matches are always 'check' — boundaries may be approximate
        if used_fingerprint:
            flag = 'check'
        else:
            flag = '' if ends_with_terminal(passage) else 'check'
        return (passage, flag)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Re-extract passage_text from Bundestag PDFs using column-aware cropping.'
    )
    parser.add_argument('--csv',    required=True, help='Path to passages_final.csv')
    parser.add_argument('--corpus', required=True, help='Path to bundestag_corpus directory')
    parser.add_argument('--index',  required=True, help='Path to index.json')
    parser.add_argument('--debug',  action='store_true',
                        help='Print page content when speaker cannot be located')
    args = parser.parse_args()

    csv_path   = args.csv
    corpus_dir = args.corpus
    index_path = args.index
    debug      = args.debug

    # Validate
    for path, label in [(csv_path, 'CSV'), (corpus_dir, 'corpus dir'), (index_path, 'index.json')]:
        if not os.path.exists(path):
            sys.exit(f"ERROR: {label} not found at: {path}")

    # Load PDF index
    print("Loading index.json...")
    with open(index_path, 'r', encoding='utf-8') as f:
        index_entries = json.load(f)
    pdf_index = {e['id']: e['filename'] for e in index_entries}
    print(f"  Loaded {len(pdf_index)} protocol entries.\n")

    # Backup CSV (once; never overwrite an existing backup)
    backup_path = csv_path.replace('.csv', '_backup_preextract.csv')
    if not os.path.exists(backup_path):
        shutil.copy2(csv_path, backup_path)
        print(f"Backup written to: {backup_path}\n")
    else:
        print(f"Backup already exists at: {backup_path} (skipping overwrite)\n")

    # Read backup to recover original passage_text for UNKNOWN-speaker rows
    backup_by_id: dict = {}
    with open(backup_path, 'r', encoding='utf-8', newline='') as f:
        for brow in csv.DictReader(f):
            pid = brow.get('passage_id') or brow.get('id', '')
            backup_by_id[pid] = brow

    # Read working CSV
    with open(csv_path, 'r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if not rows:
        sys.exit("ERROR: CSV is empty.")

    if 'extraction_flag' not in fieldnames:
        fieldnames.append('extraction_flag')

    total = len(rows)
    successful = flagged = errors = 0

    print(f"Processing {total} rows...\n")

    for i, row in enumerate(rows):
        row_num    = i + 1
        passage_id = row.get('passage_id') or row.get('id') or f'row_{row_num}'
        speaker    = (row.get('speaker') or '').strip()
        protocol_no = (row.get('protocol_no') or '').strip()

        try:
            page_no = int(row.get('page_no', 1))
        except (ValueError, TypeError):
            page_no = 1

        # Resolve PDF path
        filename = pdf_index.get(protocol_no)
        if filename:
            pdf_path = os.path.join(corpus_dir, filename)
        else:
            pdf_path = os.path.join(corpus_dir, f"{protocol_no}.pdf")

        # ------------------------------------------------------------------
        # Resolve UNKNOWN speaker from original passage_text in backup
        # ------------------------------------------------------------------
        resolved_note = ''
        if not speaker or speaker.upper() == 'UNKNOWN':
            orig = backup_by_id.get(str(passage_id), {})
            orig_text = orig.get('passage_text', '')
            recovered = extract_speaker_from_text(orig_text)
            if recovered:
                speaker = recovered
                resolved_note = f' [resolved from backup: {speaker}]'

        # Build name variants
        variants = speaker_variants(speaker)

        # ------------------------------------------------------------------
        # Extract  (pass original text as fingerprint fallback)
        # ------------------------------------------------------------------
        orig_passage = backup_by_id.get(str(passage_id), {}).get('passage_text', '')
        passage_text, flag = extract_passage(
            pdf_path, page_no, variants,
            original_text=orig_passage,
            debug=debug,
        )

        # ------------------------------------------------------------------
        # Outcome accounting
        # ------------------------------------------------------------------
        if flag in ('pdf_not_found', 'speaker_not_found'):
            outcome = f'ERROR: {flag}'
            errors += 1
            # Do NOT overwrite passage_text on error — keep original
        elif flag == 'check':
            outcome = 'flagged (check boundaries)'
            flagged += 1
            row['passage_text'] = passage_text
        else:
            outcome = 'success'
            successful += 1
            row['passage_text'] = passage_text

        row['extraction_flag'] = flag

        speaker_display = (speaker[:40] + '…') if len(speaker) > 42 else speaker
        print(f"[{row_num}/{total}] passage_id={passage_id} "
              f"speaker={speaker_display}{resolved_note} -> {outcome}")

    # Write updated CSV
    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

    # Summary
    print("\n" + "=" * 70)
    print("=== SUMMARY ===")
    print("=" * 70)
    print(f"  Total rows:          {total}")
    print(f"  Successful:          {successful}")
    print(f"  Flagged (check):     {flagged}")
    print(f"  Errors:              {errors}")
    print(f"\nOutput written to: {csv_path}")
    print(f"Backup preserved:  {backup_path}")


if __name__ == '__main__':
    main()
