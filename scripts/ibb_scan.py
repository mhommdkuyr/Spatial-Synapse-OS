#!/usr/bin/env python3
"""Adaptive Ibb commercial-place scanner.

Strategy:
1) Pull OSM first (Overpass).
2) Use OSM coordinates + an adaptive exploratory grid as Google Places seeds.
3) Refine around Google/OSM discoveries with small 40m nearby searches.
4) Merge/dedupe records while retaining source provenance.
5) Always write a report, even if one provider is unavailable.
"""

import argparse
import csv
import json
import math
import os
import re
import time
from pathlib import Path

import requests
import yaml

UA = "Spatial-Synapse-OS/1.1 (+https://github.com/mhommdkuyr/Spatial-Synapse-OS)"
GOOGLE_URL = "https://places.googleapis.com/v1/places:searchNearby"

ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")


def distance(a, b):
    r = 6371000.0
    p1, p2 = map(math.radians, (a[0], b[0]))
    dp = math.radians(b[0] - a[0])
    dl = math.radians(b[1] - a[1])
    q = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(q))


def normalize_name(value):
    if not value:
        return ""
    value = ARABIC_DIACRITICS.sub("", str(value)).casefold()
    value = re.sub(r"[^\w\u0600-\u06FF]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def grid(b, spacing):
    lat_step = spacing / 111320.0
    mid_lat = math.radians((b["south"] + b["north"]) / 2)
    lon_step = spacing / (111320.0 * max(0.2, math.cos(mid_lat)))
    y = b["south"]
    while y <= b["north"] + 1e-12:
        x = b["west"]
        while x <= b["east"] + 1e-12:
            yield round(y, 7), round(x, 7)
            x += lon_step
        y += lat_step


def refinement_centers(lat, lon, radius_m):
    # Five 40m-scale centers provide local coverage without exploding request count.
    yield float(lat), float(lon)
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * max(0.2, math.cos(math.radians(lat))))
    yield float(lat + dlat), float(lon)
    yield float(lat - dlat), float(lon)
    yield float(lat), float(lon + dlon)
    yield float(lat), float(lon - dlon)



def safe_text(value):
    return value.strip() if isinstance(value, str) else value


def normalize_osm(e):
    tags = e.get("tags", {})
    center = e.get("center", {})
    lat = e.get("lat", center.get("lat"))
    lon = e.get("lon", center.get("lon"))
    if lat is None or lon is None:
        return None

    social = {}
    for k, v in tags.items():
        if k.startswith("contact:") or k in {"facebook", "instagram", "twitter", "whatsapp"}:
            social[k] = v

    address_parts = [
        tags.get("addr:street"),
        tags.get("addr:housenumber"),
        tags.get("addr:district"),
        tags.get("addr:city"),
    ]

    return {
        "source": "osm",
        "source_id": f'{e.get("type", "element")}/{e.get("id")}',
        "name": tags.get("name") or tags.get("name:ar") or tags.get("brand"),
        "category": tags.get("shop") or tags.get("amenity") or tags.get("craft") or tags.get("office") or tags.get("tourism"),
        "latitude": float(lat),
        "longitude": float(lon),
        "address": tags.get("addr:full") or ", ".join(x for x in address_parts if x),
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "website": tags.get("website") or tags.get("contact:website"),
        "social": social,
        "opening_hours": tags.get("opening_hours"),
        "products_services": tags.get("products") or tags.get("product") or tags.get("service"),
        "raw_tags": tags,
    }


def overpass_query(b):
    # Deliberately broad commercial/place tags; exact shop categories vary in OSM.
    return f"""[out:json][timeout:300];
(
  nwr[shop]({b['south']},{b['west']},{b['north']},{b['east']});
  nwr[amenity]({b['south']},{b['west']},{b['north']},{b['east']});
  nwr[craft]({b['south']},{b['west']},{b['north']},{b['east']});
  nwr[office]({b['south']},{b['west']},{b['north']},{b['east']});
  nwr[tourism]({b['south']},{b['west']},{b['north']},{b['east']});
);
out center tags;"""


def collect_osm(b, urls):
    # Split the OSM extraction into bounded requests so a slow Overpass
    # endpoint cannot stall the whole job for many minutes.
    queries = [
        f"""[out:json][timeout:120];(
          nwr[shop]({b['south']},{b['west']},{b['north']},{b['east']});
        );out center tags;""",
        f"""[out:json][timeout:120];(
          nwr[craft]({b['south']},{b['west']},{b['north']},{b['east']});
          nwr[office]({b['south']},{b['west']},{b['north']},{b['east']});
        );out center tags;""",
        f"""[out:json][timeout:120];(
          nwr[amenity~"^(pharmacy|marketplace|bank|fuel|restaurant|cafe|fast_food)$"]({b['south']},{b['west']},{b['north']},{b['east']});
          nwr[tourism~"^(hotel|guest_house|hostel|attraction)$"]({b['south']},{b['west']},{b['north']},{b['east']});
        );out center tags;""",
    ]
    all_rows = []
    endpoint_used = None
    endpoint_errors = []
    for query in queries:
        last_error = None
        query_done = False
        for url in urls:
            try:
                response = requests.post(
                    url,
                    data=query,
                    headers={"User-Agent": UA},
                    timeout=150,
                )
                response.raise_for_status()
                elements = response.json().get("elements", [])
                rows = [r for e in elements if (r := normalize_osm(e))]
                all_rows.extend(rows)
                endpoint_used = endpoint_used or url
                query_done = True
                break
            except Exception as exc:
                last_error = exc
                endpoint_errors.append({"url": url, "error": str(exc)})
                time.sleep(2)
        if not query_done and last_error is not None:
            continue
    unique = dedupe_source(all_rows)
    return unique, {
        "enabled": True,
        "endpoint": endpoint_used,
        "elements_normalized": len(unique),
        "errors": endpoint_errors,
        "queries": len(queries),
    }


def google_headers(api_key):
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": ",".join(
            [
                "places.id",
                "places.displayName",
                "places.location",
                "places.formattedAddress",
                "places.nationalPhoneNumber",
                "places.internationalPhoneNumber",
                "places.websiteUri",
                "places.types",
                "places.regularOpeningHours",
            ]
        ),
    }


def normalize_google(place):
    loc = place.get("location") or {}
    name = (place.get("displayName") or {}).get("text")
    return {
        "source": "google",
        "source_id": place.get("id"),
        "name": name,
        "category": (place.get("types") or [None])[0],
        "latitude": loc.get("latitude"),
        "longitude": loc.get("longitude"),
        "address": place.get("formattedAddress"),
        "phone": place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber"),
        "website": place.get("websiteUri"),
        "social": {},
        "opening_hours": place.get("regularOpeningHours"),
        "products_services": None,
        "raw_tags": place,
    }


def google_search(api_key, lat, lon, radius_m, included_types):
    body = {
        "includedTypes": included_types,
        "maxResultCount": 20,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": radius_m,
            }
        },
    }
    response = requests.post(GOOGLE_URL, headers=google_headers(api_key), json=body, timeout=45)
    if response.status_code in {429, 500, 502, 503, 504}:
        raise requests.HTTPError(f"transient Google status {response.status_code}", response=response)
    response.raise_for_status()
    return response.json().get("places", [])


def collect_google(cfg, osm_rows):
    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        return [], {
            "enabled": False,
            "available": False,
            "reason": "GOOGLE_PLACES_API_KEY is not configured as a GitHub Actions secret",
        }

    gcfg = cfg["google"]
    exploratory_spacing = int(gcfg.get("exploratory_grid_spacing_m", 250))
    refine_radius = int(gcfg.get("refine_radius_m", 80))
    refine_spacing = int(gcfg.get("refine_spacing_m", 40))
    max_requests = int(gcfg.get("max_requests", 2500))
    types = gcfg.get(
        "included_types",
        [
            "store",
            "shopping_mall",
            "supermarket",
            "clothing_store",
            "convenience_store",
            "electronics_store",
            "furniture_store",
            "hardware_store",
            "home_goods_store",
            "jewelry_store",
            "shoe_store",
            "book_store",
            "florist",
            "pharmacy",
        ],
    )

    seeds = set(grid(cfg["bbox"], exploratory_spacing))
    for row in osm_rows:
        if row.get("latitude") is not None and row.get("longitude") is not None:
            seeds.add((float(row["latitude"]), float(row["longitude"])))

    rows = []
    request_count = 0
    errors = 0
    transient_errors = 0
    queries = []

    # Pass 1: exploratory coverage.
    # Nearby Search supports up to 50 included types in one request, so batch
    # all shopping types instead of multiplying the request count by category.
    for seed_lat, seed_lon in sorted(seeds):
        if request_count >= max_requests:
            break
        try:
            places = google_search(
                api_key,
                seed_lat,
                seed_lon,
                int(gcfg.get("exploratory_radius_m", 250)),
                types,
            )
            request_count += 1
            rows.extend(normalize_google(p) for p in places)
        except requests.HTTPError as exc:
            request_count += 1
            errors += 1
            if getattr(exc, "response", None) is not None and exc.response.status_code in {429, 500, 502, 503, 504}:
                transient_errors += 1
                time.sleep(1.0)
        except Exception:
            request_count += 1
            errors += 1

    # Pass 2: 40m refinement around everything discovered so far + OSM.
    # Refine around OSM anchors. Google exploratory results are already covered
    # by the broad pass; refining every Google result can multiply requests dramatically.
    refine_points = {
        (float(r["latitude"]), float(r["longitude"]))
        for r in osm_rows
        if r.get("latitude") is not None and r.get("longitude") is not None
    }
    refine_centers = set()
    for lat, lon in refine_points:
        for p in refinement_centers(lat, lon, int(gcfg.get("refine_radius_m", 40))):
            refine_centers.add((round(p[0], 7), round(p[1], 7)))
    queries = len(seeds) + len(refine_centers)

    for seed_lat, seed_lon in sorted(refine_centers):
        if request_count >= max_requests:
            break
        # Refinement uses the same batched shopping set at 40m around
        # real OSM commercial anchors.
        refine_types = gcfg.get("refine_types", types)
        try:
            places = google_search(
                api_key,
                seed_lat,
                seed_lon,
                int(gcfg.get("radius_m", 40)),
                refine_types,
            )
            request_count += 1
            rows.extend(normalize_google(p) for p in places)
        except requests.HTTPError as exc:
            request_count += 1
            errors += 1
            if getattr(exc, "response", None) is not None and exc.response.status_code in {429, 500, 502, 503, 504}:
                transient_errors += 1
                time.sleep(1.0)
        except Exception:
            request_count += 1
            errors += 1

    meta = {
        "enabled": True,
        "available": True,
        "requests": request_count,
        "errors": errors,
        "transient_errors": transient_errors,
        "exploratory_seed_count": len(seeds),
        "refine_center_count": len(refine_centers),
        "planned_query_units": queries,
        "max_requests": max_requests,
        "refine_radius_m": refine_radius,
        "refine_spacing_m": refine_spacing,
        "types": types,
    }
    return rows, meta


def dedupe_source(rows):
    seen = set()
    out = []
    for row in rows:
        key = (row.get("source"), row.get("source_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def merge(rows):
    out = []
    for r in dedupe_source(rows):
        if r.get("latitude") is None or r.get("longitude") is None:
            continue

        rn = normalize_name(r.get("name"))
        match_idx = None
        best_distance = 10**9
        for i, x in enumerate(out):
            d = distance(
                (float(r["latitude"]), float(r["longitude"])),
                (float(x["latitude"]), float(x["longitude"])),
            )
            xn = normalize_name(x.get("name"))
            if d <= 75 and rn and xn and rn == xn:
                if d < best_distance:
                    match_idx, best_distance = i, d

        if match_idx is None:
            r = dict(r)
            r["sources"] = [r["source"]]
            r["source_ids"] = [r.get("source_id")]
            out.append(r)
            continue

        x = out[match_idx]
        x["sources"] = sorted(set(x["sources"] + [r["source"]]))
        x["source_ids"] = sorted(set(x["source_ids"] + [r.get("source_id")]))
        for key in ["phone", "website", "address", "opening_hours", "products_services"]:
            if not x.get(key) and r.get(key):
                x[key] = r[key]
        if r.get("social"):
            x["social"] = {**x.get("social", {}), **r["social"]}
        if not x.get("category") and r.get("category"):
            x["category"] = r["category"]

    for row in out:
        row["source_count"] = len(row.get("sources", []))
        row["google_present"] = "google" in row.get("sources", [])
        row["osm_present"] = "osm" in row.get("sources", [])
    return out


def category_counts(rows):
    counts = {}
    for row in rows:
        key = row.get("category") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: (-x[1], x[0])))


def write_outputs(cfg, rows, report):
    Path("data").mkdir(parents=True, exist_ok=True)

    Path(cfg["output"]["jsonl"]).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )

    fields = [
        "name",
        "category",
        "latitude",
        "longitude",
        "address",
        "phone",
        "website",
        "opening_hours",
        "products_services",
        "sources",
        "source_ids",
        "source_count",
        "google_present",
        "osm_present",
    ]
    with open(cfg["output"]["csv"], "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    Path(cfg["output"]["report"]).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/ibb.yml")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    osm = []
    osm_meta = {"enabled": False}
    if cfg.get("osm", {}).get("enabled", True):
        osm, osm_meta = collect_osm(cfg["bbox"], cfg["osm"]["overpass_urls"])

    google = []
    google_meta = {"enabled": False}
    if cfg.get("google", {}).get("enabled", True):
        google, google_meta = collect_google(cfg, osm)

    rows = merge(osm + google)

    report = {
        "city": cfg["city"],
        "bbox": cfg["bbox"],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "osm_count": len(osm),
        "google_count_raw": len(google),
        "unique_count": len(rows),
        "google_only_count": sum(1 for r in rows if r["google_present"] and not r["osm_present"]),
        "osm_only_count": sum(1 for r in rows if r["osm_present"] and not r["google_present"]),
        "matched_multi_source_count": sum(1 for r in rows if r["osm_present"] and r["google_present"]),
        "category_counts": category_counts(rows),
        "osm": osm_meta,
        "google": google_meta,
        "coverage_notes": [
            "OSM is open geographic data and can contain businesses missing from Google.",
            "Google Places is not a legal guarantee of every business in an area; coverage and ranking are provider-dependent.",
            "A 40m refinement pass is used around discovered places; a blind 40m grid over the full bounding box would create an impractical number of requests.",
        ],
    }

    write_outputs(cfg, rows, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
