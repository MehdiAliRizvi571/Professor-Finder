# Professor Ranker — Full Pipeline Flow

## Bird's-eye view

```
User runs:  py -3.10 professor_ranker.py --field "mechanical engineering" -k "heat transfer" ...
                │
                ▼
[1/6]  Resolve field → OpenAlex topic IDs
[2/6]  Find qualifying authors  (works query + paper-count filter)
[3/6]  Enrich author profiles   (institution, dept verification)
[4/6]  Fetch recent papers       (last 10 per author + department string)
[5/6]  Score & rank              (keyword density + h-index + citations)
[6/6]  Save CSV
```

---

## Step 1 — Resolve field → topic IDs
**Function:** `find_topic_ids(field)`

1. Look up `field` in the hard-coded `TARGET_SUBFIELDS` dict.
   - e.g. `"mechanical engineering"` maps to subfield IDs:
     `2210, 2206, 2211, 2209, 2203, 2202` (+ all the other allowed groups)
2. For **each subfield ID**, call:
   ```
   GET https://api.openalex.org/subfields/<id>
   ```
   The response contains a `topics` list — every OpenAlex topic that belongs
   to that subfield.
3. Collect all topic IDs into a deduplicated list (e.g. `["T10236", "T10710", ...]`).

> These topic IDs **are** used in Step 2 to make sure we only grab papers that
> belong to those specific research areas. This keeps the initial pool relevant.

---

## Step 2 — Fetch qualifying authors
**Function:** `fetch_qualifying_authors(topic_ids, field)`

### 2a. The works query + pagination

The script asks OpenAlex for up to **50,000 papers** (instead of the old 1,000) so we get a much bigger starting pool.

**When a field is supplied (default mode):**
Instead of filtering by broad subfield IDs, it now filters by the **exact topic IDs** gathered in Step 1. Because the list of topic IDs can be very long (hundreds of IDs), the script splits them into smaller batches of ~100 and sends separate requests. Any paper that matches *any* of those batches is kept, and duplicates across batches are removed automatically.

```python
# simplified view — internally batched in ~100-topic chunks
works = paginate(BASE_URL + "/works", {
    "filter": "institutions.country_code:us,"
              "topics.id:T10236|T10710|...,   # ← exact topic IDs from Step 1
              "from_publication_date:2024-04-14,"
              "to_publication_date:2026-04-14,"
              "type:article",
    "select": "id,authorships",
}, max_results=50000)
```

**What `paginate()` does internally:**

```
Request page 1  →  GET /works?filter=...&per_page=100&cursor=*
                   ← { results: [...100 works...], meta: { next_cursor: "abc123" } }

Request page 2  →  GET /works?filter=...&per_page=100&cursor=abc123
                   ← { results: [...100 works...], meta: { next_cursor: "def456" } }

... repeat until:
  • next_cursor is null  (no more pages), OR
  • total collected >= max_results (5000 cap)
```

Each `work` object returned looks like:
```json
{
  "id": "https://openalex.org/W123",
  "authorships": [
    {
      "author": { "id": "https://openalex.org/A456", "display_name": "Jane Doe" },
      "institutions": [
        { "display_name": "MIT", "country_code": "US", "type": "education" }
      ]
    },
    ...
  ]
}
```

### 2b. Author counting

For every authorship in every work:
- Skip if author has no US institution.
- Increment `counts[author_id]`.
- Store the author's name + institution in `metadata[author_id]`.

### 2c. Filter by paper count

```python
qualifying = { aid: meta for aid, meta in metadata.items()
               if counts[aid] >= MIN_PAPERS }   # MIN_PAPERS = 3
```

Only authors with **≥ 3 papers** in the 2-year window proceed.

> With `--no-dept-filter`, the `topics.id` part is removed from the filter, so
> all US articles are searched regardless of field.

---

## Step 3 — Enrich author profiles
**Function:** `fetch_author_profiles(authors)`

Batch API calls (50 authors per request):
```
GET /authors?filter=openalex_id:A456|A789|A012|...&per_page=50
```

For each author returned:

| Check | What happens |
|---|---|
| `_author_matches_department()` | Looks at author's top-10 topics; if none belongs to an allowed subfield → **filtered out** |
| `_pick_institution()` | Reads `last_known_institutions` for their **current** university (not the paper's affiliation) |

Enriched profile fields saved: `name`, `institution`, `openalex_url`, `orcid`,
`homepage_url`, `h_index`, `cited_by_count`, `works_count`, `top_topics`.

---

## Step 4 — Fetch recent papers
**Function:** `fetch_recent_papers(author_ids)`

For each author:
```
GET /works?filter=authorships.author.id:<aid>,type:article
          &sort=publication_date:desc
          &per_page=10
          &select=id,title,doi,publication_year,abstract_inverted_index,
                  primary_location,authorships
```

- Reconstructs abstract from OpenAlex's **inverted index** format
  (a `{word: [position, ...]}` dict → plain text string).
- Scans `raw_affiliation_strings` on the most recent paper to extract
  the author's **department** via regex (e.g. `"Department of Mechanical Engineering, MIT"`).

---

## Step 5 — Score & rank
**Function:** `score_and_rank(profiles, author_papers, author_departments, keywords)`

For each author, search all 10 papers (title + abstract) for each keyword.

**Keyword matching is normalized:**
```
"physics-informed"  →  "physics informed"
"data_driven"       →  "data driven"
```
Both the keyword and the text are lowercased and hyphens/underscores collapsed
to spaces before comparison, so `"physics-informed"` matches `"physics informed"`.

**Score formula:**
```
keyword_score = number of unique keywords found across all papers
keyword_pct   = keyword_score / total_keywords * 100        (0–100)
h_bonus       = min(h_index, 50) / 50 * 20                  (0–20)
cite_bonus    = min(cited_by_count, 20_000) / 20_000 * 10   (0–10)

total_score   = keyword_pct + h_bonus + cite_bonus           (max ~130)
```

Authors with **0 keyword matches** are dropped entirely.
Final list is sorted by `total_score` descending (tie-break: `cited_by_count`).

---

## Step 6 — Save CSV
**Function:** `save_csv(ranked, field, keywords)`

- Increments `RUN_COUNT` in `.env`.
- Output filename: `ranked_professors_run{N}_{kw1}_{kw2}.csv`
- Encoding: **UTF-8 with BOM** (`utf-8-sig`) so Excel opens it correctly.

**CSV columns:**

| Column | Description |
|---|---|
| `rank` | 1-based rank |
| `score` | Total score (max ~130) |
| `kw_matched` | Number of keywords matched |
| `kw_total` | Total keywords searched |
| `name` | Author display name |
| `institution` | Current university (from `last_known_institutions`) |
| `department` | Parsed from raw affiliation string of most recent paper |
| `openalex_url` | Link to OpenAlex author profile |
| `orcid` | ORCID if available |
| `homepage_url` | Personal/lab website if available |
| `h_index` | OpenAlex h-index |
| `cited_by_count` | Total citations |
| `works_count` | Total publications |
| `top_topics` | Author's top 3 research topics |
| `matched_kws` | Semicolon-separated list of matched keywords |
| `missed_kws` | Semicolon-separated list of missed keywords |
| `paper1_title` … `paper10_title` | Titles of last 10 papers |
| `paper1_year` … `paper10_year` | Publication years |
| `paper1_journal` … | Journal names |
| `paper1_doi` … | DOIs |
| `paper1_abstract_snippet` … | First 300 chars of abstract |

---

## Three ways to search

| Mode | What it does | Best for |
|---|---|---|
| **Default** (field-based) | Uses the field you provide (e.g. *mechanical engineering*) to pull topic IDs, then fetches papers tagged with those topics. Only professors from those topics are shortlisted. | Finding the best professors in a specific research area |
| **No-dept-filter** (`--no-dept-filter`) | Ignores the field entirely. Looks at *all* US papers in the date range and ranks by keyword matches only. | When you care about keywords more than department — e.g. "data science" professors who could be in CS, stats, or engineering |
| **University** (`--uni`) | You name one or more universities. The script only looks at papers from those institutions and ignores the field filter. | Checking a specific school or comparing a handful of schools |
| **State** (`--state`) | You name a US state. The script loads every university in that state, then only looks at papers from those institutions. Field is ignored. | Seeing all candidates in a geographic area |

```powershell
# Default — mechanical engineering, field filter ON:
py -3.10 .\professor_ranker.py

# Custom field + keywords:
py -3.10 .\professor_ranker.py --field "environmental engineering" -k "wastewater" "membrane" "nanofiltration"

# No-dept-filter — rank ALL US authors by keyword density only:
py -3.10 .\professor_ranker.py --no-dept-filter -k "heat transfer" "thermal management"

# Specific universities — only look at MIT + Stanford papers:
py -3.10 .\professor_ranker.py --uni "MIT" "Stanford University" -k "robotics" "control systems"

# State-based — only professors at universities in Texas:
py -3.10 .\professor_ranker.py --state "Texas" -k "heat transfer" "machine learning"

# State + custom keywords (field arg is ignored in state mode):
py -3.10 .\professor_ranker.py -s "California" -k "combustion" "alternative fuels" "NOx"
```

---

## State-filter mode (`--state`)

When `--state <StateName>` is provided, the pipeline follows a different path for Step 2
and bypasses all field/department filtering:

```
[1/6]  State mode: load university names from universities_by_state.json
         └─ resolve_institution_ids()   → OpenAlex /institutions?search=<name>
                                          (filters: country_code:US, type:education)
[2b/6] fetch_authors_by_institutions()
         → /works?filter=authorships.institutions.id:<id1>|<id2>|...
                        &from_publication_date=...&to_publication_date=...
                        &type=article
         → batched in groups of 15 institution IDs
         → (work_id, author_id) deduplication across batches
         → authors with >= MIN_PAPERS qualify
[3/6]  fetch_author_profiles()          (no_dept_filter=True — no topic check)
[4/6]  fetch_recent_papers()            (last LAST_N_PAPERS papers per author)
[5/6]  score_and_rank()                 (keyword scoring; 0-match authors dropped)
[6/6]  save_csv()
```

### Key differences across modes

| Aspect | Default (field-based) | `--no-dept-filter` | `--uni` | `--state` |
|---|---|---|---|---|
| Author discovery | Papers tagged with the field's **topic IDs** | **All** US papers | Papers from named universities | Papers from universities in that state |
| Field / topic filter | ON | OFF | OFF | OFF |
| Institution restriction | Any US university | Any US university | Only named universities | Only universities in that state |
| Keyword shortlisting | Score ≥ 1 keyword | Score ≥ 1 keyword | Score ≥ 1 keyword | Score ≥ 1 keyword |
| CSV label | field name | `no_dept_filter` | uni names joined | state name |
