#!/usr/bin/env python3
"""Merge Google Maps browser discovery shards and produce audit outputs."""
from __future__ import annotations
import csv, json, math, re
from pathlib import Path

ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")

def norm(v):
    if not v: return ""
    v = ARABIC_DIACRITICS.sub("", str(v)).casefold()
    v = re.sub(r"[^\w\u0600-\u06FF]+", " ", v, flags=re.UNICODE)
    return re.sub(r"\s+", " ", v).strip()

def dist(a,b):
    r=6371000
    p1,p2=math.radians(a[0]),math.radians(b[0])
    dp=math.radians(b[0]-a[0]); dl=math.radians(b[1]-a[1])
    q=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(q))

def key(row):
    if row.get("place_id"): return "pid:"+row["place_id"]
    return "n:"+norm(row.get("name"))+"|a:"+norm(row.get("address"))

def main():
    root=Path(".scan/google_maps_browser")
    merged={}
    for p in sorted(root.glob("shard_*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                row=json.loads(line)
            except Exception:
                continue
            k=key(row)
            old=merged.get(k)
            if old is None:
                merged[k]=row
            else:
                old["queries_seen"]=sorted(set((old.get("queries_seen") or [])+(row.get("queries_seen") or [])))
                for f in ("name","address","phone","website","category","opening_hours","rating","reviews_count","latitude","longitude","google_maps_url","raw_card_text"):
                    if not old.get(f) and row.get(f): old[f]=row[f]
    rows=list(merged.values())
    Path("data").mkdir(parents=True,exist_ok=True)
    out=Path("data/google_maps_browser_results.jsonl")
    with out.open("w",encoding="utf-8") as fh:
        for r in sorted(rows,key=lambda x:(norm(x.get("name")),norm(x.get("address")))):
            fh.write(json.dumps(r,ensure_ascii=False,sort_keys=True)+"\n")
    fields=["name","category","address","phone","website","rating","reviews_count","opening_hours","latitude","longitude","google_maps_url","place_id","area","category_group","scrape_status","queries_seen"]
    with Path("data/google_maps_browser_results.csv").open("w",encoding="utf-8-sig",newline="") as fh:
        w=csv.DictWriter(fh,fieldnames=fields,extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    counts={}
    for r in rows:
        k=r.get("category") or r.get("category_group") or "unknown"; counts[k]=counts.get(k,0)+1
    report={
        "city":"Ibb, Yemen",
        "source":"Google Maps browser discovery",
        "generated_at":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "unique_records":len(rows),
        "with_place_id":sum(bool(r.get("place_id")) for r in rows),
        "with_phone":sum(bool(r.get("phone")) for r in rows),
        "with_website":sum(bool(r.get("website")) for r in rows),
        "with_coordinates":sum(r.get("latitude") is not None and r.get("longitude") is not None for r in rows),
        "query_count":sum(1 for _ in root.glob("shard_*.state.json")),
        "category_counts":dict(sorted(counts.items(),key=lambda x:(-x[1],x[0]))),
        "note":"Browser discovery is best-effort and subject to Google Maps availability, rate limits, and search-result ranking. CAPTCHA or access controls are not bypassed."
    }
    Path("data/google_maps_browser_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=="__main__": main()
