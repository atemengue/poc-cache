# Validation results — 2026-10-11

## Implementation checks

- Python unit/contract suite: **42 collected, 38 passed** (four opt-in live-stack tests are skipped unless explicitly enabled). This includes H1–H8 verdict unit tests with synthetic measurements, plus the E21 capacity-grid and E22 resilience-fault coverage. The live campaigns recorded in this snapshot predate E20–E22; those profiles are covered by the unit/contract tests and remain to be run live.
- Native Rust unit suite: **6 passed**; also passed in the Rust 1.88 Docker release build.
- `cargo clippy --locked --manifest-path app/Cargo.toml --all-targets -- -D warnings`: passed.
- `docker compose config --quiet`: passed.
- Live integration suite: **4 passed**, on an isolated `orderflow-cache-validation` project.

The runtime checks verified:

1. With stock set to 8, 32 concurrent reservations produced exactly 8 successful orders and 24 HTTP 409 responses. Stock ended at zero; eight orders and purchase events committed, and revision advanced by eight.
2. A 24-request protected-mode cold burst with 300 ms synthetic loader delay produced one `db_miss`, with the other reads served from cache or follower polling.
3. Both DB-only and cache-aside started with both Redis servers stopped. Cache-aside fell back to PostgreSQL and recovered after Redis returned. An established connection also recovered after a subsequent outage.
4. Eight TTL=1 samples had positive remaining TTLs no greater than 1100 ms.

## Benchmark setup

- Linux x86-64, 8 logical CPUs, Docker on one shared host.
- Two app replicas, each limited to 1 CPU and 512 MiB RAM; PostgreSQL pool size 12 per app.
- PostgreSQL 17, Redis 7, k6 0.54.0; Rust release image built with locked dependencies.
- 10,000 products and 250,011 activity rows for the repeated comparisons (250,000 seed rows plus integration/smoke mutations).
- 40 offered requests/s, 90% hot-product reads, keyspace 100, no writes in repeated comparisons.
- Each comparison lasted 20 seconds; three repetitions of E01 (DB-only) and E03 (hot-product-prewarmed cache-aside), always in that order.
- Both Redis databases were cleared before every case. Cache-aside TTL was 30 seconds plus up to 10% jitter.

## Repeated comparison

Values below summarize three independent run summaries, not pooled request percentiles.

| Metric | DB-only | Warm cache-aside |
|---|---:|---:|
| Median run read p95 | 112.49 ms | 2.22 ms |
| Run read p95 range | 80.38–201.64 ms | 2.19–2.32 ms |
| Median run read p99 | 211.22 ms | 2.69 ms |
| Median achieved throughput | 39.95 req/s | 40.05 req/s |
| HTTP errors | 0 | 0 |
| Dropped iterations | 0 | 0 |
| Median estimated DB snapshot reads | 781.92 | 57.22 |

Prometheus `increase()` values are scrape-sampled, extrapolated estimates and can be fractional or exceed exact request counts. Raw k6 summaries contain the client measurements. These short tests demonstrate the cache benefit for this workload, **not maximum capacity**. DB latency varied substantially. Fixed experiment order, unseeded request randomness, shared host services, and warm PostgreSQL buffers remain validity limitations.

## Smoke, failures, and freshness

The complete smoke profile finished: seven load cases and three consistency probes. All load cases had zero HTTP errors and dropped iterations. During the Redis outage case, whole-run read p95 was 207.45 ms; a whole-run percentile does not characterize the outage interval by itself.

A separate E17 run exercised all seven modes:

| Mode | Stale gateway reads after one committed activity write |
|---|---:|
| aside | 60 / 60 |
| invalidate | 0 / 60 |
| write_through | 0 / 60 |
| protected | 0 / 60 |
| l1l2 | 32 / 60 |
| sharded | 0 / 60 |
| db | 0 / 60 |

These are sequential freshness observations. They do not prove concurrent linearizability or eliminate the documented invalidation/refresh races. L1 staleness and cache-aside staleness are expected properties of those modes.

## Observability validation

The golden-signals instrumentation was exercised on the isolated stack:

- `python3 scripts/check_observability.py` ran **89 panel queries** against live Prometheus with **zero failures**, and confirmed Grafana provisioned the expected dashboard. The same check passed with `--at <measurement time> --require-load` for each of the three observability runs, which additionally requires k6 native histograms and campaign context at that timestamp.
- `promtool check rules` passed with **15 rules**; Prometheus reloaded the changed rules with HTTP 200.
- Three targeted runs (E03 cache-aside, E10 Redis kill, E12b protected-mode flush) completed with `orderflow_telemetry_complete=1` and no observability errors.
- k6 remote-write native histograms, Pushgateway phase/SLO/probe gauges, and per-run Grafana annotations (phase transitions and faults) were all queryable after the runs.
- `scripts/hypotheses.py` produced verdicts from the recorded campaign: H5 **supported** (zero errors during the Redis outage run), H1–H4/H6–H8 honestly **inconclusive** because the targeted three-run campaign lacked the DB-only baseline, capacity sweep, E14/E17/E18 arms.

Observability failures are recorded as incomplete/unknown, not as passing SLOs.

## Evidence and reproduction

Local artifacts (ignored by Git) include raw k6 JSON/logs, manifests, CSV comparisons, and Markdown reports:

- `results/validation_smoke/`
- `results/validation_repeat_1/`
- `results/validation_repeat_2/`
- `results/validation_repeat_3/`
- `results/validation_consistency/`
- `results/observability/` (panel validation reports and the targeted E03/E10/E12b campaign with per-run telemetry)

These paths are recorded from the validation runs; because they are Git-ignored they may be archived or removed between checkouts (the evidence retained in this workspace is under `results/campaign_*/`).

In total, **13 load results and 10 consistency results** were checked. The entire E01–E22 performance matrix was not executed.

Use the isolated-stack instructions in [RUST_IMPLEMENTATION.md](RUST_IMPLEMENTATION.md#live-integration-tests), then run:

```bash
./poc campaign --profile smoke --skip-start --rate 40 --duration 15
for repeat in 1 2 3; do
  ./poc campaign --profile full --skip-start --only E01 E03 \
    --rate 40 --duration 20 --outdir "results/repeat_${repeat}"
done
./poc campaign --profile full --skip-start --only E17 --rate 40 --duration 15
```

The validation stack was stopped after collection; its database/Grafana volumes and result files were retained.

## Committed campaign analyses

The PDFs in `reports/` are presentation-ready analyses of two separate campaigns, not substitutes for the validation results above. Their source run data was retained in the ignored campaign directory when these reports were prepared, but may not be present in a fresh checkout.

| Report | Source campaign | Reproduction command |
|---|---|---|
| `OrderFlow_Cache_Campaign_Analysis.pdf` | Smoke profile, 2026-10-11 15:46 UTC; 150 arrivals/s, 25 seconds per load case, 2 app replicas | `./poc up --replicas 2 && ./poc campaign --profile smoke --rate 150 --duration 25` |
| `OrderFlow_Campaign_203001_Analysis.pdf` | Full profile, campaign `campaign_20261011_203001`, started 2026-10-11 20:30:01 UTC; 150 arrivals/s, 25 seconds per load case, 2 app replicas | `./poc up --replicas 2 && ./poc campaign --profile full --rate 150 --duration 25` |

The full-profile run is a single campaign on one shared host; it is comparative evidence at the tested settings, not a replicated capacity study. Docker image versions, host resources, seed state, and workload details affect reproduction. See the PDFs and the experiment guide for the results and limitations.
