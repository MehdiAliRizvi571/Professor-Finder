"""
professor_ranker.py
────────────────────────────────────────────────────────────────
Usage:
    python professor_ranker.py --field "mechanical engineering" \
                               --keywords "finite element" "heat transfer" "fatigue"

    python professor_ranker.py --field "biomedical engineering" \
                               --keywords "drug delivery" "tissue engineering"

Pipeline:
  1. Resolve the field to OpenAlex topic IDs
  2. Pull US-based authors who published ≥3 papers in that field in last 2 years
  3. Fetch each author's full profile metadata
  4. Fetch their last 3 research papers
  5. Reconstruct abstracts (OpenAlex stores them as inverted indexes)
  6. Score each professor: +1 per unique keyword found across their abstracts
  7. Rank and save to CSV

Requirements:
    pip install requests python-dotenv

Env vars:
    OPENALEX_EMAIL   (polite-pool: faster rate limit tier — recommended)
"""

import os
import csv
import time
import argparse
import requests
from collections import defaultdict
from dotenv import load_dotenv
from datetime import date

# ── Commented out — uncomment when ready to use OpenAI keyword expansion
# import re
# import json
# from openai import OpenAI

load_dotenv()

# ── DEFAULTS (overridden by CLI args) ──────────────────────────────────────────

DEFAULT_FIELD    = "mechanical engineering"
DEFAULT_KEYWORDS = [
    "physics-informed",
    "heat transfer",
    "computational fluid dynamics",
    "machine learning",
    "sustainability",
    "predictive maintenance",
    "robotics",
    "thermal management",
    "HVAC",
    "RSM", 
    "alternative fuels",
    "biofuels",
    "spark ignition engine",
    "engine emissions",
    "surrogate model",
    "multi-objective optimization",
    "machine learning combustion",
    "response surface methodology",
    "data-driven optimization",
    "digital twin",
    "renewable energy systems",
]

# ── FIXED CONFIG ───────────────────────────────────────────────────────────────


TO_DATE   = date.today().strftime("%Y-%m-%d")
FROM_DATE = date(date.today().year - 2, date.today().month, date.today().day).strftime("%Y-%m-%d")

MIN_PAPERS    = 3        # minimum papers in the date window to qualify
MAX_AUTHORS   = 200      # cap on how many authors to process (API budget)
LAST_N_PAPERS = 3        # papers to fetch per author for keyword scoring

BASE_URL = "https://api.openalex.org"
EMAIL    = os.getenv("OPENALEX_EMAIL")
HEADERS  = {"User-Agent": f"professor-ranker/1.0 (mailto:{EMAIL})"}

# ── Commented out — uncomment when ready
# openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ── HELPERS ────────────────────────────────────────────────────────────────────

def api_get(url: str, params: dict | None = None, retries: int = 5) -> dict:
    """GET with exponential back-off for rate-limit / server errors."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = 2 ** attempt
                print(f"  [429 rate-limit] waiting {wait}s ...")
                time.sleep(wait)
            elif r.status_code >= 500:
                time.sleep(2 ** attempt)
            else:
                print(f"  [HTTP {r.status_code}] {url}")
                return {}
        except requests.RequestException as exc:
            print(f"  [network error] {exc} -- retry {attempt + 1}")
            time.sleep(2 ** attempt)
    return {}


def paginate(url: str, params: dict, max_results: int = 10_000) -> list[dict]:
    """Fetch all pages of an OpenAlex result set via cursor pagination."""
    results = []
    params  = {**params, "per_page": 100, "cursor": "*"}
    while len(results) < max_results:
        data  = api_get(url, params)
        if not data:
            break
        batch = data.get("results", [])
        results.extend(batch)
        next_cursor = data.get("meta", {}).get("next_cursor")
        if not next_cursor or not batch:
            break
        params["cursor"] = next_cursor
        time.sleep(0.12)   # ~8 req/s — well inside the polite-pool limit
    return results


def reconstruct_abstract(inverted_index: dict | None) -> str:
    """
    OpenAlex stores abstracts as an inverted index:
        { "word": [pos1, pos2, ...], ... }
    Rebuild the plain-text string from position data.
    """
    if not inverted_index:
        return ""
    max_pos = max(pos for positions in inverted_index.values() for pos in positions)
    tokens  = [""] * (max_pos + 1)
    for word, positions in inverted_index.items():
        for pos in positions:
            tokens[pos] = word
    return " ".join(tokens)


def keyword_hits(abstract: str, keywords: list[str]) -> dict[str, bool]:
    """Return {keyword: True/False} — case-insensitive substring match."""
    lo = abstract.lower()
    return {kw: kw.lower() in lo for kw in keywords}


# ── STEP 1: Resolve field to OpenAlex topic IDs ───────────────────────────────

def find_topic_ids(field: str) -> list[str]:
    """
    Search OpenAlex topics for the given field name.
    Combines a free-text search with a subfield-name filter to maximise recall.
    Returns up to 50 topic IDs (the API OR-filter cap is 100).
    """
    print(f"\n[1/5] Resolving '{field}' -> OpenAlex topic IDs ...")
    seen = set()
    ids  = []

    # Pass 1: free-text search on display_name / description
    for t in api_get(f"{BASE_URL}/topics", {"search": field, "per_page": 50}).get("results", []):
        name     = t.get("display_name", "").lower()
        subfield = (t.get("subfield") or {}).get("display_name", "").lower()
        f_name   = (t.get("field")    or {}).get("display_name", "").lower()
        terms    = field.lower().split()
        if any(term in name or term in subfield or term in f_name for term in terms):
            tid = t["id"].split("/")[-1]
            if tid not in seen:
                seen.add(tid)
                ids.append(tid)

    # Pass 2: subfield display_name filter (catches the umbrella subfield)
    query = "+".join(field.split())
    for t in api_get(f"{BASE_URL}/topics", {
        "filter": f"subfield.display_name.search:{query}",
        "per_page": 100,
    }).get("results", []):
        tid = t["id"].split("/")[-1]
        if tid not in seen:
            seen.add(tid)
            ids.append(tid)

    ids = ids[:50]
    print(f"  Found {len(ids)} topic IDs   sample: {ids[:5]}")
    return ids


# ── STEP 2: Qualifying authors (>= MIN_PAPERS in window) ─────────────────────

def fetch_qualifying_authors(topic_ids: list[str], field: str) -> dict[str, dict]:
    """
    Pull works from /works filtered by US institution + field topics + date range.
    Count papers per author; return those with >= MIN_PAPERS.
    """
    print(f"\n[2/5] Fetching recent US '{field}' works ({FROM_DATE} -> {TO_DATE}) ...")
    topic_filter = "|".join(topic_ids)

    works = paginate(f"{BASE_URL}/works", {
        "filter": (
            f"institutions.country_code:us,"
            f"topics.id:{topic_filter},"
            f"from_publication_date:{FROM_DATE},"
            f"to_publication_date:{TO_DATE},"
            f"type:article"
        ),
        "select": "id,authorships",
    }, max_results=5000)

    print(f"  Retrieved {len(works)} works")

    counts   = defaultdict(int)
    metadata = {}

    for work in works:
        for auth in work.get("authorships", []):
            author = auth.get("author", {})
            aid    = author.get("id", "")
            if not aid:
                continue
            aid = aid.split("/")[-1]

            # Confirm at least one US institution for this authorship
            us_inst = next(
                (i for i in auth.get("institutions", [])
                 if (i.get("country_code") or "").upper() == "US"),
                None,
            )
            if not us_inst:
                continue

            counts[aid] += 1
            if aid not in metadata:
                metadata[aid] = {
                    "author_id":   aid,
                    "name":        author.get("display_name", "Unknown"),
                    "institution": us_inst.get("display_name", "Unknown"),
                }

    qualifying = {aid: meta for aid, meta in metadata.items() if counts[aid] >= MIN_PAPERS}
    print(f"  Authors with >={MIN_PAPERS} papers: {len(qualifying)}")
    return qualifying


# ── STEP 3: Full author profiles ──────────────────────────────────────────────

# def fetch_author_profiles(authors: dict[str, dict]) -> dict[str, dict]:
#     """Hit /authors/<id> for rich metadata: h-index, citations, ORCID, etc."""
#     print(f"\n[3/5] Fetching author profiles (up to {MAX_AUTHORS}) ...")
#     enriched = {}
#     ids = list(authors.keys())[:MAX_AUTHORS]

#     for i, aid in enumerate(ids, 1):
#         data = api_get(f"{BASE_URL}/authors/{aid}")
#         if not data or "id" not in data:
#             enriched[aid] = authors[aid]
#         else:
#             enriched[aid] = {
#                 **authors[aid],
#                 "orcid":          (data.get("ids") or {}).get("orcid", ""),
#                 "works_count":    data.get("works_count", 0),
#                 "cited_by_count": data.get("cited_by_count", 0),
#                 "h_index":        (data.get("summary_stats") or {}).get("h_index", 0),
#                 "homepage_url":   data.get("homepage_url", ""),
#                 "openalex_url":   data.get("id", ""),
#                 "top_topics":     "; ".join(
#                     t.get("display_name", "")
#                     for t in (data.get("topics") or [])[:3]
#                 ),
#             }
#         if i % 20 == 0:
#             print(f"  ... {i}/{len(ids)} profiles fetched")
#         time.sleep(0.12)

#     print(f"  Done -- {len(enriched)} profiles enriched")
#     return enriched

def fetch_author_profiles(authors: dict[str, dict]) -> dict[str, dict]:
    print(f"\n[3/5] Fetching author profiles (up to {MAX_AUTHORS}) ...")
    enriched = {}
    ids = list(authors.keys())[:MAX_AUTHORS]

    # Process in batches of 50 instead of one by one
    batch_size = 50
    for batch_start in range(0, len(ids), batch_size):
        batch = ids[batch_start: batch_start + batch_size]
        filter_str = "|".join(batch)

        data = api_get(f"{BASE_URL}/authors", {
            "filter": f"openalex_id:{filter_str}",
            "per_page": batch_size,
        })

        for author_data in data.get("results", []):
            aid = author_data["id"].split("/")[-1]
            enriched[aid] = {
                **authors.get(aid, {}),
                "orcid":          (author_data.get("ids") or {}).get("orcid", ""),
                "works_count":    author_data.get("works_count", 0),
                "cited_by_count": author_data.get("cited_by_count", 0),
                "h_index":        (author_data.get("summary_stats") or {}).get("h_index", 0),
                "homepage_url":   author_data.get("homepage_url", ""),
                "openalex_url":   author_data.get("id", ""),
                "top_topics":     "; ".join(
                    t.get("display_name", "")
                    for t in (author_data.get("topics") or [])[:3]
                ),
            }
        print(f"  ... {min(batch_start + batch_size, len(ids))}/{len(ids)} profiles fetched")
        time.sleep(0.05)

    # Fill in any authors missing from batch results
    for aid in ids:
        if aid not in enriched:
            enriched[aid] = authors[aid]

    print(f"  Done -- {len(enriched)} profiles enriched")
    return enriched

# ── STEP 4: Last N papers per author ──────────────────────────────────────────

def fetch_recent_papers(author_ids: list[str]) -> dict[str, list[dict]]:
    """Fetch the most recent LAST_N_PAPERS articles for each author."""
    print(f"\n[4/5] Fetching last {LAST_N_PAPERS} papers per author ...")
    author_papers = {}

    for i, aid in enumerate(author_ids, 1):
        data = api_get(f"{BASE_URL}/works", {
            "filter":   f"authorships.author.id:{aid},type:article",
            "sort":     "publication_date:desc",
            "per_page": LAST_N_PAPERS,
            "select":   "id,title,doi,publication_year,abstract_inverted_index,primary_location",
        })
        papers = []
        for w in data.get("results", []):
            source = (w.get("primary_location") or {}).get("source") or {}
            papers.append({
                "title":    w.get("title", ""),
                "doi":      w.get("doi", ""),
                "year":     w.get("publication_year", ""),
                "journal":  source.get("display_name", ""),
                "abstract": reconstruct_abstract(w.get("abstract_inverted_index")),
            })
        author_papers[aid] = papers

        if i % 25 == 0:
            print(f"  ... {i}/{len(author_ids)} done")
        time.sleep(0.06)  # slight delay to avoid hitting rate limits

    return author_papers


# ── STEP 5: Score & rank ──────────────────────────────────────────────────────

def score_and_rank(
    profiles:      dict[str, dict],
    author_papers: dict[str, list[dict]],
    keywords:      list[str],
) -> list[dict]:
    """
    Score: +1 per unique keyword present in any of the author's abstracts.
    Tie-break: cited_by_count descending.
    """
    print("\n[5/5] Scoring and ranking ...")
    ranked = []

    for aid, profile in profiles.items():
        found = {kw: False for kw in keywords}
        for paper in author_papers.get(aid, []):
            for kw, hit in keyword_hits(paper["abstract"], keywords).items():
                if hit:
                    found[kw] = True

        score   = sum(found.values())
        matched = [kw for kw, hit in found.items() if hit]
        missed  = [kw for kw, hit in found.items() if not hit]

        # Flatten last-N papers into labelled columns for the CSV
        papers_flat = {}
        for n, paper in enumerate(author_papers.get(aid, []), 1):
            papers_flat[f"paper{n}_title"]            = paper["title"]
            papers_flat[f"paper{n}_year"]             = paper["year"]
            papers_flat[f"paper{n}_journal"]          = paper["journal"]
            papers_flat[f"paper{n}_doi"]              = paper["doi"]
            papers_flat[f"paper{n}_abstract_snippet"] = paper["abstract"][:300]

        ranked.append({
            **profile,
            **papers_flat,
            "score":       score,
            "matched_kws": "; ".join(matched),
            "missed_kws":  "; ".join(missed),
        })

    ranked = [r for r in ranked if r["score"] > 0]
    ranked.sort(key=lambda x: (x["score"], x.get("cited_by_count", 0)), reverse=True)
    return ranked


# ── OUTPUT: CSV ───────────────────────────────────────────────────────────────

def save_csv(ranked: list[dict], field: str, keywords: list[str]) -> str:
    """Write ranked results to a CSV. Returns the file path."""
    "SAVES CSV with filename pattern: ranked_professors_run{N}_{field}_{kw1_kw2}.csv"

    # Read, increment, and save run counter
    env_path  = os.path.join(os.path.dirname(__file__), ".env")
    run_count = int(os.getenv("RUN_COUNT", "0")) + 1

    # Rewrite the RUN_COUNT line in .env
    with open(env_path, "r") as f:
        env_lines = f.readlines()
    with open(env_path, "w") as f:
        for line in env_lines:
            f.write(f"RUN_COUNT={run_count}\n" if line.startswith("RUN_COUNT=") else line)

    # Build filename: run number + first 2 keywords
    kw_slug  = "_".join(kw.replace(" ", "-") for kw in keywords[:2])
    out_path = f"ranked_professors_run{run_count}_{kw_slug}.csv"
    base_cols = [
        "rank", "score", "name", "institution", "openalex_url", "orcid",
        "homepage_url", "h_index", "cited_by_count", "works_count",
        "top_topics", "matched_kws", "missed_kws",
    ]
    paper_cols = []
    for n in range(1, LAST_N_PAPERS + 1):
        paper_cols += [
            f"paper{n}_title", f"paper{n}_year",
            f"paper{n}_journal", f"paper{n}_doi",
            f"paper{n}_abstract_snippet",
        ]

    all_cols = base_cols + paper_cols

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_cols, extrasaction="ignore")
        writer.writeheader()
        for rank, row in enumerate(ranked, 1):
            writer.writerow({"rank": rank, **row})

    print(f"\n  CSV saved -> {out_path}  ({len(ranked)} rows, {len(keywords)} keywords scored)")
    return out_path


# ── OPTIONAL: OpenAI keyword expansion (commented out) ────────────────────────
#
# def expand_keywords_with_ai(field: str, keywords: list[str]) -> list[str]:
#     """Use GPT to suggest synonyms / variants for the keyword list."""
#     import re, json
#     prompt = (
#         f"You are a {field} research expert.\n"
#         f"Given these search keywords:\n{json.dumps(keywords, indent=2)}\n\n"
#         "Suggest up to 5 additional synonyms or closely related technical terms "
#         "that would appear in research paper abstracts for this field. "
#         "Return ONLY a JSON array of strings, no explanation."
#     )
#     response = openai_client.chat.completions.create(
#         model="gpt-4o-mini",
#         messages=[{"role": "user", "content": prompt}],
#         temperature=0.3,
#     )
#     raw = response.choices[0].message.content.strip()
#     raw = re.sub(r"^```json|```$", "", raw, flags=re.MULTILINE).strip()
#     try:
#         extras = json.loads(raw)
#         if isinstance(extras, list):
#             combined = list(dict.fromkeys(keywords + [str(k) for k in extras]))
#             print(f"  AI expanded keywords: {extras}")
#             return combined
#     except json.JSONDecodeError:
#         pass
#     return keywords


# ── CLI & MAIN ────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank US professors by keyword relevance using OpenAlex.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python professor_ranker.py \\
      --field "mechanical engineering" \\
      --keywords "finite element" "heat transfer" "fatigue"

  python professor_ranker.py \\
      --field "biomedical engineering" \\
      --keywords "drug delivery" "tissue engineering" "biomechanics"
        """,
    )
    parser.add_argument(
        "--field", "-f",
        type=str,
        default=DEFAULT_FIELD,
        help=f'Research field to search (default: "{DEFAULT_FIELD}")',
    )
    parser.add_argument(
        "--keywords", "-k",
        nargs="+",
        default=DEFAULT_KEYWORDS,
        help="One or more keywords — quote multi-word terms",
    )
    return parser.parse_args()


def main():
    args     = parse_args()
    field    = args.field.strip()
    keywords = [kw.strip() for kw in args.keywords if kw.strip()]

    print("=" * 72)
    print("  PROFESSOR RANKER -- powered by OpenAlex")
    print("=" * 72)
    print(f"  Field      : {field}")
    print(f"  Keywords   : {keywords}")
    print(f"  Date range : {FROM_DATE} -> {TO_DATE}")
    print(f"  Min papers : {MIN_PAPERS}  |  Max authors: {MAX_AUTHORS}")

    # ── Uncomment to enable AI keyword expansion ──────────────────────────────
    # keywords = expand_keywords_with_ai(field, keywords)
    # print(f"  Expanded   : {keywords}")
    # ─────────────────────────────────────────────────────────────────────────

    topic_ids = find_topic_ids(field)
    if not topic_ids:
        print(f"\n  No OpenAlex topics found for '{field}'. Try a different field name.")
        return

    authors = fetch_qualifying_authors(topic_ids, field)
    if not authors:
        print(f"\n  No qualifying authors found. Try lowering MIN_PAPERS or widening the date range.")
        return

    profiles      = fetch_author_profiles(authors)
    author_papers = fetch_recent_papers(list(profiles.keys()))
    ranked        = score_and_rank(profiles, author_papers, keywords)

    save_csv(ranked, field, keywords)


if __name__ == "__main__":
    main()
