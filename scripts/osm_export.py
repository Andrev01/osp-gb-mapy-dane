#!/usr/bin/env python3
"""
OSP G-B Asystent – eksport warstw OpenStreetMap dla całej Polski (uruchamiane przez GitHub Actions).

Dla każdej warstwy pobiera obiekty z serwerów Overpass w 16 prostokątach (te same co w aplikacji),
zostawia tylko potrzebne tagi i zapisuje plik <warstwa>.json.gz w formacie zapisu aplikacji:
[{"id": ..., "lat": ..., "lon": ..., "t": {tagi}}, ...]
Warstwa trafia do wyniku tylko wtedy, gdy pobrały się WSZYSTKIE prostokąty (bez niepełnych danych).
Na końcu zapisuje manifest.json (data, liczby punktów, rozmiary).

Dane: © autorzy OpenStreetMap, licencja ODbL.
"""
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Kolejność wg pierwszego przebiegu (2026-11-26): dresel odpowiadał, dwa pozostałe często 502/504.
# Serwer, który ostatnio odpowiedział, jest pytany jako pierwszy przy kolejnym obszarze.
SERVERS = [
    "https://ovp.dresel.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
_preferred = [SERVERS[0]]
UA = "OSP-G-B-Asystent-data-export (GitHub Actions)"

LATS = [49.0, 50.5, 52.0, 53.5, 55.0]
LONS = [14.0, 16.6, 19.1, 21.6, 24.2]
TILES = [(LATS[a], LONS[b], LATS[a + 1], LONS[b + 1]) for a in range(4) for b in range(4)]

# 1.2.6: wynik dzielony na siatkę obszarów (te same w aplikacji): 4×4 = 16, a dla hydrantów 8×8 = 64 (ponad 400 tys. punktów –
# telefon wczytuje tylko obszary z widoku). Pliki: <warstwa>-<nr>.json.gz, opis: manifest-<warstwa>.json.
GRID = {"hydranty": 8}


def grid_cells(n):
    lat0, lat1, lon0, lon1 = 49.0, 55.0, 14.0, 24.2
    dl, dn = (lat1 - lat0) / n, (lon1 - lon0) / n
    return [(lat0 + a * dl, lon0 + b * dn, lat0 + (a + 1) * dl, lon0 + (b + 1) * dn) for a in range(n) for b in range(n)]


def cell_of(lat, lon, n):
    a = min(n - 1, max(0, int((lat - 49.0) / ((55.0 - 49.0) / n))))
    b = min(n - 1, max(0, int((lon - 14.0) / ((24.2 - 14.0) / n))))
    return a * n + b

LAYERS = {
    "straznice": ['nwr["amenity"="fire_station"]'],
    "syreny": ['nwr["emergency"="siren"]', 'nwr["tower:type"="siren"]', 'nwr["man_made"="siren"]'],
    "zbiorki": ['nwr["emergency"="assembly_point"]'],
    "sluzby": ['nwr["emergency"="ambulance_station"]', 'nwr["amenity"="hospital"]', 'nwr["amenity"="police"]'],
    "woda": ['nwr["emergency"~"^(water_tank|fire_water_pond|suction_point)$"]'],
    "hydranty": ['nwr["emergency"="fire_hydrant"]'],
    "aed": ['nwr["emergency"="defibrillator"]'],
    "ladowiska": ['nwr["aeroway"~"^(helipad|heliport)$"]'],
}

KEEP_EXACT = {
    "name", "official_name", "short_name", "operator", "ref", "description", "note", "access", "indoor", "level",
    "phone", "opening_hours", "website", "emergency", "amenity", "capacity", "volume", "water_source", "flow_rate",
    "model", "manufacturer", "height", "tower:type", "man_made", "check_date", "healthcare", "healthcare:speciality",
    "defibrillator:location", "location", "aeroway", "lit", "surface", "icao",
}
KEEP_PREFIX = ("addr:", "contact:", "fire_hydrant:", "water_tank:", "siren:", "suction_point:", "couplings")


def keep_tags(tags):
    return {k: v for k, v in tags.items() if k in KEEP_EXACT or k.startswith(KEEP_PREFIX)}


def query(filters, tile):
    bbox = "(%s,%s,%s,%s);" % tile
    q = "[out:json][timeout:180];(" + "".join(f + bbox for f in filters) + ");out center tags;"
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    order = [_preferred[0]] + [x for x in SERVERS if x != _preferred[0]]
    for attempt in range(5):
        server = order[attempt % len(order)]
        try:
            req = urllib.request.Request(server, data=data, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=190) as r:
                body = json.loads(r.read().decode("utf-8"))
            if "remark" in body and "runtime error" in str(body.get("remark", "")):
                raise RuntimeError(body["remark"])
            _preferred[0] = server
            return body["elements"]
        except Exception as e:  # noqa: BLE001
            last = "%s: %s" % (server, e)
            print("   próba %d nieudana – %s" % (attempt + 1, last), flush=True)
            time.sleep(5 + attempt * 10)
    raise RuntimeError(last)


def export_layer(name, filters, out_dir):
    by_id = {}
    for i, tile in enumerate(TILES):
        print(" %s: obszar %d/16 %s" % (name, i + 1, tile), flush=True)
        for e in query(filters, tile):
            pos = e if "lat" in e else e.get("center")
            if not pos:
                continue
            oid = e["id"] * 4 + {"way": 1, "relation": 2}.get(e.get("type"), 0)
            by_id[oid] = {"id": oid, "lat": round(pos["lat"], 6), "lon": round(pos["lon"], 6), "t": keep_tags(e.get("tags", {}))}
        time.sleep(3)
    pts = list(by_id.values())
    n = GRID.get(name, 4)
    cells = [[] for _ in range(n * n)]
    for p in pts:
        cells[cell_of(p["lat"], p["lon"], n)].append(p)
    total = 0
    tiles = {}
    for i, c in enumerate(cells):
        raw = json.dumps(c, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path = os.path.join(out_dir, "%s-%d.json.gz" % (name, i))
        with gzip.open(path, "wb", compresslevel=9) as f:
            f.write(raw)
        total += os.path.getsize(path)
        tiles[str(i)] = len(c)
    print(" %s: %d punktów w %d obszarach, %.2f MB (spakowane)" % (name, len(pts), n * n, total / 1e6), flush=True)
    return {"count": len(pts), "bytes": total, "grid": n, "tiles": tiles}


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "out"
    only = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else list(LAYERS)
    os.makedirs(out_dir, exist_ok=True)
    manifest = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "© autorzy OpenStreetMap, ODbL", "layers": {}, "failed": {}}
    for name in only:
        try:
            manifest["layers"][name] = export_layer(name, LAYERS[name], out_dir)
        except Exception as e:  # noqa: BLE001
            manifest["failed"][name] = str(e)[:300]
            print(" %s: POMINIĘTA – %s" % (name, e), flush=True)
    # 1.2.6: przy jednej warstwie (zadanie równoległe) – osobny manifest, żeby zadania nie nadpisywały sobie wyników.
    man_name = "manifest-%s.json" % only[0] if len(only) == 1 else "manifest.json"
    with open(os.path.join(out_dir, man_name), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(json.dumps(manifest, ensure_ascii=False, indent=1))
    if not manifest["layers"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
