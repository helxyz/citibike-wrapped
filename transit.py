"""Last mile: how close are Citi Bike docks, and the rides that use them, to rail transit?

Inputs (all public, put them in data/):
  subway_entrances.csv     MTA "Subway Entrances and Exits" (data.ny.gov)
  path_stations.geojson    PATH stations (Jersey City open data)
  hblr_stations.geojson    NJ Transit light rail stations (only the Hudson-Bergen line is used)
File names can also be the original download names; see FILES below.

wrapped.py calls run(); it returns None (and the page simply skips the section) if the
subway file isn't there. Without the PATH file, Jersey City and Hoboken are left out of the
section rather than measured against New York subway stops.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
RADII = (100, 200, 400)        # "near transit" cut-offs in metres (straight line), shown side by side
NEAR_M, FAR_M = 200, 400       # a "feeder" ride goes from a dock beyond FAR_M to a dock within NEAR_M
BBOX = (40.40, 41.00, -74.30, -73.60)   # lat min/max, lng min/max: NYC + Hudson County; drops bad coordinates
NJ_AREAS = {"Jersey City", "Hoboken"}

FILES = {
    "subway": ["subway_entrances.csv", "*Subway_Entrances*.csv", "*subway*entrances*.csv"],
    "path": ["path_stations.geojson", "*path-stations*.geojson", "*path_stations*.geojson"],
    "hblr": ["hblr_stations.geojson", "*Light_Rail*.geojson"],
}
# The PATH file has no names; these are its 13 stations, matched by location (within 250 m).
PATH_NAMES = [(40.73403, -74.16482, "Newark Penn Station (PATH)"), (40.73945, -74.15566, "Harrison (PATH)"),
              (40.73189, -74.06281, "Journal Square (PATH)"), (40.71945, -74.04265, "Grove Street (PATH)"),
              (40.72698, -74.03469, "Newport (PATH)"), (40.71679, -74.03229, "Exchange Place (PATH)"),
              (40.73507, -74.02964, "Hoboken (PATH)"), (40.71109, -74.01066, "World Trade Center (PATH)"),
              (40.73310, -74.00621, "Christopher St (PATH)"), (40.73442, -73.99944, "9th St (PATH)"),
              (40.73733, -73.99690, "14th St (PATH)"), (40.74285, -73.99288, "23rd St (PATH)"),
              (40.74955, -73.98799, "33rd St (PATH)")]


def _find(data_dir: Path, kind: str):
    for pat in FILES[kind]:
        hits = sorted(data_dir.glob(pat))
        if hits:
            return hits[0]
    return None


def _haversine_m(la1, lo1, la2, lo2):
    la1, lo1, la2, lo2 = map(np.radians, (la1, lo1, la2, lo2))
    a = np.sin((la2 - la1) / 2) ** 2 + np.cos(la1) * np.cos(la2) * np.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _col(df: pd.DataFrame, *names):
    """First column whose name matches one of `names`, ignoring case, spaces and underscores."""
    norm = {c.lower().replace(" ", "").replace("_", ""): c for c in df.columns}
    for n in names:
        if n in norm:
            return norm[n]
    return None


def _subway(path: Path) -> pd.DataFrame:
    s = pd.read_csv(path)
    lat, lng = _col(s, "entrancelatitude", "latitude", "lat"), _col(s, "entrancelongitude", "longitude", "lng", "lon")
    if lat and lng:
        la, lo = pd.to_numeric(s[lat], errors="coerce"), pd.to_numeric(s[lng], errors="coerce")
    else:                                      # fall back to a WKT column like "POINT (-73.97 40.68)"
        geo = _col(s, "entrancegeoreference", "georeference", "geometry")
        if not geo:
            raise SystemExit(f"transit: can't find entrance coordinates in {path.name} (columns: {list(s.columns)})")
        xy = s[geo].astype(str).str.extract(r"POINT \(([-\d.]+) ([-\d.]+)\)").astype(float)
        lo, la = xy[0], xy[1]
    name = _col(s, "stopname", "stationname", "name")
    return pd.DataFrame({"name": s[name] if name else "Subway", "kind": "subway", "lat": la, "lng": lo})


def _points_geojson(path: Path):
    for f in json.loads(path.read_text()).get("features", []):
        g = f.get("geometry") or {}
        if g.get("type") == "Point" and len(g.get("coordinates", [])) >= 2:
            yield f.get("properties") or {}, float(g["coordinates"][1]), float(g["coordinates"][0])


def load_points(data_dir: Path) -> pd.DataFrame | None:
    """All rail access points as name, kind, lat, lng. None if the subway file is missing."""
    sub = _find(data_dir, "subway")
    if sub is None:
        return None
    parts = [_subway(sub)]
    p = _find(data_dir, "path")
    if p is not None:
        seen = {}
        for _, la, lo in _points_geojson(p):
            seen.setdefault((round(la, 4), round(lo, 4)), (la, lo))          # the file lists each station twice
        rows = []
        for la, lo in seen.values():
            d = [_haversine_m(la, lo, a, b) for a, b, _ in PATH_NAMES]
            i = int(np.argmin(d))
            rows.append({"name": PATH_NAMES[i][2] if d[i] < 250 else "PATH station", "kind": "path", "lat": la, "lng": lo})
        parts.append(pd.DataFrame(rows))
    h = _find(data_dir, "hblr")
    if h is not None:
        parts.append(pd.DataFrame([{"name": f'{pr.get("STATION", "Light rail")} (light rail)', "kind": "hblr", "lat": la, "lng": lo}
                                   for pr, la, lo in _points_geojson(h) if pr.get("LINE_CODE") == "HB"]))
    pts = pd.concat([x for x in parts if len(x)], ignore_index=True)
    ok = pts.lat.between(BBOX[0], BBOX[1]) & pts.lng.between(BBOX[2], BBOX[3])
    if (~ok).sum():
        print(f"transit: ignoring {(~ok).sum()} rail points with missing or out-of-area coordinates")
    return pts[ok].reset_index(drop=True)


def nearest(stations: pd.DataFrame, pts: pd.DataFrame, chunk: int = 256) -> pd.DataFrame:
    """For each station: straight-line metres to the closest rail point, and which one it is.
    Done in chunks so memory stays small (~256 x 2,200 distances at a time)."""
    la2, lo2 = pts["lat"].to_numpy(float)[None, :], pts["lng"].to_numpy(float)[None, :]
    la1, lo1 = stations["lat"].to_numpy(float), stations["lng"].to_numpy(float)
    dist, idx = np.empty(len(la1)), np.empty(len(la1), dtype=int)
    for i in range(0, len(la1), chunk):
        d = _haversine_m(la1[i:i + chunk, None], lo1[i:i + chunk, None], la2, lo2)
        idx[i:i + chunk] = d.argmin(axis=1)
        dist[i:i + chunk] = d[np.arange(len(d)), idx[i:i + chunk]]
    return pd.DataFrame({"station_id": stations["station_id"].to_numpy(), "transit_m": dist,
                         "transit_kind": pts["kind"].to_numpy()[idx], "transit_name": pts["name"].to_numpy()[idx]})


def run(con, station_area: pd.DataFrame, keep: set, data_dir: Path | None = None) -> dict | None:
    """Run transit.sql against the `rides` view and return the numbers the page needs."""
    data_dir = data_dir or ROOT / "data"
    pts = load_points(data_dir)
    if pts is None or not (pts.kind == "subway").any():
        print("transit: data/subway_entrances.csv not found or empty; skipping the last-mile section")
        return None
    counts = pts.kind.value_counts().to_dict()
    print("transit: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    keep = set(keep)
    if "path" not in counts:
        dropped = keep & NJ_AREAS
        keep -= NJ_AREAS
        if dropped:
            print(f"transit: no PATH file, so the last-mile section leaves out {' and '.join(sorted(dropped))}")

    st = station_area.dropna(subset=["lat", "lng"])
    near = nearest(st, pts)
    near = near.merge(st[["station_id", "area"]], on="station_id")
    far_out = near[near.area.isin(keep) & (near.transit_m > 3000)]
    if len(far_out):
        print(f"transit: note: {len(far_out)} docks are over 3 km from any rail point (check coordinates), e.g. "
              + ", ".join(far_out.station_id.astype(str).head(3)))
    con.register("station_transit_df", near.drop(columns="area"))
    con.execute("CREATE OR REPLACE TABLE station_transit AS SELECT * FROM station_transit_df")
    con.execute((ROOT / "transit.sql").read_text())

    ta_all = con.execute("SELECT * FROM tr_area").df().set_index("area")
    total = con.execute("SELECT SUM(rides) FROM w_totals").fetchone()[0]       # same rides view, already counted
    used = int(ta_all.rides.sum())
    nwd = int(ta_all.weekdays.max()) if len(ta_all) else 0
    ta = ta_all[ta_all.index.isin(keep)]

    def share(a, b):
        return round(float(a) / float(b), 4) if b else None

    areas = {}
    for name, r in ta.iterrows():
        d = int(r.docks)
        n_ = int(r[f"docks{NEAR_M}"])
        s_ = int(r[f"s{NEAR_M}"])
        lift = None
        if 0 < n_ < d and 0 < s_ < r.rides:
            lift = round((s_ / n_) / ((r.rides - s_) / (d - n_)), 2)
        areas[name] = {
            "rides": int(r.rides), "docks": d, "far_docks": d - n_,
            "start_near": {str(k): share(r[f"s{k}"], r.rides) for k in RADII},
            "end_near": {str(k): share(r[f"e{k}"], r.rides) for k in RADII},
            "either": {str(k): share(r[f"b{k}"], r.rides) for k in RADII},
            "both": {str(k): share(r[f"t{k}"], r.rides) for k in RADII},
            "docks_near": {str(k): share(r[f"docks{k}"], d) for k in RADII},
            "lift": lift,
            "median_start_m": round(float(r.median_start_m)), "median_end_m": round(float(r.median_end_m)),
            "am_rides": int(r.am), "pm_rides": int(r.pm),
            "am_toward": share(r.am_toward, r.am), "am_away": share(r.am_away, r.am),
            "pm_toward": share(r.pm_toward, r.pm), "pm_away": share(r.pm_away, r.pm),
        }
    fe = con.execute("SELECT * FROM tr_feeders").df()
    fe = fe[fe.area.isin(keep)].copy()
    fe["per_day"] = fe.rides / max(nwd, 1)

    def pack(df):
        return [{"name": r.name, "area": r.area, "m": round(float(r.m1)), "per_day": round(float(r.per_day), 1),
                 "to": r.to_transit} for r in df.itertuples()]
    order = ["rides", "sid"]                                   # ties broken by dock id, so reruns match
    top = fe.sort_values(order, ascending=[False, True]).head(10)
    by_area = {a: pack(g.sort_values(order, ascending=[False, True]).head(3)) for a, g in fe.groupby("area")}
    return {"radii": list(RADII), "near_m": NEAR_M, "far_m": FAR_M, "weekdays": nwd,
            "points": {k: int(v) for k, v in counts.items()},
            "coverage": share(used, total), "areas": areas,
            "feeders": pack(top), "feeders_by_area": by_area}
