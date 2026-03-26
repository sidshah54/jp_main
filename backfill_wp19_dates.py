#!/usr/bin/env python3
"""
Backfill actual session dates for Wahlperiode 19 (and 18) entries in index.json
by extracting the date from the first page of each PDF.
"""

import json
import re
from datetime import date
from pathlib import Path

import pypdf

CORPUS_DIR = Path("bundestag_corpus")
INDEX_FILE = CORPUS_DIR / "index.json"

MONTHS = {
    "Januar": 1, "Februar": 2, "März": 3, "April": 4, "Mai": 5, "Juni": 6,
    "Juli": 7, "August": 8, "September": 9, "Oktober": 10, "November": 11,
    "Dezember": 12,
}

DATE_PATTERN = re.compile(
    r"(\d{1,2})\.\s+(" + "|".join(MONTHS) + r")\s+(\d{4})"
)


def extract_date_from_pdf(pdf_path: Path) -> str | None:
    try:
        reader = pypdf.PdfReader(str(pdf_path))
        text = reader.pages[0].extract_text() or ""
        m = DATE_PATTERN.search(text)
        if m:
            day = int(m.group(1))
            month = MONTHS[m.group(2)]
            year = int(m.group(3))
            return date(year, month, day).isoformat()
    except Exception as e:
        print(f"  ERROR reading {pdf_path.name}: {e}")
    return None


def main():
    with open(INDEX_FILE, encoding="utf-8") as f:
        entries = json.load(f)

    updated = 0
    skipped = 0
    missing_pdf = 0

    for entry in entries:
        if entry.get("wahlperiode") not in (18, 19):
            continue
        if entry.get("date") is not None:
            continue  # already has a date

        pdf_path = CORPUS_DIR / entry["filename"]
        if not pdf_path.exists():
            print(f"  PDF not found: {entry['filename']}")
            missing_pdf += 1
            continue

        extracted = extract_date_from_pdf(pdf_path)
        if extracted:
            entry["date"] = extracted
            # Also fix filename: rename file from _unknown to _<date>
            new_name = pdf_path.name.replace("_unknown.pdf", f"_{extracted}.pdf")
            new_path = pdf_path.parent / new_name
            if not new_path.exists():
                pdf_path.rename(new_path)
                entry["filename"] = str(new_path.relative_to(CORPUS_DIR))
                print(f"  {entry['id']}: {extracted}  (renamed to {new_name})")
            else:
                entry["filename"] = str(new_path.relative_to(CORPUS_DIR))
                print(f"  {entry['id']}: {extracted}  (file already renamed)")
            updated += 1
        else:
            print(f"  {entry['id']}: could not extract date from {pdf_path.name}")
            skipped += 1

    # Re-sort by date
    entries.sort(key=lambda e: (e["date"] or "9999", e["wahlperiode"], e["session"]))

    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    print(f"\nDone. Updated: {updated}, Skipped (no date found): {skipped}, "
          f"Missing PDFs: {missing_pdf}")


if __name__ == "__main__":
    main()
