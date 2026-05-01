"""Quick test to verify slowness fixes."""
import time
from professor_ranker import find_topic_ids, fetch_qualifying_authors, DEFAULT_FIELD

field = DEFAULT_FIELD

t0 = time.time()
topic_ids = find_topic_ids(field)
t1 = time.time()

n_batches = (len(topic_ids) + 100 - 1) // 100
from professor_ranker import min as prof_min
max_results_per_batch = prof_min(1000, max(500, 50000 // n_batches))

print(f"\n=== FIX VERIFICATION ===")
print(f"Field: {field!r}")
print(f"Topic IDs: {len(topic_ids)}  (was 768)")
print(f"Batches: {n_batches}  (was 8)")
print(f"Max results/batch: {max_results_per_batch}")
print(f"Pages/batch: {max_results_per_batch // 100}  (was 62)")
print(f"Sleep/batch: {(max_results_per_batch // 100) * 0.12:.1f}s  (was 7.4s)")
print(f"find_topic_ids took: {t1-t0:.1f}s")

# Try fetching one batch to see timing
if topic_ids:
    from professor_ranker import paginate, BASE_URL
    from datetime import date
    TO_DATE = date.today().strftime("%Y-%m-%d")
    FROM_DATE = date(date.today().year - 2, date.today().month, date.today().day).strftime("%Y-%m-%d")
    batch = topic_ids[:min(100, len(topic_ids))]
    topic_filter = "|".join(batch)
    works_filter = (
        f"institutions.country_code:us,"
        f"topics.id:{topic_filter},"
        f"from_publication_date:{FROM_DATE},"
        f"to_publication_date:{TO_DATE},"
        f"type:article"
    )
    t2 = time.time()
    works = paginate(f"{BASE_URL}/works", {
        "filter": works_filter,
        "select": "id,authorships",
    }, max_results=max_results_per_batch)
    t3 = time.time()
    print(f"Single batch fetch: {len(works)} works in {t3-t2:.1f}s")
    print(f"Projected total fetch_qualifying_authors: ~{n_batches * (t3-t2) / 5:.0f}s (5 workers)")
