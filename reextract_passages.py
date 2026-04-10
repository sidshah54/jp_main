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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_index(index_path: str) -> dict:
    """Return {id: filename} from index.json."""
    with open(index_path, 'r', encoding='utf-8') as f:
        entries = json.load(f)
    return {entry['id']: entry['filename'] for entry in entries}


def extract_page_text(page) -> str:
    """
    Extract body text from a pdfplumber page using column-aware cropping.
    Returns left_col + newline + right_col.
    """
    width = page.width
    height = page.height
    left_text = page.crop((0, HEADER_Y, width / 2, height)).extract_text() or ''
    right_text = page.crop((width / 2, HEADER_Y, width, height)).extract_text() or ''
    return left_text + '\n' + right_text


def postprocess(text: str) -> str:
    """
    Clean extracted text:
      1. Rejoin hyphenated line-breaks.
      2. Strip standalone quadrant markers (A), (B), (C), (D).
      3. Remove running header lines.
      4. Normalize whitespace (collapse spaces, preserve paragraph breaks).
    """
    # 1. Rejoin hyphenated line-breaks: 'Versor-\ngung' -> 'Versorgung'
    text = HYPHEN_RE.sub(r'\1\2', text)

    # 2. Remove standalone quadrant markers on their own line
    text = QUADRANT_RE.sub('', text)
    # Also remove inline quadrant markers not part of a word
    text = QUADRANT_INLINE_RE.sub('', text)

    # 3. Remove running header text
    text = HEADER_TEXT_RE.sub('', text)

    # 4. Collapse multiple spaces to single space, but keep newlines
    lines = []
    for line in text.split('\n'):
        line = re.sub(r'  +', ' ', line).strip()
        lines.append(line)
    # Collapse runs of blank lines to a single blank line (paragraph break)
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
    """Return True if text ends with terminal punctuation (ignoring whitespace)."""
    stripped = text.rstrip()
    if not stripped:
        return False
    return stripped[-1] in TERMINAL_CHARS


def find_speaker_offset(full_page_text: str, speaker: str) -> int:
    """
    Return the character offset where the speaker's passage begins.
    Strategy:
      1. Try full speaker name match (case-insensitive).
      2. Try last name only.
      3. Return -1 if not found.
    """
    if not speaker:
        return -1

    speaker_clean = speaker.strip()

    # Build a regex that looks for the speaker name followed by optional title/space
    # and then (FRAKTION): pattern
    def _try_pattern(name_pattern: str) -> int:
        pat = re.compile(
            r'(?m)^' + re.escape(name_pattern) + r'[^\n]*\([A-ZÄÖÜ][^\n)]*\)\s*:',
            re.IGNORECASE | re.UNICODE
        )
        m = pat.search(full_page_text)
        if m:
            return m.start()
        return -1

    # 1. Full name
    offset = _try_pattern(speaker_clean)
    if offset >= 0:
        return offset

    # 2. Try stripping titles and matching last part
    # Remove academic titles
    name_no_title = re.sub(
        r'^(?:(?:Dr|Prof|Prof\. Dr|Drs|Dipl|Dr\.-Ing|PD Dr|Prof\.)\.\s+)+',
        '', speaker_clean
    ).strip()
    if name_no_title != speaker_clean:
        offset = _try_pattern(name_no_title)
        if offset >= 0:
            return offset

    # 3. Last name only (last word of the name)
    last_name = speaker_clean.split()[-1] if speaker_clean else ''
    if last_name:
        pat = re.compile(
            r'(?m)^[^\n]*' + re.escape(last_name) + r'[^\n]*\([A-ZÄÖÜ][^\n)]*\)\s*:',
            re.IGNORECASE | re.UNICODE
        )
        m = pat.search(full_page_text)
        if m:
            return m.start()

    return -1


def find_next_speaker_offset(text: str, start: int) -> int:
    """
    Return the offset of the next speaker marker after `start`, or -1.
    """
    m = SPEAKER_LINE_RE.search(text, start)
    if m:
        return m.start()
    return -1


def extract_passage(pdf_path: str, page_no: int, speaker: str) -> tuple:
    """
    Extract passage text for the given speaker starting at page_no (1-indexed).

    Returns:
        (passage_text: str, flag: str)
        flag is '' (success), 'check' (incomplete), 'speaker_not_found', or 'pdf_not_found'.
    """
    if not os.path.isfile(pdf_path):
        return ('', 'pdf_not_found')

    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception as e:
        return ('', f'pdf_not_found')

    with pdf:
        total_pages = len(pdf.pages)
        zero_idx = page_no - 1  # pdfplumber is 0-indexed

        if zero_idx < 0 or zero_idx >= total_pages:
            return ('', 'pdf_not_found')

        # --- Extract starting page ---
        page = pdf.pages[zero_idx]
        page_text = extract_page_text(page)

        speaker_offset = find_speaker_offset(page_text, speaker)
        if speaker_offset < 0:
            return ('', 'speaker_not_found')

        # Find where this speaker's passage ends (next speaker on same page)
        next_speaker_offset = find_next_speaker_offset(page_text, speaker_offset + 1)
        if next_speaker_offset >= 0:
            passage = page_text[speaker_offset:next_speaker_offset]
        else:
            passage = page_text[speaker_offset:]

        passage = postprocess(passage)

        # --- Continuation pages if passage ends mid-sentence ---
        extra_pages = 0
        current_page_idx = zero_idx

        while not ends_with_terminal(passage) and extra_pages < MAX_EXTRA_PAGES:
            current_page_idx += 1
            if current_page_idx >= total_pages:
                break

            next_page = pdf.pages[current_page_idx]
            next_page_text = extract_page_text(next_page)

            # Find first speaker marker on the continuation page
            first_speaker = find_next_speaker_offset(next_page_text, 0)
            if first_speaker >= 0:
                continuation = next_page_text[:first_speaker]
            else:
                continuation = next_page_text

            continuation = postprocess(continuation)
            if continuation:
                passage = passage.rstrip('\n') + '\n' + continuation

            extra_pages += 1

            # If a speaker was found, this page ends the continuation
            if first_speaker >= 0:
                break

        flag = '' if ends_with_terminal(passage) else 'check'
        return (passage, flag)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Re-extract passage_text from Bundestag PDFs.')
    parser.add_argument('--csv', required=True, help='Path to passages_final.csv')
    parser.add_argument('--corpus', required=True, help='Path to bundestag_corpus directory')
    parser.add_argument('--index', required=True, help='Path to index.json')
    args = parser.parse_args()

    csv_path = args.csv
    corpus_dir = args.corpus
    index_path = args.index

    # Validate inputs
    for path, label in [(csv_path, 'CSV'), (corpus_dir, 'corpus dir'), (index_path, 'index.json')]:
        if not os.path.exists(path):
            sys.exit(f"ERROR: {label} not found at: {path}")

    # Load index
    print("Loading index.json...")
    index = load_index(index_path)
    print(f"  Loaded {len(index)} protocol entries.\n")

    # Backup CSV
    backup_path = csv_path.replace('.csv', '_backup_preextract.csv')
    if not os.path.exists(backup_path):
        shutil.copy2(csv_path, backup_path)
        print(f"Backup written to: {backup_path}\n")
    else:
        print(f"Backup already exists at: {backup_path} (skipping overwrite)\n")

    # Read CSV
    with open(csv_path, 'r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if not rows:
        sys.exit("ERROR: CSV is empty.")

    # Ensure extraction_flag column exists
    if 'extraction_flag' not in fieldnames:
        fieldnames = list(fieldnames) + ['extraction_flag']

    # Statistics
    total = len(rows)
    successful = 0
    flagged = 0
    errors = 0

    print(f"Processing {total} rows...\n")
    print(f"{'Row':<6} {'passage_id':<20} {'speaker':<35} {'outcome'}")
    print("-" * 85)

    for i, row in enumerate(rows):
        row_num = i + 1
        passage_id = row.get('passage_id', row.get('id', f'row_{row_num}'))
        speaker = row.get('speaker', '').strip()
        protocol_no = row.get('protocol_no', '').strip()

        # Determine page number
        try:
            page_no = int(row.get('page_no', 1))
        except (ValueError, TypeError):
            page_no = 1

        # Resolve PDF path
        filename = index.get(protocol_no)
        if filename:
            pdf_path = os.path.join(corpus_dir, filename)
        else:
            # Try constructing path directly from protocol_no
            pdf_path = os.path.join(corpus_dir, f"{protocol_no}.pdf")

        # Extract passage
        passage_text, flag = extract_passage(pdf_path, page_no, speaker)

        # Determine outcome label for logging
        if flag in ('pdf_not_found', 'speaker_not_found'):
            outcome = f'ERROR: {flag}'
            errors += 1
            # Leave passage_text unchanged on error
            # (don't overwrite with empty string)
        elif flag == 'check':
            outcome = 'flagged (no terminal punct)'
            flagged += 1
            row['passage_text'] = passage_text
        else:
            outcome = 'success'
            successful += 1
            row['passage_text'] = passage_text

        row['extraction_flag'] = flag

        # Log
        speaker_display = (speaker[:32] + '...') if len(speaker) > 35 else speaker
        print(f"{row_num:<6} {str(passage_id):<20} {speaker_display:<35} {outcome}")

    # Write updated CSV
    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

    # Final summary
    print("\n" + "=" * 85)
    print("EXTRACTION SUMMARY")
    print("=" * 85)
    print(f"  Total rows:               {total}")
    print(f"  Successful:               {successful}")
    print(f"  Flagged for manual check: {flagged}")
    print(f"  Errors (pdf/speaker):     {errors}")
    print(f"\nOutput written to: {csv_path}")
    print(f"Backup preserved:  {backup_path}")


if __name__ == '__main__':
    main()
