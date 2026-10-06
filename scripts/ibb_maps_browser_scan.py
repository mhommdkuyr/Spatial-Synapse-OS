#!/usr/bin/env python3
"""Resumable Google Maps browser discovery for Ibb.

Uses Playwright against public Google Maps search pages. It does not bypass
CAPTCHAs or access controls. Searches are intentionally partitioned by
Ibb area/street and commercial category, then results are deduplicated by
Google place id or normalized name/address/coordinates.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import yaml
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout


ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
PLACE_RE = re.compile(r"/maps/place/[^/]+/([^/?#]+)")
COORD_RE = re.compile(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")


def normalize(value: str | None) -> str:
    if not value:
        return ""
    value = ARABIC_DIACRITICS.sub("", str(value)).casefold()
    value = re.sub(r"[^\w\u0600-\u06FF]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def stable_key(row: dict) -> str:
    place_id = row.get("place_id")
    if place_id:
        return "pid:" + place_id
    name = normalize(row.get("name"))
    address = normalize(row.get("address"))
    lat = row.get("latitude")
    lon = row.get("longitude")
    if name and address:
        return "na:" + hashlib.sha1((name + "|" + address).encode("utf-8")).hexdigest()
    if name and lat is not None and lon is not None:
        return "nc:" + hashlib.sha1((name + f"|{float(lat):.5f}|{float(lon):.5f}").encode("utf-8")).hexdigest()
    return "url:" + hashlib.sha1(str(row.get("google_maps_url") or "").encode("utf-8")).hexdigest()


def build_queries(cfg: dict) -> list[dict]:
    rows = []
    for area in cfg.get("areas", []):
        for category in cfg.get("categories", []):
            rows.append({
                "area": area,
                "category_group": category,
                "query": f"{category} في {area}, إب, اليمن",
            })
    return rows


async def accept_consent(page):
    labels = [
        "Accept all", "I agree", "قبول الكل", "أوافق", "السماح بكل ملفات تعريف الارتباط",
        "Alle akzeptieren", "Tout accepter", "Aceptar todo",
    ]
    for label in labels:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
            if await btn.is_visible(timeout=1200):
                await btn.click()
                await page.wait_for_timeout(1000)
                return
        except Exception:
            continue


def parse_coords(url: str):
    m = COORD_RE.search(url or "")
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def parse_place_id(url: str):
    m = PLACE_RE.search(url or "")
    return m.group(1) if m else None


async def extract_detail(page, url: str, query_meta: dict) -> dict | None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(random.uniform(1300, 2400))
        if "sorry" in page.url.lower() or "consent.google" in page.url.lower():
            await accept_consent(page)
        title = await page.title()
        text_body = await page.locator("body").inner_text(timeout=5000)
        blocked_markers = ("unusual traffic", "not a robot", "captcha", "recaptcha")
        if any(marker in (title + " " + text_body[:4000]).lower() for marker in blocked_markers):
            return {
                **query_meta,
                "source": "google_maps_browser",
                "google_maps_url": page.url,
                "place_id": parse_place_id(page.url),
                "scrape_status": "blocked",
                "error": "Google Maps presented a traffic/robot verification page",
            }

        def first_text(selector: str):
            try:
                loc = page.locator(selector).first
                if loc.is_visible(timeout=800):
                    return (await loc.inner_text()).strip()
            except Exception:
                return None

        name = None
        try:
            h = page.locator("h1").first
            if await h.is_visible(timeout=1500):
                name = (await h.inner_text()).strip()
        except Exception:
            pass

        address = None
        try:
            loc = page.locator("button[data-item-id='address'], button[aria-label*='Address']").first
            if await loc.is_visible(timeout=1200):
                label = await loc.get_attribute("aria-label")
                address = re.sub(r"^(Address|العنوان):\s*", "", label or "", flags=re.I).strip()
        except Exception:
            pass

        phone = None
        try:
            loc = page.locator("button[data-item-id*='phone'], button[aria-label*='Phone']").first
            if await loc.is_visible(timeout=1200):
                label = await loc.get_attribute("aria-label")
                phone = re.sub(r"^(Phone|هاتف):\s*", "", label or "", flags=re.I).strip()
        except Exception:
            pass

        website = None
        try:
            loc = page.locator("a[data-item-id='authority'], a[aria-label*='Website']").first
            if await loc.is_visible(timeout=1200):
                website = await loc.get_attribute("href")
        except Exception:
            pass

        category = None
        try:
            loc = page.locator("button[jsaction*='category']").first
            if await loc.is_visible(timeout=1200):
                category = (await loc.inner_text()).strip()
        except Exception:
            pass

        rating = None
        try:
            loc = page.locator("span[role='img'][aria-label*='star'], span[role='img'][aria-label*='stars']").first
            if await loc.is_visible(timeout=1200):
                label = await loc.get_attribute("aria-label")
                m = re.search(r"(\d+(?:\.\d+)?)", label or "")
                if m:
                    rating = float(m.group(1))
        except Exception:
            pass

        reviews = None
        for pattern in [r"(\d[\d,]*)\s+reviews?", r"(\d[\d,]*)\s+تقييم"]:
            m = re.search(pattern, text_body, re.I)
            if m:
                try:
                    reviews = int(m.group(1).replace(",", ""))
                except ValueError:
                    pass
                break

        hours = None
        try:
            loc = page.locator("div[aria-label*='hours'], button[aria-label*='hours'], div[aria-label*='Hours']").first
            if await loc.is_visible(timeout=1200):
                hours = await loc.get_attribute("aria-label")
        except Exception:
            pass

        lat, lon = parse_coords(page.url)
        social = {}
        for a in await page.locator("a[href]").all():
            try:
                href = await a.get_attribute("href")
                if not href:
                    continue
                low = href.lower()
                if "facebook.com" in low and "facebook" not in social:
                    social["facebook"] = href
                elif "instagram.com" in low and "instagram" not in social:
                    social["instagram"] = href
                elif "wa.me" in low and "whatsapp" not in social:
                    social["whatsapp"] = href
            except Exception:
                continue

        status = "ok" if name else "partial"
        return {
            **query_meta,
            "source": "google_maps_browser",
            "source_id": parse_place_id(page.url) or page.url,
            "place_id": parse_place_id(page.url),
            "name": name,
            "category": category,
            "address": address,
            "phone": phone,
            "website": website,
            "social": social,
            "rating": rating,
            "reviews_count": reviews,
            "opening_hours": hours,
            "latitude": lat,
            "longitude": lon,
            "google_maps_url": page.url,
            "permanently_closed": "permanently closed" in text_body.lower() or "مغلق نهائي" in text_body,
            "temporarily_closed": "temporarily closed" in text_body.lower() or "مغلق مؤقت" in text_body,
            "scrape_status": status,
        }
    except Exception as exc:
        return {
            **query_meta,
            "source": "google_maps_browser",
            "google_maps_url": url,
            "place_id": parse_place_id(url),
            "scrape_status": "error",
            "error": str(exc)[:500],
        }


async def collect_search_urls(page, query: str, max_results: int):
    search_url = "https://www.google.com/maps/search/" + quote(query, safe="") + "?hl=en"
    await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(random.uniform(1800, 3000))
    await accept_consent(page)

    body_text = (await page.locator("body").inner_text(timeout=5000))[:5000]
    low = body_text.lower()
    if any(x in low for x in ("unusual traffic", "not a robot", "captcha", "recaptcha")):
        return [], "blocked"

    try:
        feed = page.locator("div[role='feed']").first
        await feed.wait_for(state="visible", timeout=15000)
    except PlaywrightTimeout:
        return [], "no_feed"

    seen = set()
    stable_rounds = 0
    previous_count = 0
    for _ in range(35):
        links = await page.locator("a[href*='/maps/place/']").all()
        for link in links:
            try:
                href = await link.get_attribute("href")
                if not href or "/maps/place/" not in href:
                    continue
                if not href.startswith("http"):
                    href = "https://www.google.com" + href
                pid = parse_place_id(href) or href.split("?")[0]
                seen.add(pid)
            except Exception:
                continue
        if len(seen) >= max_results:
            break
        if len(seen) == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            previous_count = len(seen)
        if stable_rounds >= 4:
            break
        try:
            await feed.evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
        except Exception:
            break
        await page.wait_for_timeout(random.uniform(1000, 1800))

    urls = []
    links = await page.locator("a[href*='/maps/place/']").all()
    added = set()
    for link in links:
        try:
            href = await link.get_attribute("href")
            if not href or "/maps/place/" not in href:
                continue
            if not href.startswith("http"):
                href = "https://www.google.com" + href
            pid = parse_place_id(href) or href.split("?")[0]
            if pid not in added:
                added.add(pid)
                urls.append(href)
                if len(urls) >= max_results:
                    break
        except Exception:
            continue
    return urls, "ok"


async def run_shard(cfg, shard: int, shards: int, out_dir: Path):
    queries = build_queries(cfg)
    shard_queries = [q for i, q in enumerate(queries) if i % shards == shard]
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / f"shard_{shard}.state.json"
    result_path = out_dir / f"shard_{shard}.jsonl"

    state = {"completed_queries": [], "stats": {"queries_ok": 0, "queries_blocked": 0, "queries_no_feed": 0, "places": 0}}
    if state_path.exists():
        try:
            state.update(json.loads(state_path.read_text(encoding="utf-8")))
        except Exception:
            pass

    completed = set(state.get("completed_queries", []))
    existing = {}
    if result_path.exists():
        for line in result_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                existing[stable_key(row)] = row
            except Exception:
                continue

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        search_context = await browser.new_context(locale="en-US")
        search_page = await search_context.new_page()
        semaphore = asyncio.Semaphore(int(cfg.get("detail_concurrency", 3)))
        last_open_times = []

        async def detail_one(url, meta):
            async with semaphore:
                now = time.monotonic()
                last_open_times[:] = [t for t in last_open_times if now - t < 60]
                if len(last_open_times) >= int(cfg.get("detail_per_minute", 15)):
                    await asyncio.sleep(max(0.5, 60 - (now - last_open_times[0])))
                last_open_times.append(time.monotonic())
                context = await browser.new_context(locale="en-US")
                page = await context.new_page()
                try:
                    return await extract_detail(page, url, meta)
                finally:
                    await context.close()

        for index, meta in enumerate(shard_queries, start=1):
            qkey = meta["query"]
            if qkey in completed:
                continue
            print(json.dumps({"shard": shard, "query_index": index, "query": qkey}, ensure_ascii=False), flush=True)
            try:
                urls, status = await collect_search_urls(search_page, qkey, int(cfg.get("max_results_per_query", 60)))
                if status == "blocked":
                    state["stats"]["queries_blocked"] += 1
                    completed.add(qkey)
                    state["completed_queries"] = sorted(completed)
                    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                    await asyncio.sleep(random.uniform(20, 35))
                    continue
                if status != "ok":
                    state["stats"]["queries_no_feed"] += 1
                    completed.add(qkey)
                    state["completed_queries"] = sorted(completed)
                    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                    await asyncio.sleep(random.uniform(8, 15))
                    continue

                metas = []
                for url in urls:
                    metas.append({
                        **meta,
                        "input_url": url,
                        "queried_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    })
                for start in range(0, len(metas), 30):
                    batch = metas[start:start+30]
                    tasks = [detail_one(m["input_url"], m) for m in batch]
                    results = await asyncio.gather(*tasks, return_exceptions=False)
                    for row in results:
                        if not row:
                            continue
                        key = stable_key(row)
                        old = existing.get(key)
                        if old:
                            for field in ("phone","website","address","opening_hours","category","rating","reviews_count","latitude","longitude","google_maps_url"):
                                if not old.get(field) and row.get(field):
                                    old[field] = row[field]
                            old["queries_seen"] = sorted(set((old.get("queries_seen") or []) + [qkey]))
                        else:
                            row["queries_seen"] = [qkey]
                            existing[key] = row
                    with result_path.open("w", encoding="utf-8") as fh:
                        for row in existing.values():
                            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                    state["stats"]["places"] = len(existing)
                    print(json.dumps({"shard": shard, "progress": start+len(batch), "urls_for_query": len(urls), "unique_places": len(existing)}, ensure_ascii=False), flush=True)

                state["stats"]["queries_ok"] += 1
            except Exception as exc:
                print(json.dumps({"shard": shard, "query_error": str(exc)[:500], "query": qkey}, ensure_ascii=False), flush=True)
            finally:
                completed.add(qkey)
                state["completed_queries"] = sorted(completed)
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                await asyncio.sleep(random.uniform(float(cfg.get("delay_min", 1.5)), float(cfg.get("delay_max", 3.5))))

        await browser.close()
    print(json.dumps({"shard_done": shard, "unique_places": len(existing), "stats": state["stats"]}, ensure_ascii=False), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/ibb_maps_queries.yml")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=8)
    ap.add_argument("--output-dir", default=".scan/google_maps_browser")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    asyncio.run(run_shard(cfg, args.shard, args.shards, Path(args.output_dir)))


if __name__ == "__main__":
    main()
