# OrderFlow — Distributed Cache PoC (Rust + Python CLI)

This MGL870 laboratory evaluates architectural alternatives for a **flash-sale e-commerce product page**. A hot product receives many requests for an *expensive read projection*: stock display, product details, demand score, and counts of views, favorites and purchases. PostgreSQL stores 10,000 products and a default 250,000 activity rows, roughly 70% associated with product 1. Each uncached request aggregates a large event history. Redis can serve the computed projection without repeating that query.

**The application is implemented in Rust** (Axum, Tokio, SQLx, Redis). **All operations and experiments use the Python `./poc` CLI** (stdlib only); k6 scenarios are JavaScript. There is no Python application server. The domain is inspired by the supplied event-driven Order Placement → Inventory/Payment → Fulfillment/Shipping diagram, but intentionally only implements Product Snapshot, activity writes, and **transactional Order Placement/Inventory**. Other business components are *not* implemented or claimed.

Important correctness boundary: cached `stock` is a **display hint only**. `POST /orders` uses an atomic conditional PostgreSQL update in a transaction. It **never authorizes purchase from cache**. In real production systems, investigate outbox/event sourcing, versioning, CQRS projections, incremental aggregates, read replicas and materialized views before accepting this design.

## Requirements

- Docker Engine and Docker Compose v2; Python 3.10+ for the CLI; sufficient disk, CPU and RAM.
- A local Rust toolchain is optional. Docker builds the Rust app using a multi-stage image. The first compile may take several minutes; subsequent `mode` experiments reuse the built image.
- `curl` is useful for manual requests, and is installed in the Rust runtime image for internal probes.
- Do not expose this demonstration stack to the public internet. Ports bind to localhost; demo DB/Grafana credentials are intentionally weak.

## Quick start

```bash
cp .env.example .env
./poc up --replicas 2
./poc check
./poc status

./poc mode db
curl -i http://localhost:8080/products/1/snapshot

./poc mode aside --ttl 30
curl -i http://localhost:8080/products/1/snapshot    # db_miss
curl -i http://localhost:8080/products/1/snapshot    # cache

./poc campaign --profile smoke --rate 100 --duration 15
./poc test
./poc down
```

The `/products/1/snapshot` response header `X-Cache-Source` explains the read path (`db`, `db_miss`, `cache`, `cache_wait`, `db_fallback`). `X-App-Instance` shows which application replica responded. When testing performance, compare **achieved** k6 throughput, p95/p99, errors, dropped iterations, and server-side DB query counts rather than only cached request latency.

## CLI reference

| Command | Purpose |
|---|---|
| `./poc build` | Build application image (Rust release binary) |
| `./poc up --replicas 2` | Start PostgreSQL, Redis x2, Rust app replicas, gateway, observability |
| `./poc status` / `./poc check` / `./poc logs app --follow` | Operational checks |
| `./poc mode aside --ttl 30 --l1-ttl 1 --replicas 2 --db-delay-ms 0` | Recreate only application replicas with cache configuration; reuse binary image |
| `./poc flush` | Flush both Redis databases (never use this against production Redis) |
| `./poc benchmark --mode db --rate 100 --duration 20` | One fixed-arrival-rate k6 measurement |
| `./poc campaign --profile smoke --rate 100 --duration 15` | Classroom subset |
| `./poc campaign --profile full --rate 150 --duration 25` | Full E01–E19 campaign (E20–E22 run via the sweep/capacity/resilience profiles) |
| `./poc campaign --profile sweep --rate 40 --duration 25` | H2 capacity sweep: DB-only vs warm cache at 1×/2×/4×/8× rate |
| `./poc campaign --profile capacity --strategies db invalidate protected --rates 50 100 150 250 500 1000 --repetitions 5 --duration 120 --writes 0.05 --max-vus 2000` | H2 capacity grid (E21): strategies × rates × repetitions |
| `./poc campaign --profile resilience --strategies invalidate protected --rates 50 100 150 --duration 180 --repetitions 5 --fault redis-stop --fault-at 60 --recover-at 120` | H5 resilience grid (E22): fault injected at `--fault-at`, restored at `--recover-at` |
| `./poc campaign --only E01 E03 E17 --skip-start` | Targeted campaign over a running stack |
| `./poc test` | Run Python orchestration tests, plus native Rust unit tests if Cargo/Docker available |
| `./poc test-rust` | Require and run native Rust tests using local Cargo or Docker `test` build stage |
| `./poc down` / `./poc down --volumes` | Stop; `--volumes` **deletes database/monitoring data** |

All results are saved to timestamped directories under `results/`: `manifest.json`, raw k6 exports and logs, `comparison.csv`, `campaign.json`, `hypotheses.json` (H1–H8 verdicts) and `REPORT.md` (results table + dashboard links + verdict section). The run may take a long time because the full campaign performs 50+ benchmark cases and frequently recreates application replicas. Set `--only` for shorter experiments. The grid profiles multiply that cost: the capacity example above performs 3×6×5 = 90 runs and the resilience example 2×3×5 = 30 runs.

Grid profiles accept `--strategies` (one or more modes), `--rates` (offered requests/s), `--repetitions` (repeats per strategy/rate), `--writes`, `--hot`, `--keyspace`, and `--max-vus`. The capacity profile auto-sizes pre-allocated VUs to the highest offered rate so the constant-arrival-rate generator does not self-inflict dropped iterations. The resilience profile additionally requires `--fault`, `--fault-at`, and `--recover-at` with `0 < fault-at < recover-at < duration`; fault timing is aligned to k6's own run window.

For Rust tests without a local toolchain:

```bash
./poc test-rust
# Without local Cargo: docker build --target test -t orderflow-cache-rust-tests .
```

## REST endpoints

| Method | Endpoint | Meaning |
|---|---|---|
| GET | `/products/1/snapshot` | Cached/uncached expensive product-demand aggregation |
| GET | `/products/1/revision` | Authoritative revision used in consistency experiments |
| POST | `/products/1/activity` | `{ "kind": "favorite" }` (or `view`, `purchase`) |
| POST | `/orders` | `{ "product_id": 1, "quantity": 1 }` — safe reservation; HTTP 409 if insufficient stock |
| GET | `/orders/1` | Authoritative stored order status |
| GET | `/health` | Health depends on PostgreSQL, **not** Redis |
| GET | `/debug/config` | Current strategy and experimental parameters |
| GET | `/metrics` | Prometheus metrics |
| GET | `/docs` | Simple HTML endpoint reference (not Swagger/OpenAPI) |

Example order transaction:

```bash
curl -sS -X POST http://localhost:8080/orders \
  -H 'Content-Type: application/json' \
  -d '{"product_id":1,"quantity":1}'
```

## Cache mechanisms

| Mode | Technique | Caution |
|---|---|---|
| `db` | No Redis reads | Full DB recomputation |
| `aside` | Cache-aside with jittered TTL | Deliberate staleness after activity writes |
| `invalidate` | After-commit DEL | Crash/race between DB commit and DEL |
| `write_through` | After-commit recompute then SET | Write amplification, not atomic with DB |
| `protected` | Redis NX/PX lock + follower polling | Lock timeout or Redis failure can create duplicate DB reads |
| `l1l2` | Bounded process-local TTL + shared Redis | Cross-replica L1 staleness |
| `sharded` | CRC32(key) % 2 over two Redis servers | **Not Redis Cluster**, no replication or HA |

See `docs/ARCHITECTURE.md`, `docs/DOMAIN.md`, `docs/POC_CANVAS.md`, `docs/EXPERIMENTS.md`, `docs/TEACHING_GUIDE.md`, `docs/RUST_IMPLEMENTATION.md`, `docs/OBSERVABILITY.md` and `docs/TROUBLESHOOTING.md`. Measured validation results and reproduction details are in [`docs/VALIDATION.md`](docs/VALIDATION.md).

## Observability

- API: <http://localhost:8080/docs>
- Grafana: <http://localhost:3000> — `admin` / `admin`
  - *OrderFlow — Four Golden Signals* (`orderflow-golden`): latency, traffic, errors, saturation, plus H1–H8 drill-downs and campaign annotations.
  - *OrderFlow Cache Lab*: the original cache-mode dashboard.
- Prometheus: <http://localhost:9090>; Pushgateway: <http://localhost:9091>.
- Every campaign run publishes phase gauges, SLO contracts, and E17/E18 probe results to Pushgateway, and creates time-tagged Grafana annotations. Each report links to the exact dashboard window.
- SLO decision thresholds live in `benchmarks/slo.json` and are validated at load time; override with `ORDERFLOW_SLO_FILE`.
- Validate the live panels (fails on a broken query or missing required metric):

  ```bash
  python3 scripts/check_observability.py
  python3 scripts/check_observability.py --at <unix-time> --require-load
  ```

- The dashboard JSON is generated by `scripts/build_dashboard.py`; edit the generator, not the JSON. Alert rules are in `infra/alerts.yml`.
- Grafana panels and `scripts/benchmark_campaign.py` expect the preserved original metric names (`cache_lookup_total`, `db_queries_total`, `http_request_duration_seconds`, etc.). See [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) for metric families, the SLO contract, and measurement caveats.

## Reproducibility and caveats

The default seed is applied **only on first database-volume initialization**. To change `SEED_ROWS`, recreate the volume intentionally (`./poc down --volumes`). Campaign `E17` checks **sequential freshness**, not full linearizability; `E19` mixed writes record activity rather than creating orders. Cache falls back to PostgreSQL on Redis outage, but the database may then saturate. E13's added DB delay is synthetic, not evidence of PostgreSQL resource saturation. Redis L1 invalidation does not propagate across replicas. The validation report describes one host and workload; execute k6 on the target host before drawing capacity conclusions.

`app/Cargo.lock` pins Rust dependencies. Docker builds and the Rust test command use `--locked`; retain the lockfile when sharing or committing the project. Redis connections are lazy and time-bounded, so the API can start with PostgreSQL available even when Redis is down.
