import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_dashboard import build
from telemetry import RunTelemetry, load_slo, sample


class ObservabilityTests(unittest.TestCase):
    def test_dashboard_panels_do_not_overlap(self):
        panels = build()["panels"]
        for index, first in enumerate(panels):
            a = first["gridPos"]
            for second in panels[index+1:]:
                b = second["gridPos"]
                overlap = a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]
                self.assertFalse(overlap, (first["title"], second["title"]))

    def test_provisioned_dashboard_matches_generator(self):
        self.assertEqual(build(), json.loads((ROOT / "infra/grafana/dashboards/golden-signals.json").read_text()))

    def test_slo_rejects_invalid_error_budget(self):
        contract = load_slo()
        contract["normal"]["error_ratio"] = 1.5
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slo.json"
            path.write_text(json.dumps(contract))
            with self.assertRaises(ValueError):
                load_slo(path)

    def test_markers_persist_and_run_id_is_not_a_metric_label(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(RunTelemetry, "request", return_value=b'{"data":{"result":[{"value":[0,"1"]}]}}'):
                with RunTelemetry("E10", "invalidate", directory, lambda _: "http://localhost:3000", rate=40) as telemetry:
                    telemetry.marker("fault", "Redis stopped")
                    text = telemetry.exposition()
                    self.assertIn('orderflow_campaign_phase{phase="fault"} 1', text)
                    self.assertNotIn(Path(directory).name, text)
                    self.assertNotIn("run_id", text)
                    telemetry.marker("recovery", "Redis started")
                result = json.loads(telemetry.file.read_text())
        self.assertEqual([event["phase"] for event in result["events"]], ["warmup", "fault", "recovery", "idle"])
        self.assertEqual(result["hypotheses"], "H5")
        self.assertIn("from=", result["dashboard_url"])
        self.assertIn("to=", result["dashboard_url"])

    def test_failed_observability_is_unknown_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(RunTelemetry, "request", side_effect=OSError("unreachable")):
                with RunTelemetry("E03", "aside", directory, lambda _: "http://localhost:3000") as telemetry:
                    telemetry.check()
                    self.assertFalse(telemetry.complete)
                result = json.loads(telemetry.file.read_text())
        self.assertFalse(result["telemetry_complete"])
        self.assertEqual(result["errors"], ["unreachable"])

    def test_prometheus_label_escaping(self):
        self.assertEqual(sample("test", 1, {"label": 'a"b\\c'}), 'test{label="a\\"b\\\\c"} 1\n')
