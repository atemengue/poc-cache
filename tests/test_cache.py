"""Static contract checks. Rust cache's algorithmic unit tests live in app/src/cache.rs.

Run ./poc test-rust to compile and execute the native Rust tests.
"""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class RustContractTests(unittest.TestCase):
    def test_rust_main_and_cache_present(self):
        self.assertTrue((ROOT / 'app/src/main.rs').is_file())
        self.assertTrue((ROOT / 'app/src/cache.rs').is_file())
        self.assertTrue((ROOT / 'app/Cargo.toml').is_file())
        self.assertFalse((ROOT / 'app/main.py').exists())

    def test_benchmark_endpoint_contract(self):
        code = (ROOT / 'app/src/main.rs').read_text()
        for path in ('/products/:id/snapshot', '/products/:id/activity', '/products/:id/revision',
                     '/orders', '/orders/:id', '/health', '/metrics'):
            self.assertIn(path, code)
        self.assertIn('X-Cache-Source', code)

    def test_metrics_contract(self):
        code = (ROOT / 'app/src/metrics.rs').read_text()
        for name in ('cache_lookup_total', 'db_queries_total', 'http_request_duration_seconds'):
            self.assertIn(name, code)

    def test_cache_variants(self):
        code = (ROOT / 'app/src/config.rs').read_text()
        for name in ('db', 'aside', 'invalidate', 'write_through', 'protected', 'l1l2', 'sharded'):
            self.assertIn('"'+name+'"', code)
