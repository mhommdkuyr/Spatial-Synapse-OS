# Ibb: Google Places name-by-name enrichment

## Source-of-truth input

`data/ibb_source_names_11050.csv.gz` contains **11,050 unique source IDs and names**, extracted from the accepted Drive file `Ibb_Businesses_All.geojson` (Drive file ID `1Ng_Mo420BOEuxsGZXCw3GOzaMqkL2kHf`). The original names are preserved verbatim. The input intentionally excludes source phones, addresses, ratings, and coordinates: the earlier audit found that many of those fields were repetitive or synthetic, and they must not be treated as evidence.

## Search policy

`scripts/ibb_google_name_by_name.py` sends one official Google Places API (New) Text Search request per source name, with the query `"<exact source name>" Ibb, Yemen`. A 7 km city-centred location bias is applied. It requests Google Place ID, display name, address, coordinates, phone, website, opening hours, category, ratings, Google Maps URI, and business status.

Each result is checkpointed to JSONL immediately. Re-running the workflow resumes by `source_id` and does not re-query completed names. Candidate names are scored, but only an exact normalized name inside the Ibb bounding box is automatically marked `exact_name_in_ibb_bbox`. Strong and possible fuzzy matches remain review candidates. No result means **not found in this query**, not proof that a business does not exist.

## Required credential

The repository currently has **no GitHub Actions secrets**. Add `GOOGLE_PLACES_API_KEY` under GitHub → repository Settings → Secrets and variables → Actions. The key must have Google Places API (New) enabled and an active billing/quota configuration. Do not commit the key into source files.

## Run

Open Actions → `Ibb Google Places — one query per source name` → Run workflow.

- `pilot` searches the first 50 source names.
- `full` searches all 11,050 names individually. It is blocked unless `full_run_confirmation` is exactly `RUN_11050`. This extra confirmation exists because every name triggers a billable Google Places API request.
- `max_qps` controls request pacing (0.1–10; default 1).

The workflow uploads raw JSONL candidate evidence and the consolidated CSV/report as a 30-day artifact, then commits the consolidated CSV/report to the selected branch. It never writes to `main` unless a user deliberately runs the workflow on `main`; the intended branch is `feature/ibb-per-name-google-enrichment-2026-11-05`.

## Output fields

The consolidated CSV has one row per source name with the exact query, status, best candidate, Google Place ID, Google Maps URI, address, coordinates, phone, website, category, opening hours, rating/review count, business status, query timestamp, and error field. Candidate-level raw responses are in JSONL.

## Independently searched web pilot

`data/ibb_google_web_search_pilot_2026-11-05.csv` contains eight individually queried names from an initial Google-indexed web-search pilot. These are separate from the official Places API output and are labelled by their actual source URLs and match limitations. They must not be counted as 11,050 Places API searches.
