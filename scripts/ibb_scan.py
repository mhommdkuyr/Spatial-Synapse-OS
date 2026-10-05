#!/usr/bin/env python3
import argparse,csv,json,math,os,time
from pathlib import Path
import requests,yaml

UA="Spatial-Synapse-OS/1.0 (+https://github.com/mhommdkuyr/Spatial-Synapse-OS)"

def distance(a,b):
    R=6371000
    p1,p2=map(math.radians,(a[0],b[0]))
    dp=math.radians(b[0]-a[0]); dl=math.radians(b[1]-a[1])
    q=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(q))

def grid(b,spacing):
    lat_step=spacing/111320
    lon_step=spacing/(111320*math.cos(math.radians((b["south"]+b["north"])/2)))
    y=b["south"]
    while y<=b["north"]+1e-12:
        x=b["west"]
        while x<=b["east"]+1e-12:
            yield round(y,7),round(x,7)
            x+=lon_step
        y+=lat_step

def normalize_osm(e):
    t=e.get("tags",{}); c=e.get("center",{})
    lat=e.get("lat",c.get("lat")); lon=e.get("lon",c.get("lon"))
    if lat is None or lon is None:return None
    return {"source":"osm","source_id":str(e["id"]),"name":t.get("name") or t.get("name:ar") or t.get("brand"),
      "category":t.get("shop") or t.get("amenity") or t.get("craft") or t.get("office") or t.get("tourism"),
      "latitude":lat,"longitude":lon,
      "address":t.get("addr:full") or " ".join(v for v in [t.get("addr:street"),t.get("addr:housenumber"),t.get("addr:district")] if v),
      "phone":t.get("phone") or t.get("contact:phone"),
      "website":t.get("website") or t.get("contact:website"),
      "social":{k:v for k,v in t.items() if k.startswith("contact:") or k in ["facebook","instagram","twitter","whatsapp"]},
      "opening_hours":t.get("opening_hours"),"products_services":t.get("products") or t.get("product"),"raw_tags":t}

def collect_osm(b,urls):
    q=f"""[out:json][timeout:300];(nwr[shop]({b['south']},{b['west']},{b['north']},{b['east']});nwr[amenity]({b['south']},{b['west']},{b['north']},{b['east']});nwr[craft]({b['south']},{b['west']},{b['north']},{b['east']});nwr[office]({b['south']},{b['west']},{b['north']},{b['east']});nwr[tourism]({b['south']},{b['west']},{b['north']},{b['east']}););out center tags;"""
    err=None
    for u in urls:
        try:
            r=requests.post(u,data=q,headers={"User-Agent":UA},timeout=360);r.raise_for_status()
            return [x for e in r.json().get("elements",[]) if (x:=normalize_osm(e))]
        except Exception as e:err=e
    raise RuntimeError(f"Overpass failed: {err}")

def collect_google(cfg):
    key=os.getenv("GOOGLE_PLACES_API_KEY")
    if not key:return [],{"enabled":False,"reason":"missing GOOGLE_PLACES_API_KEY"}
    url="https://places.googleapis.com/v1/places:searchNearby"
    headers={"Content-Type":"application/json","X-Goog-Api-Key":key,
             "X-Goog-FieldMask":"places.id,places.displayName,places.location,places.formattedAddress,places.nationalPhoneNumber,places.internationalPhoneNumber,places.websiteUri,places.types,places.regularOpeningHours"}
    rows=[]; errors=0; requests_count=0
    for lat,lon in grid(cfg["bbox"],cfg["grid_spacing_m"]):
        body={"includedTypes":["store"],"maxResultCount":min(20,cfg["google"]["max_result_count"]),
              "locationRestriction":{"circle":{"center":{"latitude":lat,"longitude":lon},"radius":cfg["google"]["radius_m"]}}}
        try:
            r=requests.post(url,headers=headers,json=body,timeout=45);requests_count+=1
            if r.status_code in (429,500,502,503,504):errors+=1;time.sleep(1);continue
            r.raise_for_status()
            for p in r.json().get("places",[]):
                l=p.get("location",{});d=p.get("displayName",{})
                rows.append({"source":"google","source_id":p.get("id"),"name":d.get("text"),
                  "category":(p.get("types") or [None])[0],"latitude":l.get("latitude"),"longitude":l.get("longitude"),
                  "address":p.get("formattedAddress"),"phone":p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber"),
                  "website":p.get("websiteUri"),"social":{},"opening_hours":p.get("regularOpeningHours"),
                  "products_services":None,"raw_tags":p})
        except Exception:errors+=1
    return rows,{"enabled":True,"requests":requests_count,"errors":errors}

def merge(rows):
    out=[]
    for r in rows:
        if r.get("latitude") is None:continue
        same=None
        for i,x in enumerate(out):
            names=(r.get("name") or "").strip().casefold(),(x.get("name") or "").strip().casefold()
            if names[0] and names[0]==names[1] and distance((r["latitude"],r["longitude"]),(x["latitude"],x["longitude"]))<=75:
                same=i;break
        if same is None:
            r["sources"]=[r["source"]];r["source_ids"]=[r.get("source_id")];out.append(r)
        else:
            x=out[same];x["sources"]=sorted(set(x["sources"]+[r["source"]]))
            x["source_ids"]=sorted(set(x["source_ids"]+[r.get("source_id")]))
            for k in ["phone","website","address","opening_hours","products_services"]:
                if not x.get(k) and r.get(k):x[k]=r[k]
    return out

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--config",default="config/ibb.yml");a=ap.parse_args()
    c=yaml.safe_load(Path(a.config).read_text(encoding="utf8"));osm=collect_osm(c["bbox"],c["osm"]["overpass_urls"]) if c["osm"]["enabled"] else []
    google,gmeta=collect_google(c) if c["google"]["enabled"] else ([],{"enabled":False})
    rows=merge(osm+google);Path("data").mkdir(exist_ok=True)
    Path(c["output"]["jsonl"]).write_text("\n".join(json.dumps(x,ensure_ascii=False) for x in rows)+"\n",encoding="utf8")
    fields=["name","category","latitude","longitude","address","phone","website","opening_hours","products_services","sources","source_ids"]
    with open(c["output"]["csv"],"w",encoding="utf8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({k:x.get(k) for k in fields} for x in rows)
    report={"city":c["city"],"bbox":c["bbox"],"grid_spacing_m":c["grid_spacing_m"],"osm_count":len(osm),"google_count":len(google),"unique_count":len(rows),"google":gmeta,"generated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
    Path(c["output"]["report"]).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8");print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
