-- "Citi Bike Wrapped": one year of rides, summarized per area (borough).
-- Inputs: trips (raw trip CSVs), station_area(station_id, area, lat, lng).
-- Area = where the ride STARTED ("bikes that left a dock in the borough").
-- Distances are straight lines between stations (trip data has no route).

CREATE OR REPLACE VIEW rides AS
WITH base AS (
    SELECT t.rideable_type, t.member_casual, t.started_at, t.ended_at,
           t.start_station_id, t.start_station_name, t.end_station_id, t.end_station_name,
           epoch(t.ended_at - t.started_at) / 60.0              AS minutes,
           COALESCE(s.lat, t.start_lat)                         AS lat1,
           COALESCE(s.lng, t.start_lng)                         AS lng1,
           COALESCE(e.lat, t.end_lat)                           AS lat2,
           COALESCE(e.lng, t.end_lng)                           AS lng2,
           COALESCE(s.area, 'Other')                            AS area,
           e.area                                               AS end_area
    FROM trips t
    LEFT JOIN station_area s ON s.station_id = t.start_station_id
    LEFT JOIN station_area e ON e.station_id = t.end_station_id
    WHERE year(t.started_at) = getvariable('yr')
)
SELECT *,
       -- haversine distance in km
       2 * 6371 * asin(sqrt(pow(sin(radians(lat2 - lat1) / 2), 2)
             + cos(radians(lat1)) * cos(radians(lat2)) * pow(sin(radians(lng2 - lng1) / 2), 2))) AS km,
       start_station_id IS NOT NULL AND end_station_id IS NOT NULL
             AND start_station_id <> end_station_id                  AS is_a_to_b
FROM base
WHERE minutes BETWEEN 1 AND 1440          -- drop clock errors and lost bikes (>24h)
  AND lat2 IS NOT NULL;                   -- drop rides with no recorded end

CREATE OR REPLACE VIEW rides_speed AS
SELECT *, km / (minutes / 60.0) AS kmh FROM rides WHERE is_a_to_b;

-- 1) Headline totals per area ---------------------------------------------------
CREATE OR REPLACE TABLE w_totals AS
SELECT area,
       COUNT(*)                                                      AS rides,
       SUM(km)                                                       AS total_km,
       SUM(minutes) / 60.0                                           AS total_hours,
       AVG(km) FILTER (is_a_to_b)                                    AS avg_km,
       approx_quantile(minutes, 0.5)                                 AS median_min,
       AVG(CASE WHEN rideable_type = 'electric_bike' THEN 1 ELSE 0 END) AS ebike_share,
       AVG(CASE WHEN member_casual = 'member' THEN 1 ELSE 0 END)     AS member_share,
       COUNT(DISTINCT start_station_id)                              AS stations
FROM rides GROUP BY area;

CREATE OR REPLACE TABLE w_speed AS
SELECT area, approx_quantile(kmh, 0.5) AS median_kmh FROM rides_speed
WHERE minutes >= 3 GROUP BY area;

-- 1b) Fair fights: per-station, per-ride and share metrics, so size alone can't win
CREATE OR REPLACE TABLE w_fair AS
WITH d AS (SELECT COUNT(DISTINCT CAST(started_at AS DATE)) AS n_days FROM rides)
SELECT area,
       COUNT(*) * 1.0 / COUNT(DISTINCT start_station_id) / ANY_VALUE(d.n_days)       AS rides_per_station_day,
       AVG(CASE WHEN hour(started_at) >= 22 OR hour(started_at) < 4 THEN 1 ELSE 0 END) AS night_share,
       AVG(CASE WHEN isodow(started_at) >= 6 THEN 1 ELSE 0 END)                       AS weekend_share,
       -- winter rides (Dec-Feb) as a share of summer rides (Jun-Aug): who keeps riding in the cold
       COUNT(*) FILTER (month(started_at) IN (12, 1, 2)) * 1.0
         / NULLIF(COUNT(*) FILTER (month(started_at) IN (6, 7, 8)), 0)              AS winter_ratio,
       -- rides that finish in a different area from where they started
       AVG(CASE WHEN end_area <> area THEN 1 ELSE 0 END) FILTER (end_area IS NOT NULL) AS explorer_share
FROM rides, d
GROUP BY area;

-- 1c) Where rides went: start area -> end area (trans-borough vs within-borough)
CREATE OR REPLACE TABLE w_flows AS
SELECT area AS from_area, end_area AS to_area, COUNT(*) AS rides
FROM rides
WHERE end_area IS NOT NULL AND area <> 'Other' AND end_area <> 'Other'
GROUP BY ALL;

-- 1d) Station imbalance: departures minus arrivals, overall and in weekday rush hours
CREATE OR REPLACE TABLE w_station_flow AS
WITH d AS (
    SELECT COUNT(DISTINCT CAST(started_at AS DATE))                             AS n_days,
           COUNT(DISTINCT CAST(started_at AS DATE)) FILTER (isodow(started_at) <= 5) AS n_weekdays
    FROM rides
),
dep AS (
    SELECT start_station_id AS sid, COUNT(*) AS dep,
           COUNT(*) FILTER (isodow(started_at) <= 5 AND hour(started_at) BETWEEN 7 AND 9)   AS dep_am,
           COUNT(*) FILTER (isodow(started_at) <= 5 AND hour(started_at) BETWEEN 16 AND 18) AS dep_pm,
           arg_max(start_station_name, started_at)                                          AS name
    FROM rides WHERE start_station_id IS NOT NULL GROUP BY 1
),
arr AS (
    SELECT end_station_id AS sid, COUNT(*) AS arr,
           COUNT(*) FILTER (isodow(ended_at) <= 5 AND hour(ended_at) BETWEEN 7 AND 9)   AS arr_am,
           COUNT(*) FILTER (isodow(ended_at) <= 5 AND hour(ended_at) BETWEEN 16 AND 18) AS arr_pm
    FROM rides WHERE end_station_id IS NOT NULL GROUP BY 1
)
SELECT dep.sid, dep.name, s.area, s.lat, s.lng,
       dep.dep, COALESCE(arr.arr, 0) AS arr,
       (dep.dep - COALESCE(arr.arr, 0)) * 1.0 / d.n_days                  AS net_day,
       (dep.dep_am - COALESCE(arr.arr_am, 0)) * 1.0 / d.n_weekdays        AS am_net,   -- + = drains in the morning
       (dep.dep_pm - COALESCE(arr.arr_pm, 0)) * 1.0 / d.n_weekdays        AS pm_net
FROM dep LEFT JOIN arr USING (sid) JOIN station_area s ON s.station_id = dep.sid, d
WHERE dep.dep + COALESCE(arr.arr, 0) >= d.n_days;                       -- at least ~1 ride a day

-- 1d2) Departures per station by hour, split weekday / weekend (input to the station-type clustering)
CREATE OR REPLACE TABLE w_station_hourly AS
SELECT start_station_id AS sid, isodow(started_at) >= 6 AS is_weekend, hour(started_at) AS hr, COUNT(*) AS n
FROM rides WHERE start_station_id IS NOT NULL GROUP BY ALL;

-- 1e) Weekly rhythm: rides by day of week x hour, per area
CREATE OR REPLACE TABLE w_rhythm AS
SELECT area, isodow(started_at) AS dow, hour(started_at) AS hr, COUNT(*) AS rides
FROM rides GROUP BY ALL;

-- 2) When: rides by month, busiest hour and busiest day ---------------------------
CREATE OR REPLACE TABLE w_months AS
SELECT area, month(started_at) AS m, COUNT(*) AS rides FROM rides GROUP BY ALL;

CREATE OR REPLACE TABLE w_peaks AS
SELECT area,
       arg_max(hr, n_hr)  AS busiest_hour,
       ANY_VALUE(busiest_day) AS busiest_day, ANY_VALUE(busiest_day_rides) AS busiest_day_rides
FROM (SELECT area, hour(started_at) AS hr, COUNT(*) AS n_hr FROM rides GROUP BY ALL) h
JOIN (SELECT area, arg_max(d, n) AS busiest_day, max(n) AS busiest_day_rides
      FROM (SELECT area, CAST(started_at AS DATE) AS d, COUNT(*) AS n FROM rides GROUP BY ALL)
      GROUP BY area) d USING (area)
GROUP BY area;

-- 3) Favourites: top station and top A->B route ----------------------------------
CREATE OR REPLACE TABLE w_top_station AS
SELECT area, arg_max(start_station_name, n) AS top_station, max(n) AS top_station_rides
FROM (SELECT area, start_station_name, COUNT(*) AS n FROM rides
      WHERE start_station_name IS NOT NULL GROUP BY ALL)
GROUP BY area;

CREATE OR REPLACE TABLE w_top_route AS
SELECT area, arg_max(route, n) AS top_route, max(n) AS top_route_rides
FROM (SELECT area, start_station_name || ' → ' || end_station_name AS route, COUNT(*) AS n
      FROM rides WHERE is_a_to_b GROUP BY ALL)
GROUP BY area;

-- 4) Record-breakers: one ride each, found in a single pass (arg_max / arg_min)
CREATE OR REPLACE TABLE w_records_wide AS
WITH r AS (
    SELECT area, km, kmh, minutes,
           -- same corner written two ways ("Broadway & W 37 St" vs "W 37 St & Broadway"): a re-dock, not a ride
           list_sort(string_split(lower(trim(start_station_name)), ' & '))
             = list_sort(string_split(lower(trim(end_station_name)), ' & ')) AS same_corner,
           {'start_station_name': start_station_name, 'end_station_name': end_station_name,
            'started_at': started_at, 'minutes': minutes, 'km': km, 'kmh': kmh,
            'rideable_type': rideable_type} AS ride
    FROM rides_speed
    WHERE km >= 0.05                      -- at least 50 m apart: rules out co-located docks
)
SELECT area,
       arg_max(ride, km)                                                    AS farthest,
       arg_min(ride, km) FILTER (NOT coalesce(same_corner, false))          AS shortest,
       -- fastest: straight-line average speed, ignoring clock/GPS glitches
       arg_max(ride, kmh) FILTER (minutes >= 5 AND km >= 1 AND kmh <= 32)   AS fastest,
       -- slowest: a real ride of 1 km+ that took the scenic route (under 6 hours)
       arg_min(ride, kmh) FILTER (km >= 1 AND minutes <= 360)               AS slowest
FROM r GROUP BY area;

CREATE OR REPLACE TABLE w_records AS
SELECT area, kind, rec.start_station_name, rec.end_station_name, rec.started_at,
       rec.minutes, rec.km, rec.kmh, rec.rideable_type
FROM (
    SELECT area, 'farthest' AS kind, farthest AS rec FROM w_records_wide
    UNION ALL SELECT area, 'shortest', shortest FROM w_records_wide
    UNION ALL SELECT area, 'fastest',  fastest  FROM w_records_wide
    UNION ALL SELECT area, 'slowest',  slowest  FROM w_records_wide
) WHERE rec IS NOT NULL;
