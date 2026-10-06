#!/usr/bin/env python3
"""Merge enriched Google Maps records and run basic data-quality checks."""
from __future__ import annotations
import csv,json,re
from pathlib import Path
from collections import Counter

def norm(v):
    if not v:return ""
    v=re.sub(r"[^\w\u0600-\u06FF]+"," ",str(v).casefold(),flags=re.UNICODE)
    return re.sub(r"\s+"," ",v).strip()

def key(r):
    return ("pid",r.get("place_id")) if r.get("place_id") else ("na",norm(r.get("name"))+"|"+norm(r.get("address")))

def main():
    root=Path(".scan/google_maps_enriched")
    merged={}
    for p in sorted(root.glob("shard_*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:r=json.loads(line)
            except Exception:continue
            k=key(r)
            if k not in merged: merged[k]=r
            else:
                old=merged[k]
                old["queries_seen"]=sorted(set((old.get("queries_seen") or [])+(r.get("queries_seen") or [])))
                for f in ("name","category","address","phone","website","opening_hours","rating","reviews_count","latitude","longitude","google_maps_url","place_id"):
                    if not old.get(f) and r.get(f): old[f]=r[f]
                if old.get("scrape_status")!="ok" and r.get("scrape_status")=="ok": old["scrape_status"]="ok"
    rows=list(merged.values())
    Path("data").mkdir(parents=True,exist_ok=True)
    with Path("data/google_maps_browser_enriched.jsonl").open("w",encoding="utf-8") as fh:
        for r in sorted(rows,key=lambda r:(norm(r.get("name")),norm(r.get("address")))):
            fh.write(json.dumps(r,ensure_ascii=False,sort_keys=True)+"\n")
    fields=["name","category","address","phone","website","rating","reviews_count","opening_hours","latitude","longitude","google_maps_url","place_id","area","category_group","scrape_status","queries_seen"]
    with Path("data/google_maps_browser_enriched.csv").open("w",encoding="utf-8-sig",newline="") as fh:
        w=csv.DictWriter(fh,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(rows)
    status=Counter(r.get("scrape_status") for r in rows)
    report={
      "city":"Ibb, Yemen",
      "source":"Google Maps browser",
      "generated_at":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
      "unique_places":len(rows),
      "with_name":sum(bool(r.get("name")) for r in rows),
      "with_address":sum(bool(r.get("address")) for r in rows),
      "with_phone":sum(bool(r.get("phone")) for r in rows),
      "with_website":sum(bool(r.get("website")) for r in rows),
      "with_hours":sum(bool(r.get("opening_hours")) for r in rows),
      "with_coordinates":sum(r.get("latitude") is not None and r.get("longitude") is not None for r in rows),
      "status_counts":dict(status),
      "category_counts":dict(Counter(r.get("category") or r.get("category_group") or "unknown" for r in rows).most_common()),
      "quality_checks":{
        "duplicate_keys":0,
        "empty_names":sum(not bool(r.get("name")) for r in rows),
        "blocked_records":sum(r.get("scrape_status")=="blocked" for r in rows),
        "error_records":sum(r.get("scrape_status")=="error" for r in rows)
      },
      "note":"This is a browser-discovery dataset. Google Maps search ranking and access controls mean it cannot mathematically guarantee every business in Ibb. The workflow does not bypass CAPTCHA or robot checks."
    }
    Path("data/google_maps_browser_enriched_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=="__main__":main()
