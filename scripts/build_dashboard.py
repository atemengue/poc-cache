#!/usr/bin/env python3
"""Generate the provisioned golden-signals dashboard. Run after editing this file."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DS = {"type": "prometheus", "uid": "prometheus"}
APP = 'strategy=~"$strategy",instance=~"$replica"'
BUSINESS = 'operation=~"snapshot|activity|orders|revision|order_lookup"'
CONTAINER = 'container_label_com_docker_compose_project=~"$project",container_label_com_docker_compose_service=~"$service",image!=""'
K6 = 'strategy=~"$strategy"'


def rate(metric, labels="", group=""):
    by = f" by ({group})" if group else ""
    return f'sum{by}(rate({metric}{{{labels}}}[$__rate_interval]))'


def quantile(metric, labels, p=.95, group=""):
    by = "le" + ("," + group if group else "")
    return f'1000 * histogram_quantile({p}, {rate(metric + "_bucket", labels, by)})'


def variable(name, query, *, custom=False, multi=True):
    result = {"name": name, "label": name.title(), "type": "custom" if custom else "query",
              "query": query, "multi": multi, "includeAll": multi, "refresh": 2,
              "current": {"text": "All", "value": "$__all"} if multi else {}}
    if multi:
        result["allValue"] = ".*"
    if not custom:
        result["datasource"] = DS
    return result


def build():
    panels, next_id, y = [], 1, 0

    def row(title, description=""):
        nonlocal next_id, y
        y = max((p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels), default=0)
        panels.append({"id": next_id, "type": "row", "title": title, "description": description,
                       "collapsed": False, "panels": [], "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}})
        next_id += 1
        y += 1

    def panel(title, queries, unit="short", description="", kind="timeseries", width=12, mappings=None):
        nonlocal next_id, y
        previous = panels[-1] if panels else None
        x = 0
        if previous and previous["type"] != "row" and previous["gridPos"]["x"] + previous["gridPos"]["w"] < 24:
            x = previous["gridPos"]["x"] + previous["gridPos"]["w"]
        elif previous and previous["type"] != "row":
            y = previous["gridPos"]["y"] + previous["gridPos"]["h"]
        height = 4 if kind in ("stat", "table") else 8
        targets = [{"refId": chr(65+i), "expr": expr, "legendFormat": legend,
                    "range": kind == "timeseries", "instant": kind != "timeseries",
                    "format": "table" if kind == "table" else "time_series"} for i, (legend, expr) in enumerate(queries)]
        defaults = {"unit": unit, "noValue": "Unknown / no samples", "mappings": mappings or [],
                    "custom": {"spanNulls": False}}
        options = {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull"]},
                   "tooltip": {"mode": "multi"}}
        if kind == "stat":
            options = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                       "textMode": "value_and_name", "colorMode": "value", "graphMode": "none"}
        panels.append({"id": next_id, "type": kind, "title": title, "description": description,
                       "datasource": DS, "targets": targets, "fieldConfig": {"defaults": defaults, "overrides": []},
                       "options": options, "gridPos": {"x": x, "y": y, "w": width, "h": height}})
        next_id += 1

    row("Experiment context — select a report's time-window link", "Run identifiers live in annotations and reports, never metric labels. App filters do not filter host-wide or campaign-wide panels.")
    panel("Experiment / hypotheses / strategy", [("context", "orderflow_campaign_info")], kind="table", width=12)
    panel("Current phase (1 = active)", [("{{phase}}", "orderflow_campaign_phase == 1")], kind="stat", width=6)
    panel("Telemetry complete", [("complete", "orderflow_telemetry_complete and on() (time() - orderflow_campaign_heartbeat_seconds < 15)")], kind="stat", width=6,
          mappings=[{"type": "value", "options": {"0": {"text": "INCOMPLETE", "color": "red"}, "1": {"text": "Complete", "color": "green"}}}])
    panel("Configured workload", [("replicas", "orderflow_campaign_replicas"), ("write fraction", "orderflow_campaign_write_fraction"), ("hot fraction", "orderflow_campaign_hot_fraction")], kind="stat")
    panel("SLO contracts: normal / degraded", [("{{contract}} p95 (s)", "orderflow_slo_p95_seconds"), ("{{contract}} errors (fraction)", "orderflow_slo_error_ratio"), ("recovery (s)", "orderflow_slo_recovery_seconds")], kind="stat")

    row("1 · LATENCY — successful requests and failures measured separately")
    read = APP + ',route="/products/:id/snapshot",outcome="success"'
    panel("Successful read latency — p50 / p95 / p99", [(f"p{int(p*100)}", quantile("http_request_duration_seconds", read, p)) for p in (.5, .95, .99)] +
          [("{{contract}} p95 SLO", "1000 * orderflow_slo_p95_seconds")], "ms", "Replica histogram buckets are summed before computing percentiles. Normal and degraded contracts are provisional settings in benchmarks/slo.json.")
    panel("Successful writes and orders — p95 / p99", [(f"{{{{route}}}} p{int(p*100)}", quantile("http_request_duration_seconds", APP + ',route=~"/orders|/products/:id/activity",outcome="success"', p, "route")) for p in (.95, .99)], "ms")
    panel("Failed requests — p95", [("{{outcome}}", quantile("http_request_duration_seconds", APP + ',outcome=~"client_error|server_error|business_rejection"', group="outcome"))], "ms", "409 order conflicts are expected business rejections, not availability failures.")
    panel("DB operation latency — p95", [("{{operation}}", quantile("db_query_duration_seconds", APP, group="operation"))], "ms", "Operation time includes pool waiting and any configured synthetic delay; it is not pure PostgreSQL execution time.")
    panel("Redis command latency — p95", [("node {{node}} {{operation}} {{outcome}}", quantile("cache_redis_duration_seconds", APP, group="node,operation,outcome"))], "ms", "Includes connection establishment, Redis wait, and timeout. Correlate with Redis exporter and container metrics.")
    panel("Follower polling latency — p95", [("{{outcome}}", quantile("cache_follower_wait_seconds", APP, group="outcome"))], "ms")
    panel("Client read latency — window p95 / p99", [(f"p{int(p*100)}", f'1000 * histogram_quantile({p}, sum(rate(k6_http_req_duration_seconds{{{K6},operation="read",expected_response="true"}}[$__rate_interval])))') for p in (.95, .99)], "ms", "k6 native histograms permit windowed quantiles; raw compressed samples are retained for exact fault-window analysis.")
    panel("Configured p99 limits", [("{{contract}}", "1000 * orderflow_slo_p99_seconds")], "ms")

    row("2 · TRAFFIC — offered, achieved, successful, and avoided work")
    panel("Offered / achieved / successful requests", [("offered", "orderflow_campaign_offered_rps"), ("k6 achieved", rate("k6_http_reqs_total", K6)), ("API successful", rate("http_outcomes_total", APP + "," + BUSINESS + ',outcome="success"'))], "reqps")
    panel("Traffic by operation and outcome", [("{{operation}} {{outcome}}", rate("http_outcomes_total", APP + "," + BUSINESS, "operation,outcome"))], "reqps")
    panel("Traffic by app replica", [("{{instance}}", rate("http_outcomes_total", APP + "," + BUSINESS, "instance"))], "reqps")
    panel("Cache hits by layer / DB loader origins", [("{{layer}} hits", rate("cache_lookup_total", APP + ',layer=~"$layer",result="hit"', "layer")), ("DB {{origin}}", rate("db_snapshot_loads_total", APP, "origin"))], "ops", "Request and write-through refresh loads are separate. Cache lookup counts include protected-mode polling; they are not a request hit ratio.")
    panel("Layer-specific lookup hit ratios", [("{{layer}}", rate("cache_lookup_total", APP + ',layer=~"$layer",result="hit"', "layer") + " / " + rate("cache_lookup_total", APP + ',layer=~"$layer",result=~"hit|miss"', "layer"))], "percentunit", "No denominator means unknown. L1 hits do not enter the L2 denominator. Errors/invalid values are shown in the error panels.")
    panel("Redis commands by node", [("{{job}}", rate("redis_commands_processed_total", 'job=~"$redis"', "job"))], "ops")
    panel("Dropped iterations / second", [("dropped", "orderflow:dropped_rps")], "ops", "Absent drop counters are interpreted as zero only while iteration counter samples exist. Missing generator samples remain unknown.")
    panel("Achieved / offered throughput", [("ratio", rate("k6_http_reqs_total", K6) + " / on() (orderflow_campaign_offered_rps > 0)")], "percentunit")

    row("3 · ERRORS — unexpected failures, expected rejections, and degradation")
    err = rate("http_outcomes_total", APP + "," + BUSINESS + ',outcome=~"client_error|server_error"')
    total = rate("http_outcomes_total", APP + "," + BUSINESS)
    panel("Unexpected HTTP error ratio", [("unexpected", err + " / " + total), ("{{contract}} SLO", "orderflow_slo_error_ratio")], "percentunit", "Excludes health probes and expected 409 order conflicts. Includes unexpected client and server errors; a zero denominator is unknown.")
    panel("HTTP errors by operation", [("{{operation}} {{outcome}}", rate("http_outcomes_total", APP + "," + BUSINESS + ',outcome=~"client_error|server_error"', "operation,outcome"))], "reqps")
    panel("Expected business rejections", [("{{operation}}", rate("http_outcomes_total", APP + ',outcome="business_rejection"', "operation"))], "reqps")
    panel("Redis failures and timeouts", [("{{node}} {{outcome}}", rate("cache_redis_duration_seconds_count", APP + ',outcome=~"error|timeout"', "node,outcome"))], "ops")
    panel("Database errors / acquisition timeouts", [("{{operation}} {{kind}}", rate("db_errors_total", APP, "operation,kind"))], "ops")
    panel("Fallbacks to PostgreSQL", [("{{reason}}", rate("cache_fallback_total", APP, "reason"))], "ops", "Fallbacks may return HTTP 200; correlate with DB saturation and latency rather than classifying them as HTTP failures.")
    panel("Cache invalidation / SET failures", [("invalidations", rate("cache_invalidation_total", APP + ',result="error"')), ("SET failures", rate("cache_redis_operations_total", APP + ',operation="set",status="error"'))], "ops")
    panel("Fault / recovery phase error ratio", [("unexpected", '(' + err + ' / ' + total + ') and on() (sum(orderflow_campaign_phase{phase=~"fault|recovery"}) == 1)')], "percentunit", "Rolling window can straddle phase boundaries. Use saved sample timestamps for exact phase acceptance.")

    row("4 · SATURATION — resource limits, queues, and bottleneck evidence")
    cpu = rate("container_cpu_usage_seconds_total", CONTAINER, "name,container_label_com_docker_compose_service")
    panel("Container CPU usage — cores", [("{{name}}", cpu)], "short", "Service/project selectors apply to container panels. No CPU quota means a percentage-of-quota cannot be calculated.")
    quota = f'max by(name,container_label_com_docker_compose_service)(container_spec_cpu_quota{{{CONTAINER}}} / container_spec_cpu_period{{{CONTAINER}}})'
    panel("CPU utilization relative to quota", [("{{name}}", cpu + " / (" + quota + " > 0)")], "percentunit")
    panel("CPU throttled periods", [("{{name}}", rate("container_cpu_cfs_throttled_periods_total", CONTAINER, "name") + " / " + rate("container_cpu_cfs_periods_total", CONTAINER, "name"))], "percentunit")
    panel("Memory working set / finite limit", [("{{name}}", f'container_memory_working_set_bytes{{{CONTAINER}}} / (container_spec_memory_limit_bytes{{{CONTAINER}}} > 0 < 1e15)')], "percentunit", "Containers without a finite memory limit are omitted, not reported as unconstrained zero-percent usage.")
    panel("Memory usage / limits", [("{{name}} working set", f'container_memory_working_set_bytes{{{CONTAINER}}}'), ("{{name}} limit", f'container_spec_memory_limit_bytes{{{CONTAINER}}} > 0 < 1e15')], "bytes")
    panel("Container OOM events", [("{{name}}", rate("container_oom_events_total", CONTAINER, "name"))], "ops")
    panel("PostgreSQL pool used / limit", [("{{instance}}", f'db_pool_connections{{{APP},state="used"}} / ignoring(state) db_pool_connections{{{APP},state="limit"}}')], "percentunit")
    panel("Connection acquisition wait — p95", [("{{outcome}}", quantile("db_pool_acquire_duration_seconds", APP, group="outcome"))], "ms")
    panel("PostgreSQL connections / maximum", [("{{state}}", 'sum by(state)(pg_stat_activity_count{datname="orderflow"})'), ("server maximum", "pg_settings_max_connections")], "short")
    panel("PostgreSQL locks not granted", [("{{mode}}", 'sum by(mode)(pg_locks_count{datname="orderflow",granted="false"})')], "short", "Exporter availability/version determines lock-wait coverage. Missing series are unknown.")
    panel("Redis memory / maxmemory", [("{{job}}", 'redis_memory_used_bytes{job=~"$redis"} / (redis_memory_max_bytes{job=~"$redis"} > 0)')], "percentunit")
    panel("Redis evictions / blocked clients", [("{{job}} evictions/s", rate("redis_evicted_keys_total", 'job=~"$redis"', "job")), ("{{job}} blocked", 'redis_blocked_clients{job=~"$redis"}')], "short")
    panel("Gateway connection pressure", [("active", "nginx_connections_active"), ("reading", "nginx_connections_reading"), ("writing", "nginx_connections_writing"), ("waiting", "nginx_connections_waiting")], "short")
    panel("Gateway unhandled connections / second", [("unhandled", rate("nginx_connections_accepted") + " - " + rate("nginx_connections_handled"))], "ops")
    panel("Load generator active / max VUs", [("active", "k6_vus"), ("max", "k6_vus_max")], "short")
    panel("Load generator CPU — cores", [("k6", rate("container_cpu_usage_seconds_total", 'container_label_com_docker_compose_project=~"$project",container_label_com_docker_compose_service="k6",image!=""'))], "short")
    panel("Host CPU busy", [("host", '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[$__rate_interval]))')], "percentunit", "Host-wide, including unrelated processes and other Compose projects.")
    panel("Host memory used", [("host", "1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes")], "percentunit")

    row("H1–H3 · Performance, capacity, and DB protection")
    panel("Explicit SQL attempts by operation", [("{{operation}}", rate("db_sql_statements_total", APP, "operation"))], "ops", "Excludes BEGIN/COMMIT/ROLLBACK and driver-internal queries. db_queries_total remains a logical operation counter.")
    panel("DB snapshot loads / successful reads", [("ratio", rate("db_snapshot_loads_total", APP) + " / " + rate("http_outcomes_total", APP + ',operation="snapshot",outcome="success"'))], "short", "Includes write-through refresh work. Compare matched throughput and write mix; this is not itself the H3 reduction versus baseline.")

    row("H4 / H7 · Freshness observations — not a linearizability proof")
    panel("Last sequential probe stale fraction", [("stale fraction", "orderflow_probe_stale_fraction")], "percentunit", "Last E17 result, not continuously measured freshness. Missing data is unknown.")
    panel("Observed staleness / freshness budget", [("last stale response", "orderflow_probe_last_stale_seconds"), ("observation window", "orderflow_probe_window_seconds"), ("declared budget", "orderflow_slo_freshness_seconds")], "s", "Last stale observation is a lower bound. A short probe cannot establish an upper bound; never infer pass from this chart alone.")
    panel("Stale replica probes", [("stale replicas", "orderflow_probe_stale_replica_count")], "short")
    panel("Inventory invariant violations from runtime probe", [("violations", "orderflow_probe_inventory_violations")], "short", "Only emitted by the opt-in concurrent-order test. No probe means unknown, not zero violations.")

    row("H5–H6 · Failure recovery and stampede coordination")
    panel("Redis availability", [("{{job}}", 'redis_up{job=~"$redis"}')], "short")
    panel("Distributed lock outcomes", [("{{result}}", rate("cache_wait_total", APP, "result"))], "ops")
    panel("Refill work and follower fallback", [("DB request loads", rate("db_snapshot_loads_total", APP + ',origin="request"')), ("leaders", rate("cache_wait_total", APP + ',result="leader"')), ("follower fallbacks", rate("cache_wait_total", APP + ',result="timeout_or_error"'))], "ops", "Use flush/expiry annotations and exact raw counters to calculate amplification per event; a rolling ratio can cross event boundaries.")
    panel("Active SLO alerts", [("{{alertname}} {{alertstate}}", 'ALERTS{alertname=~"OrderFlow.*"}')], "short", "Rules use rolling windows and require sustained breaches. A short run may not fire an alert despite failing an acceptance threshold.")

    row("H7–H8 · Cache layers and Redis scale-out")
    panel("L1 and L2 misses", [("{{layer}}", rate("cache_lookup_total", APP + ',result="miss"', "layer"))], "ops")
    panel("Application Redis operations by shard", [("node {{node}} {{operation}}", rate("cache_redis_operations_total", APP, "node,operation"))], "ops")
    panel("Redis CPU — cores", [("{{job}} user", rate("redis_cpu_user_seconds_total", 'job=~"$redis"', "job")), ("{{job}} system", rate("redis_cpu_sys_seconds_total", 'job=~"$redis"', "job"))], "short")
    panel("Shard request share", [("node {{node}}", rate("cache_redis_operations_total", APP + ',operation="get"', "node") + " / ignoring(node) group_left " + rate("cache_redis_operations_total", APP + ',operation="get"'))], "percentunit", "One hot key stays on one shard. Throughput improvement alone does not prove a Redis bottleneck.")

    row("Telemetry health — missing data is never a passing measurement")
    panel("Scrape targets", [("{{job}} {{instance}}", "up")], "short")
    panel("Campaign heartbeat age", [("age", "time() - orderflow_campaign_heartbeat_seconds")], "s")

    return {"title": "OrderFlow — Four Golden Signals", "uid": "orderflow-golden", "schemaVersion": 39,
            "version": 1, "refresh": "5s", "time": {"from": "now-15m", "to": "now"},
            "tags": ["orderflow", "SRE", "golden-signals", "H1-H8"], "panels": panels,
            "templating": {"list": [variable("project", "label_values(orderflow_app_info, project)", multi=False),
                variable("strategy", "label_values(orderflow_app_info, strategy)"),
                variable("replica", 'label_values(orderflow_app_info{strategy=~"$strategy"}, instance)'),
                variable("service", "app,db,redis1,redis2,gateway,k6", custom=True),
                variable("layer", "l1,l2", custom=True),
                variable("redis", 'label_values(redis_up{job=~"redis[12]"}, job)')]},
            "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                "enable": True, "hide": False, "name": "Campaign phases and faults", "type": "dashboard",
                "iconColor": "rgba(255, 163, 0, 1)"}]},
            "links": [{"title": "Original cache lab", "type": "link", "url": "/d/orderflow-cache", "keepTime": True}]}


if __name__ == "__main__":
    destination = ROOT / "infra/grafana/dashboards/golden-signals.json"
    destination.write_text(json.dumps(build(), indent=2) + "\n")
    print(destination)
