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
    con.register("station_area_df", station_areas(con))
    con.execute("CREATE TABLE station_area AS SELECT * FROM station_area_df")
    con.execute(f"SET VARIABLE yr = {a.year}")
    con.execute((ROOT / "wrapped.sql").read_text())

    raw = con.execute(f"SELECT COUNT(*) FROM trips WHERE year(started_at) = {a.year}").fetchone()[0]
    tot = con.execute("SELECT * FROM w_totals").df().set_index("area")
    tot = tot[(tot.index != "Other") & (tot.rides >= 100)].sort_values("rides", ascending=False)
    speed = con.execute("SELECT * FROM w_speed").df().set_index("area")
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
            "months": [int(x) for x in m], "top_month": MONTHS[int(m.idxmax()) - 1],
            "busiest_hour": int(p.busiest_hour),
            "busiest_day": pd.Timestamp(p.busiest_day).strftime("%a %b %-d"),
            "busiest_day_rides": int(p.busiest_day_rides),
            "top_station": top_st.top_station.get(name), "top_station_rides": int(top_st.top_station_rides.get(name, 0)),
            "top_route": top_rt.top_route.get(name), "top_route_rides": int(top_rt.top_route_rides.get(name, 0)),
            "records": rec,
        })

    cats = [("rides", "Most rides", True), ("total_km", "Most distance covered", True),
            ("total_hours", "Most hours in the saddle", True), ("avg_km", "Longest average ride", True),
            ("median_kmh", "Fastest typical rider", True), ("ebike_share", "Most electric", True)]
    categories = []
    for key, label, high in cats:
        ranked = sorted(areas, key=lambda x: x[key], reverse=high)
        categories.append({"key": key, "label": label, "winner": ranked[0]["name"],
                           "ranking": [{"name": r["name"], "value": r[key]} for r in ranked]})
    wins = {ar["name"]: sum(c["winner"] == ar["name"] for c in categories) for ar in areas}

    out = {"year": a.year, "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
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
