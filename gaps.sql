-- Filling the gaps: does a ride's start and end share a subway line?
-- Inputs: rides (wrapped.sql) and station_lines(station_id, m200, m400, m600, u, v), where mR is a bitmask of
-- the subway lines with an entrance within R metres of the dock, and u/v are Manhattan-grid coordinates in km
-- (u runs crosstown, v runs uptown). Only A-to-B rides of at least 1 km between two New York City docks count.

CREATE OR REPLACE VIEW gp_rides AS
SELECT r.area, r.end_area, r.started_at, r.start_station_id AS s1, r.end_station_id AS s2,
       r.start_station_name AS n1, r.end_station_name AS n2, r.km,
       CASE WHEN r.km < 2 THEN 1 WHEN r.km < 3 THEN 2 WHEN r.km < 5 THEN 3 ELSE 4 END AS band,
       CASE WHEN r.area <> 'Manhattan' OR r.end_area <> 'Manhattan' THEN 0
            WHEN abs(a.u - b.u) > 2 * abs(a.v - b.v) THEN 1      -- crosstown
            WHEN abs(a.v - b.v) > 2 * abs(a.u - b.u) THEN 2      -- uptown / downtown
            ELSE 3 END AS dir,
       a.m200 AS a200, b.m200 AS b200, a.m400 AS a400, b.m400 AS b400, a.m600 AS a600, b.m600 AS b600
FROM rides r
JOIN station_lines a ON a.station_id = r.start_station_id
JOIN station_lines b ON b.station_id = r.end_station_id
WHERE r.is_a_to_b AND r.km >= 1;

-- One row per (start area, end area, distance band, Manhattan direction):
-- n = all such rides; bR = rides with a subway line near both ends; oR = of those, rides whose ends share a line.
CREATE OR REPLACE TABLE gp_cells AS
SELECT area, end_area, band, dir, COUNT(*) AS n,
       COUNT(*) FILTER (a200 <> 0 AND b200 <> 0) AS b200, COUNT(*) FILTER ((a200 & b200) <> 0) AS o200,
       COUNT(*) FILTER (a400 <> 0 AND b400 <> 0) AS b400, COUNT(*) FILTER ((a400 & b400) <> 0) AS o400,
       COUNT(*) FILTER (a600 <> 0 AND b600 <> 0) AS b600, COUNT(*) FILTER ((a600 & b600) <> 0) AS o600
FROM gp_rides GROUP BY ALL;

-- Busiest gap pairs at 400 m: subway near both ends, no shared line. Both directions counted together.
CREATE OR REPLACE TABLE gp_pairs AS
SELECT least(s1, s2) AS p1, greatest(s1, s2) AS p2,
       arg_max(CASE WHEN s1 < s2 THEN n1 ELSE n2 END, started_at) AS name1,
       arg_max(CASE WHEN s1 < s2 THEN n2 ELSE n1 END, started_at) AS name2,
       arg_max(CASE WHEN s1 < s2 THEN area ELSE end_area END, started_at) AS area1,
       arg_max(CASE WHEN s1 < s2 THEN end_area ELSE area END, started_at) AS area2,
       ANY_VALUE(km) AS km, COUNT(*) AS rides
FROM gp_rides
WHERE a400 <> 0 AND b400 <> 0 AND (a400 & b400) = 0
GROUP BY 1, 2
ORDER BY rides DESC, p1, p2
LIMIT 200;
