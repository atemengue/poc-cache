#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /db/schema.sql
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v rows="${SEED_ROWS:-250000}" <<'SQL'
INSERT INTO order_events(product_id,kind,created_at)
SELECT CASE WHEN n % 10 < 7 THEN 1 ELSE 2+(n % 99) END,
CASE WHEN n % 10 < 6 THEN 'view' WHEN n % 10 < 9 THEN 'favorite' ELSE 'purchase' END,
now() - (n % 7200) * interval '1 second'
FROM generate_series(1,:rows) n;
ANALYZE products;
ANALYZE order_events;
SQL
