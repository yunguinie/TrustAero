BEGIN;
SET LOCAL jit = off;
SET LOCAL ta.tenant = '3';
SET LOCAL ta.purpose = 'research';
SET LOCAL ta.can_view_sensitive = 'false';
SELECT count(*) AS n, sum(e.region_id) AS region_sum,
       round(avg(e.magnitude)::numeric, 6) AS magnitude_mean,
       count(e.sensitive_value) AS visible_sensitive
FROM governed_events e JOIN regions r USING (region_id)
WHERE e.event_time >= DATE '2024-03-01'
  AND e.event_time < DATE '2024-10-01'
  AND e.magnitude >= 3.0;
COMMIT;
