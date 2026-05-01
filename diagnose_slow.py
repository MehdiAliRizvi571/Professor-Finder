"""
Quick diagnostic to show why fetch_qualifying_authors is slow.
Runs steps 1 & 2 with timing and count logging.
"""
import time
from professor_ranker import (
    find_topic_ids,
    fetch_qualifying_authors,
    ALLOWED_SUBFIELD_IDS,
    TARGET_SUBFIELDS,
    DEFAULT_FIELD,
)

print("=" * 60)
print("DIAGNOSTIC: fetch_qualifying_authors slowness")
print("=" * 60)

field = DEFAULT_FIELD
print(f"\nDefault field: {field!r}")
print(f"TARGET_SUBFIELDS keys: {list(TARGET_SUBFIELDS.keys())}")
print(f"ALLOWED_SUBFIELD_IDS count: {len(ALLOWED_SUBFIELD_IDS)}")
print(f"ALLOWED_SUBFIELD_IDS: {sorted(ALLOWED_SUBFIELD_IDS)}")

t0 = time.time()
topic_ids = find_topic_ids(field)
t1 = time.time()
print(f"\n[DIAG] find_topic_ids took {t1-t0:.1f}s  ->  {len(topic_ids)} topic IDs")

n_batches = (len(topic_ids) + 100 - 1) // 100
max_results_per_batch = max(1000, 50000 // n_batches)
print(f"[DIAG] n_batches={n_batches}  max_results_per_batch={max_results_per_batch}")
print(f"[DIAG] pages per batch ≈ {max_results_per_batch // 100}  (at 100 per page)")
print(f"[DIAG] sleep per batch ≈ {(max_results_per_batch // 100) * 0.12:.1f}s  (0.12s/page)")

# Show first few topic IDs
print(f"[DIAG] First 10 topic IDs: {topic_ids[:10]}")

# Run a single batch as a smoke-test to see timing
t2 = time.time()
if topic_ids:
    batch = topic_ids[:100]
    topic_filter = "|".join(batch)
    from datetime import date
    TO_DATE   = date.today().strftime("%Y-%m-%d")
    FROM_DATE = date(date.today().year - 2, date.today().month, date.today().day).strftime("%Y-%m-%d")
    works_filter = (
        f"institutions.country_code:us,"
        f"topics.id:{topic_filter},"
        f"from_publication_date:{FROM_DATE},"
        f"to_publication_date:{TO_DATE},"
        f"type:article"
    )
    from professor_ranker import paginate, BASE_URL
    works = paginate(f"{BASE_URL}/works", {
        "filter": works_filter,
        "select": "id,authorships",
    }, max_results=max_results_per_batch)
    t3 = time.time()
    print(f"\n[DIAG] SINGLE batch (100 topics) fetched {len(works)} works in {t3-t2:.1f}s")
    print(f"[DIAG] Projected total time (sequential): {n_batches * (t3-t2):.1f}s")
    print(f"[DIAG] Projected total time (15 workers): {((n_batches / 15) + 1) * (t3-t2):.1f}s")
