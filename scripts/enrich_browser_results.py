#!/usr/bin/env python3
"""Enrich unique Google Maps discovery URLs with place details."""
from __future__ import annotations
import argparse, asyncio, csv, json, random, time
from pathlib import Path
from playwright.async_api import async_playwright
from scripts.ibb_maps_browser_scan import extract_detail, parse_place_id, parse_coords, stable_key

async def enrich(cfg, shard, shards, input_path, out_dir):
    rows=[]
    for line in Path(input_path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    targets=[r for i,r in enumerate(rows) if i % shards == shard]
    out_dir.mkdir(parents=True,exist_ok=True)
    out_path=out_dir/f"shard_{shard}.jsonl"
    state_path=out_dir/f"shard_{shard}.state.json"
    done=set()
    if state_path.exists():
        try: done=set(json.loads(state_path.read_text(encoding="utf-8")).get("done",[]))
        except Exception: pass
    existing={}
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                r=json.loads(line); existing[stable_key(r)]=r; done.add(r.get("google_maps_url") or r.get("input_url"))
            except Exception: pass

    sem=asyncio.Semaphore(int(cfg.get("detail_concurrency",3)))
    last=[]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True)

        async def one(row):
            async with sem:
                now=time.monotonic()
                last[:] = [t for t in last if now-t<60]
                rpm=int(cfg.get("detail_per_minute",12))
                if len(last)>=rpm:
                    await asyncio.sleep(max(0.5,60-(now-last[0])))
                last.append(time.monotonic())
                context=await browser.new_context(locale="en-US")
                page=await context.new_page()
                try:
                    meta={
                        "area": row.get("area"),
                        "category_group": row.get("category_group"),
                        "input_url": row.get("google_maps_url") or row.get("input_url"),
                        "discovered_queries": row.get("queries_seen") or [],
                    }
                    return await extract_detail(page, row.get("google_maps_url") or row.get("input_url"), meta)
                finally:
                    await context.close()

        for pos in range(0,len(targets),25):
            batch=[r for r in targets[pos:pos+25] if (r.get("google_maps_url") or r.get("input_url")) not in done]
            results=await asyncio.gather(*(one(r) for r in batch),return_exceptions=False)
            for r in results:
                if r:
                    k=stable_key(r)
                    old=existing.get(k)
                    if old:
                        for f in ("name","category","address","phone","website","opening_hours","rating","reviews_count","latitude","longitude","google_maps_url","place_id"):
                            if not old.get(f) and r.get(f): old[f]=r[f]
                        old["scrape_status"]=r.get("scrape_status",old.get("scrape_status"))
                        old["queries_seen"]=sorted(set((old.get("queries_seen") or [])+(r.get("discovered_queries") or [])))
                    else:
                        r["queries_seen"]=r.get("discovered_queries") or []
                        existing[k]=r
            with out_path.open("w",encoding="utf-8") as fh:
                for r in existing.values(): fh.write(json.dumps(r,ensure_ascii=False,sort_keys=True)+"\n")
            done.update((r.get("google_maps_url") or r.get("input_url")) for r in batch)
            state_path.write_text(json.dumps({"done":sorted(x for x in done if x)},ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({"shard":shard,"processed":min(pos+25,len(targets)),"unique_enriched":len(existing)},ensure_ascii=False),flush=True)
        await browser.close()
    print(json.dumps({"shard_done":shard,"unique_enriched":len(existing)},ensure_ascii=False),flush=True)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--config",default="config/ibb_maps_queries.yml")
    ap.add_argument("--shard",type=int,required=True)
    ap.add_argument("--shards",type=int,default=8)
    ap.add_argument("--input",default="data/google_maps_browser_results.jsonl")
    ap.add_argument("--output-dir",default=".scan/google_maps_enriched")
    a=ap.parse_args()
    cfg=json.loads(json.dumps(__import__("yaml").safe_load(Path(a.config).read_text(encoding="utf-8"))))
    asyncio.run(enrich(cfg,a.shard,a.shards,a.input,Path(a.output_dir)))

if __name__=="__main__": main()
