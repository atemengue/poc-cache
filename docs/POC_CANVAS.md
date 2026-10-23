# Architectural PoC Canvas — OrderFlow

## Decision statement

**Which distributed caching strategy should serve a popular, frequently refreshed product demand snapshot endpoint while preserving acceptable freshness and surviving cache disruptions?**

The product owner wants a product page that responds quickly during a flash sale. SRE wants to protect PostgreSQL and detect stampedes. Engineering wants to avoid unnecessary failure modes. Updates to activity and purchases change the snapshot, so readers must understand eventual consistency. The source of truth is always PostgreSQL.

| Canvas element | Specification |
|---|---|
| Architectural problem | Popular product detail page causes many duplicate DB aggregations |
| Context | Two or more horizontally scaled Rust Axum replicas, Nginx, PostgreSQL, Redis |
| Stakeholders | Student architect, product owner, DB operator, SRE, shopper |
| Decision | DB-only vs cache-aside vs invalidation vs write-through vs distributed single-flight vs L1/L2 vs sharding |
| Primary uncertainty | Is caching beneficial enough to warrant staleness and failure complexity? |
| Critical assumption | Many shoppers view the same product and tolerate bounded staleness of *engagement statistics* |
| Business risk | Product snapshot can be stale, but **inventory reservations / payment decisions must not** be inferred from it |
| Hypotheses | H1–H8 (below) |
| Scenarios | A flash-sale product, mixed reads/writes, cold/warm, TTL, loss of Redis, cache flush, slow DB, multi-replica, sharding |
| Measurements | k6 achieved RPS, p95/p99, error rate, dropped iterations; cache hit ratio; DB read count; Prometheus system metrics |
| Experiments | E01–E22: `docs/EXPERIMENTS.md` |
| Decision rules | Compare *matched workloads* and apply the SLOs below |
| Outcome | **Adopt**, **Revise**, or **Reject** with evidence and documented limitations |

## Assumptions to test

- A popular product attracts a disproportionate fraction of views; cache reuse is plausible.
- The product snapshot is an aggregation of historical product activity events; the cost is not negligible even with a normal product_id index.
- Shoppers accept demand-statistic totals becoming outdated by a few seconds; they would not accept outdated authoritative inventory.
- The database remains reachable during the cache outage tests.
- Host CPU/memory/network resources are not silently different across alternatives.

## Falsifiable hypotheses

| ID | Hypothesis | Planned evidence |
|---|---|---|
| H1 | Warm cache reduces read p95 by at least 50% vs DB-only | E01 vs E03, same rate and replicas |
| H2 | Cached mode sustains at least 2× DB-only traffic before SLO violations | Repeat E01/E03 with increasing `--rate`; observe achieved throughput and dropped iterations. E21 (`--profile capacity`) automates the grid and aggregates repetitions (median + pass-fraction) |
| H3 | At **observed** L2 hit ratio ≥80%, DB read operations decrease ≥70% | Prometheus cache/db query counters E03 and E07–E09; matched rates |
| H4 | Without a cache, app replicas increase DB work; with shared cache, most repeated reads avoid DB | E14 at 1/2/4 replicas; compare DB query rate |
| H5 | Redis unavailable does not automatically produce 500 for every request | E10 k6 errors and DB fallback counter, subject to DB headroom. E22 (`--profile resilience`) injects/restores Redis at explicit offsets and reports the fault-window read error rate and recovery time |
| H6 | Distributed single-flight reduces redundant DB reads after a flush | E12a vs E12b and E16a vs E16b |
| H7 | Write-aware strategies reduce stale reads compared with basic aside | E17 revision consistency probes |
| H8 | L1 improves latency but may serve stale data from another replica | E17 l1l2 and E18 |

## Suggested quality-attribute scenarios (ATAM-inspired)

**QA-PERF-01:** Given 150 read requests/s for the same product on 2 app instances and a warm cache, when the page is read, achieve read p95 below 100 ms with fewer than 1% HTTP errors and fewer than 1% dropped iterations. Compare to DB-only under identical load.

**QA-RES-01:** While shoppers refresh the page, kill Redis; the system should continue responding through PostgreSQL (potentially with higher p95). Accept/reject based on observed errors, not on the existence of fallback code alone.

**QA-FRESH-01:** Immediately after one engagement is committed, a new read should reflect the revision for strategies claiming near-freshness. Measure stale reads across requests; this experiment does **not** prove linearizability under concurrent writes.

**QA-SCALE-01:** During growth from 1 to 4 replicas, cached read workloads should not increase DB reads linearly when a hot object is repeatedly requested.

## Provisional success/acceptance thresholds

These are **decision thresholds**, not measurements or universal service promises. Adjust before starting an assessment, and report changes to students.

The machine-readable source is [`benchmarks/slo.json`](../benchmarks/slo.json). It encodes a *normal* and a *degraded* contract (the latter for a cache outage while PostgreSQL still has headroom), a recovery deadline, and per-strategy freshness budgets. The campaign and alerts read these values rather than duplicating numbers; see [`OBSERVABILITY.md`](OBSERVABILITY.md).

| Measure | Proposed criterion |
|---|---|
| Read latency p95 | < 100 ms |
| Read latency p99 | < 250 ms |
| HTTP error rate | < 1% outside deliberate failure periods |
| k6 dropped iterations | < 1% of scheduled iterations |
| Warm-cache hit ratio | ≥ 80% when product locality supports it |
| DB query reduction | ≥ 70% at comparable offered/achieved load |
| Immediate stale fraction | 0% for low-concurrency invalidate/write-through experiment (expect caveats) |
| Redis loss | Graceful degraded service while DB has capacity |

The dropped-iterations budget above is the **alerting** threshold (`drop_ratio` in `benchmarks/slo.json`, enforced by `infra/alerts.yml`). The hypothesis verdicts are stricter: a run counts as passing only with **exactly zero** dropped iterations, because a dropped iteration means k6 did not deliver the offered rate and the measurement cannot support a capacity claim. The gap is intentional, not an inconsistency.

Each hypothesis maps to one of the Four Golden Signals so the dashboard and the SLO contract back the same claim:

| Signal | Backs | Dashboard row |
|---|---|---|
| Latency | H1, H5 | 1 · Latency |
| Traffic | H2, H3, H4, H6 | 2 · Traffic |
| Errors | H5, H6 | 3 · Errors |
| Saturation | H2, H5, H8 | 4 · Saturation |

H7/H8 freshness and layer evidence appear in the H4/H7 and H7/H8 drill-down rows. A short sequential probe (`orderflow_probe_*`) is a **lower bound** on staleness, not a linearizability proof.

## Decision table (complete after measurements)

| Alternative | Benefit evidence | Harm evidence | Decision | ADR follow-up |
|---|---|---|---|---|
| DB only | E01 | E01, E13 | Pending | Establish noncached source of truth |
| Cache-aside | E02–E09 | E12/E17 stale | Pending | Define TTL and stale tolerance |
| Invalidation | E17 | Write overhead/races | Pending | Revisit transactional outbox/versioning |
| Write-through | E17 | Write amplification, races | Pending | Compare to async precalculation |
| Protected | E12/E16 | Lock overhead, timeout | Pending | Plan lock-loss behavior |
| L1/L2 | E18 | E17 remote L1 stale | Pending | Add pub/sub or short L1 TTL |
| Sharded | E15 | Redis node failure, rebalancing | Pending | Only if Redis itself saturates |

Students should produce an ADR with **measured evidence**, the mechanism adopted, rejected alternatives, known consistency limitations, and the next experiment required. A decision of "revise" is valid if failure protection works but causes unacceptable DB overload.
