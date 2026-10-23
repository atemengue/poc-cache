"""Opt-in destructive fixtures on an isolated *-validation Compose project.

Run with ORDERFLOW_RUNTIME_TESTS=1 and COMPOSE_PROJECT_NAME=orderflow-cache-validation.
The project must already be running. Never run alongside a benchmark campaign.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_campaign as campaign
from telemetry import RunTelemetry


@unittest.skipUnless(os.environ.get("ORDERFLOW_RUNTIME_TESTS") == "1", "opt-in live stack tests")
class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("COMPOSE_PROJECT_NAME", "").endswith("-validation"):
            raise RuntimeError("Runtime tests require an isolated *-validation Compose project")
        campaign.ensure_healthy()

    def request(self, path, payload=None):
        request = urllib.request.Request(
            campaign.service_url("gateway") + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response), response.headers.get("X-Cache-Source")

    def sql(self, statement):
        return subprocess.check_output(
            ["docker", "compose", "exec", "-T", "db", "psql", "-U", "demo", "-d", "orderflow",
             "-v", "ON_ERROR_STOP=1", "-At", "-c", statement], cwd=campaign.ROOT, text=True).strip()

    def test_concurrent_orders_cannot_oversell(self):
        monitor = RunTelemetry("INVENTORY", "invalidate", campaign.ROOT / "results/runtime", campaign.service_url)
        monitor.__enter__()
        self.addCleanup(monitor.__exit__, None, None, None)
        campaign.configure("invalidate")
        campaign.flush()
        # Dedicated validation fixture, independent of the hot product benchmarks.
        self.sql("DELETE FROM orders WHERE product_id=10000; "
                 "DELETE FROM order_events WHERE product_id=10000; "
                 "UPDATE products SET stock=8, revision=0 WHERE id=10000")
        self.assertEqual(self.request("/products/10000/snapshot")[1]["stock"], 8)
        monitor.marker("consistency", "32 concurrent reservations against stock=8")
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            responses = list(pool.map(lambda _: self.request("/orders", {"product_id": 10000, "quantity": 1}), range(32)))
        stock_revision = self.sql("SELECT stock || ',' || revision FROM products WHERE id=10000")
        orders = self.sql("SELECT count(*) FROM orders WHERE product_id=10000")
        events = self.sql("SELECT count(*) FROM order_events WHERE product_id=10000")
        violations = sum([sum(status == 200 for status, _, _ in responses) != 8,
                          stock_revision != "0,8", orders != "8", events != "8"])
        monitor.probes["orderflow_probe_inventory_violations"] = violations
        monitor.push()
        self.assertEqual(sum(status == 200 for status, _, _ in responses), 8)
        self.assertEqual(sum(status == 409 for status, _, _ in responses), 24)
        self.assertEqual(stock_revision, "0,8")
        self.assertEqual(orders, "8")
        self.assertEqual(events, "8")
        for status, body, _ in responses:
            if status == 200:
                order = self.request(f"/orders/{body['order_id']}")
                self.assertEqual(order[1]["status"], "created")
        self.assertEqual(self.request("/orders", {"product_id": 10000, "quantity": 0})[0], 422)
        self.assertEqual(self.request("/products/10000/snapshot")[1]["stock"], 0)

    def test_protected_cold_burst_coalesces(self):
        campaign.configure("protected", db_extra_ms=300)
        campaign.flush()
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
            responses = list(pool.map(lambda _: self.request("/products/9999/snapshot"), range(24)))
        self.assertTrue(all(status == 200 for status, _, _ in responses))
        self.assertEqual(sum(source == "db_miss" for _, _, source in responses), 1)
        self.assertTrue(all(source in ("db_miss", "cache", "cache_wait") for _, _, source in responses))

    def test_redis_outage_startup_and_recovery(self):
        campaign.compose("stop", "redis1", "redis2")
        try:
            campaign.configure("db")
            self.assertEqual(self.request("/products/9999/snapshot")[2], "db")
            campaign.configure("aside")
            self.assertEqual(self.request("/products/9999/snapshot")[2], "db_fallback")
        finally:
            campaign.compose("up", "-d", "redis1", "redis2")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.request("/products/9999/snapshot")[2] == "cache":
                break
            time.sleep(0.5)
        else:
            self.fail("Cache did not recover after Redis restart")
        # Exercise loss/reconnection of an established connection as well.
        campaign.compose("stop", "redis1")
        try:
            self.assertEqual(self.request("/products/9999/snapshot")[2], "db_fallback")
        finally:
            campaign.compose("up", "-d", "redis1")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.request("/products/9999/snapshot")[2] == "cache":
                break
            time.sleep(0.5)
        else:
            self.fail("Established Redis connection did not recover")

    def test_short_ttl_jitter_is_at_most_ten_percent(self):
        campaign.configure("aside", ttl=1)
        for _ in range(8):
            campaign.flush()
            self.assertEqual(self.request("/products/9999/snapshot")[0], 200)
            ttl = int(subprocess.check_output(
                ["docker", "compose", "exec", "-T", "redis1", "redis-cli", "PTTL", "snapshot:v1:product:9999"],
                cwd=campaign.ROOT, text=True))
            self.assertGreater(ttl, 0)
            self.assertLessEqual(ttl, 1100)


if __name__ == "__main__":
    unittest.main()
