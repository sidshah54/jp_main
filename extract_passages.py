"""
Bundestag Passage Extraction Script
====================================
Extracts passages around keyword hits from Bundestag Plenarprotokolle PDFs
and outputs a CSV ready for frame coding.

Folder structure expected:
    corpus/
        2017/
            18_205_2016-11-30.pdf
            ...
        2018/
            ...
        ...

Output: passages.csv
"""

import pdfplumber
import csv
import os
import re
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

# Path to your top-level corpus folder containing year subfolders
CORPUS_DIR = "./bundestag_corpus"

# Output CSV path
OUTPUT_CSV = "./passages.csv"

# Words on each side of keyword hit to include as context (~200-300 words total)
CONTEXT_WORDS = 150

# Search terms
KEYWORDS = [
    "Lithium",
    "Lithiumabbau",
    "heimische Rohstoffe",
    "kritische Rohstoffe",
    "Rohstoffsicherheit",
    "Versorgungssicherheit",
    "Batteriemetalle",
    "Oberrheingraben",
    "strategische Autonomie",
]

# CSV column headers
FIELDNAMES = [
    "passage_id",
    "date",
    "protocol_no",
    "speaker",
    "fraktion",
    "session_type",
    "trigger_keyword",
    "page_no",
    "passage_text",
    "dominant_frame",
    "secondary_frame",
    "notes",
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_filename(filename):
    """
    Parses protocol number and date from filenames like: 18_205_2016-11-30
    Returns (protocol_no, date) as strings.
    """
    stem = Path(filename).stem  # strip .pdf
    parts = stem.split("_")
    if len(parts) >= 3:
        protocol_no = f"{parts[0]}_{parts[1]}"   # e.g. "18_205"
        date = parts[2]                            # e.g. "2016-11-30"
    else:
        protocol_no = stem
        date = ""
    return protocol_no, date


def extract_context(words, hit_index, context_words=CONTEXT_WORDS):
    """
    Given a list of words and the index of a keyword hit,
    returns a passage of +/- context_words around the hit.
    """
    start = max(0, hit_index - context_words)
    end = min(len(words), hit_index + context_words)
    return " ".join(words[start:end])


def find_keyword_hits(page_text, keyword):
    """
    Returns list of word-level indices where the keyword appears in page_text.
    Case-sensitive to match your index.json configuration.
    """
    words = page_text.split()
    hits = []
    kw_words = keyword.split()
    kw_len = len(kw_words)

    for i in range(len(words) - kw_len + 1):
        if words[i:i + kw_len] == kw_words:
            hits.append(i)

    return words, hits


def infer_session_type(text):
    """
    Rough heuristic: if the protocol mentions Ausschuss für Wirtschaft,
    tag as committee hearing, otherwise plenary.
    """
    if "Ausschuss für Wirtschaft" in text[:2000]:
        return "Ausschuss für Wirtschaft und Energie"
    return "Plenary"

# ── Main extraction ────────────────────────────────────────────────────────────

def extract_passages():
    corpus_path = Path(CORPUS_DIR)
    rows = []
    passage_id = 1

    # Walk year folders in chronological order
    year_folders = sorted([f for f in corpus_path.iterdir() if f.is_dir()])

    for year_folder in year_folders:
        pdf_files = sorted(year_folder.glob("*.pdf"))
        print(f"\nProcessing {year_folder.name}/ — {len(pdf_files)} PDFs")

        for pdf_path in pdf_files:
            protocol_no, date = parse_filename(pdf_path.name)
            print(f"  {pdf_path.name}")

            try:
                with pdfplumber.open(pdf_path) as pdf:
                    # Read full text once to determine session type
                    first_pages_text = ""
                    for page in pdf.pages[:3]:
                        t = page.extract_text()
                        if t:
                            first_pages_text += t
                    session_type = infer_session_type(first_pages_text)

                    for page_num, page in enumerate(pdf.pages, start=1):
                        page_text = page.extract_text()
                        if not page_text:
                            continue

                        for keyword in KEYWORDS:
                            words, hits = find_keyword_hits(page_text, keyword)
                            if not hits:
                                continue

                            for hit_index in hits:
                                passage_text = extract_context(words, hit_index)

                                rows.append({
                                    "passage_id": passage_id,
                                    "date": date,
                                    "protocol_no": protocol_no,
                                    "speaker": "",        # fill manually
                                    "fraktion": "",       # fill manually
                                    "session_type": session_type,
                                    "trigger_keyword": keyword,
                                    "page_no": page_num,
                                    "passage_text": passage_text,
                                    "dominant_frame": "",   # fill manually
                                    "secondary_frame": "",  # fill manually
                                    "notes": "",
                                })
                                passage_id += 1

            except Exception as e:
                print(f"    ERROR reading {pdf_path.name}: {e}")

    # Write CSV
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n✓ Done. {passage_id - 1} passages extracted → {OUTPUT_CSV}")


if __name__ == "__main__":
    extract_passages()
