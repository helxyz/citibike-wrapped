-- "Citi Bike Wrapped": one year of rides, summarized per area (borough).
-- Inputs: trips (raw trip CSVs), station_area(station_id, area, lat, lng).
-- Area = where the ride STARTED ("bikes that left a dock in the borough").
-- Distances are straight lines between stations (trip data has no route).

CREATE OR REPLACE VIEW rides AS
WITH base AS (
    SELECT t.rideable_type, t.member_casual, t.started_at,
           t.start_station_id, t.start_station_name, t.end_station_id, t.end_station_name,
           epoch(t.ended_at - t.started_at) / 60.0              AS minutes,
           COALESCE(s.lat, t.start_lat)                         AS lat1,
           COALESCE(s.lng, t.start_lng)                         AS lng1,
           COALESCE(e.lat, t.end_lat)                           AS lat2,
           COALESCE(e.lng, t.end_lng)                           AS lng2,
           COALESCE(s.area, 'Other')                            AS area
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
           {'start_station_name': start_station_name, 'end_station_name': end_station_name,
            'started_at': started_at, 'minutes': minutes, 'km': km, 'kmh': kmh,
            'rideable_type': rideable_type} AS ride
    FROM rides_speed
    WHERE km >= 0.05                      -- at least 50 m apart: rules out co-located docks
)
SELECT area,
       arg_max(ride, km)                                                    AS farthest,
       arg_min(ride, km)                                                    AS shortest,
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
