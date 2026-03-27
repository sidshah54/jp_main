"""
Bundestag Passage Extraction Script
====================================
Extracts passages around keyword hits from Bundestag Plenarprotokolle PDFs
and outputs a CSV ready for frame coding.

Supports pause/resume: press Ctrl+C to stop. Progress is saved after each PDF.
Re-running the script will skip already-processed files and append new rows.

Folder structure expected:
    bundestag_corpus/
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
import json
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

CORPUS_DIR    = "./bundestag_corpus"
OUTPUT_CSV    = "./passages.csv"
CHECKPOINT    = "./extraction_checkpoint.json"  # tracks completed PDFs
CONTEXT_WORDS = 150

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

def load_checkpoint():
    """Returns (set of completed pdf paths, next passage_id)."""
    if Path(CHECKPOINT).exists():
        data = json.loads(Path(CHECKPOINT).read_text())
        return set(data["completed"]), data["next_passage_id"]
    return set(), 1


def save_checkpoint(completed, next_passage_id):
    Path(CHECKPOINT).write_text(
        json.dumps({"completed": list(completed), "next_passage_id": next_passage_id},
                   indent=2)
    )


def parse_filename(filename):
    stem = Path(filename).stem
    parts = stem.split("_")
    if len(parts) >= 3:
        return f"{parts[0]}_{parts[1]}", parts[2]
    return stem, ""


def extract_context(words, hit_index):
    start = max(0, hit_index - CONTEXT_WORDS)
    end   = min(len(words), hit_index + CONTEXT_WORDS)
    return " ".join(words[start:end])


def find_keyword_hits(page_text, keyword):
    words    = page_text.split()
    kw_words = keyword.split()
    kw_len   = len(kw_words)
    hits = [i for i in range(len(words) - kw_len + 1)
            if words[i:i + kw_len] == kw_words]
    return words, hits


def infer_session_type(text):
    if "Ausschuss für Wirtschaft" in text[:2000]:
        return "Ausschuss für Wirtschaft und Energie"
    return "Plenary"


def open_csv_for_append(next_passage_id):
    """Opens passages.csv for appending; writes header only if starting fresh."""
    write_header = not Path(OUTPUT_CSV).exists() or next_passage_id == 1
    f = open(OUTPUT_CSV, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
    if write_header:
        writer.writeheader()
    return f, writer

# ── Main extraction ────────────────────────────────────────────────────────────

def extract_passages():
    completed, passage_id = load_checkpoint()

    if completed:
        print(f"Resuming — {len(completed)} PDFs already done, "
              f"next passage_id = {passage_id}")

    corpus_path  = Path(CORPUS_DIR)
    year_folders = sorted([f for f in corpus_path.iterdir() if f.is_dir()])

    csv_file, writer = open_csv_for_append(passage_id)

    try:
        for year_folder in year_folders:
            pdf_files = sorted(year_folder.glob("*.pdf"))
            print(f"\nProcessing {year_folder.name}/ — {len(pdf_files)} PDFs")

            for pdf_path in pdf_files:
                # Use year/filename as key so it's stable regardless of CWD or path format
                pdf_key = f"{year_folder.name}/{pdf_path.name}"

                if pdf_key in completed:
                    print(f"  [skip] {pdf_path.name}")
                    continue

                protocol_no, date = parse_filename(pdf_path.name)
                print(f"  {pdf_path.name}")

                try:
                    with pdfplumber.open(pdf_path) as pdf:
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
                                for hit_index in hits:
                                    writer.writerow({
                                        "passage_id":      passage_id,
                                        "date":            date,
                                        "protocol_no":     protocol_no,
                                        "speaker":         "",
                                        "fraktion":        "",
                                        "session_type":    session_type,
                                        "trigger_keyword": keyword,
                                        "page_no":         page_num,
                                        "passage_text":    extract_context(words, hit_index),
                                        "dominant_frame":  "",
                                        "secondary_frame": "",
                                        "notes":           "",
                                    })
                                    passage_id += 1

                except Exception as e:
                    print(f"    ERROR reading {pdf_path.name}: {e}")

                # Save progress after each PDF so Ctrl+C loses at most one file
                completed.add(pdf_key)
                save_checkpoint(completed, passage_id)

    except KeyboardInterrupt:
        print(f"\n\nPaused. {len(completed)} PDFs done, {passage_id - 1} passages so far.")
        print("Re-run the script to resume from where you left off.")
    finally:
        csv_file.close()

    if not csv_file.closed:
        csv_file.close()

    total_done = len(completed)
    total_all  = sum(len(list(yf.glob("*.pdf")))
                     for yf in year_folders)
    if total_done == total_all:
        print(f"\n✓ Done. {passage_id - 1} passages extracted → {OUTPUT_CSV}")
        Path(CHECKPOINT).unlink(missing_ok=True)  # clean up checkpoint


if __name__ == "__main__":
    extract_passages()
