"""Citi Bike Wrapped: a year of rides, borough vs borough.

    1. Download a year of trip files from https://s3.amazonaws.com/tripdata/index.html
       (NYC: 2025-citibike-tripdata.zip or the 12 monthly zips; Jersey City/Hoboken:
       JC-2025MM-citibike-tripdata.csv.zip) and unzip them anywhere, e.g. ~/Downloads/citibike-2025/.
    2. python wrapped.py --year 2025 --trips "$HOME/Downloads/citibike-2025/**/*.csv"
    3. Open output/wrapped_2025.html (also writes wrapped_2025.json).

A full NYC year is ~45M rows; DuckDB handles it on a laptop in a few minutes.
"""
import argparse, glob, json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
from shapely.geometry import Point, shape

import gaps
import transit

ROOT = Path(__file__).resolve().parent
BOROUGHS = ROOT / "data" / "borough_boundaries.geojson"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def station_areas(con) -> pd.DataFrame:
    """One row per station id: median location and which area it sits in."""
    st = con.execute("""
        SELECT station_id, approx_quantile(lat, 0.5) AS lat, approx_quantile(lng, 0.5) AS lng FROM (
            SELECT start_station_id AS station_id, start_lat AS lat, start_lng AS lng FROM trips
            UNION ALL SELECT end_station_id, end_lat, end_lng FROM trips)
        WHERE station_id IS NOT NULL AND lat IS NOT NULL GROUP BY 1""").df()
    feats = json.loads(BOROUGHS.read_text())["features"]
    polys = [(f["properties"].get("BoroName") or f["properties"].get("boroname"), shape(f["geometry"]))
             for f in feats]

    def area(r):
        sid = str(r.station_id)
        if sid.startswith("JC"):
            return "Jersey City"
        if sid.startswith("HB"):
            return "Hoboken"
        p = Point(r.lng, r.lat)
        for name, poly in polys:
            if poly.contains(p):
                return name
        name, poly = min(polys, key=lambda np_: np_[1].distance(p))
        return name if poly.distance(p) < 0.003 else "Other"   # piers / shoreline docks
    st["area"] = [area(r) for r in st.itertuples()]
    return st


def station_types(hourly: pd.DataFrame, min_rides: int, k: int = 3, seed: int = 0):
    """Cluster stations by WHEN rides leave them (k-means, written out with numpy).

    Features per station: share of its weekday departures in each hour (24 numbers that sum to 1)
    plus its weekend share of all departures. Clusters are then named by their shape:
    the one whose departures peak most in the weekday morning = homes, evening = workplaces,
    the rest = leisure (midday and weekends).
    """
    import numpy as np
    wk = hourly[~hourly.is_weekend].pivot_table(index="sid", columns="hr", values="n", aggfunc="sum").reindex(columns=range(24)).fillna(0)
    we = hourly[hourly.is_weekend].groupby("sid").n.sum()
    total = wk.sum(axis=1) + we.reindex(wk.index).fillna(0)
    keep = total[total >= min_rides].index
    wk = wk.loc[keep]
    prof = wk.div(wk.sum(axis=1).replace(0, 1), axis=0).to_numpy()
    wshare = (we.reindex(keep).fillna(0) / total.loc[keep]).to_numpy()
    X = np.column_stack([prof, wshare])
    rng = np.random.default_rng(seed)
    best = None
    for _ in range(10):                                  # several k-means++ starts, keep the tightest
        cent = [X[rng.integers(len(X))]]
        for _ in range(k - 1):
            d = np.min(((X[:, None, :] - np.array(cent)[None]) ** 2).sum(-1), axis=1)
            cent.append(X[rng.choice(len(X), p=d / d.sum())])
        cent = np.array(cent)
        for _ in range(100):
            lab = ((X[:, None, :] - cent[None]) ** 2).sum(-1).argmin(1)
            new = np.array([X[lab == j].mean(0) if (lab == j).any() else cent[j] for j in range(k)])
            if np.allclose(new, cent):
                break
            cent = new
        inertia = ((X - cent[lab]) ** 2).sum()
        if best is None or inertia < best[0]:
            best = (inertia, lab, cent)
    _, lab, cent = best
    am = cent[:, 7:10].sum(1); pm = cent[:, 16:19].sum(1)
    home = int(np.argmax(am - pm)); work = int(np.argmax(pm - am))
    names = {home: "home", work: "work"}
    for j in range(k):
        names.setdefault(j, "leisure")
    types = pd.Series([names[j] for j in lab], index=keep)
    profiles = {}
    for j, nm in names.items():
        rows = X[lab == j]
        profiles[nm] = {"hourly": [round(float(v), 4) for v in rows[:, :24].mean(0)],
                        "weekend": round(float(rows[:, 24].mean()), 4), "stations": int(len(rows))}
    return types, profiles


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--trips", required=True, help="glob of trip CSVs, quoted")
    ap.add_argument("--out", default=str(ROOT / "output"))
    ap.add_argument("--memory", help="e.g. 6GB; cap DuckDB's memory use")
    ap.add_argument("--threads", type=int, help="fewer threads use less memory")
    a = ap.parse_args()
    files = sorted(glob.glob(a.trips, recursive=True))
    if not files:
        raise SystemExit(f"no files match {a.trips}")

    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")   # lets DuckDB stream instead of buffering
    if a.memory:
        con.execute(f"SET memory_limit = '{a.memory}'")
    if a.threads:
        con.execute(f"SET threads = {a.threads}")
    paths = ", ".join(f"'{f}'" for f in files)
    if all(f.endswith(".parquet") for f in files):      # made by prepare.py: much smaller on disk
        con.execute(f"CREATE VIEW trips AS SELECT * FROM read_parquet([{paths}], union_by_name = true)")
    else:
        con.execute(f"""CREATE VIEW trips AS SELECT * FROM read_csv([{paths}], union_by_name = true,
            types = {{'start_station_id': 'VARCHAR', 'end_station_id': 'VARCHAR'}})""")
    sa = station_areas(con)
    con.register("station_area_df", sa)
    con.execute("CREATE TABLE station_area AS SELECT * FROM station_area_df")
    con.execute(f"SET VARIABLE yr = {a.year}")
    con.execute((ROOT / "wrapped.sql").read_text())

    raw = con.execute(f"SELECT COUNT(*) FROM trips WHERE year(started_at) = {a.year}").fetchone()[0]
    tot = con.execute("SELECT * FROM w_totals").df().set_index("area")
    tot = tot[(tot.index != "Other") & (tot.rides >= 100)].sort_values("rides", ascending=False)
    tr = transit.run(con, sa, set(tot.index))        # None when the transit files aren't in data/
    gp = gaps.run(con, sa, set(tot.index))           # None without subway line information
    speed = con.execute("SELECT * FROM w_speed").df().set_index("area")
    fair = con.execute("SELECT * FROM w_fair").df().set_index("area")
    flows = con.execute("SELECT * FROM w_flows ORDER BY rides DESC").df()
    sflow = con.execute("SELECT * FROM w_station_flow").df()
    rhythm = con.execute("SELECT * FROM w_rhythm").df()
    n_days = con.execute("SELECT COUNT(DISTINCT CAST(started_at AS DATE)) FROM rides").fetchone()[0]
    stypes, sprofiles = station_types(con.execute("SELECT * FROM w_station_hourly").df(), min_rides=2 * n_days)
    months = con.execute("SELECT * FROM w_months").df()
    peaks = con.execute("SELECT * FROM w_peaks").df().set_index("area")
    top_st = con.execute("SELECT * FROM w_top_station").df().set_index("area")
    top_rt = con.execute("SELECT * FROM w_top_route").df().set_index("area")
    recs = con.execute("SELECT * FROM w_records").df()

    def ride(r):
        return {"from": r.start_station_name, "to": r.end_station_name,
                "date": r.started_at.strftime("%b %-d"), "minutes": round(float(r.minutes), 1),
                "km": round(float(r.km), 2), "kmh": round(float(r.kmh), 1),
                "ebike": r.rideable_type == "electric_bike"}

    areas = []
    for name, t in tot.iterrows():
        m = months[months.area == name].set_index("m").rides.reindex(range(1, 13), fill_value=0)
        p = peaks.loc[name]
        rec = {r.kind: ride(r) for r in recs[recs.area == name].itertuples()}
        areas.append({
            "name": name, "rides": int(t.rides), "stations": int(t.stations),
            "total_km": round(float(t.total_km)), "total_hours": round(float(t.total_hours)),
            "avg_km": round(float(t.avg_km), 2), "median_min": round(float(t.median_min), 1),
            "median_kmh": round(float(speed.median_kmh.get(name, float("nan"))), 1),
            "ebike_share": round(float(t.ebike_share), 4), "member_share": round(float(t.member_share), 4),
            "rides_per_station_day": round(float(fair.rides_per_station_day[name]), 2),
            "night_share": round(float(fair.night_share[name]), 4),
            "weekend_share": round(float(fair.weekend_share[name]), 4),
            "winter_ratio": round(float(fair.winter_ratio[name]), 4),
            "explorer_share": round(float(fair.explorer_share[name]), 4),
            "feeder_share": (tr or {}).get("areas", {}).get(name, {}).get("am_toward"),
            "shadow_index": gaps.shadow_for(gp, name),
            # 7 x 24 grid: rows Mon..Sun, columns 0..23h
            "rhythm": [[int(rhythm[(rhythm.area == name) & (rhythm.dow == dw) & (rhythm.hr == h)].rides.sum())
                        for h in range(24)] for dw in range(1, 8)],
            "within_rides": int(flows[(flows.from_area == name) & (flows.to_area == name)].rides.sum()),
            "cross_rides": int(flows[(flows.from_area == name) & (flows.to_area != name)].rides.sum()),
            "months": [int(x) for x in m], "top_month": MONTHS[int(m.idxmax()) - 1],
            "busiest_hour": int(p.busiest_hour),
            "busiest_day": pd.Timestamp(p.busiest_day).strftime("%a %b %-d"),
            "busiest_day_rides": int(p.busiest_day_rides),
            "top_station": top_st.top_station.get(name), "top_station_rides": int(top_st.top_station_rides.get(name, 0)),
            "top_route": top_rt.top_route.get(name), "top_route_rides": int(top_rt.top_route_rides.get(name, 0)),
            "records": rec,
        })

    # Every category is per station, per ride or a share, so a borough can't win just by being bigger.
    cats = [("rides_per_station_day", "Busiest docks", "Rides per station per day"),
            ("ebike_share", "Most electric", "Share of rides on an e-bike"),
            ("member_share", "Most loyal", "Share of rides by annual members"),
            ("weekend_share", "Weekend warriors", "Share of rides on Saturday or Sunday"),
            ("feeder_share", "Last-mile feeders", "Share of weekday-morning rides from a dock 1/4 mile or more from rail to one within 650 ft"),
            ("shadow_index", "Subway shadow", "Rides a single subway line could also have made, vs. random destinations")]
    categories = []
    import math
    for key, label, desc in cats:
        missing = lambda v: v is None or (isinstance(v, float) and math.isnan(v))
        have = [ar for ar in areas if not missing(ar[key])]
        if len(have) < 2 or (key not in ("feeder_share", "shadow_index") and len(have) < len(areas)):
            for ar in areas:          # e.g. no summer months in the data: skip the category
                ar[key] = None
            continue
        # last-mile feeders and gap fillers may leave out an area (e.g. Jersey City/Hoboken without the PATH file)
        ranked = sorted(have, key=lambda x: x[key], reverse=True)
        categories.append({"key": key, "label": label, "desc": desc, "winner": ranked[0]["name"],
                           "ranking": [{"name": r["name"], "value": r[key]} for r in ranked]})
    wins = {ar["name"]: sum(c["winner"] == ar["name"] for c in categories) for ar in areas}

    keep = {ar["name"] for ar in areas}
    flow_list = [{"from": r.from_area, "to": r.to_area, "rides": int(r.rides)}
                 for r in flows.itertuples() if r.from_area in keep]
    stations_out = [{"name": r.name, "area": r.area, "lat": round(float(r.lat), 5), "lng": round(float(r.lng), 5),
                     "net": round(float(r.net_day), 2), "am": round(float(r.am_net), 2), "pm": round(float(r.pm_net), 2),
                     "type": stypes.get(r.sid)}
                    for r in sflow.itertuples() if r.area in keep]
    # simplified borough outlines for the station map (small enough to embed)
    outlines = []
    for f in json.loads(BOROUGHS.read_text())["features"]:
        g = shape(f["geometry"]).simplify(0.0012, preserve_topology=True)
        polys = [g] if g.geom_type == "Polygon" else list(g.geoms)
        outlines += [[[round(x, 4), round(y, 4)] for x, y in p.exterior.coords] for p in polys if p.area > 2e-6]
    type_counts = {}
    for st_ in stations_out:
        if st_["type"]:
            type_counts.setdefault(st_["area"], {}).setdefault(st_["type"], 0)
            type_counts[st_["area"]][st_["type"]] += 1
    out = {"year": a.year, "flows": flow_list, "station_types": {"profiles": sprofiles, "by_area": type_counts}, "stations": stations_out, "transit": tr, "gaps": gp, "outlines": outlines, "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
           "files": len(files), "rides_in_files": int(raw),
           "rides_counted": int(tot.rides.sum()), "areas": areas, "categories": categories, "wins": wins}
    od = Path(a.out); od.mkdir(parents=True, exist_ok=True)
    (od / f"wrapped_{a.year}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    tpl = (ROOT / "template.html").read_text()
    head = '<!doctype html>\n<html lang="en"><meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    data = json.dumps(out, ensure_ascii=False, default=str).replace("</", "<\\/")
    (od / f"wrapped_{a.year}.html").write_text(head + tpl.replace("/*__DATA__*/null", data))
    print(f"{out['rides_counted']:,} of {raw:,} rides summarized across {len(areas)} areas -> {od}/wrapped_{a.year}.html")


if __name__ == "__main__":
    main()
