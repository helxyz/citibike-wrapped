# Citi Bike Wrapped

A Spotify-Wrapped-style recap of a year of Citi Bike rides, with the boroughs competing
head to head. Built from Citi Bike's public [trip histories](https://citibikenyc.com/system-data).

**Live page:** _<add link>_

## What it shows, per borough (plus Jersey City and Hoboken)

- Total rides, distance covered, hours ridden and e-bike share
- Rides per month, with the busiest month highlighted
- Record rides: farthest, shortest hop, fastest and slowest, with their stations
- Local favourites: top station, top route, busiest day, rush hour, member share and typical speed
- Weekly rhythm: a day-of-week x hour heat map for each borough
- Morning drain, evening fill: a station map of net bikes leaving or arriving in weekday rush hours
  (rebalancing demand), with the biggest drainers and fillers
- Homes, hubs and all-day stations: stations clustered (k-means, numpy) by the hour-of-day shape of
  their weekday departures plus weekend share; shown as average profiles, a mix per borough and a map view
- Crossing the line: a heat map of where rides started (rows) and ended (columns), so trans-borough
  rides (e.g. Brooklyn → Manhattan) can be compared with rides that stayed in their borough
- **Head to head:** nine fair-fight categories, so the biggest borough can't win on size alone:
  busiest docks (rides per station per day), longest average ride, fastest typical rider, most
  electric, most loyal (member share), night owls, weekend warriors, winter warriors (Dec–Feb rides
  as a share of Jun–Aug) and boldest explorers (rides ending in another area)

## How it is counted

- A ride belongs to the borough where it **started**. Stations are placed in boroughs by a
  point-in-polygon test against NYC borough boundaries (`data/borough_boundaries.geojson`,
  NYC Department of City Planning via [dwillis/nyc-maps](https://github.com/dwillis/nyc-maps)).
  Station IDs starting `JC` or `HB` are Jersey City and Hoboken.
- Distances and speeds are shown in miles and mph (computed in km, converted for display).
- **Distances are straight lines** between start and end stations, because trip data has no
  route. Real rides are longer.
- Rides under 1 minute or over 24 hours, and rides with no recorded end, are excluded.
- "Fastest" ignores rides under 5 minutes or 1 km and speeds above 32 km/h (clock or GPS glitches).
  "Slowest" is the slowest ride of at least 1 km that finished within 6 hours.

## Run it

```bash
pip install -r requirements.txt
# unzip a year of trip files somewhere outside this folder, e.g. ~/Downloads/citibike-2025/
python wrapped.py --year 2025 --trips "$HOME/Downloads/citibike-2025/**/*.csv"
open output/wrapped_2025.html
```

A full NYC year (~45M rides) takes a few minutes on a laptop with DuckDB.

**Short on disk space?** Unzipped CSVs for a year take ~10 GB. `prepare.py` converts the
downloaded zips to compressed Parquet one file at a time (~1–1.5 GB for the year), and can
delete each zip as it goes:

```bash
python prepare.py --zips ~/Downloads/citibike-2025 --out ~/Downloads/citibike-2025-parquet --delete-zips
python wrapped.py --year 2025 --trips "$HOME/Downloads/citibike-2025-parquet/*.parquet"
```

Memory use is small: the statistics stream over the files in one pass (records use
`arg_max`/`arg_min` rather than sorting), so a 40M-ride year runs in under a minute with about
1.2 GB of RAM. Add `--memory 4GB --threads 4` on a small laptop. To run it on a Slurm cluster, use `run_cluster.sh`.

## Files

```
run_cluster.sh    Slurm job script for running on a computing cluster
prepare.py        shrinks downloaded zips to Parquet, one file at a time
wrapped.py        loads trip CSVs or Parquet into DuckDB, assigns boroughs, writes JSON + HTML
wrapped.sql       all the statistics (window functions, arg_max, haversine distance)
template.html     the page; the script fills in the data
data/             borough boundaries
output/           generated pages
```

Independent analysis; not affiliated with Lyft or Citi Bike.
