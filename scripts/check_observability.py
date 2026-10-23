#!/usr/bin/env python3
"""Validate provisioned panels against live Prometheus/Grafana; no load is generated."""
import argparse
import base64
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from benchmark_campaign import service_url

ROOT = Path(__file__).resolve().parents[1]


def query(expression, at):
    url = service_url("prometheus") + "/api/v1/query?" + urllib.parse.urlencode({"query": expression, "time": at})
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--at", type=float, default=None, help="Unix timestamp inside a recorded load interval")
    parser.add_argument("--require-load", action="store_true", help="Also require k6 histograms and campaign context")
    parser.add_argument("--output", type=Path, help="Write validation evidence as JSON")
    args = parser.parse_args()
    at = args.at or time.time()
    dashboard = json.loads((ROOT / "infra/grafana/dashboards/golden-signals.json").read_text())
    project = os.environ.get("COMPOSE_PROJECT_NAME", "orderflow-cache")
    replacements = {"$__rate_interval": "30s", "$strategy": ".*", "$replica": ".*", "$project": project,
                    "$service": ".*", "$layer": ".*", "$redis": "redis[12]"}
    results, failures = [], []
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            expression = target["expr"]
            for variable, value in replacements.items():
                expression = expression.replace(variable, value)
            try:
                response = query(expression, at)
                if response["status"] != "success":
                    raise ValueError(response)
                data = response["data"]["result"]
                values = data if isinstance(data, list) else []
                finite = sum(1 for item in values if isinstance(item, dict) and "value" in item and math.isfinite(float(item["value"][1])))
                results.append({"panel": panel["title"], "ref": target["refId"], "series": len(values), "finite_series": finite,
                                "warnings": response.get("warnings", [])})
            except (OSError, ValueError) as error:
                detail = error.read().decode() if isinstance(error, urllib.error.HTTPError) else str(error)
                failures.append({"panel": panel["title"], "query": expression, "error": detail})
    required = ["http_outcomes_total", "db_pool_connections", "cache_redis_duration_seconds_count", "db_sql_statements_total",
                'container_cpu_usage_seconds_total{container_label_com_docker_compose_project="' + project + '",container_label_com_docker_compose_service="app"}',
                "node_cpu_seconds_total", "nginx_up", "redis_memory_max_bytes", "pg_stat_activity_count"]
    if args.require_load:
        required += ["k6_http_req_duration_seconds", "k6_http_reqs_total", "orderflow_campaign_phase", "orderflow_slo_p95_seconds"]
    for expression in required:
        if not query(f"count({expression})", at)["data"]["result"]:
            failures.append({"missing_required_metric": expression})
    credentials = base64.b64encode(os.environ.get("GRAFANA_API_CREDENTIALS", "admin:admin").encode()).decode()
    request = urllib.request.Request(service_url("grafana") + "/api/dashboards/uid/orderflow-golden",
                                     headers={"Authorization": "Basic " + credentials})
    with urllib.request.urlopen(request, timeout=10) as response:
        provisioned = json.load(response)
    if provisioned["dashboard"]["title"] != dashboard["title"]:
        failures.append({"grafana": "unexpected provisioned dashboard"})
    report = {"at": at, "project": project, "queries_checked": len(results), "failures": failures, "panels": results}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != "panels"}, indent=2))
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
