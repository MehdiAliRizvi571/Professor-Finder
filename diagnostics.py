"""
diagnostics.py
────────────────────────────────────────────────────────────────────────────────
Run per-professor diagnostics to determine WHY a professor was absent from the
ranked CSV produced by professor_ranker.py --uni "Washington State University".

For each professor it checks:
  Stage A - Is the author findable in OpenAlex at all?
  Stage B - Are they associated with WSU in OpenAlex?
  Stage C - Do they have >= MIN_PAPERS (3) articles in the last 2 years?
  Stage D - Do any of their last 10 papers match the DEFAULT_KEYWORDS?

Usage:
    py -3.10 diagnostics.py
"""

import sys, os, re, time, json
import requests

# Force UTF-8 stdout so non-latin characters in paper titles don't crash on Windows cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import date
from dotenv import load_dotenv

load_dotenv()

# ── Mirror constants from professor_ranker.py ──────────────────────────────────
BASE_URL  = "https://api.openalex.org"
EMAIL     = os.getenv("OPENALEX_EMAIL", "")
HEADERS   = {"User-Agent": f"professor-ranker-diag/1.0 (mailto:{EMAIL})"}
MIN_PAPERS = 3
LAST_N     = 10
TO_DATE    = date.today().strftime("%Y-%m-%d")
FROM_DATE  = date(date.today().year - 2, date.today().month, date.today().day).strftime("%Y-%m-%d")
WSU_NAME   = "Washington State University"

DEFAULT_KEYWORDS = [
    "machine learning combustion", "NSGA-III", "surrogate model",
    "spark ignition engine", "internal combustion engine", "physics-informed",
    "alternative fuels", "convex hull", "response surface methodology",
    "fuel blend", "alcohol gasoline", "oxygenated fuel", "biofuels",
    "brake thermal efficiency", "NOx prediction", "emission reduction",
    "emissions optimization", "engine emissions", "gradient boosting",
    "XGBoost", "evolutionary algorithm", "Pareto optimization",
    "data-driven optimization", "digital twin", "knowledge graph",
    "scientific machine learning", "neural operator", "Neo4j",
    "graph neural network", "stochastic modeling", "Gaussian process",
    "techno-economic analysis", "LCA", "life cycle assessment",
    "multi-objective optimization", "heat transfer", "computational fluid dynamics",
    "data driven thermodynamics", "thermal management", "renewable energy systems",
    "energy optimization", "decarbonization", "energy transition",
    "machine learning", "artificial intelligence", "sustainability",
]

NOT_FOUND_PROFS = [
    "Hu Yueqi",
    "Emily Larsen",
    "Changki Mo",
    "Charles Pezeshki",
    "Anura Rathnayake",
    "John Swensen",
]

FOUND_PROFS = [
    "Amit Bandyopadhyay",
    "Mehdi Hosseinzadeh",
    "Satyajit Mojumder",
    "Lloyd Smith",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def api_get(url, params=None):
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 ** attempt)
            elif r.status_code >= 500:
                time.sleep(2 ** attempt)
            else:
                return {}
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return {}


def _normalize(text):
    return re.sub(r'[\s\-_]+', ' ', text.lower()).strip()


def keyword_hits(text, keywords):
    norm = _normalize(text)
    return {kw: (_normalize(kw) in norm) for kw in keywords}


def reconstruct_abstract(inverted_index):
    if not inverted_index:
        return ""
    max_pos = max(pos for positions in inverted_index.values() for pos in positions)
    tokens  = [""] * (max_pos + 1)
    for word, positions in inverted_index.items():
        for pos in positions:
            tokens[pos] = word
    return " ".join(tokens)


# ── Core diagnostic per professor ─────────────────────────────────────────────

def diagnose(name: str, is_expected_found: bool = False) -> dict:
    label = "EXPECTED FOUND" if is_expected_found else "MISSING"
    print(f"\n{'='*72}")
    print(f"  [{label}]  {name}")
    print(f"{'='*72}")

    result = {
        "name":             name,
        "expected_found":   is_expected_found,
        "openalex_found":   False,
        "wsu_affiliated":   False,
        "recent_papers":    0,
        "passes_min_papers": False,
        "keyword_hits":     0,
        "matched_keywords": [],
        "top_topics":       [],
        "failure_stage":    None,
        "notes":            [],
        "all_paper_titles": [],
    }

    # ── Stage A: Search OpenAlex for this author ──────────────────────────────
    data = api_get(f"{BASE_URL}/authors", {"search": name, "per_page": 5})
    candidates = data.get("results", [])
    if not candidates:
        result["failure_stage"] = "A - Not found in OpenAlex at all"
        result["notes"].append("No author record in OpenAlex matching this name.")
        print(f"  [A] NOT IN OPENALEX — no author record found for '{name}'")
        return result

    # Pick best candidate: prefer one affiliated with WSU
    best = None
    for c in candidates:
        lki = c.get("last_known_institutions") or []
        for inst in lki:
            if "washington state" in (inst.get("display_name") or "").lower():
                best = c
                break
        if best:
            break
    if not best:
        best = candidates[0]

    aid       = best["id"].split("/")[-1]
    disp_name = best.get("display_name", "?")
    lki       = best.get("last_known_institutions") or []
    inst_names = [i.get("display_name", "") for i in lki]
    topics_raw = best.get("topics") or []
    top_topics = [t.get("display_name", "") for t in topics_raw[:6]]
    result["top_topics"]   = top_topics
    result["openalex_found"] = True

    print(f"  [A] Found in OpenAlex:  display_name='{disp_name}'  id={aid}")
    print(f"      Last known inst(s): {inst_names}")
    print(f"      Top topics:         {top_topics}")

    # ── Stage B: WSU affiliation check ────────────────────────────────────────
    wsu_affiliated = any(
        "washington state" in (i.get("display_name") or "").lower()
        for i in lki
    )
    result["wsu_affiliated"] = wsu_affiliated

    if not wsu_affiliated:
        result["failure_stage"] = "B - Not affiliated with WSU in OpenAlex"
        result["notes"].append(
            f"Last known institution(s) in OpenAlex: {inst_names}. "
            "WSU not among them — fetch_authors_by_institutions would miss this author "
            "unless they published a WSU-affiliated paper in the date window."
        )
        print(f"  [B] WSU AFFILIATION MISSING in last_known_institutions")

    # ── Stage C: Count recent WSU-affiliated papers ────────────────────────────
    # Query works authored by this person from WSU in the date window
    wsu_data = api_get(f"{BASE_URL}/authors/{aid}")
    wsu_inst_ids = []
    for aff in (wsu_data.get("affiliations") or []):
        inst = aff.get("institution", {})
        if "washington state" in (inst.get("display_name") or "").lower():
            wsu_inst_ids.append(inst.get("id", "").split("/")[-1])

    # Also try last_known_institutions
    for inst in lki:
        if "washington state" in (inst.get("display_name") or "").lower():
            iid = (inst.get("id") or "").split("/")[-1]
            if iid and iid not in wsu_inst_ids:
                wsu_inst_ids.append(iid)

    print(f"  [C] WSU institution IDs found for this author: {wsu_inst_ids}")

    # Count papers in the date window (any institution, just by author)
    recent_works_data = api_get(f"{BASE_URL}/works", {
        "filter":   f"authorships.author.id:{aid},type:article,from_publication_date:{FROM_DATE},to_publication_date:{TO_DATE}",
        "sort":     "publication_date:desc",
        "per_page": 50,
        "select":   "id,title,publication_year,authorships,abstract_inverted_index",
    })
    recent_works = recent_works_data.get("results", [])
    result["recent_papers"] = len(recent_works)
    result["passes_min_papers"] = len(recent_works) >= MIN_PAPERS

    print(f"  [C] Recent articles ({FROM_DATE} -> {TO_DATE}): {len(recent_works)}")
    if len(recent_works) < MIN_PAPERS:
        result["failure_stage"] = result["failure_stage"] or f"C - Only {len(recent_works)} recent paper(s) (need {MIN_PAPERS})"
        result["notes"].append(
            f"Only {len(recent_works)} article(s) in the last 2 years in OpenAlex. "
            f"MIN_PAPERS={MIN_PAPERS} threshold not met."
        )

    # Check if any recent work is WSU-affiliated (matching how fetch_authors_by_institutions works)
    wsu_affiliated_recent = 0
    for w in recent_works:
        for auth in (w.get("authorships") or []):
            if aid in ((auth.get("author") or {}).get("id") or ""):
                for inst in (auth.get("institutions") or []):
                    if "washington state" in (inst.get("display_name") or "").lower():
                        wsu_affiliated_recent += 1
                        break
    print(f"  [C] Of those, WSU-affiliated works (used for counting): {wsu_affiliated_recent}")
    if wsu_affiliated_recent < MIN_PAPERS:
        stage_c_msg = f"C - Only {wsu_affiliated_recent} WSU-affiliated papers in window (need {MIN_PAPERS})"
        if result["failure_stage"] is None or "B" not in result["failure_stage"]:
            result["failure_stage"] = result["failure_stage"] or stage_c_msg
        result["notes"].append(
            f"Only {wsu_affiliated_recent} recent works have an explicit WSU affiliation in OpenAlex metadata."
        )

    # ── Stage D: Keyword matching on last 10 papers ───────────────────────────
    papers_data = api_get(f"{BASE_URL}/works", {
        "filter":   f"authorships.author.id:{aid},type:article",
        "sort":     "publication_date:desc",
        "per_page": LAST_N,
        "select":   "id,title,publication_year,abstract_inverted_index",
    })
    papers = papers_data.get("results", [])

    found_kws = {kw: False for kw in DEFAULT_KEYWORDS}
    titles    = []
    for p in papers:
        title    = p.get("title") or ""
        abstract = reconstruct_abstract(p.get("abstract_inverted_index"))
        text     = title + " " + abstract
        titles.append(f"[{p.get('publication_year','')}] {title[:100]}")
        for kw, hit in keyword_hits(text, DEFAULT_KEYWORDS).items():
            if hit:
                found_kws[kw] = True

    matched = [kw for kw, hit in found_kws.items() if hit]
    result["keyword_hits"]     = len(matched)
    result["matched_keywords"] = matched
    result["all_paper_titles"] = titles

    print(f"  [D] Papers fetched for keyword scoring: {len(papers)}")
    print(f"  [D] Keywords matched: {len(matched)} / {len(DEFAULT_KEYWORDS)}")
    if matched:
        print(f"      Matched: {matched}")
    else:
        print(f"      NO KEYWORDS MATCHED -- this author would be DROPPED by score_and_rank()")
        if result["failure_stage"] is None:
            result["failure_stage"] = "D - Zero keyword matches (filtered by score_and_rank)"
            result["notes"].append(
                "Author passes pipeline stages A-C but has 0 keyword matches across their "
                f"last {LAST_N} papers. score_and_rank() drops authors with kw_matched==0."
            )

    print(f"  [D] Recent paper titles:")
    for t in titles[:10]:
        print(f"      - {t}")

    time.sleep(0.3)
    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 72)
    print("  PROFESSOR RANKER - DIAGNOSTICS")
    print(f"  WSU - Date window: {FROM_DATE} -> {TO_DATE}  |  MIN_PAPERS={MIN_PAPERS}")
    print("=" * 72)

    results = []
    print("\n\n>>> DIAGNOSING: PROFESSORS NOT FOUND IN CSV <<<")
    for name in NOT_FOUND_PROFS:
        r = diagnose(name, is_expected_found=False)
        results.append(r)
        time.sleep(0.5)

    print("\n\n>>> CONTROL GROUP: PROFESSORS FOUND IN CSV <<<")
    for name in FOUND_PROFS:
        r = diagnose(name, is_expected_found=True)
        results.append(r)
        time.sleep(0.5)

    # ── Summary table ─────────────────────────────────────────────────────────
    print("\n\n" + "=" * 72)
    print("  DIAGNOSTIC SUMMARY")
    print("=" * 72)
    header = f"{'Name':<28} {'OA?':>4} {'WSU?':>5} {'Rec':>4} {'KW':>4}  Failure stage"
    print(header)
    print("-" * 72)
    for r in results:
        tag = "Y" if r["expected_found"] else "N"
        print(
            f"  [{tag}] {r['name']:<24} "
            f"{'Y' if r['openalex_found'] else 'N':>4} "
            f"{'Y' if r['wsu_affiliated'] else 'N':>5} "
            f"{r['recent_papers']:>4} "
            f"{r['keyword_hits']:>4}  "
            f"{r['failure_stage'] or 'NONE (should be in CSV)'}"
        )

    print("\n\n" + "=" * 72)
    print("  PER-PROFESSOR NOTES & ROOT CAUSES")
    print("=" * 72)
    for r in results:
        tag  = "FOUND" if r["expected_found"] else "MISSING"
        print(f"\n  [{tag}] {r['name']}")
        print(f"    Top topics : {r['top_topics']}")
        print(f"    Matched KWs: {r['matched_keywords']}")
        for note in r["notes"]:
            print(f"    [!] {note}")

    print("\n\n" + "=" * 72)
    print("  KEYWORD GAP ANALYSIS")
    print("  (Topics found in missing profs that have NO matching keyword)")
    import sys; sys.stdout.reconfigure(errors='replace') if hasattr(sys.stdout, 'reconfigure') else None
    print("=" * 72)
    missing_results = [r for r in results if not r["expected_found"]]
    all_topics = []
    for r in missing_results:
        all_topics.extend(r["top_topics"])
    from collections import Counter
    topic_counts = Counter(all_topics)
    print(f"  Top topics across {len(NOT_FOUND_PROFS)} missing professors:")
    for topic, cnt in topic_counts.most_common(20):
        print(f"    {cnt}x  {topic}")


if __name__ == "__main__":
    main()
