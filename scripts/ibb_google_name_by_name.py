#!/usr/bin/env python3
"""Search every Ibb source name separately using the official Google Places API (New).

Input rows are treated as search keys only. Source addresses, phones, ratings and
coordinates are intentionally not trusted or copied into Google result fields.
One API request is sent for each source name. Results are resumable and retain
candidate-level provenance; fuzzy candidates are never silently promoted to verified.
"""
from __future__ import annotations
import argparse, csv, difflib, gzip, json, os, re, sys, time, unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import requests

API_URL = "https://places.googleapis.com/v1/places:searchText"
CITY_CENTER = {"latitude": 13.9750, "longitude": 44.1833}
IBB_BBOX = {"west": 44.12, "east": 44.25, "south": 13.90, "north": 14.04}
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.location",
    "places.nationalPhoneNumber", "places.internationalPhoneNumber", "places.websiteUri",
    "places.regularOpeningHours", "places.primaryTypeDisplayName", "places.types",
    "places.googleMapsUri", "places.rating", "places.userRatingCount",
    "places.businessStatus", "places.plusCode"
])
OUT_FIELDS = [
    "source_id", "source_name", "search_query", "search_status", "match_decision",
    "match_score", "best_match_name", "google_place_id", "google_maps_url",
    "address", "latitude", "longitude", "phone_international", "phone_national",
    "website", "category", "opening_hours", "rating", "user_rating_count",
    "business_status", "plus_code", "provider", "queried_at", "error"
]

class ApiFatalError(RuntimeError):
    pass

def normalize(value: object) -> str:
    s = unicodedata.normalize("NFKC", str(value or "")).casefold()
    s = re.sub(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]", "", s)
    s = s.translate(str.maketrans({"أ":"ا", "إ":"ا", "آ":"ا", "ى":"ي", "ؤ":"و", "ئ":"ي"}))
    s = re.sub(r"[^\w\u0600-\u06ff]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()

def name_similarity(source_name: str, candidate_name: str) -> float:
    a, b = normalize(source_name), normalize(candidate_name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    at, bt = set(a.split()), set(b.split())
    overlap = (2 * len(at & bt) / (len(at) + len(bt))) if at and bt else 0.0
    return round(max(seq, overlap), 4)

def inside_ibb(place: dict) -> bool | None:
    """Return True/False only when Google supplies coordinates; None means unknown."""
    loc = place.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        return None
    return IBB_BBOX["south"] <= float(lat) <= IBB_BBOX["north"] and IBB_BBOX["west"] <= float(lon) <= IBB_BBOX["east"]

def address_mentions_ibb(place: dict) -> bool:
    address = str(place.get("formattedAddress") or "").casefold()
    return "ibb" in address or "إب" in address

def decision_for(source_name: str, place: dict) -> tuple[float, str]:
    display = (place.get("displayName") or {}).get("text", "")
    score = name_similarity(source_name, display)
    local = inside_ibb(place)
    address_local = address_mentions_ibb(place)
    exact = normalize(source_name) == normalize(display) and bool(normalize(display))
    if exact and local is True:
        return score, "exact_name_in_ibb_bbox"
    if exact and local is None and address_local:
        return score, "exact_name_address_only_review_required"
    if score >= 0.90 and local is True:
        return score, "strong_candidate_review_required"
    if score >= 0.74 and (local is True or (local is None and address_local)):
        return score, "possible_candidate_review_required"
    if score >= 0.90 and local is False:
        return score, "name_match_outside_ibb_bbox_review"
    return score, "weak_or_unrelated_candidate"

def read_input(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    required = {"source_id", "source_name"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"Input must contain columns {sorted(required)}")
    if len({r["source_id"] for r in rows}) != len(rows):
        raise ValueError("source_id values are not unique")
    return rows

def load_completed(jsonl_path: Path) -> tuple[set[str], list[dict]]:
    completed, records = set(), []
    if jsonl_path.exists():
        with jsonl_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    if rec.get("source_id"):
                        completed.add(str(rec["source_id"]))
                        records.append(rec)
                except json.JSONDecodeError:
                    # Ignore a possibly interrupted final line; the next run will retry it.
                    continue
    return completed, records

def places_search(session: requests.Session, api_key: str, name: str, max_retries: int) -> dict:
    query = f'"{name}" Ibb, Yemen'
    body = {
        "textQuery": query,
        "languageCode": "ar",
        "regionCode": "YE",
        "maxResultCount": 5,
        "locationBias": {"circle": {"center": CITY_CENTER, "radius": 7000.0}}
    }
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK}
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            response = session.post(API_URL, headers=headers, json=body, timeout=45)
            if response.status_code == 429 or response.status_code >= 500:
                wait = response.headers.get("Retry-After")
                delay = float(wait) if wait and wait.replace(".", "", 1).isdigit() else min(60, 2 ** attempt)
                time.sleep(delay)
                last_error = f"HTTP {response.status_code}: {response.text[:500]}"
                continue
            if response.status_code in (400, 401, 403):
                raise ApiFatalError(f"Google Places API rejected request (HTTP {response.status_code}): {response.text[:1000]}")
            response.raise_for_status()
            return {"query": query, "places": response.json().get("places", [])}
        except ApiFatalError:
            raise
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < max_retries:
                time.sleep(min(60, 2 ** attempt))
    raise RuntimeError(last_error or "Google Places API request failed")

def flatten_record(rec: dict) -> dict:
    candidates = rec.get("candidates") or []
    best = max(candidates, key=lambda x: x.get("match_score", 0), default={})
    place = best.get("place") or {}
    loc = place.get("location") or {}
    hours = place.get("regularOpeningHours")
    plus = place.get("plusCode")
    display = (place.get("displayName") or {}).get("text")
    category = (place.get("primaryTypeDisplayName") or {}).get("text") or ",".join(place.get("types") or [])
    if rec.get("search_status") != "ok":
        decision = "api_error"
    else:
        decision = best.get("match_decision") or "no_google_result"
    return {
        "source_id": rec.get("source_id"), "source_name": rec.get("source_name"),
        "search_query": rec.get("search_query"), "search_status": rec.get("search_status"),
        "match_decision": decision, "match_score": best.get("match_score"),
        "best_match_name": display, "google_place_id": place.get("id"),
        "google_maps_url": place.get("googleMapsUri") or rec.get("google_maps_search_url"),
        "address": place.get("formattedAddress"), "latitude": loc.get("latitude"),
        "longitude": loc.get("longitude"), "phone_international": place.get("internationalPhoneNumber"),
        "phone_national": place.get("nationalPhoneNumber"), "website": place.get("websiteUri"),
        "category": category, "opening_hours": json.dumps(hours, ensure_ascii=False) if hours else None,
        "rating": place.get("rating"), "user_rating_count": place.get("userRatingCount"),
        "business_status": place.get("businessStatus"), "plus_code": json.dumps(plus, ensure_ascii=False) if plus else None,
        "provider": "Google Places API (New)", "queried_at": rec.get("queried_at"), "error": rec.get("error")
    }

def write_exports(records: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "ibb_google_name_enrichment_11050.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow(flatten_record(rec))
    counts = Counter(flatten_record(r)["match_decision"] for r in records)
    report = {
        "source_records_total": 11050,
        "records_written": len(records),
        "google_queries_completed": sum(1 for r in records if r.get("search_status") == "ok"),
        "api_errors": sum(1 for r in records if r.get("search_status") != "ok"),
        "match_decision_counts": dict(counts),
        "provider": "Google Places API (New)",
        "query_policy": "One exact source name per request, suffixed with Ibb, Yemen",
        "source_input": "data/ibb_source_names_11050.csv.gz",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limitations": ["Google ranking is not exhaustive.", "Only exact normalized names inside the Ibb bounding box are automatically classified as exact-name matches.", "Strong and possible candidates require human review.", "A no-result response is not proof that a business does not exist."]
    }
    (out_dir / "ibb_google_name_enrichment_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/ibb_source_names_11050.csv.gz")
    ap.add_argument("--out-dir", default=".scan/google_name_by_name")
    ap.add_argument("--max-records", type=int, default=0, help="0 means all source names")
    ap.add_argument("--max-qps", type=float, default=1.0)
    ap.add_argument("--max-retries", type=int, default=5)
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()
    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if not api_key:
        print("ERROR: repository secret GOOGLE_PLACES_API_KEY is required; no search was sent.", file=sys.stderr)
        return 2
    if not (0.1 <= args.max_qps <= 10):
        ap.error("--max-qps must be between 0.1 and 10")
    input_rows = read_input(Path(args.input))
    if args.max_records > 0:
        input_rows = input_rows[:args.max_records]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "google_places_per_name.jsonl"
    completed, records = (set(), []) if args.no_resume else load_completed(jsonl_path)
    session = requests.Session()
    interval = 1.0 / args.max_qps
    print(json.dumps({"input_names": len(input_rows), "already_completed": len(completed), "to_search": sum(1 for r in input_rows if str(r["source_id"]) not in completed), "max_qps": args.max_qps}, ensure_ascii=False), flush=True)
    last_request_start = 0.0
    try:
        with jsonl_path.open("a", encoding="utf-8") as out:
            for index, source in enumerate(input_rows, start=1):
                sid = str(source["source_id"])
                if sid in completed:
                    continue
                wait = interval - (time.monotonic() - last_request_start)
                if wait > 0:
                    time.sleep(wait)
                last_request_start = time.monotonic()
                query = f'"{source["source_name"]}" Ibb, Yemen'
                now = datetime.now(timezone.utc).isoformat()
                try:
                    result = places_search(session, api_key, source["source_name"], args.max_retries)
                    candidates = []
                    for place in result.get("places", []):
                        score, decision = decision_for(source["source_name"], place)
                        candidates.append({"match_score": score, "match_decision": decision, "place": place})
                    candidates.sort(key=lambda x: (x["match_score"], inside_ibb(x["place"])), reverse=True)
                    rec = {
                        "source_id": sid, "source_name": source["source_name"], "search_query": result["query"],
                        "search_status": "ok", "google_maps_search_url": "https://www.google.com/maps/search/?api=1&query=" + quote(query),
                        "candidates": candidates, "candidate_count": len(candidates), "queried_at": now
                    }
                except ApiFatalError:
                    raise
                except Exception as exc:
                    rec = {
                        "source_id": sid, "source_name": source["source_name"], "search_query": query,
                        "search_status": "api_error", "google_maps_search_url": "https://www.google.com/maps/search/?api=1&query=" + quote(query),
                        "candidates": [], "candidate_count": 0, "queried_at": now, "error": str(exc)[:1000]
                    }
                out.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
                out.flush()
                records.append(rec); completed.add(sid)
                if len(records) % 50 == 0:
                    write_exports(load_completed(jsonl_path)[1], out_dir)
                    print(json.dumps({"progress_completed_this_run": len(records), "processed_index": index, "source_id": sid}, ensure_ascii=False), flush=True)
    except ApiFatalError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        # Preserve all records collected before the fatal API error.
        all_records = load_completed(jsonl_path)[1]
        write_exports(all_records, out_dir)
        return 3
    finally:
        session.close()
    all_records = load_completed(jsonl_path)[1]
    write_exports(all_records, out_dir)
    print(json.dumps({"finished": True, "completed_records": len(all_records), "output_csv": str(out_dir / 'ibb_google_name_enrichment_11050.csv')}, ensure_ascii=False), flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
