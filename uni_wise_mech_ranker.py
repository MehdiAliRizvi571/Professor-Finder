"""
uni_wise_mech_ranker.py
────────────────────────────────────────────────────────────────
Iterates every university listed in universities_by_state.json,
runs the professor-ranker pipeline institution-by-institution,
retains ONLY professors whose parsed department contains "mech"
(case-insensitive / hyphen-agnostic), and appends results
incrementally to a single master CSV.

Usage:
    python uni_wise_mech_ranker.py

Output:
    ranked_mech_professors_all_unis.csv   (one file, appended uni-by-uni)
"""

import os
import sys
import json
import csv
import time

# ── IMPORTS FROM professor_ranker ─────────────────────────────────────────────
from professor_ranker import (
    resolve_institution_ids,
    fetch_authors_by_institutions,
    fetch_author_profiles,
    fetch_recent_papers,
    score_and_rank,
    _normalize,
    DEFAULT_KEYWORDS,
    MIN_PAPERS,
    LAST_N_PAPERS,
    FROM_DATE,
    TO_DATE,
)

# ── CSV SCHEMA (must mirror score_and_rank + global rank) ─────────────────────
_BASE_COLS = [
    "rank", "score", "kw_matched", "kw_total", "name", "institution",
    "department", "openalex_url", "orcid",
    "homepage_url", "h_index", "cited_by_count", "works_count",
    "top_topics", "matched_kws", "missed_kws",
]
_PAPER_COLS = []
for n in range(1, LAST_N_PAPERS + 1):
    _PAPER_COLS += [
        f"paper{n}_title", f"paper{n}_year",
        f"paper{n}_journal", f"paper{n}_doi",
        f"paper{n}_abstract_snippet",
    ]
ALL_COLS = _BASE_COLS + _PAPER_COLS


# ── HELPERS ───────────────────────────────────────────────────────────────────

def load_all_universities(json_path: str) -> list[tuple[str, str]]:
    """Return [(state_name, university_name), ...] from the JSON file."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    results = []
    for state, entries in data.items():
        for entry in entries:
            # entry is [name, url]
            uni_name = entry[0]
            results.append((state, uni_name))
    return results


def has_mech_department(department: str) -> bool:
    """Case-insensitive, hyphen-agnostic check for 'mech' in department."""
    return "mech" in _normalize(department)


def append_rows(csv_path: str, rows: list[dict], write_header: bool) -> None:
    """Append rows to CSV. Write header only when write_header=True."""
    mode = "w" if write_header else "a"
    with open(csv_path, mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=ALL_COLS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(script_dir, "universities_by_state.json")
    out_path = os.path.join(script_dir, "ranked_mech_professors_all_unis.csv")

    keywords = DEFAULT_KEYWORDS
    unis = load_all_universities(json_path)

    print("=" * 72)
    print("  UNI-WISE MECHANICAL PROFESSOR RANKER")
    print("=" * 72)
    print(f"  Universities : {len(unis)}")
    print(f"  Keywords     : {len(keywords)}")
    print(f"  Date range   : {FROM_DATE} -> {TO_DATE}")
    print(f"  Min papers   : {MIN_PAPERS}")
    print(f"  Output       : {out_path}")
    print("=" * 72)

    # If CSV already exists and has content, don't overwrite the header.
    header_written = os.path.exists(out_path) and os.path.getsize(out_path) > 0

    global_rank = 0
    total_profiles = 0
    total_mech = 0
    total_ranked = 0
    skipped = 0

    for idx, (state, uni_name) in enumerate(unis, 1):
        t0 = time.time()
        print(f"\n[{idx}/{len(unis)}] {uni_name}  ({state})")

        try:
            # 1) Resolve OpenAlex institution ID
            inst_map = resolve_institution_ids([uni_name])
            if not inst_map:
                print(f"  -> Institution not found in OpenAlex, skipping.")
                skipped += 1
                continue

            # 2) Fetch qualifying authors at this institution
            authors = fetch_authors_by_institutions(inst_map)
            if not authors:
                print(f"  -> No qualifying authors (>= {MIN_PAPERS} papers), skipping.")
                skipped += 1
                continue

            # 3) Enrich profiles (no built-in dept filter — we apply our own)
            profiles = fetch_author_profiles(authors, no_dept_filter=True)
            if not profiles:
                print(f"  -> No profiles after enrichment, skipping.")
                skipped += 1
                continue

            # 4) Fetch last N papers & parse raw-affiliation departments
            author_ids = list(profiles.keys())
            author_papers, author_departments = fetch_recent_papers(author_ids)

            # 5) Keep only professors whose department contains "mech"
            mech_profiles = {}
            mech_papers = {}
            mech_depts = {}
            for aid in profiles:
                dept = author_departments.get(aid, "")
                if has_mech_department(dept):
                    mech_profiles[aid] = profiles[aid]
                    mech_papers[aid] = author_papers.get(aid, [])
                    mech_depts[aid] = dept

            if not mech_profiles:
                print(
                    f"  -> {len(profiles)} profiles, 0 with 'mech' department, skipping."
                )
                skipped += 1
                continue

            # 6) Score & rank the mech subset
            ranked = score_and_rank(mech_profiles, mech_papers, mech_depts, keywords)

            # 7) Assign global rank and append to CSV
            rows = []
            for r in ranked:
                global_rank += 1
                r["rank"] = global_rank
                rows.append(r)

            append_rows(out_path, rows, not header_written)
            header_written = True

            total_profiles += len(profiles)
            total_mech += len(mech_profiles)
            total_ranked += len(rows)
            elapsed = time.time() - t0
            print(
                f"  -> {len(profiles)} profiles | "
                f"{len(mech_profiles)} mech-dept | "
                f"{len(rows)} ranked | appended | {elapsed:.1f}s"
            )

        except KeyboardInterrupt:
            print("\n  Interrupted by user. Exiting gracefully ...")
            break
        except Exception as exc:
            print(f"  -> ERROR: {exc}")
            skipped += 1
            continue

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  SUMMARY")
    print("=" * 72)
    print(f"  Universities processed : {idx - skipped}/{len(unis)}  ({skipped} skipped/failed)")
    print(f"  Total profiles seen    : {total_profiles}")
    print(f"  Mech-dept professors   : {total_mech}")
    print(f"  Final ranked rows      : {total_ranked}")
    print(f"  CSV output             : {out_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()
