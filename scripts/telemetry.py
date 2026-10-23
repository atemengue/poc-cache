"""Campaign markers, bounded Prometheus gauges, and Grafana annotations (stdlib)."""
import base64
import json
import math
import os
from pathlib import Path
import threading
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PHASES = ("idle", "warmup", "measurement", "fault", "recovery", "consistency")
HYPOTHESES = {
    "E01": "H1 H2 H3", "E02": "H1", "E03": "H1 H2 H3",
    "E04": "H1 H4", "E05": "H1 H4", "E06": "H1 H4",
    "E07": "H3", "E08": "H3", "E09": "H3", "E10": "H5", "E11": "H5",
    "E12": "H6", "E13": "H3 H5", "E14": "H2 H3", "E15": "H8",
    "E16": "H6", "E17": "H4 H7 H8", "E18": "H8", "E19": "H1 H3 H4",
    "E20": "H2", "E21": "H2", "E22": "H5",
}


def load_slo(path=None):
    path = Path(path or os.environ.get("ORDERFLOW_SLO_FILE", ROOT / "benchmarks/slo.json"))
    data = json.loads(path.read_text())
    for phase in ("normal", "degraded"):
        for field in ("p95_seconds", "p99_seconds", "error_ratio", "drop_ratio"):
            value = data[phase][field]
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid SLO: {phase}.{field}")
            if field.endswith("ratio") and value > 1:
                raise ValueError(f"Invalid ratio: {phase}.{field}")
    if data["recovery_seconds"] <= 0:
        raise ValueError("Recovery deadline must be positive")
    for value in data["freshness_seconds"].values():
        if not math.isfinite(value) or value < 0:
            raise ValueError("Freshness limits must be finite and nonnegative")
    return data


def sample(name, value, labels=None):
    suffix = ""
    if labels:
        suffix = "{" + ",".join(f"{key}={json.dumps(str(val))}" for key, val in labels.items()) + "}"
    return f"{name}{suffix} {value}\n"


class RunTelemetry:
    def __init__(self, experiment, strategy, outdir, service_url, *, rate=0, replicas=2, writes=0, hot=.9):
        self.experiment, self.strategy = experiment, strategy
        self.outdir, self.service_url = Path(outdir), service_url
        self.rate, self.replicas, self.writes, self.hot = rate, replicas, writes, hot
        self.slo = load_slo()
        self.started = time.time()
        self.ended = None
        self.phase = "warmup"
        self.active = True
        self.complete = False
        self.events, self.errors = [], []
        self.probes = {}
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.hypotheses = HYPOTHESES.get(experiment[:3], "unassigned")
        self.file = self.outdir / f"telemetry_{time.time_ns()}_{experiment}_{strategy}.json"

    def request(self, service, path, body=None, method=None):
        headers = {}
        if service == "grafana":
            credentials = os.environ.get("GRAFANA_API_CREDENTIALS", "admin:admin")
            headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
            headers["Content-Type"] = "application/json"
        else:
            headers["Content-Type"] = "text/plain; version=0.0.4"
        request = urllib.request.Request(self.service_url(service) + path, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.read()

    def record_error(self, error):
        message = str(error)
        with self.lock:
            if message not in self.errors:
                self.errors.append(message)
                print(f"OBSERVABILITY: {message}", flush=True)

    def marker(self, phase, text):
        if phase not in PHASES:
            raise ValueError(f"Unknown phase: {phase}")
        now = time.time()
        with self.lock:
            self.phase = phase
            self.events.append({"time": now, "phase": phase, "text": text})
        payload = {"dashboardUID": "orderflow-golden", "time": int(now * 1000),
                   "tags": ["orderflow", phase, self.experiment, self.strategy, *self.hypotheses.split()],
                   "text": f"{self.outdir.name}/{self.experiment}/{self.strategy}: {text}"}
        try:
            self.request("grafana", "/api/annotations", json.dumps(payload).encode(), "POST")
        except Exception as exc:
            self.record_error(exc)
        self.push()

    def exposition(self):
        with self.lock:
            phase, active, complete = self.phase, self.active, self.complete
            probes = dict(self.probes)
        lines = [sample("orderflow_campaign_active", int(active)),
                 sample("orderflow_campaign_heartbeat_seconds", time.time()),
                 sample("orderflow_telemetry_complete", int(complete)),
                 sample("orderflow_campaign_info", 1, {"experiment": self.experiment, "strategy": self.strategy,
                        "hypotheses": self.hypotheses}),
                 sample("orderflow_campaign_offered_rps", self.rate if active and phase in ("measurement", "fault", "recovery") else 0),
                 sample("orderflow_campaign_replicas", self.replicas),
                 sample("orderflow_campaign_write_fraction", self.writes),
                 sample("orderflow_campaign_hot_fraction", self.hot)]
        lines += [sample("orderflow_campaign_phase", int(active and phase == name), {"phase": name}) for name in PHASES]
        for contract in ("normal", "degraded"):
            for key, value in self.slo[contract].items():
                lines.append(sample(f"orderflow_slo_{key}", value, {"contract": contract}))
        lines += [sample("orderflow_slo_recovery_seconds", self.slo["recovery_seconds"]),
                  sample("orderflow_slo_freshness_seconds", self.slo["freshness_seconds"][self.strategy])]
        lines += [sample(key, value) for key, value in probes.items()]
        return "".join(lines)

    def push(self):
        try:
            self.request("pushgateway", "/metrics/job/orderflow_campaign", self.exposition().encode(), "PUT")
        except Exception as exc:
            self.record_error(exc)

    def check(self):
        try:
            # Scrape health alone is insufficient: verify container discovery too.
            query = ('(sum(up{job="orderflow-rust"}) >= bool ' + str(self.replicas) + ') * '
                     '(min(up{job=~"postgres|redis1|redis2|nginx|cadvisor|node|campaign"}) == bool 1) * '
                     '(count(count by(job)(up{job=~"postgres|redis1|redis2|nginx|cadvisor|node|campaign"})) == bool 7) * '
                     '(count(container_cpu_usage_seconds_total{container_label_com_docker_compose_service="app"}) > bool 0)')
            if self.rate and self.phase in ("measurement", "fault", "recovery"):
                query += ' * (sum(count_over_time(k6_http_reqs_total[10s])) > bool 0)'
            payload = json.loads(self.request("prometheus", "/api/v1/query?" + urllib.parse.urlencode({"query": query})))
            result = payload["data"]["result"]
            with self.lock:
                self.complete = bool(result and float(result[0]["value"][1]) == 1)
        except Exception as exc:
            with self.lock:
                self.complete = False
            self.record_error(exc)

    def heartbeat(self):
        while not self.stop.wait(2):
            self.check()
            self.push()

    def __enter__(self):
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.marker("warmup", "Configure replicas, reset caches, and warm up")
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()
        return self

    def summary(self):
        end = self.ended or time.time()
        try:
            base = self.service_url("grafana")
            link = f"{base}/d/orderflow-golden?from={int(self.started*1000)}&to={int(end*1000)}"
        except Exception:
            link = None
        return {"dashboard_url": link, "telemetry_file": self.file.name,
                "telemetry_complete": self.complete, "hypotheses": self.hypotheses}

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=15)
        self.check()
        self.ended = time.time()
        self.active = False
        self.marker("idle", "Run failed" if exc else "Run completed")
        self.file.write_text(json.dumps({**self.summary(), "started": self.started, "ended": self.ended,
            "events": self.events, "errors": self.errors, "slo": self.slo, "probes": self.probes}, indent=2))
