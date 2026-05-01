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
import re
import csv
import json
import time
import random
import argparse
import requests
from typing import Optional
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from datetime import date

# ── Commented out — uncomment when ready to use OpenAI keyword expansion
# import re
# import json
# from openai import OpenAI

load_dotenv()

# ── PARALLEL PROCESSING CONFIG ─────────────────────────────────────────────────
MAX_PARALLEL_WORKERS = 15  # Number of parallel API calls for enriching profiles and fetching papers

# ── DEFAULTS (overridden by CLI args) ──────────────────────────────────────────

DEFAULT_FIELD    = "mechanical engineering"

# Map user-facing field names to OpenAlex subfield IDs.
# Use /subfields endpoint to discover IDs:  https://api.openalex.org/subfields?search=...
TARGET_SUBFIELDS = {
    "mechanical engineering": [
        2210,   # Mechanical Engineering
        2206,   # Computational Mechanics
        2211,   # Mechanics of Materials
        2209,   # Industrial and Manufacturing Engineering
        2203,   # Automotive Engineering
        2202,   # Aerospace Engineering
        2207,   # Control and Systems Engineering       ← new (digital twins, control)
        2208,   # Fluid Flow and Transfer Processes     ← new (energy/thermal work)
        2213,   # Safety, Risk, Reliability and Quality ← new (systems reliability)
    ],
    "thermal & energy engineering": [
        2102,   # Energy Engineering and Power Technology
        2105,   # Renewable Energy, Sustainability and the Environment
        2100,   # General Energy
    ],
    "environmental engineering": [
        2305,   # Environmental Engineering
    ],
    "computer science": [
        1706,   # Computer Science Applications
        1702,   # Artificial Intelligence
        1712,   # Software
        1710,   # Signal Processing                    ← new (pose estimation work)
        1711,   # Computer Vision and Pattern Recognition ← new (pose estimation)
        1708,   # Human-Computer Interaction           ← new (clinical AI interfaces)
        1709,   # Information Systems                  ← new (knowledge graphs, CureMD)
    ],
    "biomedical engineering": [                        # ← entire new category
        2204,   # Biomedical Engineering
    ],
    "mathematics & optimisation": [                    # ← entire new category
        2604,   # Applied Mathematics
        2605,   # Computational Mathematics
        2606,   # Control and Optimization             ← directly your NSGA-III work
        2613,   # Statistics and Probability
    ],
    "decision & management science": [                 # ← new, for ops research overlap
        1803,   # Management Science and Operations Research
    ],
}
# Flat set of all allowed subfield IDs (union of every group above)
ALLOWED_SUBFIELD_IDS = {sid for ids in TARGET_SUBFIELDS.values() for sid in ids}

DEFAULT_KEYWORDS = [
    # High discrimination — niche to your exact work
    "machine learning combustion",
    "NSGA-III",
    "surrogate model",
    "spark ignition engine",
    "internal combustion engine",
    "physics-informed",
    "alternative fuels",
    "convex hull",
    "response surface methodology",
    "fuel blend",
    "alcohol gasoline",
    "oxygenated fuel",
    "biofuels",
    "brake thermal efficiency",
    "NOx prediction",
    "emission reduction",
    "emissions optimization",
    "engine emissions",
    "gradient boosting",
    "XGBoost",
    "evolutionary algorithm",
    "Pareto optimization",
    "data-driven optimization",
    "digital twin",
    "knowledge graph",
    "scientific machine learning",
    "neural operator",
    "Neo4j",
    "graph neural network",
    "stochastic modeling",
    "Gaussian process",
    "techno-economic analysis",
    "LCA",
    "life cycle assessment",

    # Medium discrimination — specific but broader
    "multi-objective optimization",
    "heat transfer",
    "computational fluid dynamics",
    "data driven thermodynamics",
    "thermal management",
    "renewable energy systems",
    "energy optimization",
    "decarbonization",
    "energy transition",
    "manufacturing",
    "robotics",
    "energy harvesting",
    "vibration",
    "control systems",
    "additive manufacturing",
    "digital shadow",

    # Low discrimination — broad but still additive
    "machine learning",
    "artificial intelligence",
    "sustainability",
]

# ── FIXED CONFIG ───────────────────────────────────────────────────────────────


TO_DATE   = date.today().strftime("%Y-%m-%d")
FROM_DATE = date(date.today().year - 2, date.today().month, date.today().day).strftime("%Y-%m-%d")

MIN_PAPERS    = 3        # minimum papers in the date window to qualify
MAX_AUTHORS   = 30000     # cap on how many authors to process (API budget)
LAST_N_PAPERS = 10       # papers to fetch per author for keyword scoring

BASE_URL = "https://api.openalex.org"
EMAIL    = os.getenv("OPENALEX_EMAIL")
HEADERS  = {"User-Agent": f"professor-ranker/1.0 (mailto:{EMAIL})"}

# ── Commented out — uncomment when ready
# openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ── HELPERS ────────────────────────────────────────────────────────────────────

def api_get(url: str, params: Optional[dict] = None, retries: int = 5) -> dict:
    """GET with exponential back-off with jitter for rate-limit / server errors."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                base_wait = 2 ** attempt
                jitter = random.uniform(0, 0.5 * base_wait)
                wait = base_wait + jitter
                print(f"  [429 rate-limit] waiting {wait:.2f}s ...")
                time.sleep(wait)
            elif r.status_code >= 500:
                base_wait = 2 ** attempt
                jitter = random.uniform(0, 0.5 * base_wait)
                time.sleep(base_wait + jitter)
            else:
                print(f"  [HTTP {r.status_code}] {url}")
                return {}
        except requests.RequestException as exc:
            print(f"  [network error] {exc} -- retry {attempt + 1}")
            base_wait = 2 ** attempt
            jitter = random.uniform(0, 0.5 * base_wait)
            time.sleep(base_wait + jitter)
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


def reconstruct_abstract(inverted_index: Optional[dict]) -> str:
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


def _normalize(text: str) -> str:
    """Lowercase and collapse hyphens/underscores/extra whitespace to single spaces."""
    return re.sub(r'[\s\-_]+', ' ', text.lower()).strip()


def keyword_hits(text: str, keywords: list[str]) -> dict[str, bool]:
    """Return {keyword: True/False} — normalized, case-insensitive substring match.
    Hyphens and underscores in both keyword and text are treated as spaces, so
    'physics-informed' matches 'physics informed' and vice versa.
    """
    norm = _normalize(text)
    return {kw: _normalize(kw) in norm for kw in keywords}


# Patterns that indicate a department segment in a raw affiliation string.
# Ordered from most to least specific.
_DEPT_PATTERNS = [
    re.compile(
        r'(?:Department|Dept\.?|School|Division|Faculty|Institute|Center|College)'   # anchor word
        r'\s+of\s+([^,;\n]{5,80})',                                                  # "of <name>"
        re.IGNORECASE,
    ),
    re.compile(
        r'([^,;\n]{5,80}?)\s+(?:Department|Dept\.?)',                                # "<name> Department"
        re.IGNORECASE,
    ),
]


def parse_department(raw_strings: list[str]) -> str:
    """
    Extract a department name from a list of raw affiliation strings
    (the verbatim text an author puts on their paper).
    Returns the first match found, or an empty string if nothing matches.
    """
    for raw in raw_strings:
        for pat in _DEPT_PATTERNS:
            m = pat.search(raw)
            if m:
                dept = m.group(0).strip().rstrip(",;")
                # Keep it short — truncate at 80 chars
                return dept[:80]
    return ""


# ── STATE FILTER: Load universities by state & resolve to OpenAlex IDs ──────────

def load_state_universities(state: str) -> list[str]:
    """
    Return university names for a US state from universities_by_state.json.
    Performs case-insensitive exact match first, then partial match fallback.
    """
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universities_by_state.json")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Exact case-insensitive match
    for key, unis in data.items():
        if key.lower() == state.lower():
            names = [u[0] for u in unis]
            print(f"  Found {len(names)} universities in '{key}'")
            return names

    # Partial match fallback
    matches = [k for k in data if state.lower() in k.lower()]
    if matches:
        key   = matches[0]
        names = [u[0] for u in data[key]]
        print(f"  Partial match: '{key}' -> {len(names)} universities")
        return names

    valid = ", ".join(list(data.keys())[:10])
    raise ValueError(f"State '{state}' not found. Valid examples: {valid} ...")


def resolve_institution_ids(uni_names: list[str]) -> dict[str, str]:
    """
    For each university name, query OpenAlex /institutions to get its ID.
    Only accepts US education institutions.
    Returns {university_name: openalex_id}.
    """
    print(f"  Resolving {len(uni_names)} universities to OpenAlex IDs ...")
    id_map  = {}
    skipped = 0

    for name in uni_names:
        data = api_get(f"{BASE_URL}/institutions", {
            "search":   name,
            "filter":   "country_code:US,type:education",
            "per_page": 1,
        })
        results = data.get("results", [])
        if results:
            oa_id         = results[0]["id"].split("/")[-1]
            id_map[name]  = oa_id
        else:
            print(f"  [Institution not found] '{name}'")
            skipped += 1
        time.sleep(0.05)

    print(f"  Resolved {len(id_map)} / {len(uni_names)}  ({skipped} not found in OpenAlex)")
    return id_map


def fetch_authors_by_institutions(inst_id_map: dict[str, str]) -> dict[str, dict]:
    """
    Pull recent works from all target institutions (no field/subfield filter).
    Count papers per author; return those with >= MIN_PAPERS.
    Institution IDs are batched (BATCH per request) to stay within URL limits.
    Uses (work_id, author_id) deduplication so cross-batch works aren't double-counted.
    """
    all_inst_ids = list(set(inst_id_map.values()))
    print(f"\n[2b/6] Fetching recent works from {len(all_inst_ids)} institutions "
          f"({FROM_DATE} -> {TO_DATE}) ...")

    counts   = defaultdict(int)
    metadata = {}
    seen     = set()   # (work_id, author_id) — deduplication across batches
    BATCH    = 15
    n_batches = (len(all_inst_ids) + BATCH - 1) // BATCH

    for b_start in range(0, len(all_inst_ids), BATCH):
        batch       = all_inst_ids[b_start: b_start + BATCH]
        batch_set   = set(batch)
        inst_filter = "|".join(batch)
        works_filter = (
            f"authorships.institutions.id:{inst_filter},"
            f"from_publication_date:{FROM_DATE},"
            f"to_publication_date:{TO_DATE},"
            f"type:article"
        )
        works = paginate(f"{BASE_URL}/works", {
            "filter": works_filter,
            "select": "id,authorships",
        }, max_results=5000)

        print(f"  Batch {b_start // BATCH + 1}/{n_batches}: {len(works)} works")

        for work in works:
            wid = (work.get("id") or "").split("/")[-1]
            for auth in work.get("authorships", []):
                author = auth.get("author") or {}
                aid    = (author.get("id") or "").split("/")[-1]
                if not aid or (wid, aid) in seen:
                    continue

                # Only count if affiliated with one of the batch institutions
                target_inst = next(
                    (i for i in auth.get("institutions", [])
                     if (i.get("id") or "").split("/")[-1] in batch_set),
                    None,
                )
                if not target_inst:
                    continue

                seen.add((wid, aid))
                counts[aid] += 1
                if aid not in metadata:
                    metadata[aid] = {
                        "author_id":   aid,
                        "name":        author.get("display_name", "Unknown"),
                        "institution": target_inst.get("display_name", "Unknown"),
                    }

        time.sleep(0.1)

    qualifying = {aid: m for aid, m in metadata.items() if counts[aid] >= MIN_PAPERS}
    print(f"  Authors with >={MIN_PAPERS} recent papers: {len(qualifying)}")
    return qualifying


# ── STEP 1: Resolve field to OpenAlex subfield IDs & topic IDs ─────────────────

def resolve_subfield_ids(field: str) -> list[int]:
    """
    Map the user-supplied field name to a list of OpenAlex subfield IDs.
    First checks the hard-coded TARGET_SUBFIELDS map; if the field isn't
    there, falls back to a search on the /subfields endpoint.
    Always returns the union of ALL allowed subfield IDs so that every
    target department is covered.
    """
    # Always use the full union of allowed subfields
    subfield_ids = list(ALLOWED_SUBFIELD_IDS)

    # If the user typed a specific field that appears in TARGET_SUBFIELDS,
    # log which group matched; but we still include everything.
    matched_key = None
    for key in TARGET_SUBFIELDS:
        if key in field.lower():
            matched_key = key
            break

    if matched_key:
        print(f"  Matched target group '{matched_key}' -> subfield IDs {TARGET_SUBFIELDS[matched_key]}")
    else:
        # Fallback: search OpenAlex for subfields matching the field name
        data = api_get(f"{BASE_URL}/subfields", {"search": field, "per_page": 10})
        for sf in data.get("results", []):
            sf_id = int(sf["id"].split("/")[-1])
            if sf_id not in ALLOWED_SUBFIELD_IDS:
                subfield_ids.append(sf_id)
                print(f"  Added subfield via search: {sf.get('display_name')} (id={sf_id})")

    return subfield_ids


def find_topic_ids(field: str) -> list[str]:
    """
    Resolve the field to OpenAlex topic IDs by pulling the exact topic lists
    from the /subfields/<id> endpoint for each target subfield.
    This is far more precise than free-text search which matches unrelated
    topics just because they contain the word 'engineering'.
    """
    print(f"\n[1/6] Resolving '{field}' -> OpenAlex topic IDs via subfields ...")
    subfield_ids = resolve_subfield_ids(field)
    print(f"  Using subfield IDs: {subfield_ids}")

    seen = set()
    ids  = []

    for sf_id in subfield_ids:
        data = api_get(f"{BASE_URL}/subfields/{sf_id}")
        sf_name = data.get("display_name", f"subfield-{sf_id}")
        topics  = data.get("topics", [])
        count   = 0
        for t in topics:
            tid = t["id"].split("/")[-1]
            if tid not in seen:
                seen.add(tid)
                ids.append(tid)
                count += 1
        print(f"  {sf_name}: {count} topics")
        time.sleep(0.05)

    print(f"  Total unique topic IDs: {len(ids)}   sample: {ids[:5]}")
    return ids


# ── STEP 2: Qualifying authors (>= MIN_PAPERS in window) ─────────────────────

def fetch_qualifying_authors(topic_ids: list[str], field: str, no_dept_filter: bool = False) -> dict[str, dict]:
    """
    Pull works from /works filtered by US institution + date range.
    When no_dept_filter=False (default), also filters by topic IDs resolved
    from the field name. Count papers per author; return those with >= MIN_PAPERS.
    """
    print(f"\n[2/6] Fetching recent US '{field}' works ({FROM_DATE} -> {TO_DATE}) ...")
    if no_dept_filter:
        print("  [dept filter OFF] — searching all fields, no subfield restriction")

    if no_dept_filter:
        works_filter = (
            f"institutions.country_code:us,"
            f"from_publication_date:{FROM_DATE},"
            f"to_publication_date:{TO_DATE},"
            f"type:article"
        )
        works = paginate(f"{BASE_URL}/works", {
            "filter": works_filter,
            "select": "id,authorships",
        }, max_results=50000)
    else:
        # Batch topic IDs to avoid URL length issues (~100 per batch)
        TOPIC_BATCH = 100
        all_works = []
        seen_work_ids = set()
        n_batches = (len(topic_ids) + TOPIC_BATCH - 1) // TOPIC_BATCH

        for b_start in range(0, len(topic_ids), TOPIC_BATCH):
            batch = topic_ids[b_start:b_start + TOPIC_BATCH]
            topic_filter = "|".join(batch)
            works_filter = (
                f"institutions.country_code:us,"
                f"topics.id:{topic_filter},"
                f"from_publication_date:{FROM_DATE},"
                f"to_publication_date:{TO_DATE},"
                f"type:article"
            )
            batch_works = paginate(f"{BASE_URL}/works", {
                "filter": works_filter,
                "select": "id,authorships",
            }, max_results=50000)

            # Deduplicate across topic batches
            for w in batch_works:
                wid = w.get("id", "")
                if wid not in seen_work_ids:
                    seen_work_ids.add(wid)
                    all_works.append(w)

            print(f"  Batch {b_start // TOPIC_BATCH + 1}/{n_batches}: {len(batch_works)} works, {len(all_works)} unique total")

        works = all_works

    print(f"  Retrieved {len(works)} unique works")

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

def _pick_institution(author_data: dict, fallback: str = "Unknown") -> str:
    """
    Pick the best institution name from the author profile.
    Priority: last_known_institutions (US, education) > first last_known > fallback.
    """
    lki = author_data.get("last_known_institutions") or []

    # Prefer a US education institution
    for inst in lki:
        if (inst.get("country_code") or "").upper() == "US" and inst.get("type") == "education":
            return inst.get("display_name", fallback)

    # Fallback to any US institution
    for inst in lki:
        if (inst.get("country_code") or "").upper() == "US":
            return inst.get("display_name", fallback)

    # Fallback to first entry
    if lki:
        return lki[0].get("display_name", fallback)

    # last_known_institutions may be empty — try affiliations with most recent year
    for aff in (author_data.get("affiliations") or []):
        inst = aff.get("institution", {})
        if (inst.get("country_code") or "").upper() == "US" and inst.get("type") == "education":
            return inst.get("display_name", fallback)

    return fallback


def _author_matches_department(author_data: dict) -> bool:
    """
    Verify the author's research topics overlap with our target subfields.
    Checks the author's top topics — at least one must belong to an allowed subfield.
    """
    for t in (author_data.get("topics") or [])[:10]:
        subfield = t.get("subfield") or {}
        sf_id_str = subfield.get("id", "")
        if sf_id_str:
            try:
                sf_id = int(sf_id_str.split("/")[-1])
                if sf_id in ALLOWED_SUBFIELD_IDS:
                    return True
            except (ValueError, IndexError):
                pass
    return False


def _fetch_author_batch(batch: list[str], authors: dict[str, dict], no_dept_filter: bool) -> tuple[dict[str, dict], int]:
    """Helper function to fetch a single batch of author profiles."""
    enriched = {}
    filtered_out = 0
    filter_str = "|".join(batch)
    
    data = api_get(f"{BASE_URL}/authors", {
        "filter": f"openalex_id:{filter_str}",
        "per_page": len(batch),
    })
    
    for author_data in data.get("results", []):
        aid = author_data["id"].split("/")[-1]
        
        # ── Department verification: skip authors whose topics don't overlap ──
        if not no_dept_filter and not _author_matches_department(author_data):
            filtered_out += 1
            continue
        
        # ── Use last_known_institutions for CORRECT institution ──
        institution = _pick_institution(author_data,
                                        fallback=authors.get(aid, {}).get("institution", "Unknown"))
        
        enriched[aid] = {
            **authors.get(aid, {}),
            "institution":    institution,
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
    
    return enriched, filtered_out


def fetch_author_profiles(authors: dict[str, dict], no_dept_filter: bool = False) -> dict[str, dict]:
    print(f"\n[3/6] Fetching author profiles (up to {MAX_AUTHORS}) in parallel ({MAX_PARALLEL_WORKERS} workers) ...")
    enriched = {}
    filtered_out = 0
    ids = list(authors.keys())[:MAX_AUTHORS]

    # Process in batches of 50, but run multiple batches in parallel
    batch_size = 50
    batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
    
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_WORKERS) as executor:
        # Submit all batch jobs
        future_to_batch = {
            executor.submit(_fetch_author_batch, batch, authors, no_dept_filter): idx
            for idx, batch in enumerate(batches)
        }
        
        # Collect results as they complete
        completed = 0
        for future in as_completed(future_to_batch):
            batch_enriched, batch_filtered = future.result()
            enriched.update(batch_enriched)
            filtered_out += batch_filtered
            completed += 1
            print(f"  ... {completed}/{len(batches)} batches completed ({len(enriched)} profiles so far)")

    dept_msg = "(dept filter OFF)" if no_dept_filter else f"{filtered_out} filtered (wrong department)"
    print(f"  Done -- {len(enriched)} profiles enriched, {dept_msg}")
    return enriched

# ── STEP 4: Last N papers per author ──────────────────────────────────────────

def _fetch_author_papers(aid: str) -> tuple[str, list[dict], str]:
    """Helper function to fetch papers for a single author."""
    data = api_get(f"{BASE_URL}/works", {
        "filter":   f"authorships.author.id:{aid},type:article",
        "sort":     "publication_date:desc",
        "per_page": LAST_N_PAPERS,
        "select":   "id,title,doi,publication_year,abstract_inverted_index,primary_location,authorships",
    })
    papers = []
    dept = ""
    dept_found = False
    
    for w in data.get("results", []):
        source = (w.get("primary_location") or {}).get("source") or {}
        papers.append({
            "title":    w.get("title", ""),
            "doi":      w.get("doi", ""),
            "year":     w.get("publication_year", ""),
            "journal":  source.get("display_name", ""),
            "abstract": reconstruct_abstract(w.get("abstract_inverted_index")),
        })
        # Extract department from the most recent paper where this author appears
        if not dept_found:
            for auth in (w.get("authorships") or []):
                if aid in ((auth.get("author") or {}).get("id") or ""):
                    raw = auth.get("raw_affiliation_strings") or []
                    dept = parse_department(raw)
                    if dept:
                        dept_found = True
                    break
    
    return aid, papers, dept


def fetch_recent_papers(author_ids: list[str]) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """
    Fetch the most recent LAST_N_PAPERS articles for each author in parallel.
    Also extracts the raw affiliation string from the most recent paper to
    derive a department name via parse_department().
    Returns (author_papers, author_departments).
    """
    print(f"\n[4/6] Fetching last {LAST_N_PAPERS} papers per author in parallel ({MAX_PARALLEL_WORKERS} workers) ...")
    author_papers = {}
    author_departments: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_WORKERS) as executor:
        # Submit all author paper fetch jobs
        future_to_aid = {
            executor.submit(_fetch_author_papers, aid): aid
            for aid in author_ids
        }
        
        # Collect results as they complete
        completed = 0
        for future in as_completed(future_to_aid):
            aid, papers, dept = future.result()
            author_papers[aid] = papers
            if dept:
                author_departments[aid] = dept
            completed += 1
            if completed % 25 == 0:
                print(f"  ... {completed}/{len(author_ids)} authors processed")
    
    print(f"  ... {len(author_ids)}/{len(author_ids)} authors processed")
    return author_papers, author_departments


# ── STEP 5: Score & rank ──────────────────────────────────────────────────────

def score_and_rank(
    profiles:           dict[str, dict],
    author_papers:      dict[str, list[dict]],
    author_departments: dict[str, str],
    keywords:           list[str],
) -> list[dict]:
    """
    Scoring formula (per author):
      keyword_score  = count of unique keywords found in titles + abstracts
      keyword_pct    = keyword_score / len(keywords) * 100
      h_index_bonus  = min(h_index, 50) / 50 * 20          (up to 20 pts)
      cite_bonus     = min(cited_by_count, 20000) / 20000 * 10  (up to 10 pts)
      mech_bonus     = 5 if 'mech' in department (normalized)  (5 pts)
      total_score    = keyword_pct + h_index_bonus + cite_bonus + mech_bonus  (max ~135)

    Tie-break: cited_by_count descending.
    """
    print("\n[5/6] Scoring and ranking ...")
    ranked = []

    for aid, profile in profiles.items():
        found = {kw: False for kw in keywords}
        for paper in author_papers.get(aid, []):
            # Check both abstract AND title for keyword hits
            text = (paper.get("title") or "") + " " + (paper.get("abstract") or "")
            for kw, hit in keyword_hits(text, keywords).items():
                if hit:
                    found[kw] = True

        kw_score = sum(found.values())
        kw_pct   = (kw_score / len(keywords) * 100) if keywords else 0

        h_index        = profile.get("h_index", 0) or 0
        cited_by_count = profile.get("cited_by_count", 0) or 0
        h_bonus   = min(h_index, 50) / 50 * 20
        cite_bonus = min(cited_by_count, 20000) / 20000 * 10
        
        # Mechanical engineering department bonus
        department = author_departments.get(aid, "")
        mech_bonus = 5 if "mech" in _normalize(department) else 0
        
        total_score = round(kw_pct + h_bonus + cite_bonus + mech_bonus, 1)
        # total_score = round(kw_pct, 1)  # For pure keyword ranking, comment out bonuses


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
            "score":       total_score,
            "kw_matched":  kw_score,
            "kw_total":    len(keywords),
            "matched_kws": "; ".join(matched),
            "missed_kws":  "; ".join(missed),
            "department":  author_departments.get(aid, ""),
        })

    ranked = [r for r in ranked if r["kw_matched"] > 0]
    ranked.sort(key=lambda x: (x["score"], x.get("cited_by_count", 0)), reverse=True)
    return ranked


# ── OUTPUT: CSV ───────────────────────────────────────────────────────────────

def save_csv(ranked: list[dict], field: str, keywords: list[str]) -> str:
    """Write ranked results to a CSV. Returns the file path."""
    "SAVES CSV with filename pattern: ranked_professors_run{N}_{field}_{kw1_kw2}.csv"

    # Build filename: ranked_professors_<state_or_uni_or_field>_<first_two_keywords>.csv
    label_slug = (field or DEFAULT_FIELD).strip().replace(" ", "-")
    kw_slug    = "_".join(kw.strip().replace(" ", "-") for kw in (keywords[:2] if keywords else DEFAULT_KEYWORDS[:2]))
    out_path   = f"ranked_professors_{label_slug}_{kw_slug}.csv"
    
    base_cols = [
        "rank", "score", "kw_matched", "kw_total", "name", "institution",
        "department", "openalex_url", "orcid",
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

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=all_cols, extrasaction="ignore")
        writer.writeheader()
        for rank, row in enumerate(ranked, 1):
            writer.writerow({"rank": rank, **row})

    print(f"\n[6/6] CSV saved -> {out_path}  ({len(ranked)} rows, {len(keywords)} keywords scored)")
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
    parser.add_argument(
        "--no-dept-filter",
        action="store_true",
        default=False,
        help="Skip department/topic filtering — rank ALL US authors by keyword density only",
    )
    parser.add_argument(
        "--state", "-s",
        type=str,
        default=None,
        metavar="STATE",
        help=(
            "Restrict search to universities in a specific US state "
            "(e.g., 'Texas', 'California'). "
            "Bypasses field/department filtering — all professors at those "
            "universities are candidates; keyword hits determine shortlisting. "
            "Default: all US universities."
        ),
    )
    parser.add_argument(
        "--uni", "-u",
        nargs="+",
        default=None,
        metavar="UNI",
        help=(
            "Restrict search to one or more specific universities "
            "(e.g., 'MIT', 'UT Austin', 'Texas A&M'). "
            "Partial names and abbreviations work. "
            "Bypasses field/department filtering — same as --state but for individual institutions. "
            "Quote multi-word names."
        ),
    )
    return parser.parse_args()


def main():
    args           = parse_args()
    field          = args.field.strip()
    keywords       = [kw.strip() for kw in args.keywords if kw.strip()]
    no_dept_filter = args.no_dept_filter
    state          = args.state.strip() if args.state else None
    uni_names_arg  = [u.strip() for u in args.uni if u.strip()] if args.uni else None

    print("=" * 72)
    print("  PROFESSOR RANKER -- powered by OpenAlex")
    print("=" * 72)
    print(f"  Field      : {field}")
    print(f"  Keywords   : {keywords}")
    print(f"  Date range : {FROM_DATE} -> {TO_DATE}")
    print(f"  Min papers : {MIN_PAPERS}  |  Max authors: {MAX_AUTHORS}")
    if uni_names_arg:
        print(f"  University : {', '.join(uni_names_arg)}  (dept filter OFF — all fields searched)")
    elif state:
        print(f"  State      : {state}  (dept filter OFF — all fields searched)")
    else:
        print(f"  Dept filter: {'OFF (--no-dept-filter)' if no_dept_filter else 'ON'}")

    # ── Uncomment to enable AI keyword expansion ──────────────────────────────
    # keywords = expand_keywords_with_ai(field, keywords)
    # print(f"  Expanded   : {keywords}")
    # ─────────────────────────────────────────────────────────────────────────

    if uni_names_arg:
        # ── University-based mode: resolve name(s) directly -> authors ────────
        print(f"\n[1/6] University mode: resolving {len(uni_names_arg)} institution(s) ...")
        inst_id_map = resolve_institution_ids(uni_names_arg)
        if not inst_id_map:
            print("  Could not resolve any university. Try a different name or spelling.")
            return
        authors        = fetch_authors_by_institutions(inst_id_map)
        no_dept_filter = True
        csv_label      = "_".join(n.replace(" ", "-") for n in uni_names_arg)[:60]
    elif state:
        # ── State-based mode: resolve universities -> institution IDs -> authors ──
        print(f"\n[1/6] State mode: loading universities for '{state}' ...")
        try:
            uni_names = load_state_universities(state)
        except ValueError as e:
            print(f"  Error: {e}")
            return
        inst_id_map = resolve_institution_ids(uni_names)
        if not inst_id_map:
            print("  No institutions could be resolved. Check the state name.")
            return
        authors        = fetch_authors_by_institutions(inst_id_map)
        no_dept_filter = True
        csv_label      = state
    else:
        # ── Standard field-based mode ─────────────────────────────────────────
        topic_ids = find_topic_ids(field)
        if not topic_ids:
            print(f"\n  No OpenAlex topics found for '{field}'. Try a different field name.")
            return
        authors   = fetch_qualifying_authors(topic_ids, field, no_dept_filter=no_dept_filter)
        csv_label = field

    if not authors:
        print("\n  No qualifying authors found. Try lowering MIN_PAPERS or widening the date range.")
        return

    profiles                          = fetch_author_profiles(authors, no_dept_filter=no_dept_filter)
    author_papers, author_departments = fetch_recent_papers(list(profiles.keys()))
    ranked                            = score_and_rank(profiles, author_papers, author_departments, keywords)

    save_csv(ranked, csv_label, keywords)


if __name__ == "__main__":
    main()
