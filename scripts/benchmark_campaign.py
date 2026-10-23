#!/usr/bin/env python3
"""Orchestrate the reproducible E01–E22 comparative/failure/consistency campaigns.

Profiles: ``full``, ``smoke``, ``sweep`` (E20 capacity sweep), ``capacity`` (E21
strategy×rate×repetition grid) and ``resilience`` (E22 explicit-timed Redis fault).
Requires Docker Compose and a reachable local port 8080. Dependencies: stdlib only.
See docs/EXPERIMENTS.md. Experiment results are measured, never synthetic.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
from functools import lru_cache
import gzip
import json
import hashlib
import math
import os
import platform
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from telemetry import RunTelemetry, load_slo
from hypotheses import markdown as hypothesis_markdown, verdicts as hypothesis_verdicts

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[dict] = []
ONLY_IDS: set[str] = set()
MODES = ("db", "aside", "invalidate", "write_through", "protected", "l1l2", "sharded")
FAULT_ALIASES = {"redis-stop": "stop_redis", "redis-restart": "restart_redis", "flush": "flush"}


@lru_cache(maxsize=None)
def service_url(service):
    """Use Compose's resolved ports, including .env and shell overrides."""
    config = json.loads(subprocess.check_output(
        ["docker", "compose", "config", "--format", "json"], cwd=ROOT, text=True))
    port = config["services"][service]["ports"][0]["published"]
    return f"http://127.0.0.1:{port}"


def enabled(experiment):
    return not ONLY_IDS or any(experiment == item or experiment.startswith(item) for item in ONLY_IDS)


def cmd(*args, check=True, log=None, env=None):
    argv = list(map(str, args))
    print("$", " ".join(argv), flush=True)
    if log:
        with Path(log).open("w") as output:
            return subprocess.run(argv, cwd=ROOT, env=env, stdout=output,
                                  stderr=subprocess.STDOUT, check=check)
    return subprocess.run(argv, cwd=ROOT, env=env, check=check)


def compose(*args, **kwargs):
    return cmd("docker", "compose", *args, **kwargs)


def http_json(path, method="GET", payload=None, timeout=20):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(service_url("gateway") + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def ensure_healthy(timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if http_json("/health", timeout=3)["status"] == "ok":
                return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"API not responding on {service_url('gateway')} after {timeout}s")


def flush():
    for service in ("redis1", "redis2"):
        compose("exec", "-T", service, "redis-cli", "FLUSHDB")


def prime(hot=1, times=30):
    for _ in range(times):
        http_json(f"/products/{hot}/snapshot", timeout=30)


def configure(strategy="aside", ttl=30, l1_ttl=1, replicas=2, db_extra_ms=0):
    """Recreate *only* app replicas: DB/cache volumes/monitoring remain running."""
    env = os.environ.copy()
    env.update(CACHE_MODE=strategy, CACHE_TTL_SEC=str(ttl),
               L1_TTL_SEC=str(l1_ttl), DB_QUERY_EXTRA_MS=str(db_extra_ms))
    print(f"\nCONFIG mode={strategy} ttl={ttl} l1={l1_ttl} "
          f"apps={replicas} db_extra_ms={db_extra_ms}", flush=True)
    cmd("docker", "compose", "up", "-d", "--no-deps", "--no-build", "--force-recreate",
        "--scale", f"app={replicas}", "app", env=env)
    ensure_healthy()
    # Docker embedded DNS and Prometheus discovery are refreshed every ~5s.
    time.sleep(6)


def prom_query(query, timestamp):
    address = service_url("prometheus") + "/api/v1/query?" + urllib.parse.urlencode({"query": query, "time": timestamp})
    try:
        with urllib.request.urlopen(address, timeout=8) as response:
            result = json.load(response)
        series = result["data"]["result"]
        return float(series[0]["value"][1]) if series else None
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return None


def metric(summary, name, field, default=None):
    entry = summary.get("metrics", {}).get(name, {})
    values = entry.get("values", entry)
    # --summary-export uses legacy flattened metrics and calls Rate.rate 'value'.
    # handleSummary uses nested values with a 'rate' field instead.
    if field == "rate" and field not in values:
        return values.get("value", default)
    return values.get(field, default)


def cache_lookup_query(result, window):
    return f'sum(increase(cache_lookup_total{{layer="l2",result="{result}"}}[{window}s]))'


def _sleep_until(deadline):
    """Block until monotonic `deadline`; used for explicit fault timing."""
    while time.monotonic() < deadline:
        time.sleep(0.25)


def _wait_for_k6_window(logfile):
    """Wait until the k6 log reports its run window (alignment for --fault-at)."""
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        try:
            text = Path(logfile).read_text(errors="replace")
        except OSError:
            text = ""
        if "running (" in text:
            return
        time.sleep(0.25)
    raise RuntimeError(f"k6 never entered its run window: {logfile}")


def _read_samples(samples_path):
    """Parse gzipped k6 JSON samples into (time_since_first, metric, value) rows."""
    first, rows = None, []
    try:
        opener = gzip.open(samples_path, "rt")
    except OSError:
        return []
    with opener:
        for line in opener:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") != "Point":
                continue
            tags = obj.get("data", {}).get("tags", {}) or {}
            if tags.get("operation") != "read":
                continue
            metric = obj.get("metric")
            if metric not in ("http_req_duration", "http_req_failed"):
                continue
            try:
                ts = dt.datetime.fromisoformat(obj["data"]["time"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError, TypeError):
                continue
            if first is None:
                first = ts
            rows.append((ts - first, metric, float(obj["data"]["value"])))
    return sorted(rows)


def fault_window_stats(samples_path, fault_at, recover_at, degraded_p95_ms):
    """Resilience evidence from raw k6 samples for one explicit-timed run.

    - fault_error_rate: read failure rate inside [fault_at, recover_at).
    - outside_error_rate: read failure rate before the fault and after recovery.
    - recovery_observed_seconds: first second s after recover_at whose 10-second
      read-p95 window stays <= degraded_p95_ms; None means the budget was not re-met.
    """
    rows = _read_samples(samples_path)
    if not rows:
        return None
    errors = [(t, v) for t, m, v in rows if m == "http_req_failed"]
    durations = [(t, v) for t, m, v in rows if m == "http_req_duration"]
    windowed = lambda lo, hi: [v for t, v in errors if t >= lo and (hi is None or t < hi)]
    fe, fc = sum(windowed(fault_at, recover_at)), len(windowed(fault_at, recover_at))
    be, bc = sum(windowed(0, fault_at)), len(windowed(0, fault_at))
    ae, ac = sum(windowed(recover_at, None)), len(windowed(recover_at, None))
    fault_error = fe / fc if fc else None
    outside_error = (be + ae) / (bc + ac) if (bc + ac) else None
    end = max((t for t, _ in durations), default=0)
    recovery = None
    horizon = int(end) - int(recover_at)
    for offset in range(0, horizon + 1):
        window = [v for t, v in durations if recover_at + offset <= t < recover_at + offset + 10]
        if len(window) < 10:
            continue
        window.sort()
        p95 = window[min(len(window) - 1, int(0.95 * len(window)))]
        if p95 <= degraded_p95_ms:
            recovery = offset
            break
    return {"fault_error_rate": fault_error, "outside_error_rate": outside_error,
            "recovery_observed_seconds": recovery}


def fault_action(fault):
    print(f"\n*** FAULT INJECTION: {fault} ***\n", flush=True)
    if fault == "flush":
        flush()
    elif fault == "stop_redis":
        compose("stop", "redis1")
    elif fault == "restart_redis":
        compose("restart", "redis1")
    else:
        raise ValueError(f"unknown fault {fault}")


def run_bench(run_id, description, **kwargs):
    if not enabled(run_id):
        return None
    with RunTelemetry(run_id, kwargs.get("strategy", "aside"), kwargs["outdir"], service_url,
                      rate=kwargs["rate"], replicas=kwargs.get("replicas", 2),
                      writes=kwargs.get("writes", 0), hot=kwargs.get("hot", .9)) as monitor:
        row = _run_bench(run_id, description, monitor=monitor, **kwargs)
    row.update(monitor.summary())
    save_reports(kwargs["outdir"])
    return row


def _run_bench(run_id, description, *, rate, duration, strategy="aside", ttl=30,
              l1_ttl=1, replicas=2, db_extra_ms=0, writes=0, hot=0.90,
              keyspace=100, cold=False, warm=False, fault=None, pre_vus=120,
              max_vus=500, outdir=None, monitor, repetition=1, fault_at=None,
              recover_at=None):
    if not enabled(run_id):
        return None
    configure(strategy, ttl, l1_ttl, replicas, db_extra_ms)
    # Each case starts from a known shared-cache state; app recreation clears L1.
    flush()
    if warm:
        prime(times=40)
    slug = f"{len(RESULTS)+1:03d}_{run_id}_{strategy}"
    dest = Path(outdir) / (slug + ".json")
    logfile = Path(outdir) / (slug + ".log")
    # k6 duration includes 's'; slow systems may require higher VU limits.
    envs = {
        "TARGET_URL": "http://gateway:8080", "RATE": str(rate),
        "DURATION": f"{duration}s", "WRITE_PERCENT": str(writes),
        "HOT_PERCENT": str(hot), "KEYSPACE": str(keyspace),
        "PRE_VUS": str(pre_vus), "MAX_VUS": str(max_vus),
        "CACHE_STRATEGY": strategy,
        "K6_PROMETHEUS_RW_SERVER_URL": "http://prometheus:9090/api/v1/write",
        "K6_PROMETHEUS_RW_TREND_AS_NATIVE_HISTOGRAM": "true",
        "K6_PROMETHEUS_RW_PUSH_INTERVAL": "2s",
        "K6_PROMETHEUS_RW_STALE_MARKERS": "true",
    }
    argv = ["docker", "compose", "run", "-T", "--rm", "--no-deps"]
    for key, value in envs.items():
        argv += ["-e", f"{key}={value}"]
    in_container = "/results/" + dest.relative_to(ROOT / "results").as_posix()
    argv += ["k6", "run", "--out", "experimental-prometheus-rw", "--out", "json=" + in_container.removesuffix(".json") + "_samples.json.gz",
             "--summary-export", in_container, "/benchmarks/load.js"]
    print(f"\nRUN {slug}: {description} @ {rate}/s for {duration}s", flush=True)
    start = time.time()
    monitor.marker("measurement", "Start k6 measurement (container startup precedes first request)")
    with logfile.open("w") as f:
        proc = subprocess.Popen(argv, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)
        try:
            if fault:
                if fault_at is None:
                    # Legacy fraction-based timing (full/smoke profiles): give the
                    # cache time to warm before breaking it.
                    time.sleep(max(2.0, duration * 0.35))
                    monitor.marker("fault", f"Begin {fault}")
                    fault_action(fault)
                    if fault == "stop_redis":
                        time.sleep(max(2.0, duration * 0.30))
                        compose("up", "-d", "redis1")
                    monitor.marker("recovery", f"{fault} intervention finished; observe recovery")
                else:
                    # Explicit timing (resilience profile): align to k6's own run
                    # window so --fault-at / --recover-at are relative to the load.
                    _wait_for_k6_window(logfile)
                    window_start = time.monotonic()
                    _sleep_until(window_start + fault_at)
                    monitor.marker("fault", f"Begin {fault} at {fault_at}s of the run window")
                    fault_action(fault)
                    if recover_at is not None:
                        _sleep_until(window_start + recover_at)
                        if fault == "stop_redis":
                            compose("up", "-d", "redis1")
                        monitor.marker("recovery", f"{fault} restored at {recover_at}s; observe recovery")
            rc = proc.wait(timeout=duration + 90)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            if fault == "stop_redis":
                compose("up", "-d", "redis1")
    end = time.time()
    if rc != 0:
        print(f"WARNING: k6 return code {rc}; consult {logfile}", file=sys.stderr)
    summary = {}
    if dest.exists():
        with dest.open() as f:
            summary = json.load(f)
    # Cover the measured process window, excluding pre-run cache priming.
    # Scrape sampling and increase() extrapolation still make these estimates.
    window = max(1, math.ceil(end - start))
    lookup_hits = prom_query(cache_lookup_query("hit", window), end)
    lookup_misses = prom_query(cache_lookup_query("miss", window), end)
    db_reads = prom_query(f'sum(increase(db_queries_total{{operation="read_pulse"}}[{window}s]))', end)
    offered = metric(summary, "http_reqs", "count")
    dropped = metric(summary, "dropped_iterations", "count", 0 if offered is not None else None)
    fault_stats = None
    if fault_at is not None and recover_at is not None:
        samples_path = Path(outdir) / (dest.stem + "_samples.json.gz")
        if samples_path.exists():
            degraded_p95_ms = load_slo()["degraded"]["p95_seconds"] * 1000
            fault_stats = fault_window_stats(samples_path, fault_at, recover_at, degraded_p95_ms)
            if fault_stats is None:
                print("WARNING: no readable k6 read samples; fault-window evidence unavailable",
                      file=sys.stderr)
    row = {
        "experiment": run_id, "description": description, "strategy": strategy,
        "ttl_s": ttl, "l1_ttl_s": l1_ttl, "replicas": replicas,
        "db_delay_ms": db_extra_ms, "requested_rate": rate,
        "duration_s": duration, "writes_fraction": writes, "hot_fraction": hot,
        "cache_temperature": "hot_product_warm" if warm else "cold",
        "keyspace": keyspace, "fault": fault or "", "repetition": repetition,
        "fault_at_s": fault_at, "recover_at_s": recover_at,
        "fault_window_error_rate": fault_stats["fault_error_rate"] if fault_stats else None,
        "outside_error_rate": fault_stats["outside_error_rate"] if fault_stats else None,
        "recovery_observed_seconds": fault_stats["recovery_observed_seconds"] if fault_stats else None,
        "requests": offered,
        "achieved_rps": metric(summary, "http_reqs", "rate"),
        "http_p95_ms": metric(summary, "http_req_duration", "p(95)"),
        "read_p95_ms": metric(summary, "read_latency_ms", "p(95)"),
        "read_p99_ms": metric(summary, "read_latency_ms", "p(99)"),
        "error_rate": metric(summary, "http_req_failed", "rate"),
        "dropped_iterations": dropped,
        "client_cache_hit_ratio": metric(summary, "client_cache_hit_ratio", "rate"),
        "server_l2_hits": lookup_hits, "server_l2_misses": lookup_misses,
        "server_db_reads": db_reads,
        "server_l2_hit_ratio": (lookup_hits / (lookup_hits + lookup_misses)
                                 if lookup_hits is not None and lookup_misses is not None
                                 and lookup_hits + lookup_misses > 0 else None),
        "raw_json": dest.name if dest.exists() else "", "log": logfile.name,
        "k6_exit_code": rc, "ended_utc": dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat(),
        "samples_file": dest.stem + "_samples.json.gz",
    }
    RESULTS.append(row)
    print(f"RESULT achieved={row['achieved_rps']}rps read_p95={row['read_p95_ms']}ms "
          f"errors={row['error_rate']} hits={row['client_cache_hit_ratio']} "
          f"dropped={dropped}", flush=True)
    save_reports(outdir)
    return row


def consistency(experiment, strategy, *, outdir, ttl=30):
    if not enabled(experiment):
        return None
    with RunTelemetry(experiment, strategy, outdir, service_url) as monitor:
        _consistency(experiment, strategy, outdir=outdir, ttl=ttl, monitor=monitor)
    RESULTS[-1].update(monitor.summary())
    save_reports(outdir)


def _consistency(experiment, strategy, *, outdir, ttl, monitor):
    configure(strategy, ttl=ttl, l1_ttl=3, replicas=2)
    flush()
    prime(times=60)
    if strategy == "l1l2":
        # Warm *both* replica-local L1 instances, not just whichever Nginx picks.
        # Compose indices start at 1 for --scale app=2.
        for replica_index in (1, 2):
            cmd("docker", "compose", "exec", "-T", "--index", str(replica_index),
                "app", "curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:8000/products/1/snapshot")
    before = http_json("/products/1/revision")["revision"]
    monitor.marker("consistency", "Commit activity, then probe gateway and replica freshness")
    updated = http_json("/products/1/activity", method="POST", payload={"kind": "purchase"})["revision"]
    committed = time.monotonic()
    assert updated == before + 1, (updated, before)
    # Direct probes expose cross-replica L1 staleness even if the load balancer
    # happens to route the next 60 requests to one container.
    replica_observed = {}
    if strategy == "l1l2":
        for replica_index in (1, 2):
            output = subprocess.check_output(
                ["docker", "compose", "exec", "-T", "--index", str(replica_index),
                 "app", "curl", "-fsS", "http://127.0.0.1:8000/products/1/snapshot"],
                cwd=ROOT, text=True, timeout=40
            )
            replica_observed[f"app_{replica_index}"] = int(json.loads(output)["revision"])
    observations = [(http_json("/products/1/snapshot")["revision"], time.monotonic() - committed) for _ in range(60)]
    observed = [revision for revision, _ in observations]
    stale = sum(rev < updated for rev in observed)
    row = {"experiment": experiment, "strategy": strategy, "kind": "consistency",
           "before_revision": before, "after_revision": updated,
           "reads": len(observed), "stale_reads": stale,
           "stale_fraction": stale / len(observed), "observed_revisions": sorted(set(observed)),
           "replica_revisions_after_write": replica_observed,
           "stale_replica_probes": sum(v < updated for v in replica_observed.values()),
           "last_stale_observed_seconds": max((elapsed for rev, elapsed in observations if rev < updated), default=None),
           "observation_window_seconds": observations[-1][1]}
    # Last stale observation is a lower bound, not time-to-freshness or a proof
    # that the freshness contract holds beyond this short sequential probe.
    monitor.probes.update(orderflow_probe_stale_fraction=row["stale_fraction"],
                          orderflow_probe_window_seconds=row["observation_window_seconds"],
                          orderflow_probe_stale_replica_count=row["stale_replica_probes"])
    if row["last_stale_observed_seconds"] is not None:
        monitor.probes["orderflow_probe_last_stale_seconds"] = row["last_stale_observed_seconds"]
    monitor.push()
    RESULTS.append(row)
    (Path(outdir) / f"{experiment}_{strategy}_consistency.json").write_text(json.dumps(row, indent=2))
    print(f"CONSISTENCY {strategy}: {stale}/{len(observed)} stale, observed {set(observed)}", flush=True)
    save_reports(outdir)


def save_reports(outdir):
    outdir = Path(outdir)
    (outdir / "campaign.json").write_text(json.dumps(RESULTS, indent=2))
    keys = ["experiment", "description", "strategy", "ttl_s", "l1_ttl_s", "replicas", "cache_temperature",
            "db_delay_ms", "requested_rate", "duration_s", "writes_fraction", "hot_fraction",
            "keyspace", "fault", "repetition", "fault_at_s", "recover_at_s",
            "fault_window_error_rate", "outside_error_rate", "recovery_observed_seconds",
            "requests", "achieved_rps", "http_p95_ms", "read_p95_ms",
            "read_p99_ms", "error_rate", "dropped_iterations", "client_cache_hit_ratio",
            "server_l2_hits", "server_l2_misses", "server_db_reads", "server_l2_hit_ratio",
            "stale_reads", "stale_fraction", "raw_json", "samples_file", "log", "k6_exit_code", "ended_utc",
            "hypotheses", "dashboard_url", "telemetry_file", "telemetry_complete"]
    with (outdir / "comparison.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(RESULTS)
    lines = ["# OrderFlow Rust benchmark campaign — measured results", "",
             "> Results are **not** claims of maximum capacity. RPS is achieved throughput, not the target. "
             "Look at dropped iterations and errors before comparing p95.", "",
             "| Experiment | Mode | Offered/s | Achieved/s | Read p95 ms | p99 ms | Errors | Hit rate | Dropped |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    def fmt(value, digits=1, multiplier=1):
        return "—" if value is None else f"{float(value)*multiplier:.{digits}f}"
    for r in RESULTS:
        if r.get("kind") == "consistency":
            lines.append(f"| {r['experiment']} stale {r['stale_reads']}/{r['reads']} "
                         f"| {r['strategy']} | — | — | — | — | — | — | — |")
        else:
            lines.append("| {experiment} | {strategy} | {requested_rate} | {achieved} | {p95} "
                         "| {p99} | {err}% | {hit}% | {drop} |".format(
                             **r, achieved=fmt(r.get("achieved_rps")),
                             p95=fmt(r.get("read_p95_ms")), p99=fmt(r.get("read_p99_ms")),
                             err=fmt(r.get("error_rate"), 2, 100),
                             hit=fmt(r.get("client_cache_hit_ratio"), 1, 100),
                              drop=fmt(r.get("dropped_iterations"), 0)))
    links = [f"- {r['experiment']} / {r['strategy']}: [golden signals]({r['dashboard_url']}) — telemetry complete: {r.get('telemetry_complete')}"
             for r in RESULTS if r.get("dashboard_url")]
    lines += ["", "## Dashboard time windows", "", *links, "", "## Interpret correctly", "",
              "- All rows use a shared Postgres data set. Write experiments mutate data but do not change its size dramatically.",
              "- For fairness, match arrival rates, replica counts, distributions, hardware and cache temperature.",
              "- An 'L2 hit ratio' from Prometheus can be unavailable for very short runs or exporter/scrape failures.",
              "- DB-only is costly intentionally: it recomputes a plausible flash-sale product activity aggregation.",
              "- A low p95 during dropped iterations is NOT evidence of adequate throughput.",
              "- Review raw k6 summaries and fault logs before accepting architectural hypotheses.", ""]
    verdict_rows = hypothesis_verdicts(RESULTS)
    (outdir / "hypotheses.json").write_text(json.dumps(verdict_rows, indent=2) + "\n")
    lines += ["", "## Hypothesis verdicts", "",
              "> Derived from the runs in this directory against `benchmarks/slo.json`. "
              "Missing data yields `inconclusive`, never a pass.", "",
              *hypothesis_markdown(verdict_rows), ""]
    (outdir / "REPORT.md").write_text("\n".join(lines))


def full(args, outdir):
    d, r = args.duration, args.rate
    common = dict(rate=r, duration=d, outdir=outdir, pre_vus=args.vus, max_vus=args.max_vus)
    # E01: reference point. E02/E03: cold/warm.
    run_bench("E01", "DB-only baseline", strategy="db", **common)
    run_bench("E02", "Cold cache-aside", strategy="aside", cold=True, **common)
    run_bench("E03", "Warm cache-aside", strategy="aside", warm=True, **common)
    for experiment, ttl in (("E04", 1), ("E05", 10), ("E06", 60)):
        run_bench(experiment, f"TTL sensitivity {ttl}s", strategy="aside", ttl=ttl, cold=True, **common)
    # Note: HOT_PERCENT is input skew, NOT guaranteed/cache-hit ratio.
    for experiment, hot in (("E07", .50), ("E08", .80), ("E09", .95)):
        run_bench(experiment, f"Access locality {hot:.0%} hot", strategy="aside",
                  hot=hot, keyspace=10000, cold=True, **common)
    run_bench("E10", "Redis stop and recover", strategy="invalidate", warm=True,
              fault="stop_redis", **common)
    run_bench("E11", "Redis restart", strategy="invalidate", warm=True,
              fault="restart_redis", **common)
    run_bench("E12a", "Cache flush without coalescing", strategy="aside", warm=True,
              hot=1.0, fault="flush", **common)
    run_bench("E12b", "Cache flush WITH distributed coalescing", strategy="protected", warm=True,
              hot=1.0, fault="flush", **common)
    run_bench("E13a", "DB slowdown, no cache", strategy="db", db_extra_ms=35, **common)
    run_bench("E13b", "DB slowdown, Redis protection", strategy="protected", db_extra_ms=35,
              warm=True, **common)
    for count in (1, 2, 4):
        run_bench("E14", f"App horizontal scaling {count} replicas", strategy="aside",
                  replicas=count, warm=True, **common)
    run_bench("E15a", "Single Redis, broad keyspace", strategy="aside", hot=.2,
              keyspace=300, warm=True, **common)
    run_bench("E15b", "Two-way sharded Redis, broad keyspace", strategy="sharded", hot=.2,
              keyspace=300, warm=True, **common)
    run_bench("E16a", "Stampede without distributed lock", strategy="aside", hot=1.0,
              fault="flush", **common)
    run_bench("E16b", "Stampede with distributed single-flight", strategy="protected", hot=1.0,
              fault="flush", **common)
    for mode in ("aside", "invalidate", "write_through", "protected", "l1l2", "sharded", "db"):
        consistency("E17", mode, outdir=outdir)
    run_bench("E18a", "Redis L2 alone", strategy="aside", warm=True, **common)
    run_bench("E18b", "Local L1 + Redis L2", strategy="l1l2", warm=True, **common)
    # Three workload scenarios (with the exact same hot-key skew).
    for writes, label in ((.05, "95/5"), (.20, "80/20"), (.50, "50/50")):
        for mode in ("db", "aside", "invalidate", "protected", "l1l2", "sharded", "write_through"):
            run_bench("E19", f"Read/write mix {label}", strategy=mode, writes=writes,
                      warm=True, **common)


def sweep(args, outdir):
    """Capacity sweep for H2: raise offered rate until each arm breaches the normal SLO."""
    common = dict(duration=args.duration, outdir=outdir, pre_vus=args.vus, max_vus=args.max_vus)
    rates = sorted({args.rate * factor for factor in (1, 2, 4, 8)})
    for strategy in ("db", "aside"):
        for rate in rates:
            run_bench("E20", f"Capacity sweep {strategy} @ {rate}/s", strategy=strategy,
                      rate=rate, warm=True, **common)


def capacity(args, outdir):
    """Capacity grid (E21, H2): strategies x rates x repetitions, all warm."""
    pre_vus = min(args.max_vus, max((*args.rates, args.vus)))
    common = dict(duration=args.duration, outdir=outdir, pre_vus=pre_vus,
                  max_vus=args.max_vus, writes=args.writes, hot=args.hot,
                  keyspace=args.keyspace, warm=True)
    for strategy in args.strategies:
        for rate in sorted(args.rates):
            for rep in range(1, args.repetitions + 1):
                run_bench("E21", f"Capacity {strategy} @ {rate}/s rep {rep}/{args.repetitions}",
                          strategy=strategy, rate=rate, repetition=rep, **common)


def resilience(args, outdir):
    """Resilience grid (E22, H5): strategies x rates x reps with explicit fault timing."""
    fault = FAULT_ALIASES[args.fault]
    common = dict(duration=args.duration, outdir=outdir, pre_vus=args.vus,
                  max_vus=args.max_vus, writes=args.writes, hot=args.hot,
                  keyspace=args.keyspace, warm=True, fault=fault,
                  fault_at=args.fault_at, recover_at=args.recover_at)
    for strategy in args.strategies:
        for rate in sorted(args.rates):
            for rep in range(1, args.repetitions + 1):
                run_bench("E22", f"Resilience {strategy} @ {rate}/s rep {rep}/{args.repetitions}",
                          strategy=strategy, rate=rate, repetition=rep, **common)


def smoke(args, outdir):
    common = dict(rate=args.rate, duration=args.duration, outdir=outdir,
                  pre_vus=args.vus, max_vus=args.max_vus)
    run_bench("E01", "DB-only baseline", strategy="db", **common)
    run_bench("E02", "Cold cache", strategy="aside", cold=True, **common)
    run_bench("E03", "Warm cache", strategy="aside", warm=True, **common)
    run_bench("E12a", "Flush without protection", strategy="aside", warm=True,
              hot=1.0, fault="flush", **common)
    run_bench("E12b", "Flush with protection", strategy="protected", warm=True,
              hot=1.0, fault="flush", **common)
    consistency("E17", "aside", outdir=outdir)
    consistency("E17", "invalidate", outdir=outdir)
    consistency("E17", "l1l2", outdir=outdir)
    run_bench("E18", "L1+L2", strategy="l1l2", warm=True, **common)
    run_bench("E10", "Redis outage and DB fallback", strategy="invalidate",
              warm=True, fault="stop_redis", **common)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", choices=("full", "smoke", "sweep", "capacity", "resilience"),
                        default="full")
    parser.add_argument("--rate", type=int, default=150, help="offered requests/s")
    parser.add_argument("--duration", type=int, default=25, help="seconds per load run")
    parser.add_argument("--vus", type=int, default=180)
    parser.add_argument("--max-vus", type=int, default=700)
    parser.add_argument("--strategies", nargs="+", choices=MODES, metavar="MODE",
                        help="grid strategies for capacity/resilience profiles")
    parser.add_argument("--rates", nargs="+", type=int, metavar="RPS",
                        help="grid offered rates for capacity/resilience profiles")
    parser.add_argument("--repetitions", type=int, default=1,
                        help="repeats per (strategy, rate) for grid profiles")
    parser.add_argument("--writes", type=float, default=0.0,
                        help="write fraction per run (grid profiles)")
    parser.add_argument("--hot", type=float, default=0.90,
                        help="hot-product fraction for grid profiles")
    parser.add_argument("--keyspace", type=int, default=100,
                        help="keyspace for grid profiles")
    parser.add_argument("--fault", choices=tuple(FAULT_ALIASES), metavar="FAULT",
                        help="redis-stop, redis-restart, or flush (resilience profile)")
    parser.add_argument("--fault-at", type=int, metavar="SECONDS",
                        help="seconds into the run window to inject --fault")
    parser.add_argument("--recover-at", type=int, metavar="SECONDS",
                        help="seconds into the run window to restore the dependency")
    parser.add_argument("--outdir", help="relative or absolute output directory")
    parser.add_argument("--only", nargs="+", metavar="E01",
                        help="only these experiment IDs/prefixes, e.g. --only E01 E03 E17")
    parser.add_argument("--skip-start", action="store_true", help="stack already built and started")
    parser.add_argument("--no-restore", action="store_true", help="leave last experiment's app mode")
    return parser


def validate_args(parser, args):
    if args.rate < 1 or args.duration < 4 or args.vus < 1 or args.max_vus < args.vus:
        parser.error("rate>=1, duration>=4, vus>=1, max-vus>=vus")
    if args.profile in ("capacity", "resilience"):
        problems = []
        if not args.strategies:
            problems.append("--strategies required for the capacity/resilience profiles")
        if not args.rates or any(r < 1 for r in args.rates):
            problems.append("--rates must be one or more positive integers")
        if not (0 <= args.writes <= 1):
            problems.append("--writes must be between 0 and 1")
        if args.repetitions < 1:
            problems.append("--repetitions must be >= 1")
        if args.profile == "resilience":
            if not args.fault:
                problems.append("--fault required for the resilience profile")
            if not (args.fault_at and args.recover_at):
                problems.append("--fault-at and --recover-at required for the resilience profile")
            elif not (0 < args.fault_at < args.recover_at < args.duration):
                problems.append("expected 0 < --fault-at < --recover-at < --duration")
        if problems:
            parser.error(" ".join(problems))


def main():
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    outdir = Path(args.outdir or f"results/campaign_{stamp}")
    if not outdir.is_absolute():
        outdir = ROOT / outdir
    outdir = outdir.resolve()
    if ROOT / "results" not in outdir.parents and outdir != ROOT / "results":
        raise SystemExit("--outdir must be within project results/ (mounted in k6 container)")
    outdir.mkdir(parents=True, exist_ok=True)
    # k6 service sees all outputs at /results; build relative path for summary-export.
    # The typical outdir is results/campaign_..., mounted at /results/campaign_...
    print(f"Campaign outputs: {outdir}\nProfile: {args.profile}", flush=True)
    (outdir / "manifest.json").write_text(json.dumps({
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "profile": args.profile, "rate": args.rate, "duration_s": args.duration,
        "preallocated_vus": args.vus, "max_vus": args.max_vus,
        "strategies": args.strategies, "rates": args.rates,
        "repetitions": args.repetitions, "writes": args.writes,
        "fault": args.fault, "fault_at_s": args.fault_at, "recover_at_s": args.recover_at,
        "platform": platform.platform(), "logical_cpus": os.cpu_count(),
        "cargo_lock_sha256": hashlib.sha256((ROOT / "app/Cargo.lock").read_bytes()).hexdigest(),
        "requested_seed_rows_environment": os.environ.get("SEED_ROWS"),
        "warning": "CPU, Docker memory, actual DB dataset, and disk affect results",
    }, indent=2))
    global ONLY_IDS
    ONLY_IDS = set(args.only or [])
    try:
        if not args.skip_start:
            compose("up", "-d", "--build", "--scale", "app=2")
        ensure_healthy(timeout=180)
        # Record the *actual* data size, not only the configured SEED_ROWS. The
        # database volume may predate changes in .env.
        try:
            count = subprocess.check_output(
                ["docker", "compose", "exec", "-T", "db", "psql", "-U", "demo", "-d", "orderflow",
                 "-At", "-c", "SELECT count(*) FROM order_events"], cwd=ROOT, text=True, timeout=30
            ).strip()
            manifest_file = outdir / "manifest.json"
            manifest = json.loads(manifest_file.read_text())
            manifest["actual_activity_rows_at_start"] = int(count)
            manifest["app_image_ids"] = subprocess.check_output(
                ["docker", "compose", "images", "-q", "app"], cwd=ROOT, text=True, timeout=30
            ).split()
            manifest_file.write_text(json.dumps(manifest, indent=2))
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"WARNING: could not count seeded rows: {exc}", file=sys.stderr)
        # Pre-pull / initialize the k6 image so failure timing begins near the actual load.
        compose("run", "-T", "--rm", "--no-deps", "k6", "version")
        {"full": full, "smoke": smoke, "sweep": sweep,
         "capacity": capacity, "resilience": resilience}[args.profile](args, outdir)
        print(f"\nDONE: {outdir / 'REPORT.md'}\nCSV: {outdir / 'comparison.csv'}", flush=True)
    finally:
        try:
            compose("up", "-d", "redis1", "redis2")
            if not args.no_restore:
                configure("aside", ttl=30, l1_ttl=1, replicas=2)
        except Exception as exc:
            print(f"WARNING: stack recovery failed: {exc}", file=sys.stderr)
        save_reports(outdir)


if __name__ == "__main__":
    main()
