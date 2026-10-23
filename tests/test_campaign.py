import gzip
import importlib.util
import datetime as dt
from pathlib import Path
import tempfile
import unittest
import json
import sys
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'benchmark_campaign.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('benchmark_campaign', SCRIPT)
campaign = importlib.util.module_from_spec(spec)
spec.loader.exec_module(campaign)


class MeasurementTests(unittest.TestCase):
    def test_k6_legacy_export(self):
        summary = {"metrics": {
            "http_reqs": {"count": 100, "rate": 10},
            "http_req_failed": {"value": 0.02},
            "read_latency_ms": {"p(95)": 5.5},
            "dropped_iterations": {"count": 7},
        }}
        self.assertEqual(campaign.metric(summary, "http_reqs", "rate"), 10)
        self.assertEqual(campaign.metric(summary, "http_req_failed", "rate"), 0.02)
        self.assertEqual(campaign.metric(summary, "read_latency_ms", "p(95)"), 5.5)
        self.assertEqual(campaign.metric(summary, "dropped_iterations", "count"), 7)

    def test_k6_nested_summary_and_missing_metrics(self):
        summary = {"metrics": {"http_req_failed": {"values": {"rate": 0}}}}
        self.assertEqual(campaign.metric(summary, "http_req_failed", "rate"), 0)
        self.assertIsNone(campaign.metric(summary, "http_reqs", "rate"))

    def test_l2_query_excludes_l1(self):
        query = campaign.cache_lookup_query("hit", 20)
        self.assertIn('layer="l2"', query)
        self.assertIn('result="hit"', query)

    def test_ports_use_compose_resolution(self):
        campaign.service_url.cache_clear()
        config = {"services": {"gateway": {"ports": [{"published": "18080"}]}}}
        try:
            with patch.object(campaign.subprocess, "check_output", return_value=json.dumps(config)):
                self.assertEqual(campaign.service_url("gateway"), "http://127.0.0.1:18080")
        finally:
            campaign.service_url.cache_clear()

    def test_consistency_reads_replica_json(self):
        campaign.RESULTS.clear()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(campaign, "RunTelemetry"), patch.object(campaign, "configure"), patch.object(campaign, "flush"), \
                 patch.object(campaign, "prime"), patch.object(campaign, "cmd"), \
                 patch.object(campaign, "http_json", side_effect=[{"revision": 1}, {"revision": 2}] + [{"revision": 2}] * 60), \
                 patch.object(campaign.subprocess, "check_output", side_effect=[
                     '{"id":1,"revision":2}', '{"id":1,"revision":1}']):
                campaign.consistency("E17", "l1l2", outdir=directory)
        row = campaign.RESULTS[-1]
        self.assertEqual(row["replica_revisions_after_write"], {"app_1": 2, "app_2": 1})
        self.assertEqual(row["stale_replica_probes"], 1)
        self.assertEqual(row["stale_reads"], 0)


class ReportTests(unittest.TestCase):
    def setUp(self):
        campaign.RESULTS.clear()

    def test_empty_report(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign.save_reports(directory)
            self.assertTrue((Path(directory) / 'comparison.csv').exists())
            self.assertIn('not', (Path(directory) / 'REPORT.md').read_text())

    def test_missing_measurements_are_not_reported_as_zero(self):
        campaign.RESULTS.append(dict(
            experiment='FAILED', strategy='db', requested_rate=100,
            achieved_rps=None, read_p95_ms=None, read_p99_ms=None,
            error_rate=None, client_cache_hit_ratio=None, dropped_iterations=None))
        with tempfile.TemporaryDirectory() as directory:
            campaign.save_reports(directory)
            report = (Path(directory) / 'REPORT.md').read_text()
        self.assertIn('| FAILED | db | 100 | — | — | — | —% | —% | — |', report)

    def test_one_row_and_consistency(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign.RESULTS.extend([
                dict(experiment='E01',strategy='db',requested_rate=100,achieved_rps=97,
                     read_p95_ms=50.25,read_p99_ms=80,error_rate=0,
                     client_cache_hit_ratio=0,dropped_iterations=0),
                dict(experiment='E17',strategy='aside',kind='consistency',reads=60,stale_reads=59),
            ])
            campaign.save_reports(directory)
            report = (Path(directory) / 'REPORT.md').read_text()
            self.assertIn('E01',report)
            self.assertIn('E17 stale 59/60',report)
            self.assertIn('97.0',report)

    def test_grid_rows_include_repetition_and_fault_timing_fields(self):
        camp = campaign
        with tempfile.TemporaryDirectory() as directory:
            row = dict(experiment='E21', strategy='invalidate', repetition=3, fault_at_s=60,
                       recover_at_s=120, fault_window_error_rate=0.02,
                       recovery_observed_seconds=None,
                       requested_rate=100, read_p95_ms=None)
            camp.RESULTS.append(row)
            camp.save_reports(directory)
            csv_text = (Path(directory) / 'comparison.csv').read_text()
            self.assertIn('repetition', csv_text)
            self.assertIn('fault_at_s', csv_text)
            self.assertIn('recovery_observed_seconds', csv_text)
            self.assertIn('0.02', csv_text)


class ProfilePlanTests(unittest.TestCase):
    def test_capacity_grid_plan(self):
        args = campaign.build_parser().parse_args([
            '--profile', 'capacity', '--strategies', 'db', 'invalidate', 'protected',
            '--rates', '50', '100', '150', '250', '500', '1000',
            '--repetitions', '5', '--duration', '120', '--writes', '0.05', '--max-vus', '2000'])
        campaign.validate_args(campaign.build_parser(), args)
        calls = []
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(campaign, 'run_bench', side_effect=lambda run_id, description, **kw: calls.append((run_id, description, kw))):
            campaign.capacity(args, directory)
        self.assertEqual(len(calls), 3 * 6 * 5)  # 90 runs
        plans = {(run_id, kw['strategy'], kw['rate'], kw['repetition']) for run_id, _, kw in calls}
        self.assertEqual(len(plans), 90)
        self.assertTrue(all(kw['warm'] for _, _, kw in calls))
        self.assertTrue(all(kw['writes'] == 0.05 for _, _, kw in calls))
        self.assertTrue(all(kw['hot'] == 0.90 for _, _, kw in calls))
        # pre_vus auto-scaled to min(max_vus, max(rates, vus)) for constant-arrival-rate headroom.
        self.assertTrue(all(kw['pre_vus'] == 1000 for _, _, kw in calls))
        self.assertTrue(all(kw['max_vus'] == 2000 for _, _, kw in calls))
        # strategy outer loop preserving the CLI order.
        self.assertEqual(calls[0][2]['strategy'], 'db')
        self.assertEqual(calls[30][2]['strategy'], 'invalidate')
        self.assertEqual(calls[0][2]['rate'], 50)
        self.assertEqual(calls[0][2]['repetition'], 1)
        self.assertEqual(calls[4][2]['repetition'], 5)
        self.assertEqual(calls[5][2]['rate'], 100)

    def test_resilience_grid_plan(self):
        args = campaign.build_parser().parse_args([
            '--profile', 'resilience', '--strategies', 'invalidate', 'protected',
            '--rates', '50', '100', '150', '--duration', '180',
            '--repetitions', '5', '--fault', 'redis-stop', '--fault-at', '60', '--recover-at', '120'])
        campaign.validate_args(campaign.build_parser(), args)
        calls = []
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(campaign, 'run_bench', side_effect=lambda run_id, description, **kw: calls.append((run_id, description, kw))):
            campaign.resilience(args, directory)
        self.assertEqual(len(calls), 2 * 3 * 5)  # 30 runs
        for run_id, _, kw in calls:
            self.assertEqual(run_id, 'E22')
            self.assertTrue(kw['warm'])
            self.assertEqual(kw['fault'], 'stop_redis')  # redis-stop alias mapped
            self.assertEqual(kw['fault_at'], 60)
            self.assertEqual(kw['recover_at'], 120)
            self.assertEqual(kw['writes'], 0.0)

    def test_campaign_parser_example_commands(self):
        p = campaign.build_parser()
        a = p.parse_args(['--profile', 'capacity', '--strategies', 'db', 'invalidate', 'protected',
                          '--rates', '50', '100', '150', '250', '500', '1000',
                          '--repetitions', '5', '--duration', '120', '--writes', '0.05', '--max-vus', '2000'])
        self.assertEqual(a.strategies, ['db', 'invalidate', 'protected'])
        self.assertEqual(a.rates, [50, 100, 150, 250, 500, 1000])
        b = p.parse_args(['--profile', 'resilience', '--strategies', 'invalidate', 'protected',
                          '--rates', '50', '100', '150', '--duration', '180', '--repetitions', '5',
                          '--fault', 'redis-stop', '--fault-at', '60', '--recover-at', '120'])
        self.assertEqual(b.profile, 'resilience')
        self.assertEqual((b.fault, b.fault_at, b.recover_at), ('redis-stop', 60, 120))

    def test_resilience_rejects_bad_fault_timing(self):
        parser = campaign.build_parser()
        cases = [
            ['--profile', 'resilience', '--strategies', 'invalidate', '--rates', '50',
             '--duration', '180', '--fault', 'redis-stop', '--recover-at', '120'],          # missing fault-at
            ['--profile', 'resilience', '--strategies', 'invalidate', '--rates', '50',
             '--duration', '120', '--fault', 'redis-stop', '--fault-at', '60', '--recover-at', '120'],  # recover >= duration
            ['--profile', 'resilience', '--strategies', 'invalidate', '--rates', '50',
             '--duration', '180', '--fault', 'redis-stop', '--fault-at', '120', '--recover-at', '60'],  # reversed
        ]
        for argv in cases:
            with self.assertRaises(SystemExit, msg=argv):
                campaign.validate_args(parser, parser.parse_args(argv))

    def test_capacity_rejects_missing_rates(self):
        parser = campaign.build_parser()
        args = parser.parse_args(['--profile', 'capacity', '--strategies', 'db'])
        with self.assertRaises(SystemExit):
            campaign.validate_args(parser, args)


class FaultWindowTests(unittest.TestCase):
    @staticmethod
    def _write_samples(path, sample_rate=50, total_seconds=30, fault_seconds=(10, 20)):
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

        def point(metric, elapsed, value, status):
            stamp = (base + dt.timedelta(seconds=elapsed)).isoformat().replace('+00:00', 'Z')
            tags = {"operation": "read", "status": status, "strategy": "aside",
                    "scenario": "fixed_arrivals", "expected_response": "true"}
            return json.dumps({"metric": metric, "type": "Point",
                               "data": {"time": stamp, "value": value, "tags": tags}}) + "\n"

        with gzip.open(path, "wt") as f:
            for i in range(sample_rate * total_seconds):
                elapsed = i / sample_rate
                failed = int(fault_seconds[0] <= elapsed < fault_seconds[1])
                status = "500" if failed else "200"
                f.write(point("http_req_duration", elapsed, 5.0, status))
                f.write(point("http_req_failed", elapsed, float(failed), status))
        return sample_rate * total_seconds

    def test_fault_window_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            samples = Path(directory) / "samples.json.gz"
            self._write_samples(samples)
            stats = campaign.fault_window_stats(samples, 10, 20, degraded_p95_ms=500)
        self.assertIsNotNone(stats)
        self.assertAlmostEqual(stats["fault_error_rate"], 1.0, places=2)
        self.assertAlmostEqual(stats["outside_error_rate"], 0.0, places=3)
        self.assertEqual(stats["recovery_observed_seconds"], 0)

    def test_fault_window_missing_samples_is_none(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "nope.json.gz"
            self.assertIsNone(campaign.fault_window_stats(missing, 10, 20, 500))

    def test_recovery_not_detected_within_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            samples = Path(directory) / "samples.json.gz"
            base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
            with gzip.open(samples, "wt") as f:
                for i in range(25 * 30):  # 30s at 25 rps
                    elapsed = i / 25
                    value = 600.0 if elapsed >= 10 else 3.0  # p95 stays above 500 ms budget
                    stamp = (base + dt.timedelta(seconds=elapsed)).isoformat().replace('+00:00', 'Z')
                    f.write(json.dumps({"metric": "http_req_duration", "type": "Point",
                                        "data": {"time": stamp, "value": value,
                                                 "tags": {"operation": "read", "status": "200", "strategy": "aside"}}}) + "\n")
                    f.write(json.dumps({"metric": "http_req_failed", "type": "Point",
                                        "data": {"time": stamp, "value": 0,
                                                 "tags": {"operation": "read", "status": "200", "strategy": "aside"}}}) + "\n")
            stats = campaign.fault_window_stats(samples, 10, 20, degraded_p95_ms=500)
        self.assertEqual(stats["recovery_observed_seconds"], None)
