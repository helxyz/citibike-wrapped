-- Last mile: rides and docks by distance to subway / PATH / light rail.
-- Inputs: rides (see wrapped.sql) and station_transit(station_id, transit_m, transit_kind, transit_name),
-- where transit_m is the straight-line distance from a dock to the nearest rail access point.
-- Only rides with a recorded start AND end dock are used (dockless endings have no dock to measure from).

CREATE OR REPLACE VIEW tr_rides AS
SELECT r.area, r.started_at, r.start_station_id AS sid, r.start_station_name AS sname,
       s.transit_m AS m1, e.transit_m AS m2, e.transit_name AS tname2,
       isodow(r.started_at) <= 5 AS weekday, hour(r.started_at) AS hr
FROM rides r
JOIN station_transit s ON s.station_id = r.start_station_id
JOIN station_transit e ON e.station_id = r.end_station_id;

-- One row per area. s = ride starts near, e = ride ends near, b = either end near, t = both ends near.
-- docks* = docks (that had a ride start) near transit. am/pm = weekday commute windows (6-10am, 4-8pm);
-- toward = from a dock beyond 400 m to a dock within 200 m, away = the reverse.
CREATE OR REPLACE TABLE tr_area AS
SELECT area, COUNT(*) AS rides,
       COUNT(*) FILTER (m1 <= 100) AS s100, COUNT(*) FILTER (m1 <= 200) AS s200, COUNT(*) FILTER (m1 <= 400) AS s400,
       COUNT(*) FILTER (m2 <= 100) AS e100, COUNT(*) FILTER (m2 <= 200) AS e200, COUNT(*) FILTER (m2 <= 400) AS e400,
       COUNT(*) FILTER (m1 <= 100 OR m2 <= 100) AS b100, COUNT(*) FILTER (m1 <= 200 OR m2 <= 200) AS b200,
       COUNT(*) FILTER (m1 <= 400 OR m2 <= 400) AS b400,
       COUNT(*) FILTER (m1 <= 100 AND m2 <= 100) AS t100, COUNT(*) FILTER (m1 <= 200 AND m2 <= 200) AS t200,
       COUNT(*) FILTER (m1 <= 400 AND m2 <= 400) AS t400,
       COUNT(DISTINCT sid) AS docks,
       COUNT(DISTINCT sid) FILTER (m1 <= 100) AS docks100, COUNT(DISTINCT sid) FILTER (m1 <= 200) AS docks200,
       COUNT(DISTINCT sid) FILTER (m1 <= 400) AS docks400,
       approx_quantile(m1, 0.5) AS median_start_m, approx_quantile(m2, 0.5) AS median_end_m,
       COUNT(DISTINCT CAST(started_at AS DATE)) FILTER (weekday) AS weekdays,
       COUNT(*) FILTER (weekday AND hr BETWEEN 6 AND 9) AS am,
       COUNT(*) FILTER (weekday AND hr BETWEEN 6 AND 9 AND m1 > 400 AND m2 <= 200) AS am_toward,
       COUNT(*) FILTER (weekday AND hr BETWEEN 6 AND 9 AND m1 <= 200 AND m2 > 400) AS am_away,
       COUNT(*) FILTER (weekday AND hr BETWEEN 16 AND 19) AS pm,
       COUNT(*) FILTER (weekday AND hr BETWEEN 16 AND 19 AND m1 > 400 AND m2 <= 200) AS pm_toward,
       COUNT(*) FILTER (weekday AND hr BETWEEN 16 AND 19 AND m1 <= 200 AND m2 > 400) AS pm_away
FROM tr_rides GROUP BY area;

-- Docks that feed transit in the morning (a dock name can change during the year: keep the latest): weekday 6-10am rides from far docks to docks near rail.
CREATE OR REPLACE TABLE tr_feeders AS
SELECT area, sid, arg_max(sname, started_at) AS name, ANY_VALUE(m1) AS m1,
       mode(tname2) AS to_transit, COUNT(*) AS rides
FROM tr_rides
WHERE weekday AND hr BETWEEN 6 AND 9 AND m1 > 400 AND m2 <= 200
GROUP BY area, sid;
