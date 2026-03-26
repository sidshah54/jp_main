#!/usr/bin/env python3
"""
Rename PDF files on disk to match the filenames recorded in index.json.
Run this after pulling index.json if your local PDFs still have _unknown in their names.
"""

import json
from pathlib import Path

CORPUS_DIR = Path("bundestag_corpus")
INDEX_FILE = CORPUS_DIR / "index.json"

with open(INDEX_FILE, encoding="utf-8") as f:
    entries = json.load(f)

renamed = 0
already_ok = 0
missing = 0

for entry in entries:
    target = CORPUS_DIR / entry["filename"]
    if target.exists():
        already_ok += 1
        continue

    # Look for the _unknown version
    wp = entry["wahlperiode"]
    sess = entry["session"]
    year_str = str(entry["year"]) if entry.get("year") else "unknown"
    unknown_path = CORPUS_DIR / year_str / f"{wp}_{sess:03d}_unknown.pdf"

    if unknown_path.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        unknown_path.rename(target)
        print(f"  Renamed: {unknown_path.name} -> {target.name}")
        renamed += 1
    else:
        missing += 1

print(f"\nDone. Renamed: {renamed}, Already correct: {already_ok}, Not found: {missing}")
