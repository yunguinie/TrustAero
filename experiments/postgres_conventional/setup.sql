\set ON_ERROR_STOP on
DROP VIEW IF EXISTS governed_events;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS regions;
DROP ROLE IF EXISTS ta_direct;
DROP ROLE IF EXISTS ta_rls;

CREATE ROLE ta_direct LOGIN PASSWORD 'benchmark-only' BYPASSRLS;
CREATE ROLE ta_rls LOGIN PASSWORD 'benchmark-only';

CREATE TABLE regions (
  region_id integer PRIMARY KEY,
  region_name text NOT NULL
);
INSERT INTO regions
SELECT i, 'region-' || i FROM generate_series(0, 99) AS i;

CREATE TABLE events (
  event_id bigint PRIMARY KEY,
  tenant_id integer NOT NULL,
  region_id integer NOT NULL REFERENCES regions(region_id),
  event_time date NOT NULL,
  magnitude double precision NOT NULL,
  sensitive_value text NOT NULL
);
INSERT INTO events
SELECT
  i,
  (i % 10)::integer,
  (i % 100)::integer,
  DATE '2024-01-01' + ((i * 17) % 366)::integer,
  1.0 + ((i * 37) % 600)::double precision / 100.0,
  'secret-' || i
FROM generate_series(1, 1000000) AS i;

CREATE INDEX events_tenant_time_idx ON events(tenant_id, event_time);
CREATE INDEX events_region_idx ON events(region_id);
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE events FORCE ROW LEVEL SECURITY;
CREATE POLICY governed_access ON events
  FOR SELECT TO ta_rls
  USING (
    tenant_id = current_setting('ta.tenant', true)::integer
    AND current_setting('ta.purpose', true) = 'research'
  );

CREATE VIEW governed_events WITH (security_barrier = true, security_invoker = true) AS
SELECT
  event_id,
  tenant_id,
  region_id,
  event_time,
  magnitude,
  CASE
    WHEN current_setting('ta.can_view_sensitive', true) = 'true' THEN sensitive_value
    ELSE NULL
  END AS sensitive_value
FROM events;

GRANT SELECT ON events, regions TO ta_direct;
GRANT SELECT ON events, regions, governed_events TO ta_rls;
ANALYZE events;
ANALYZE regions;
