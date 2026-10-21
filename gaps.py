"""Filling the gaps: do Citi Bike riders favour trips the subway can't make in one seat?

For every New York City dock we list the subway lines with an entrance within 200, 400 or 600 m
(from the "Daytime Routes" column of data/subway_entrances.csv). A ride of 1 km or more whose two
docks both have a line nearby is "rail-served"; it is ONE-SEAT if the two ends share a line and a
GAP ride if they don't (a transfer would be needed).

Raw shares mislead: far-apart docks rarely share a line, so a borough of long rides would look
gap-heavy just from distance. So each borough's actual gap share is compared with an EXPECTED share:
the gap share of all dock pairs in the same start/end borough and distance band, i.e. what the dock
layout alone predicts. index = actual gap share / expected gap share; above 1 means riders pick
gap trips more often than the layout predicts.

Jersey City and Hoboken are left out: the PATH and light rail files have no line information.
"""
from pathlib import Path

import numpy as np
import pandas as pd

import transit

ROOT = Path(__file__).resolve().parent
RADII = (200, 400, 600)
HEADLINE_R = 400                     # about a five-minute walk
NYC = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]
BANDS = {1: "1-2 km", 2: "2-3 km", 3: "3-5 km", 4: "5+ km"}
SHUTTLES = {"M": "S 42 St", "B": "S Franklin Av", "Q": "S Rockaway Park"}   # three different shuttles share "S"
# Central Park, corner to corner (lat, lng): 59 St & 5 Av, 59 St & CPW, 110 St & CPW, 110 St & 5 Av.
# A dock within PARK_EDGE_KM of it is a "park edge" dock; trips between two of them are park loops.
PARKS = {"Central Park": [(40.7644, -73.9730), (40.7681, -73.9816), (40.8005, -73.9582), (40.7969, -73.9496)]}
PARK_EDGE_KM = 0.1
THETA = np.radians(29)               # Manhattan's street grid is rotated ~29 degrees from true north


def grid_uv(lat, lng):
    """Kilometre coordinates along Manhattan's grid: u crosstown, v uptown."""
    x = np.asarray(lng, float) * np.cos(np.radians(40.75)) * 111.32
    y = np.asarray(lat, float) * 110.57
    return x * np.cos(THETA) - y * np.sin(THETA), x * np.sin(THETA) + y * np.cos(THETA)


def load_lines(data_dir: Path) -> pd.DataFrame | None:
    """Subway entrances as lat, lng, lines (a set of route names). None without route information."""
    path = transit._find(data_dir, "subway")
    if path is None:
        return None
    s = pd.read_csv(path)
    rcol, bcol = transit._col(s, "daytimeroutes", "routes"), transit._col(s, "borough")
    pts = transit._subway(path)
    if rcol is None:
        print("gaps: the subway file has no 'Daytime Routes' column; skipping the gap section")
        return None

    def parse(routes, boro):
        out = set()
        for t in str(routes).split():
            out.add(SHUTTLES.get(str(boro), "S") if t == "S" else t)
        return frozenset(out)
    pts["lines"] = [parse(r, b) if pd.notna(r) else frozenset()
                    for r, b in zip(s[rcol], s[bcol] if bcol else [None] * len(s))]
    ok = pts.lat.between(transit.BBOX[0], transit.BBOX[1]) & pts.lng.between(transit.BBOX[2], transit.BBOX[3])
    return pts[ok & (pts.lines.map(len) > 0)].reset_index(drop=True)


def station_lines(docks: pd.DataFrame, ent: pd.DataFrame):
    """Per dock: bitmask of lines within each radius, plus grid coordinates. Returns (table, line names)."""
    names = sorted(set().union(*ent.lines))
    if len(names) > 62:
        raise SystemExit("gaps: more than 62 subway lines; the bitmask needs widening")
    bit = {n: 1 << i for i, n in enumerate(names)}
    emask = np.array([sum(bit[l] for l in ls) for ls in ent.lines], dtype=np.int64)
    out = {f"m{r}": np.zeros(len(docks), dtype=np.int64) for r in RADII}
    la, lo = docks.lat.to_numpy(float), docks.lng.to_numpy(float)
    for i in range(0, len(docks), 256):                       # chunks keep memory small
        d = transit._haversine_m(la[i:i + 256, None], lo[i:i + 256, None],
                                 ent.lat.to_numpy(float)[None, :], ent.lng.to_numpy(float)[None, :])
        for r in RADII:
            near = d <= r
            out[f"m{r}"][i:i + 256] = [np.bitwise_or.reduce(emask[row]) if row.any() else 0 for row in near]
    u, v = grid_uv(la, lo)
    t = pd.DataFrame({"station_id": docks.station_id.to_numpy(), "area": docks.area.to_numpy(), **out, "u": u, "v": v})
    return t, names


def park_edge_docks(docks: pd.DataFrame) -> set:
    """Station ids of docks on the edge of a park listed in PARKS."""
    from shapely.geometry import Point, Polygon
    shapes = []
    for corners in PARKS.values():
        u, v = grid_uv([c[0] for c in corners], [c[1] for c in corners])
        shapes.append(Polygon(list(zip(u, v))).buffer(PARK_EDGE_KM))
    u, v = grid_uv(docks.lat, docks.lng)
    return {sid for sid, x, y in zip(docks.station_id, u, v) if any(s.contains(Point(x, y)) for s in shapes)}


def baseline(t: pd.DataFrame, areas: list) -> pd.DataFrame:
    """All ordered pairs of distinct docks >= 1 km apart, counted per (start area, end area, band, dir):
    how many have a line near both ends (bR) and how many of those share a line (oR)."""
    t = t[t.area.isin(areas)].reset_index(drop=True)
    ai = {a: i for i, a in enumerate(areas)}
    A = t.area.map(ai).to_numpy()
    u, v = t.u.to_numpy(), t.v.to_numpy()
    xy = np.c_[u, v]
    nA = len(areas)
    nC = 4 * 4 * nA * nA
    acc = {k: np.zeros(nC) for k in ["n"] + [f"{p}{r}" for r in RADII for p in "bo"]}
    man = ai.get("Manhattan", -1)
    for i in range(0, len(t), 256):
        sl = slice(i, i + 256)
        km = np.hypot(xy[sl, None, 0] - xy[None, :, 0], xy[sl, None, 1] - xy[None, :, 1])   # grid km == plane km
        keep = km >= 1
        band = np.where(km < 2, 1, np.where(km < 3, 2, np.where(km < 5, 3, 4)))
        du, dv = np.abs(u[sl, None] - u[None, :]), np.abs(v[sl, None] - v[None, :])
        both_man = (A[sl, None] == man) & (A[None, :] == man)
        dr = np.where(~both_man, 0, np.where(du > 2 * dv, 1, np.where(dv > 2 * du, 2, 3)))
        cell = ((A[sl, None] * nA + A[None, :]) * 4 + (band - 1)) * 4 + dr
        c = cell[keep]
        acc["n"] += np.bincount(c, minlength=nC)
        for r in RADII:
            m = t[f"m{r}"].to_numpy()
            ma, mb = m[sl, None], m[None, :]
            both = ((ma != 0) & (mb != 0))[keep]
            one = ((ma & mb) != 0)[keep]
            acc[f"b{r}"] += np.bincount(c, weights=both, minlength=nC)
            acc[f"o{r}"] += np.bincount(c, weights=one, minlength=nC)
    idx = np.arange(nC)
    df = pd.DataFrame({"area": [areas[k // (16 * nA)] for k in idx],
                       "end_area": [areas[(k // 16) % nA] for k in idx],
                       "band": (idx // 4) % 4 + 1, "dir": idx % 4, **acc})
    return df[df.n > 0]


def index_for(result: dict | None, area: str):
    """The headline gap index for one area, or None (no data, or the area isn't covered)."""
    d = ((result or {}).get("areas", {}).get(area) or {}).get(str(HEADLINE_R))
    return d["index"] if d else None


def shadow_for(result: dict | None, area: str):
    """One-seat share / random-baseline one-seat share for one area: above 1 = trips follow single
    subway lines more often than chance. None when there is no data for the area."""
    d = ((result or {}).get("areas", {}).get(area) or {}).get(str(HEADLINE_R))
    if not d or d.get("expected_gap") is None or d["expected_gap"] >= 1:
        return None
    return round((1 - d["gap"]) / (1 - d["expected_gap"]), 3)


def run(con, station_area: pd.DataFrame, keep: set, data_dir: Path | None = None) -> dict | None:
    data_dir = data_dir or ROOT / "data"
    ent = load_lines(data_dir)
    if ent is None:
        return None
    areas = [a for a in NYC if a in keep]
    if not areas:
        return None
    docks = station_area.dropna(subset=["lat", "lng"])
    t, names = station_lines(docks, ent)
    t_nyc = t[t.area.isin(areas)]
    con.register("station_lines_df", t_nyc[["station_id", "m200", "m400", "m600", "u", "v"]])
    con.execute("CREATE OR REPLACE TABLE station_lines AS SELECT * FROM station_lines_df")
    con.execute((ROOT / "gaps.sql").read_text())
    cells = con.execute("SELECT * FROM gp_cells").df()
    cells = cells[cells.area.isin(areas) & cells.end_area.isin(areas)]
    base = baseline(t_nyc, areas)
    key = ["area", "end_area", "band", "dir"]
    m = cells.merge(base, on=key, how="left", suffixes=("", "_base")).fillna(0)

    def summarize(g):
        """Actual vs expected for a group of cells, at each radius."""
        out = {}
        for r in RADII:
            served = g[f"b{r}"].sum()
            if served == 0:
                out[str(r)] = None
                continue
            bb, ob = g[f"b{r}_base"].to_numpy(float), g[f"o{r}_base"].to_numpy(float)
            rate = np.divide(ob, bb, out=np.full(len(bb), np.nan), where=bb > 0)   # layout's one-seat rate per cell
            w = g[f"b{r}"].to_numpy(float)
            ok = ~np.isnan(rate)
            exp_one = float((w[ok] * rate[ok]).sum() / w[ok].sum()) if w[ok].sum() else None
            act_one = float(g[f"o{r}"].sum() / served)
            gap, egap = 1 - act_one, (1 - exp_one) if exp_one is not None else None
            out[str(r)] = {"rides": int(served), "served_share": round(float(served / g.n.sum()), 4),
                           "gap": round(gap, 4), "expected_gap": round(egap, 4) if egap is not None else None,
                           "index": round(gap / egap, 3) if egap else None}
        return out

    by_area = {a: summarize(g) for a, g in m.groupby("area")}
    overall = summarize(m)
    mm = m[(m.area == "Manhattan") & (m.end_area == "Manhattan")]
    manhattan = {"crosstown": summarize(mm[mm.dir == 1]), "updown": summarize(mm[mm.dir == 2])} if len(mm) else None

    n_days = con.execute("SELECT COUNT(DISTINCT CAST(started_at AS DATE)) FROM rides").fetchone()[0]
    pairs = con.execute("SELECT * FROM gp_pairs").df()
    pairs = pairs[pairs.area1.isin(areas) & pairs.area2.isin(areas)]
    edge = park_edge_docks(docks)              # loops around Central Park are leisure, not transport gaps
    pairs = pairs[~(pairs.p1.isin(edge) & pairs.p2.isin(edge))].head(10)
    lines_of = dict(zip(t.station_id, t[f"m{HEADLINE_R}"]))
    def decode(mask):
        return [n for i, n in enumerate(names) if int(mask) >> i & 1]
    top = [{"a": r.name1, "b": r.name2, "area_a": r.area1, "area_b": r.area2, "km": round(float(r.km), 2),
            "per_day": round(r.rides / max(n_days, 1), 1),
            "lines_a": decode(lines_of.get(r.p1, 0)), "lines_b": decode(lines_of.get(r.p2, 0))}
           for r in pairs.itertuples()]
    print(f"gaps: {len(names)} subway lines; {overall[str(HEADLINE_R)]['rides'] if overall[str(HEADLINE_R)] else 0:,} "
          f"rail-served rides at {HEADLINE_R} m")
    return {"radii": list(RADII), "r": HEADLINE_R, "min_km": 1, "areas": by_area, "overall": overall,
            "manhattan": manhattan, "pairs": top, "lines": names}
