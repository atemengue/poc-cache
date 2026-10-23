#!/usr/bin/env python3
"""Derive H1-H8 verdicts from measured campaign rows and the SLO contract.

Verdicts are always one of ``supported``, ``refuted`` or ``inconclusive``; missing
data yields ``inconclusive``, never a silent pass. The module has no side effects and
turns a ``campaign.json`` (or the in-memory RESULTS list) into machine-readable
verdicts plus a Markdown table.

Run standalone against a recorded campaign:

    python3 scripts/hypotheses.py results/campaign_YYYYmmdd_HHMMSS
"""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys

from telemetry import load_slo

# Strategies that react to a committed write instead of waiting for the TTL.
WRITE_AWARE = ("invalidate", "write_through", "protected")
VERDICTS = ("supported", "refuted", "inconclusive")


def _runs(results):
    return [r for r in results if r.get("kind") != "consistency"]


def _consistency(results):
    return [r for r in results if r.get("kind") == "consistency"]


def _select(results, experiment=None, strategy=None):
    rows = _runs(results)
    if experiment is not None:
        rows = [r for r in rows if r.get("experiment") == experiment]
    if strategy is not None:
        rows = [r for r in rows if r.get("strategy") == strategy]
    return rows


def _median(rows, field):
    values = [float(r[field]) for r in rows if isinstance(r.get(field), (int, float))]
    return statistics.median(values) if values else None


def _stale_fraction(results, strategy):
    return _median([r for r in _consistency(results) if r.get("strategy") == strategy], "stale_fraction")


def _meets(rows, contract):
    """Highest offered rate among runs satisfying the SLO, and whether any run breached it."""
    passing = [r for r in rows if _is_passing(r, contract)]
    rates = [float(r["requested_rate"]) for r in passing if isinstance(r.get("requested_rate"), (int, float))]
    return (max(rates) if rates else None, bool(rows) and len(passing) < len(rows))


def _is_passing(row, contract):
    p95, p99 = row.get("read_p95_ms"), row.get("read_p99_ms")
    err, dropped = row.get("error_rate"), row.get("dropped_iterations")
    if None in (p95, p99, err, dropped):
        return False
    return (p95 <= contract["p95_seconds"] * 1000 and p99 <= contract["p99_seconds"] * 1000
            and err <= contract["error_ratio"] and dropped == 0)


def _result(name, statement, criterion, verdict, evidence, caveats=""):
    assert verdict in VERDICTS, verdict
    return {"hypothesis": name, "statement": statement, "criterion": criterion,
            "verdict": verdict, "evidence": evidence, "caveats": caveats}


def _h1(results, slo):
    db = _median(_select(results, "E01", "db"), "read_p95_ms")
    cache = _median(_select(results, "E03", "aside"), "read_p95_ms")
    if db is None or cache is None or db <= 0:
        return _result("H1", "Warm cache cuts read p95 by ≥50% vs DB-only",
                       "E03 p95 ≤ 50% of E01 p95", "inconclusive",
                       {"db_only_p95_ms": db, "cache_p95_ms": cache})
    improvement = 1 - cache / db
    return _result("H1", "Warm cache cuts read p95 by ≥50% vs DB-only",
                   "E03 p95 ≤ 50% of E01 p95", "supported" if improvement >= .5 else "refuted",
                   {"db_only_p95_ms": db, "cache_p95_ms": cache, "improvement": improvement})


def _h2(results, slo):
    grid = [r for r in _runs(results) if r.get("experiment") in ("E20", "E21")]
    if grid:
        return _h2_grid(grid, slo)
    return _h2_classic(results, slo)


def _rate_summary(rows):
    return {"median_p95_ms": _median(rows, "read_p95_ms"),
            "median_p99_ms": _median(rows, "read_p99_ms"),
            "median_error_rate": _median(rows, "error_rate"),
            "dropped_iterations": _median(rows, "dropped_iterations")}


def _is_summary_passing(summary, contract):
    if None in (summary["median_p95_ms"], summary["median_p99_ms"],
                summary["median_error_rate"], summary["dropped_iterations"]):
        return False
    return (summary["median_p95_ms"] <= contract["p95_seconds"] * 1000
            and summary["median_p99_ms"] <= contract["p99_seconds"] * 1000
            and summary["median_error_rate"] <= contract["error_ratio"]
            and summary["dropped_iterations"] == 0)


def _h2_grid(grid, slo):
    """Aggregate E20/E21 capacity grids per (strategy, rate), medians over repetitions."""
    normal = slo["normal"]
    groups = {}
    for r in grid:
        s, rate = r.get("strategy"), r.get("requested_rate")
        if not s or not isinstance(rate, (int, float)):
            continue
        groups.setdefault(s, {}).setdefault(rate, []).append(r)
    per_strategy = {}
    for s, rates in groups.items():
        details = {}
        passing = []
        for rate in sorted(rates):
            rep_rows = rates[rate]
            summary = _rate_summary(rep_rows)
            pass_fraction = (sum(1 for r in rep_rows if _is_passing(r, normal)) / len(rep_rows)
                             if rep_rows else 0.0)
            passed = pass_fraction >= 0.8 and _is_summary_passing(summary, normal)
            details[rate] = {"repetitions": len(rep_rows), "pass_fraction": pass_fraction,
                             "median_p95_ms": summary["median_p95_ms"],
                             "median_error_rate": summary["median_error_rate"],
                             "passes": passed}
            if passed:
                passing.append(rate)
        per_strategy[s] = {"passing_rate": max(passing) if passing else None, "rates": details}
    db = per_strategy.get("db", {})
    cached = {s: info for s, info in per_strategy.items() if s != "db"}
    ratios = {}
    if db.get("passing_rate"):
        for s, info in cached.items():
            if info.get("passing_rate"):
                ratios[s] = info["passing_rate"] / db["passing_rate"]
    best = max(ratios.values()) if ratios else None
    evidence = {
        "per_strategy_passing_rate": {s: info["passing_rate"] for s, info in per_strategy.items()},
        "pass_fraction_by_arm": {f"{s}@{r}": round(d["pass_fraction"], 3)
                                 for s, info in per_strategy.items() for r, d in info["rates"].items()},
        "median_p95_ms_by_arm": {f"{s}@{r}": d["median_p95_ms"]
                                 for s, info in per_strategy.items() for r, d in info["rates"].items()},
        "ratios_vs_db": ratios, "best_cached_ratio": best, "db_passing_rate": db.get("passing_rate"),
    }
    if not cached or best is None:
        return _result("H2", "Cached mode sustains ≥2× DB-only traffic before SLO violations",
                       "cached passing rate ≥ 2 × db passing rate at ≥0.8 rep pass-fraction",
                       "inconclusive", evidence,
                       "No passing rate observed for a cached arm and the db arm at the same grid")
    return _result("H2", "Cached mode sustains ≥2× DB-only traffic before SLO violations",
                   "cached passing rate ≥ 2 × db passing rate at ≥0.8 rep pass-fraction",
                   "supported" if best >= 2 else "refuted", evidence)


def _h2_classic(results, slo):
    db = [r for r in _runs(results) if r.get("strategy") == "db"]
    cache = [r for r in _runs(results) if r.get("strategy") == "aside"]
    db_rates, db_breached = _meets(db, slo["normal"])
    cache_rates, cache_breached = _meets(cache, slo["normal"])
    distinct = lambda rows: len({r.get("requested_rate") for r in rows if r.get("requested_rate")})
    evidence = {"db_only_passing_rate": db_rates, "cache_passing_rate": cache_rates,
                "db_only_breached": db_breached, "cache_breached": cache_breached,
                "distinct_db_rates": distinct(db), "distinct_cache_rates": distinct(cache)}
    if distinct(db) < 3 or distinct(cache) < 3:
        return _result("H2", "Cached mode sustains ≥2× DB-only traffic before SLO violations",
                       "cache passing rate ≥ 2 × db passing rate across a rate sweep",
                       "inconclusive", evidence,
                       "Needs a capacity sweep: ./poc campaign --profile sweep")
    if db_rates is None or cache_rates is None:
        return _result("H2", "Cached mode sustains ≥2× DB-only traffic before SLO violations",
                       "cache passing rate ≥ 2 × db passing rate across a rate sweep",
                       "inconclusive", evidence, "No passing rate observed for one arm")
    ratio = cache_rates / db_rates
    evidence["ratio"] = ratio
    return _result("H2", "Cached mode sustains ≥2× DB-only traffic before SLO violations",
                   "cache passing rate ≥ 2 × db passing rate across a rate sweep",
                   "supported" if ratio >= 2 else "refuted", evidence)


def _h3(results, slo):
    db, cache = _select(results, "E01", "db"), _select(results, "E03", "aside")
    dig = lambda rows: _ratio(_median(rows, "server_db_reads"), _median(rows, "requests"))
    baseline, cached = dig(db), dig(cache)
    hit = _median(cache, "client_cache_hit_ratio")
    evidence = {"db_reads_per_request_db_only": baseline, "db_reads_per_request_cache": cached,
                "l2_hit_ratio": hit}
    if baseline in (None, 0) or cached is None:
        return _result("H3", "≥80% observed L2 hit ratio cuts DB read operations ≥70%",
                       "server DB reads/request reduced ≥70% at L2 hit ≥80%", "inconclusive", evidence)
    reduction = 1 - cached / baseline
    evidence["reduction"] = reduction
    verdict = "supported" if reduction >= .7 and hit is not None and hit >= .8 else "refuted"
    caveats = "" if hit is not None and hit >= .8 else "L2 hit ratio below 80% or unknown; the premise is unmet"
    return _result("H3", "≥80% observed L2 hit ratio cuts DB read operations ≥70%",
                   "server DB reads/request reduced ≥70% at L2 hit ≥80%", verdict, evidence, caveats)


def _ratio(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _h4(results, slo):
    rows = [r for r in _select(results, "E14", "aside") if isinstance(r.get("replicas"), int)]
    counts = sorted({r["replicas"] for r in rows})
    per_replica = {count: _ratio(_median([r for r in rows if r["replicas"] == count], "server_db_reads"),
                                 _median([r for r in rows if r["replicas"] == count], "requests"))
                   for count in counts}
    values = [v for v in per_replica.values() if v is not None]
    evidence = {"db_reads_per_request_by_replica": per_replica}
    if len(values) < 3:
        return _result("H4", "Shared cache keeps DB work from growing with replica count",
                       "DB reads/request stay within 1.5× from 1 to 4 replicas", "inconclusive", evidence,
                       "Needs E14 at 1/2/4 replicas and a DB-only multi-replica baseline")
    growth = max(values) / min(values)
    evidence["growth"] = growth
    return _result("H4", "Shared cache keeps DB work from growing with replica count",
                   "DB reads/request stay within 1.5× from 1 to 4 replicas",
                   "supported" if growth <= 1.5 else "refuted", evidence,
                   "No DB-only multi-replica baseline in this dataset")


def _h5(results, slo):
    # Prefer the resilience grid (E22): per-run error rate inside the fault window.
    window_rows = [r for r in _runs(results)
                   if r.get("experiment") == "E22" and r.get("fault_window_error_rate") is not None]
    evidence = {"degraded_error_ratio": slo["degraded"]["error_ratio"]}
    if window_rows:
        worst = max(r["fault_window_error_rate"] for r in window_rows)
        recoveries = [r.get("recovery_observed_seconds") for r in window_rows]
        evidence.update({
            "worst_fault_window_error_rate": worst,
            "recovery_observed_seconds": [v for v in recoveries if v is not None],
            "recovery_violations": sum(1 for v in recoveries if v is None),
            "recovery_deadline_seconds": slo["recovery_seconds"],
            "runs": len(window_rows),
        })
        verdict = "supported" if worst <= slo["degraded"]["error_ratio"] else "refuted"
        return _result("H5", "A Redis outage does not fail every request",
                       "fault-window read error rate ≤ degraded budget (E22)",
                       verdict, evidence,
                       "Fault-window rates come from k6 samples in [fault_at, recover_at); "
                       "recovery deadlines are reported alongside, not folded into the verdict")
    # Fallback: the whole-run E10 window (includes the fault plus recovery).
    rows = _select(results, "E10", "invalidate")
    err, dropped = _median(rows, "error_rate"), _median(rows, "dropped_iterations")
    evidence.update(error_rate=err, dropped_iterations=dropped)
    if err is None:
        return _result("H5", "A Redis outage does not fail every request", "error rate ≤ degraded budget",
                       "inconclusive", evidence)
    verdict = "supported" if err <= slo["degraded"]["error_ratio"] else "refuted"
    caveats = "Whole-run window includes the fault; use the dashboard fault window for the interval"
    return _result("H5", "A Redis outage does not fail every request", "error rate ≤ degraded budget",
                   verdict, evidence, caveats)


def _h6(results, slo):
    evidence, comparisons = {}, []
    for label, unsafe, safe in (("E12", "E12a", "E12b"), ("E16", "E16a", "E16b")):
        aside = _median(_select(results, unsafe), "server_db_reads")
        protected = _median(_select(results, safe), "server_db_reads")
        evidence[f"{label}_aside_db_reads"] = aside
        evidence[f"{label}_protected_db_reads"] = protected
        if aside is not None and protected is not None:
            comparisons.append(protected < aside)
    if not comparisons:
        return _result("H6", "Distributed single-flight reduces redundant DB reads after a flush",
                       "protected DB reads < aside DB reads for matched flush runs", "inconclusive", evidence)
    return _result("H6", "Distributed single-flight reduces redundant DB reads after a flush",
                   "protected DB reads < aside DB reads for matched flush runs",
                   "supported" if all(comparisons) else "refuted", evidence)


def _h7(results, slo):
    aside = _stale_fraction(results, "aside")
    fractions = {mode: _stale_fraction(results, mode) for mode in WRITE_AWARE}
    evidence = {"aside_stale_fraction": aside, **{f"{m}_stale_fraction": v for m, v in fractions.items()}}
    known = [v for v in fractions.values() if v is not None]
    if aside is None or not known:
        return _result("H7", "Write-aware strategies reduce stale reads vs basic aside",
                       "every write-aware stale fraction < aside stale fraction", "inconclusive", evidence)
    if aside == 0:
        return _result("H7", "Write-aware strategies reduce stale reads vs basic aside",
                       "every write-aware stale fraction < aside stale fraction", "inconclusive", evidence,
                       "No aside staleness was observed, so the comparison is uninformative")
    verdict = "supported" if max(known) < aside else "refuted"
    return _result("H7", "Write-aware strategies reduce stale reads vs basic aside",
                   "every write-aware stale fraction < aside stale fraction", verdict, evidence)


def _h8(results, slo):
    aside = _median(_select(results, "E18a", "aside"), "read_p95_ms")
    l1l2 = _median(_select(results, "E18b", "l1l2"), "read_p95_ms")
    stale = _stale_fraction(results, "l1l2")
    replica = _median([r for r in _consistency(results) if r.get("strategy") == "l1l2"], "stale_replica_probes")
    evidence = {"l2_only_p95_ms": aside, "l1l2_p95_ms": l1l2, "l1l2_stale_fraction": stale,
                "l1l2_stale_replica_probes": replica}
    if aside is None or l1l2 is None:
        return _result("H8", "L1 improves latency but can serve replica-stale data",
                       "l1l2 p95 < L2-only p95 AND cross-replica staleness observed", "inconclusive", evidence,
                       "Needs E18a/E18b and E17 l1l2")
    latency_improved = l1l2 < aside
    evidence["latency_improved"] = latency_improved
    stale_observed = (stale or 0) > 0 or (replica or 0) > 0
    if latency_improved and stale_observed:
        verdict = "supported"
    elif latency_improved:
        verdict = "inconclusive"
        return _result("H8", "L1 improves latency but can serve replica-stale data",
                       "l1l2 p95 < L2-only p95 AND cross-replica staleness observed", verdict, evidence,
                       "Latency improved but no cross-replica staleness was observed; run E17 l1l2")
    else:
        verdict = "refuted"
    return _result("H8", "L1 improves latency but can serve replica-stale data",
                   "l1l2 p95 < L2-only p95 AND cross-replica staleness observed", verdict, evidence)


def verdicts(results, slo=None):
    slo = slo or load_slo()
    return [_h1(results, slo), _h2(results, slo), _h3(results, slo), _h4(results, slo),
            _h5(results, slo), _h6(results, slo), _h7(results, slo), _h8(results, slo)]


def markdown(rows):
    lines = ["| Hypothesis | Verdict | Evidence | Criterion |", "|---|---|---|---|"]
    order = {"supported": 0, "refuted": 1, "inconclusive": 2}
    for row in sorted(rows, key=lambda r: order[r["verdict"]]):
        evidence = "; ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}"
                             for k, v in row["evidence"].items())
        lines.append(f"| {row['hypothesis']} — {row['statement']} | **{row['verdict']}** | {evidence} "
                     f"| {row['criterion']} |")
    return lines


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: hypotheses.py <results-directory>")
    results = json.loads((Path(sys.argv[1]) / "campaign.json").read_text())
    rows = verdicts(results)
    (Path(sys.argv[1]) / "hypotheses.json").write_text(json.dumps(rows, indent=2) + "\n")
    print("\n".join(markdown(rows)))


if __name__ == "__main__":
    main()
