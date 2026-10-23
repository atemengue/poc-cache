# Live-class script (45–60 minutes)

**Prerequisite:** Run `./poc up --replicas 2` ahead of class; verify `./poc check` and allow data seeding/building to finish. Have terminals, API docs, `app/src/cache.rs`, `app/src/storage.rs` and Grafana open. For short runs try `--rate 40` if the laptop has limited resources.

## Scene 1 — The problem (5 min)

**Say:** “A flash-sale laptop attracts thousands of shoppers. They repeatedly refresh the product page to see demand and availability. Should each viewer ask the database to re-count hundreds of thousands of interactions? We are comparing architectural mechanisms, not asserting that Redis is always the answer.”

**Screen:** `http://localhost:8080/products/1/snapshot`, then `db/schema.sql` and `app/src/storage.rs` (`Store::snapshot`). Highlight the aggregation, indexed `product_id`, and the hot product 1.

**Commands:**

```bash
curl -s http://localhost:8080/products/1/snapshot
curl -s http://localhost:8080/products/1/revision
```

**Question:** “Which fields could safely be seconds old? What if this were remaining seats instead?”

## Scene 2 — First hit vs cache hit (5 min)

**Say:** “We turn the aggregation response into a reusable object. This is still a distributed cache because two independently deployed application instances share Redis.”

**Screen:** `app/src/cache.rs`, `GET /products/{id}/snapshot` handler, Grafana cache-hit panel.

**Commands:**

```bash
docker compose exec -T redis1 redis-cli FLUSHDB
curl -si http://localhost:8080/products/1/snapshot | grep -Ei 'HTTP/|X-Cache-Source'
curl -si http://localhost:8080/products/1/snapshot | grep -Ei 'HTTP/|X-Cache-Source'
docker compose exec -T redis1 redis-cli KEYS 'snapshot:*'
```

**Point:** The cached value is a complete aggregated payload, not the raw SQL result set or the source of truth. Show `X-App-Instance` in the headers to identify which app replica answered. Use `E17_l1l2_consistency.json` to inspect both replica-local revisions after a write.

## Scene 3 — Measured performance (10 min)

**Screen:** Terminal running k6 and Grafana dashboard; show `REPORT.md` only after generation.

**Commands:**

```bash
./poc campaign --profile smoke --skip-start --rate 100 --duration 12
```

This performs E01 DB baseline, E02 cold, E03 warm, E12 flush without/with protection, E17 consistency, E18 L1/L2, E10 Redis outage. The script recreates app containers and may occupy several minutes; for a strict live-time limit, run it before the session and use the generated REPORT.md in class.

**Say:** “Offered throughput is not achieved throughput. Compare read p95, actual RPS, dropped iterations, and errors. If the load generator cannot meet its target, the fastest latency alone does not establish scalability.”

## Scene 4 — Cache consistency in 3 API calls (7 min)

**Screen:** `GET /products/1/snapshot`, `GET /products/1/revision`, `POST /products/1/activity`, show version mismatch.

```bash
# Deliberately stale cache-aside mode
docker compose exec -T redis1 redis-cli FLUSHDB
curl -s http://localhost:8080/products/1/snapshot
curl -s -X POST http://localhost:8080/products/1/activity \
 -H 'Content-Type: application/json' -d '{"kind":"favorite"}'
curl -s http://localhost:8080/products/1/revision
curl -s http://localhost:8080/products/1/snapshot
```

After a campaign, default restoration is `aside` with TTL 30s. Ensure you execute the four calls before TTL expiry; repeat flush/prime if needed. Then explain the `invalidate` mechanism in `app/src/main.rs`. A controlled E17 comparison uses `E17_*_consistency.json`.

**Say:** “Fast does not necessarily mean fresh. We accept seconds-old popularity counts; we must not cache a critical inventory balance using the same assumptions.”

## Scene 5 — Stampede and failure (10 min)

**Screen:** Grafana application DB reads/sec vs Redis cache hit rate while running E12 and E10; open fault sections of `scripts/benchmark_campaign.py`.

**Say:** “When a popular key expires, multiple replicas can all miss and simultaneously ask PostgreSQL to perform the same work. The protected mode uses Redis SET NX and follower polling. A different concern arises when Redis is entirely unavailable: fallback prevents cache errors from directly becoming 500 responses, but it can shift the traffic problem back to PostgreSQL.”

**Show:** `cache_wait_total{result="coalesced"}`, `cache_fallback_total` and `db_queries_total{operation="read_pulse"}` in Grafana Explore.

**Ask:** “What happens if the leader crashes? How long should a lock live? Does fallback guarantee zero errors when the DB saturates?”

## Scene 6 — Decision (10 min)

**Screen:** Generated `results/.../REPORT.md` plus `docs/POC_CANVAS.md`.

**Ask students to write:** one architectural decision record with hypotheses accepted/rejected, evidence files, operational risks, stale-data budget, and one follow-up experiment. **Emphasize** that distributed caching is an evidence-based trade-off, not a default replacement for data-model optimization.

## Instructor fallback commands

```bash
# Stack / logs
docker compose ps
docker compose logs --tail=100 app
docker compose logs --tail=100 db

# Restore cache node after fault
docker compose up -d redis1 redis2

# Restore standard classroom scenario
CACHE_MODE=aside CACHE_TTL_SEC=30 docker compose up -d --no-deps \
  --no-build --force-recreate --scale app=2 app

# Shut down WITHOUT erasing seeded DB
./poc down
```
