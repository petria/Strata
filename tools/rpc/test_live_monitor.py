import importlib.util
from pathlib import Path
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("live_monitor", Path(__file__).with_name("live_monitor.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class LiveMonitorTest(unittest.TestCase):
    def test_history_ticks_without_inference_and_records_outage_gaps(self):
        class Source:
            healthy = True
            def snapshot(self):
                return {"gpu_monitor": {"sampled_at": time.time() if self.healthy else 1,
                                        "cpu_pct": 42, "disk_read_mb": 2, "ram_used": 123}}
        source = Source()
        server = ThreadingHTTPServer(("127.0.0.1", 0), m.handler(source, []))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        relay = m.Relay(f"http://127.0.0.1:{server.server_port}/metrics")
        try:
            deadline = time.monotonic() + 4
            while len(relay.snapshot().get("history", {}).get("worker_cpu", [])) < 2:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.05)
            data = relay.snapshot()
            self.assertEqual(data["history"]["worker_cpu"], [42, 42])
            self.assertTrue(data["fresh"])
            source.healthy = False
            while relay.snapshot().get("fresh"):
                self.assertLess(time.monotonic(), deadline + 2)
                time.sleep(.05)
            self.assertIsNone(relay.snapshot()["history"]["worker_cpu"][-1])
        finally:
            relay.close()
            server.shutdown()
            server.server_close()

class NetworkSamplerTest(unittest.TestCase):
    def test_elapsed_time_and_binary_units_and_counter_reset(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            stats = Path(root) / "eth-test" / "statistics"
            stats.mkdir(parents=True)
            def counters(rx, tx):
                (stats / "rx_bytes").write_text(str(rx))
                (stats / "tx_bytes").write_text(str(tx))
            sampler = m.NetworkSampler("eth-test", root)
            counters(100, 200)
            self.assertIsNone(sampler.sample(now=10)["down_kib_s"])
            counters(100 + 1024 * 1024, 200 + 4096)
            sample = sampler.sample(now=12)
            self.assertEqual(sample["down_kib_s"], 512)
            self.assertEqual(sample["up_kib_s"], 2)
            counters(1, 1)
            self.assertIsNone(sampler.sample(now=13)["down_kib_s"])

    def test_unavailable_interface_reports_unknown(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            sample = m.NetworkSampler("missing", root).sample()
            self.assertIsNone(sample["down_kib_s"])
            self.assertIsNone(sample["up_kib_s"])
