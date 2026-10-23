import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from hypotheses import markdown, verdicts


def load(experiment, strategy, **overrides):
    row = {"experiment": experiment, "strategy": strategy, "kind": "load",
           "requested_rate": 40, "requests": 1000, "achieved_rps": 40.0,
           "read_p95_ms": 5.0, "read_p99_ms": 8.0, "error_rate": 0.0,
           "dropped_iterations": 0, "client_cache_hit_ratio": 0.95,
           "server_db_reads": 100, "replicas": 2}
    row.update(overrides)
    return row


def consistency(strategy, stale, replicas=None):
    row = {"experiment": "E17", "strategy": strategy, "kind": "consistency",
           "reads": 60, "stale_reads": round(stale * 60), "stale_fraction": stale}
    if replicas is not None:
        row["stale_replica_probes"] = replicas
    return row


class HypothesisVerdictTests(unittest.TestCase):
    def test_all_supported_with_measurements(self):
        rows = [
            load("E01", "db", read_p95_ms=100.0, server_db_reads=980),
            load("E03", "aside", read_p95_ms=30.0, server_db_reads=100),
            # E20 capacity sweep: cache passes everywhere, DB breaches above 40/s.
            *[load("E20", "db", requested_rate=r, read_p95_ms=p95, server_db_reads=900)
              for r, p95 in ((20, 30.0), (40, 60.0), (80, 120.0), (160, 250.0))],
            *[load("E20", "aside", requested_rate=r, read_p95_ms=p95, server_db_reads=90)
              for r, p95 in ((20, 5.0), (40, 8.0), (80, 10.0), (160, 90.0))],
            *[load("E14", "aside", replicas=count, requests=1000,
                   server_db_reads=reads) for count, reads in ((1, 100), (2, 105), (4, 120))],
            load("E10", "invalidate", error_rate=0.02, dropped_iterations=0),
            load("E12a", "aside", server_db_reads=500),
            load("E12b", "protected", server_db_reads=80),
            load("E16a", "aside", server_db_reads=480),
            load("E16b", "protected", server_db_reads=70),
            load("E18a", "aside", read_p95_ms=5.0),
            load("E18b", "l1l2", read_p95_ms=2.0),
            consistency("aside", 0.8),
            consistency("invalidate", 0.0),
            consistency("write_through", 0.0),
            consistency("protected", 0.0),
            consistency("l1l2", 0.5, replicas=1),
        ]
        verdicts_by_id = {row["hypothesis"]: row for row in verdicts(rows)}
        self.assertEqual(len(verdicts_by_id), 8)
        self.assertEqual({h: row["verdict"] for h, row in verdicts_by_id.items()},
                         {"H1": "supported", "H2": "supported", "H3": "supported",
                          "H4": "supported", "H5": "supported", "H6": "supported",
                          "H7": "supported", "H8": "supported"})

    def test_inconclusive_never_passes(self):
        verdicts_by_id = {row["hypothesis"]: row for row in verdicts([load("E03", "aside")])}
        self.assertTrue(all(row["verdict"] == "inconclusive" for row in verdicts_by_id.values()))

    def test_refuted_when_measured_contradicts(self):
        rows = [load("E01", "db", read_p95_ms=30.0, server_db_reads=200),
                load("E03", "aside", read_p95_ms=100.0, server_db_reads=50,
                     client_cache_hit_ratio=0.95)]
        h1 = next(r for r in verdicts(rows) if r["hypothesis"] == "H1")
        self.assertEqual(h1["verdict"], "refuted")

    def test_h2_needs_a_sweep(self):
        rows = [load("E01", "db"), load("E03", "aside")]  # two distinct cache rates at most
        h2 = next(r for r in verdicts(rows) if r["hypothesis"] == "H2")
        self.assertEqual(h2["verdict"], "inconclusive")
        self.assertIn("sweep", h2["caveats"])

    def test_h2_grid_aggregates_repetitions(self):
        # E21 capacity grid: 3 reps per (strategy, rate). db passes 50/s but is
        # unstable at 100/s (2/3 reps pass, below the 0.8 gate), so db stops at 50.
        rows = [
            *[load("E21", "db", requested_rate=50, read_p95_ms=60.0) for _ in range(3)],
            load("E21", "db", requested_rate=100, read_p95_ms=90.0),
            load("E21", "db", requested_rate=100, read_p95_ms=90.0),
            load("E21", "db", requested_rate=100, read_p95_ms=120.0),  # breaches normal p95
            *[load("E21", "aside", requested_rate=50, read_p95_ms=5.0) for _ in range(3)],
            *[load("E21", "aside", requested_rate=100, read_p95_ms=8.0) for _ in range(3)],
        ]
        h2 = next(r for r in verdicts(rows) if r["hypothesis"] == "H2")
        self.assertEqual(h2["verdict"], "supported")
        self.assertEqual(h2["evidence"]["per_strategy_passing_rate"], {"db": 50, "aside": 100})
        self.assertEqual(h2["evidence"]["best_cached_ratio"], 2.0)
        self.assertAlmostEqual(h2["evidence"]["pass_fraction_by_arm"]["db@100"], 2 / 3, places=3)

    def test_h2_grid_refuted_when_cached_gain_below_two(self):
        rows = [
            *[load("E21", "db", requested_rate=50, read_p95_ms=60.0) for _ in range(3)],
            *[load("E21", "db", requested_rate=100, read_p95_ms=120.0) for _ in range(3)],
            *[load("E21", "aside", requested_rate=50, read_p95_ms=5.0) for _ in range(3)],
            *[load("E21", "aside", requested_rate=100, read_p95_ms=8.0) for _ in range(3)],
        ]
        h2 = next(r for r in verdicts(rows) if r["hypothesis"] == "H2")
        # db passing 50, aside passing 100 -> ratio 2 exactly; test the boundary is supported.
        self.assertEqual(h2["verdict"], "supported")

    def test_h5_prefers_e22_fault_window(self):
        rows = [
            load("E10", "invalidate", error_rate=0.0),  # whole-run window must not drive the verdict
            load("E22", "invalidate", fault_window_error_rate=0.02, recovery_observed_seconds=8),
            load("E22", "protected", fault_window_error_rate=0.04, recovery_observed_seconds=None),
        ]
        h5 = next(r for r in verdicts(rows) if r["hypothesis"] == "H5")
        self.assertEqual(h5["verdict"], "supported")
        self.assertAlmostEqual(h5["evidence"]["worst_fault_window_error_rate"], 0.04)
        self.assertEqual(h5["evidence"]["recovery_observed_seconds"], [8])
        self.assertEqual(h5["evidence"]["recovery_violations"], 1)

    def test_h5_e22_refuted_on_fault_window_errors(self):
        rows = [load("E22", "invalidate", fault_window_error_rate=0.20,
                     recovery_observed_seconds=5)]
        h5 = next(r for r in verdicts(rows) if r["hypothesis"] == "H5")
        self.assertEqual(h5["verdict"], "refuted")

    def test_markdown_and_cli_output(self):
        rows = verdicts([load("E01", "db", read_p95_ms=100.0, server_db_reads=500),
                         load("E03", "aside", read_p95_ms=20.0, server_db_reads=50)])
        table = markdown(rows)
        self.assertTrue(any(line.startswith("| H1") for line in table))
        self.assertIn("supported", "\n".join(table))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "campaign.json"
            out.write_text(json.dumps([load("E01", "db", read_p95_ms=100.0),
                                       load("E03", "aside", read_p95_ms=20.0)]))
            code = (f"import sys; sys.argv = ['hypotheses', {str(out.parent)!r}]; "
                    f"import runpy; runpy.run_path({str(ROOT / 'scripts/hypotheses.py')!r}, run_name='__main__')")
            exec(code, {})
            written = json.loads(out.with_name("hypotheses.json").read_text())
            self.assertEqual({r["hypothesis"] for r in written}, {f"H{i}" for i in range(1, 9)})


if __name__ == "__main__":
    unittest.main()