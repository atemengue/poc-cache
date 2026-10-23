# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

OrderFlow is an MGL870 lab PoC that compares distributed-cache strategies for one expensive read projection, `GET /products/{id}/snapshot`. That endpoint aggregates a large activity history per product. The repo has two halves:

- **Rust service** (`app/`, Axum + Tokio + SQLx + Redis). It is built and run only through Docker. A local Rust toolchain is optional.
- **Python orchestration** (`./poc` CLI + `scripts/`, **stdlib only, no third-party packages**). It drives Docker Compose, k6 (`benchmarks/load.js`), Prometheus/Pushgateway and Grafana. There is no Python app server.

## Commands

```bash
cp .env.example .env
./poc up --replicas 2          # build + start full stack (gateway on :8080)
./poc check / ./poc status / ./poc logs app --follow
./poc mode <db|aside|invalidate|write_through|protected|l1l2|sharded> --ttl 30 --l1-ttl 1 --replicas 2 --db-delay-ms 0
./poc benchmark --mode db --rate 100 --duration 20
./poc campaign --profile smoke --rate 100 --duration 15
./poc campaign --only E01 E03 E17 --skip-start   # targeted experiments on a running stack
./poc down                     # --volumes DELETES DB/monitoring data (needed to change SEED_ROWS)
```

Tests:

```bash
./poc test                     # python -m unittest discover -s tests -v, then Rust tests if cargo/docker exist
./poc test-rust                # Rust only: local `cargo test --locked`, else `docker build --target test`
python3 -m unittest tests.test_campaign -v                          # one Python module
python3 -m unittest tests.test_campaign.<Class>.<test_name> -v      # one Python test
cargo test --locked --manifest-path app/Cargo.toml l1_expiry        # one Rust test
python3 scripts/check_observability.py [--at <unix-time> --require-load]   # validate live Grafana/Prometheus queries
```

`tests/test_runtime.py` is opt-in and destructive. It needs `ORDERFLOW_RUNTIME_TESTS=1` and `COMPOSE_PROJECT_NAME=orderflow-cache-validation` on an already-running isolated stack, and must never run alongside a campaign. Most other Python tests are static contract checks that read the Rust source (for example, route strings in `main.rs`). If you rename routes, metrics or CLI flags, update those tests.

## Architecture

- `app/src/main.rs`: Axum handlers and response headers. `X-Cache-Source` is one of `db|db_miss|cache|cache_wait|db_fallback`, and `X-App-Instance` names the replica that answered.
- `app/src/storage.rs`: explicit SQL through SQLx. `POST /products/{id}/activity` bumps `revision` and appends an event in one transaction. `POST /orders` reserves stock with a conditional `UPDATE ... WHERE stock >= $2` in a transaction and returns 409 when stock is short.
- `app/src/cache.rs`: all strategies, selected by the `CACHE_MODE` env var. Cache-aside uses jittered TTL. The `protected` mode is a Redis `SET NX PX` lock plus follower polling and a token-checked Lua release. `l1l2` is a bounded process-local L1 over Redis. `sharded` is `CRC32(key) % 2` across `redis1`/`redis2`, which is not Redis Cluster. Invalidation and refresh happen **after DB commit**. Redis failures fall back to PostgreSQL. Rust unit tests live in `#[cfg(test)]` modules here and in `metrics.rs`.
- `app/src/config.rs` / `metrics.rs`: env parsing and Prometheus metrics.
- Correctness invariant: cached `stock` is display-only. Order placement must never authorize stock from the cache.

Orchestration flow:

- `./poc` is a thin argparse layer. `campaign` re-execs `scripts/benchmark_campaign.py` with forwarded flags. Other commands call its functions directly.
- `configure()` switches modes by recreating **only** the `app` service (`--no-deps --no-build --force-recreate`) with new env vars. DB, Redis and monitoring keep running.
- `benchmark_campaign.py` defines experiments E01–E22 and the profiles `smoke`, `full`, `sweep`, `capacity` and `resilience`. It runs k6, queries Prometheus, injects faults, and writes timestamped output under `results/` (`manifest.json`, `comparison.csv`, `campaign.json`, `hypotheses.json`, `REPORT.md`). `--outdir` must stay under `results/`.
- `scripts/hypotheses.py` (pure, no side effects) turns campaign rows plus the SLO thresholds in `benchmarks/slo.json` into H1–H8 verdicts: `supported`, `refuted` or `inconclusive`. Missing data must yield `inconclusive`. It can be run standalone: `python3 scripts/hypotheses.py results/campaign_<ts>`.
- `scripts/telemetry.py` pushes phase gauges to Pushgateway and creates Grafana annotations.

Infra: `docker-compose.yml` runs Postgres (seeded by `db/schema.sql` + `db/init.sh` **only on first volume init**), two Redis instances, scaled `app` replicas behind Nginx (`infra/nginx.conf`), Prometheus (`infra/prometheus.yml`, `infra/alerts.yml`), Grafana, exporters, cAdvisor and k6.

## Conventions and gotchas

- **Grafana dashboard JSON is generated** by `scripts/build_dashboard.py`. Edit the generator, not `infra/grafana/dashboards/*.json`.
- Keep the existing metric names (`cache_lookup_total`, `db_queries_total`, `http_request_duration_seconds`, …). Dashboards and `benchmark_campaign.py` depend on them.
- Keep `app/Cargo.lock`. Docker and test builds use `--locked`.
- The Dockerfile caches dependencies with a stub `main.rs` layer and has a separate `test` stage, tagged `orderflow-cache-rust-tests` so it doesn't overwrite the Compose image.
- Docs in `docs/` (ARCHITECTURE, EXPERIMENTS, OBSERVABILITY, VALIDATION, TROUBLESHOOTING, …) explain the experiments and their caveats in detail. Read them before changing an experiment or a claim.
