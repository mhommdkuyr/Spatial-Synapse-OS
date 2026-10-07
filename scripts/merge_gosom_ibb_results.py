#!/usr/bin/env python3
"""Merge gosom Google Maps CSV shards with OSM/browser datasets."""
from __future__ import annotations
import csv,json,math,re
from pathlib import Path
from collections import Counter

def norm(v):
    if not v:return ""
    v=re.sub(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]","",str(v).casefold())
    v=re.sub(r"[^\w\u0600-\u06FF]+"," ",v,flags=re.UNICODE)
    return re.sub(r"\s+"," ",v).strip()

def dist(a,b):
    r=6371000.0
    p1,p2=math.radians(a[0]),math.radians(b[0])
    dp=math.radians(b[0]-a[0]); dl=math.radians(b[1]-a[1])
    q=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(q))

def parse_num(v):
    try:return float(v)
    except:return None

def google_key(r):
    for k in ("place_id","data_id","cid"):
        if r.get(k): return ("id",str(r[k]))
    name=norm(r.get("title") or r.get("name"))
    addr=norm(r.get("address") or r.get("complete_address"))
    lat=parse_num(r.get("latitude")); lon=parse_num(r.get("longitude"))
    if name and addr:return ("na",name+"|"+addr)
    if name and lat is not None and lon is not None:return ("nc",name+f"|{lat:.5f}|{lon:.5f}")
    return None

def load_csvs(root):
    rows=[]
    for p in sorted(root.rglob("*.csv")):
        try:
            with p.open("r",encoding="utf-8-sig",newline="") as fh:
                for r in csv.DictReader(fh):
                    r["_file"]=str(p)
                    rows.append(r)
        except Exception:
            continue
    return rows

def load_jsonl(path):
    out=[]
    if not path.exists():return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:out.append(json.loads(line))
        except Exception:pass
    return out

def in_ibb_bbox(r):
    lat=parse_num(r.get("latitude"))
    lon=parse_num(r.get("longitude"))
    if lat is None or lon is None:
        return False
    return 13.90 <= lat <= 14.04 and 44.12 <= lon <= 44.25

def main():
    g_all=load_csvs(Path(".scan/gosom"))
    g=[r for r in g_all if in_ibb_bbox(r)]
    filtered_out_of_scope=len(g_all)-len(g)
    merged={}
    for r in g:
        k=google_key(r)
        if not k:continue
        if k not in merged: merged[k]=r
        else:
            old=merged[k]
            for a,b in r.items():
                if a.startswith("_"):continue
                if not old.get(a) and b:old[a]=b

    google=list(merged.values())
    osm=load_jsonl(Path("data/ibb_places.jsonl"))
    browser=load_jsonl(Path("data/google_maps_browser_discovery.json"))
    browser=browser[0].get("records",[]) if browser and isinstance(browser[0],dict) and "records" in browser[0] else browser

    final=[]
    for r in google:
        lat=parse_num(r.get("latitude")); lon=parse_num(r.get("longitude"))
        final.append({
            "name":r.get("title") or r.get("name"),
            "category":r.get("category"),
            "address":r.get("complete_address") or r.get("address"),
            "phone":r.get("phone"),
            "website":r.get("website"),
            "opening_hours":r.get("open_hours") or r.get("opening_hours"),
            "latitude":lat,
            "longitude":lon,
            "rating":parse_num(r.get("review_rating") or r.get("rating")),
            "review_count":r.get("review_count") or r.get("reviews_count"),
            "google_maps_url":r.get("link") or r.get("google_maps_url") or r.get("url"),
            "place_id":r.get("place_id"),
            "cid":r.get("cid"),
            "plus_code":r.get("plus_code"),
            "description":r.get("descriptions") or r.get("description"),
            "about":r.get("about"),
            "emails":r.get("emails"),
            "images":r.get("images"),
            "status":r.get("status"),
            "source":"gosom_google_maps",
            "query_id":r.get("id"),
        })

    def dedupe_existing(rows):
        out={}; unnamed=[]
        for r in rows:
            pid=r.get("place_id")
            k=("id",str(pid)) if pid else None
            if not k:
                n=norm(r.get("name")); a=norm(r.get("address"))
                lat=r.get("latitude"); lon=r.get("longitude")
                if n and a:k=("na",n+"|"+a)
                elif n and lat is not None and lon is not None:k=("nc",n+f"|{float(lat):.5f}|{float(lon):.5f}")
            if k and k not in out: out[k]=r
            elif k:
                old=out[k]
                for field in ("phone","website","opening_hours","description","about","emails","images","rating","review_count","google_maps_url"):
                    if not old.get(field) and r.get(field):old[field]=r[field]
            else: unnamed.append(r)
        return list(out.values())+unnamed

    final=dedupe_existing(final)

    # Attach OSM source evidence to nearby exact-name records.
    matched=0
    for r in final:
        if r.get("latitude") is None or r.get("longitude") is None:continue
        rn=norm(r.get("name"))
        for o in osm:
            if not rn or rn != norm(o.get("name")):continue
            if o.get("latitude") is None or o.get("longitude") is None:continue
            if dist((r["latitude"],r["longitude"]),(float(o["latitude"]),float(o["longitude"]))) <= 75:
                r["osm_source_id"]=o.get("source_id")
                r["sources"]=["google","osm"]
                matched+=1
                break
        if "sources" not in r:r["sources"]=["google"]

    out=Path("data/ibb_google_gosom.csv")
    fields=["name","category","address","phone","website","opening_hours","latitude","longitude","rating","review_count","google_maps_url","place_id","cid","plus_code","description","about","emails","images","status","source","sources","osm_source_id","query_id"]
    with out.open("w",encoding="utf-8-sig",newline="") as fh:
        w=csv.DictWriter(fh,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(final)

    Path("data/ibb_google_gosom.jsonl").write_text(
        "".join(json.dumps(r,ensure_ascii=False,sort_keys=True)+"\n" for r in final),
        encoding="utf-8",
    )

    report={
      "city":"Ibb, Yemen",
      "source":"gosom/google-maps-scraper",
      "generated_at":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
      "raw_google_rows":len(g),
      "raw_google_rows_before_bbox":len(g_all),
      "filtered_out_of_scope_rows":filtered_out_of_scope,
      "unique_google_rows":len(final),
      "matched_with_osm_by_name_and_75m":matched,
      "with_place_id":sum(bool(r.get("place_id")) for r in final),
      "with_phone":sum(bool(r.get("phone")) for r in final),
      "with_website":sum(bool(r.get("website")) for r in final),
      "with_opening_hours":sum(bool(r.get("opening_hours")) for r in final),
      "with_coordinates":sum(r.get("latitude") is not None and r.get("longitude") is not None for r in final),
      "categories":dict(Counter(r.get("category") or "unknown" for r in final).most_common()),
      "coverage_note":"Dense area/category queries plus an independent grid pass. Results are filtered to the configured Ibb bounding box to exclude nearby districts returned by broad Google queries. This remains best-effort; provider ranking/access controls mean absolute completeness cannot be mathematically guaranteed."
    }
    Path("data/ibb_google_gosom_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=="__main__":main()
