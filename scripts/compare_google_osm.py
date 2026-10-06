#!/usr/bin/env python3
"""Cross-source audit: Google Maps browser + OpenStreetMap."""
from __future__ import annotations
import csv,json,math,re
from pathlib import Path

def norm(v):
    if not v:return ""
    v=re.sub(r"[^\w\u0600-\u06FF]+"," ",str(v).casefold(),flags=re.UNICODE)
    return re.sub(r"\s+"," ",v).strip()

def hav(a,b):
    if None in (a,b): return None
    lat1,lon1=a;lat2,lon2=b
    p=math.pi/180
    x=(lat2-lat1)*p*6371000
    y=(lon2-lon1)*p*6371000*math.cos((lat1+lat2)*p/2)
    return (x*x+y*y)**0.5

def read_jsonl(path):
    rows=[]
    p=Path(path)
    if not p.exists():return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        try: rows.append(json.loads(line))
        except Exception: pass
    return rows

def place_key(r):
    if r.get("place_id"): return ("pid",r["place_id"])
    n=norm(r.get("name")); a=norm(r.get("address"))
    lat=r.get("latitude"); lon=r.get("longitude")
    if n and lat is not None and lon is not None: return ("geo",n,round(float(lat),4),round(float(lon),4))
    return ("nameaddr",n,a)

def main():
    g=read_jsonl("data/google_maps_browser_results.jsonl")
    o=read_jsonl("data/ibb_places.jsonl")
    gmap={place_key(r):r for r in g}
    osmmap={place_key(r):r for r in o}
    matches=[]; google_only=[]; osm_only=[]
    used=set()
    osm_names=[(norm(r.get("name")),r) for r in o if r.get("name")]
    for gr in g:
        matched=None
        for orow in o:
            if not gr.get("name") or not orow.get("name"): continue
            gn,on=norm(gr.get("name")),norm(orow.get("name"))
            if gn and on and (gn==on or gn in on or on in gn):
                d=hav((gr.get("latitude"),gr.get("longitude")),(orow.get("latitude"),orow.get("longitude")))
                if d is None or d<=150:
                    matched=(orow,d);break
        if matched:
            matches.append({"google":gr,"osm":matched[0],"distance_m":matched[1]})
            used.add(id(matched[0]))
        else: google_only.append(gr)
    for r in o:
        if id(r) not in used: osm_only.append(r)
    report={
      "city":"Ibb, Yemen",
      "generated_at":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
      "google_records":len(g),
      "osm_records":len(o),
      "name_geo_matches":len(matches),
      "google_only":len(google_only),
      "osm_only":len(osm_only),
      "note":"Name/address matching is heuristic; Google Maps browser results and OSM remain separately identifiable."
    }
    Path("data/google_osm_comparison_current.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    with Path("data/google_osm_matches_current.jsonl").open("w",encoding="utf-8") as fh:
        for x in matches: fh.write(json.dumps(x,ensure_ascii=False)+"\n")
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=="__main__": main()
