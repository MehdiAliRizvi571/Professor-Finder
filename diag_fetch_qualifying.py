"""Diagnostic script for fetch_qualifying_authors slowness."""
import time
from professor_ranker import (
    find_topic_ids, api_get, paginate, BASE_URL,
    FROM_DATE, TO_DATE,
)

FIELD = "mechanical engineering"
TOPIC_BATCH = 100

def main():
    print("=" * 60)
    print("DIAGNOSTIC: fetch_qualifying_authors bottleneck")
    print("=" * 60)

    # Step 1: How many topic IDs?
    t0 = time.time()
    topic_ids = find_topic_ids(FIELD)
    t1 = time.time()
    n_batches = (len(topic_ids) + TOPIC_BATCH - 1) // TOPIC_BATCH
    print(f"\n1. find_topic_ids took {t1 - t0:.1f}s")
    print(f"   Total topic IDs: {len(topic_ids)}  ->  {n_batches} batches of ~{TOPIC_BATCH}")

    # Step 2: Single-page (no pagination) test for first batch
    print(f"\n2. Single-page query test (batch 1/{n_batches}) ...")
    batch1 = topic_ids[:TOPIC_BATCH]
    topic_filter = "|".join(batch1)
    works_filter = (
        f"institutions.country_code:us,"
        f"topics.id:{topic_filter},"
        f"from_publication_date:{FROM_DATE},"
        f"to_publication_date:{TO_DATE},"
        f"type:article"
    )
    t0 = time.time()
    single = api_get(
        f"{BASE_URL}/works",
        {"filter": works_filter, "select": "id,authorships", "per_page": 100, "cursor": "*"}
    )
    t1 = time.time()
    if single:
        n_results = len(single.get("results", []))
        meta = single.get("meta", {})
        print(f"   Response time: {t1 - t0:.2f}s")
        print(f"   Results on first page: {n_results}")
        print(f"   Meta: {meta}")
    else:
        print(f"   FAILED or empty response in {t1 - t0:.2f}s")

    # Step 3: Small pagination test (max_results=500) for first batch
    print(f"\n3. Small pagination test (max_results=500) ...")
    t0 = time.time()
    small = paginate(
        f"{BASE_URL}/works",
        {"filter": works_filter, "select": "id,authorships"},
        max_results=500,
    )
    t1 = time.time()
    print(f"   Paginate time: {t1 - t0:.1f}s  |  Works returned: {len(small)}")
    if len(small) >= 500:
        print("   >>> This batch has >=500 works — full pagination would be very slow.")

    # Step 4: Estimate total time
    print(f"\n4. ESTIMATE:")
    print(f"   If batch 1 returns >=500 works in {t1 - t0:.1f}s,")
    print(f"   then full 50,000 pagination per batch would need ~{50000 / 500:.0f}x more requests.")
    print(f"   With {n_batches} batches sequentially, total time could exceed 10+ minutes.")

    print("\n" + "=" * 60)
    print("RECOMMENDATION: Parallelize batch fetching + cap total works")
    print("=" * 60)


if __name__ == "__main__":
    main()
