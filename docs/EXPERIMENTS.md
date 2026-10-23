# Experiment protocol — E01 to E22

## Required independent variables

Each k6 run controls:

- **Cache mode:** `db`, `aside`, `invalidate`, `write_through`, `protected`, `l1l2`, `sharded`.
- **Cache TTL:** 1, 10, 30, or 60 seconds. L2 includes up to 10% jitter to reduce synchronized expiration. TTL is not a guarantee of freshness.
- **Read/write ratio:** 100/0 for isolation; 95/5, 80/20, 50/50 for write churn.
- **Demand:** offered requests/s via k6 `constant-arrival-rate`; never conflate requested with achieved RPS.
- **Popularity:** fraction of requests hitting product ID 1, vs random product IDs in a declared keyspace (2–10000).
- **Deployment:** app replicas 1, 2, or 4, single vs two independent Redis shards.
- **Failure:** Redis stop, restart, cache flush, DB delay.
- **Repetition and fault timing:** grid profiles repeat each `(strategy, rate)` cell `--repetitions` times; E22 (`resilience`) injects its fault at an explicit `--fault-at` and restores the dependency at `--recover-at`, both in seconds into the k6 run window.

## Experimental matrix

| ID | Mode / condition | Question answered | Automation |
|---|---|---|---|
| E01 | DB-only | How much expensive work can PostgreSQL support? | `full`, `smoke` |
| E02 | Cache-aside; L2 flushed before load | What happens with a cold cache? | `full`, `smoke` |
| E03 | Cache-aside; hot product prewarmed | Does repeated reading benefit? | `full`, `smoke` |
| E04 | Cache-aside TTL 1s | Effect of rapid expiration | `full` |
| E05 | Cache-aside TTL 10s | Midrange TTL | `full` |
| E06 | Cache-aside TTL 60s | Long-lived cached values | `full` |
| E07 | 50% requests to hot product | How does weaker locality affect the observed hit rate? | `full` |
| E08 | 80% to hot product | Medium locality | `full` |
| E09 | 95% to hot product | Strong locality | `full` |
| E10 | Kill Redis during load, later restore | DB fallback during cache outage | `full`, `smoke` |
| E11 | Restart Redis during load | Recovery and rewarming | `full` |
| E12a/b | Flush cache; aside vs protected | Does locking prevent miss storm? | `full`, `smoke` |
| E13a/b | Add 35ms per DB read; DB-only vs cache | Is caching useful under DB pressure? | `full` |
| E14 | 1,2,4 replicas | How does shared caching scale with app replicas? | `full` |
| E15a/b | Single Redis vs 2-shard Redis | Does partitioning help broad keyspace workloads? | `full` |
| E16a/b | 100% reads of same product; flush; aside vs protected | Distributed stampede without and with SET NX | `full` |
| E17 | Mutation then 60 reads; all 7 modes | How many cached revisions are stale? | `full`, 3 modes in `smoke` |
| E18a/b | L2-only vs L1+L2 | Does local caching reduce network cost? | `full`, L1 only in `smoke` |
| E19 | 95/5, 80/20, 50/50 across all seven modes | At which update rate does caching lose advantage? | `full` |
| E20 | DB-only vs warm cache-aside at 1×/2×/4×/8× base rate | What is the maximum SLO-compliant throughput of each arm? (H2) | `sweep` |
| E21 | Any set of strategies at any declared rates, repeated N× | How does maximum SLO-compliant throughput compare across cached arms vs DB-only? (H2) | `capacity` |
| E22 | Strategies under an explicit-timed Redis fault | Does an outage fail every request, and how fast does read p95 recover? (H5) | `resilience` |

### Exact execution

```bash
./poc campaign --profile smoke --rate 100 --duration 15
./poc campaign --profile full --rate 150 --duration 25 --vus 180 --max-vus 700
# Fast, targeted hypothesis test (E01, E03, E17 only):
./poc campaign --profile full --only E01 E03 E17 --rate 40 --duration 15
```

- The script **builds/starts the stack** (unless `--skip-start`), changes environment variables, recreates only app containers, waits for `/health`, flushes both shared caches before every case, optionally warms the hot product, starts k6 inside the Compose network, injects faults while k6 is running, waits for completion, queries Prometheus counters, and appends to CSV/JSON/Markdown reports. App recreation also resets L1. CLI service ports are resolved through Compose, including `.env` values.
- The script restores both Redis services and returns the application to `aside` with 2 replicas at the end, including an exception path. Use `--no-restore` to inspect the final experiment mode.
- Failures and overloads are expected; k6 may return nonzero and the campaign still records available evidence. A missing summary is reflected as blank fields; inspect that run's `.log`.
- `--outdir results/my_campaign` is supported. Put outputs under the `results/` mount; other paths cannot be written by the k6 container.
- Use a dedicated testing host: the run impacts shared DB/cache. **Do not run two campaigns against one stack concurrently.**

### Capacity sweep

The full suite controls traffic at a single target per case for fairness. To find each strategy's **maximum SLO-compliant throughput**, use the dedicated sweep profile, which runs DB-only and warm cache-aside at 1×, 2×, 4×, and 8× the base rate:

```bash
./poc campaign --profile sweep --rate 40 --duration 25
```

For finer control, repeat the same profile at increasing request rates (example):

```bash
for rate in 50 100 200 400 800; do
  ./poc campaign --profile smoke --skip-start \
    --rate "$rate" --duration 25 --only E01 E03
done
```

Use `--profile full --only E01 E03` instead of the example smoke command to compare just the baseline and the cache at each rate (no failure tests). Mark a configuration **infeasible** once errors, dropped iterations, or latency breach its declared SLO; do not treat k6's configured rate as achieved capacity. For this repeated-run style (E01/E03 without a sweep or grid), `scripts/hypotheses.py` marks H2 as **inconclusive** unless both arms have at least three distinct offered rates. Capacity sweeps and grids (E20/E21) do not use that rule; they adjudicate per `(strategy, rate)` cell as described under **Capacity grid**.

### Capacity grid

E21 generalises the sweep: choose the strategies, the offered rates, and the number of repetitions per cell. Each run is warm and starts from a known cache state.

```bash
./poc campaign --profile capacity \
  --strategies db invalidate protected \
  --rates 50 100 150 250 500 1000 \
  --repetitions 5 --duration 120 --writes 0.05 --max-vus 2000
```

- Ordering: **strategy → rate → repetition**. The run slug stays `NNN_E21_{strategy}`; the repetition is a row field, and the description carries `rep i/n`.
- Pre-allocated VUs are auto-sized to `min(--max-vus, max(rates, --vus))` so the constant-arrival-rate generator has headroom; a low `--vus` would otherwise self-inflict `dropped_iterations` at high rates.
- **H2 adjudication (repetitions):** rows are grouped per `(strategy, rate)`. A single run passes only when its p95/p99/error rate meet the normal SLO **and** it dropped **zero** iterations (a dropped iteration means the target did not receive the offered rate, so the run cannot support a capacity claim — this is stricter than the `< 1%` alerting budget in `benchmarks/slo.json`). A cell *passes* when the **median** p95/p99/errors meet the normal SLO and meet the zero-drop condition **and** the per-repetition pass-fraction is ≥ 0.8; a cell that passes on its median but fails 20%+ of repetitions is treated as unstable and not as a passing rate. Each strategy's passing rate is its highest passing offered rate. Cached arms are every strategy except `db`. H2 is `supported` when the best cached arm reaches **≥ 2×** the `db` passing rate. With one repetition per cell (e.g. the `sweep` profile) the pass-fraction check reduces to the single run's own SLO result.
- Cost: the example is 3×6×5 = **90 runs × 120 s ≈ 3 h** plus per-run configure/recreate overhead. Use `--only E21` and `--skip-start` to keep a stack warm, and expect each run to flush both Redis caches and recreate the app replicas.

### Resilience grid

E22 injects a fault at an explicit time and restores the dependency at an explicit time, so the outage and recovery windows are comparable across runs.

```bash
./poc campaign --profile resilience \
  --strategies invalidate protected \
  --rates 50 100 150 \
  --duration 180 --repetitions 5 \
  --fault redis-stop --fault-at 60 --recover-at 120
```

- Validation requires `0 < --fault-at < --recover-at < --duration`. `--fault` accepts `redis-stop`, `redis-restart`, and `flush`; `redis-stop` maps to the existing `stop_redis` action (Redis1 is brought back at `--recover-at`, with a finally-path restore as a backstop).
- **Timing alignment:** the runner waits for k6's own log to report its run window (`running (…)`) and then times `--fault-at` / `--recover-at` from there, so the fault is relative to the load rather than to container startup. There is still a sub-second skew plus the JSON-sample timestamp resolution; the dashboard fault/recovery annotations are the authoritative time markers.
- **Per-run evidence:** after the run, the saved k6 `_samples.json.gz` is parsed per second for `read` requests. `comparison.csv` gains `fault_window_error_rate` (read failures inside `[fault-at, recover-at)`), `outside_error_rate`, and `recovery_observed_seconds` (the first second after `--recover-at` whose trailing 10-second read-p95 stays within the degraded p95 budget; `None` means the budget was never re-met). Rows also carry `fault`, `fault_at_s`, `recover_at_s`, and `repetition`.
- **H5 adjudication:** when E22 rows exist, H5 uses the **worst fault-window read error rate** across runs against the degraded error budget; recovery deadlines are reported as evidence but are not folded into the H5 verdict. Without E22 rows, H5 falls back to the whole-run E10 window.
- Cost: the example is 2×3×5 = **30 runs × 180 s ≈ 1.5 h**. A 60-second post-recovery observation window remains inside each 180 s run.

### Failure and recovery methodology

E10: Redis1 stops ~35% into the k6 window and is restored after an additional ~30% of the window. Record the relevant region of the Grafana time series; total-run p95 hides short spikes. E11 restarts Redis1 mid-window. E12/E16 flush both Redis caches ~35% into load. The `resilience` profile (E22) replaces these fractions with explicit `--fault-at`/`--recover-at` offsets timed from k6's own run window and records the fault-window read error rate and recovery time. These interventions are logged; run-level summary fields are not a substitute for time-resolved metrics.

E13 uses an **explicit synthetic 35ms delay per DB product snapshot read**. It is labeled synthetic and must not be misrepresented as database resource contention. For genuine DB stress increase order_events row count or target throughput and inspect PostgreSQL CPU/query behavior. `DB_QUERY_EXTRA_MS` changes app query timing, not a PostgreSQL server setting.

E15 is **two independent Redis instances with application-managed CRC32 sharding**, not Redis Cluster. It has no replica, automatic failover, or key rebalancing. The experiment measures sharding overhead/benefit at a chosen workload; it does not establish Redis saturation without extra high-load tests.

### Consistency probe limitations

E17 fetches the authoritative DB revision, warms the cache, commits one purchase-activity event, then performs 60 reads through the gateway. A revision below the committed revision counts as stale. This captures immediate post-write staleness for a sequential test. The `l1l2` case also warms both replicas directly and records two explicit per-replica probes after the write (separate from the 60 gateway reads), so a stale remote L1 does not depend on accidental gateway routing. It is **not** a concurrent linearizability, lost-update, or transactional atomicity proof. An invalidation failure or a DB–cache race can still produce stale values even when E17 yields 0.

## Data collection and validity

Primary comparison metrics: read p95/p99 in **milliseconds**, achieved RPS, HTTP error rate, `dropped_iterations`, `client_cache_hit_ratio`. These come from k6's native JSON summary. Cache L2 hits/misses and DB queries are also sampled from Prometheus using counter increases around each run. Scraping is 5s; very short runs can give incomplete/null values. Preserve the source `.log`, `.json`, `manifest.json`, and plots/time periods to make observations reproducible.

Time-resolved evidence: each run also pushes k6 native histograms via Prometheus remote write (windowed quantiles), campaign phase gauges to Pushgateway, and phase/fault annotations to Grafana. Use the dashboard time-window link in the run's report or `REPORT.md` to inspect the exact interval, including short fault spikes that a whole-run percentile hides. `orderflow_telemetry_complete` must be 1 before treating a run's SLO as passing; otherwise the measurement is incomplete. See [`OBSERVABILITY.md`](OBSERVABILITY.md).

Controls: same host, same seed, equivalent traffic, same app CPU/memory limits, same replica count for direct strategy comparisons, warmed/cold state explicitly stated. Do not run another background load generator. Check k6 capacity separately: an overloaded generator can drop iterations while the target remains healthy. Repeat 3–5 times and report median and dispersion before making a research-grade claim. Random data generation is non-deterministic (no fixed k6 random seed in this PoC); repeated distributions are statistically similar, not identical.

## Data analysis formulae

- **DB read reduction** = `1 - cached_DB_reads / baseline_DB_reads`, only for matched *achieved* requests and equivalent read ratios.
- **Read latency improvement** = `1 - cache_p95 / baseline_p95`.
- **Hit ratio** = `hits / (hits + misses)` using the same cache layer and measurement window. The `HOT_PERCENT` workload parameter is **not** the actual cache-hit ratio.
- **Availability during failure** = successful requests / attempted requests over failure time window; report separately from full-run summary.
- **Staleness** = `count(response.revision < DB.revision_after_write) / observed_reads`.

## Hypothesis verdicts

`scripts/hypotheses.py` turns a `campaign.json` plus `benchmarks/slo.json` into an H1–H8
verdict per hypothesis. Each verdict is `supported`, `refuted`, or `inconclusive` and
carries the measured evidence and the exact criterion used. Missing data yields
**inconclusive**, never a silent pass. Every report now includes a **Hypothesis
verdicts** section and writes `hypotheses.json` next to `campaign.json`.

Standalone:

```bash
python3 scripts/hypotheses.py results/campaign_YYYYmmdd_HHMMSS
```

H2 deliberately requires a capacity sweep or grid (see above); E20/E21 are that automation,
and E21 additionally aggregates repetitions per (strategy, rate). H5 prefers the E22
fault-window read error rate when resilience runs are present.

## A decision record template

```text
Decision: Adopt / Revise / Reject _____________________
Claimed quality: performance | resilience | freshness | operability
Matched workload: rate _____ achieved _____ read/write mix _____
Dataset: activity rows _____ app replicas _____ CPU _____
Evidence files: REPORT.md + [specific raw JSON] + Grafana time interval
Observation: measured p95 _____, p99 _____, errors _____, stale reads _____
Alternative rejected: ______  Reason: ______
Failure risks: ______
Validity threats: generator capacity, hot-set skew, scrape interval, order effects
Next experiment: ______
```
