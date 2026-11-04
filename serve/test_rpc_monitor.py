from pathlib import Path
import unittest

from serve.frontend import ChatTemplate
from serve.server import ByteTokenizer, MockEngine, Service
from serve.rpc_monitor import WORKER_SERIES, add_remote_gpu, worker_row


def stage(sampled_at, cpu_pct=37.5, disk_read_mb=4.25, disk_write_mb=0.75, ram_used=20 * 1024**3):
    """An RPCMETRICS snapshot as the engine publishes it, with the worker host's readings."""
    return {"reported_at": sampled_at, "worker": {"hardware": {"gpu_monitor": {
        "name": "RTX 3090", "util": 40, "sampled_at": sampled_at, "cpu_pct": cpu_pct,
        "disk_read_mb": disk_read_mb, "disk_write_mb": disk_write_mb,
        "ram_used": ram_used, "ram_total": 32 * 1024**3,
        "cpu_name": "AMD Ryzen 9 5900X 12-Core Processor", "cores": 12, "threads": 24}}}}


class WorkerHistoryTests(unittest.TestCase):
    """GET /metrics' history for the worker's own series. The local sampler can only read its own
    machine, so serve/server.py records the worker's samples as RPCMETRICS lines arrive."""

    def setUp(self):
        self.tok = ByteTokenizer()
        self.engine = MockEngine(self.tok, "Hello.", max_context=4096)
        self.engine.info = {}
        self.svc = Service(self.engine, self.tok, ChatTemplate(Path(__file__).parent / "chat_template.jinja"))
        self.svc.rpc_monitor_remote_gpu = True

    def test_no_worker_leaves_the_history_as_the_sampler_wrote_it(self):
        self.assertNotIn("worker_cpu", self.svc.metrics()["history"])
        self.assertNotIn("worker_disk_read_mb", self.svc.metrics()["history"])

    def test_a_worker_sample_adds_its_point_to_each_series(self):
        self.engine.info["rpc_stage"] = stage(1000.0)
        history = self.svc.metrics()["history"]
        self.assertEqual(history["worker_cpu"], [37.5])
        self.assertEqual(history["worker_disk_read_mb"], [4.25])
        self.assertEqual(history["worker_ram_used"], [20 * 1024**3])
        # The write rate is a card's sub-line on both hosts, so it gets no curve of its own.
        self.assertNotIn("worker_disk_write_mb", history)

    def test_one_line_is_one_point_on_the_curve(self):
        self.engine.info["rpc_stage"] = stage(1000.0)
        self.svc.metrics()
        self.svc.metrics()                       # the page polls every second; the sample is the same
        self.assertEqual(len(self.svc.metrics()["history"]["worker_cpu"]), 1)
        self.engine.info["rpc_stage"] = stage(1001.0, cpu_pct=52.0)
        self.assertEqual(self.svc.metrics()["history"]["worker_cpu"], [37.5, 52.0])

    def test_an_older_sample_adds_nothing(self):
        self.engine.info["rpc_stage"] = stage(1000.0)
        self.svc.metrics()
        self.engine.info["rpc_stage"] = stage(999.0, cpu_pct=90.0)
        self.assertEqual(self.svc.metrics()["history"]["worker_cpu"], [37.5])

    def test_the_local_series_are_their_own(self):
        self.engine.info["rpc_stage"] = stage(1000.0)
        m = self.svc.metrics()
        # The worker is the second row, and its host readings stay on that row.
        self.assertEqual([g["source"] for g in m["hardware"]["gpus"]], ["local", "rpc-worker"])
        self.assertEqual(m["hardware"]["gpus"][1]["cpu_pct"], 37.5)
        self.assertEqual(m["hardware"]["gpus"][1]["disk_write_mb"], 0.75)   # the row carries it for the card's sub-line
        self.assertNotIn("cpu_pct", m["hardware"])

    def test_a_sample_without_a_time_is_not_counted(self):
        self.engine.info["rpc_stage"] = stage(None)
        self.assertEqual(self.svc.metrics()["history"]["worker_cpu"], [])

    def test_the_native_line_carries_the_worker_readings_to_the_projection(self):
        # src/program/generate.cpp puts the worker's telemetry object after "RPCMETRICS " unchanged, so the
        # host readings ride the same line as PCIe and GPU temp. This is that line: parse it as the engine
        # reader does, then read what the Monitor's cards get from it.
        from serve.server import StrataEngine
        line = ('RPCMETRICS {"phase":"baseline","client":{"windows":1},"worker":{"windows":3,"prefill_rows":9,'
                '"accepted_rows":2,"checkpoint_bytes":0,"sent_bytes":10,"received_bytes":11,"compute_ms":1.0,'
                '"hardware":{"expert_slots":1200,"gpu_monitor":{"name":"RTX 3090","util":40,"sampled_at":1000.0,'
                '"cpu_pct":37.5,"disk_read_mb":4.25,"disk_write_mb":0.75,"ram_used":21474836480,'
                '"ram_total":34359738368,"cpu_name":"AMD Ryzen 9 5900X 12-Core Processor","cores":12,"threads":24}},'
                '"coordinator_prefill":{"tokens":0}}}')
        sink = type("EngineWithoutAProcess", (), {"info": {}})()     # the reader writes only engine.info
        StrataEngine._parse_rpc_metrics(sink, line)
        self.engine.info = sink.info
        metrics = self.svc.metrics()
        row = worker_row(metrics["hardware"])
        self.assertEqual((row["cpu_pct"], row["disk_read_mb"], row["disk_write_mb"]), (37.5, 4.25, 0.75))
        self.assertEqual(row["cpu_name"], "AMD Ryzen 9 5900X 12-Core Processor")
        self.assertEqual(metrics["history"]["worker_cpu"], [37.5])
        self.assertEqual(metrics["history"]["worker_disk_read_mb"], [4.25])

    def test_the_worker_series_are_the_local_ones_under_a_worker_prefix(self):
        # The two hosts' curves have to be the same shape, because the cards show them side by side. The
        # local sampler's names are the base: worker_cpu is the worker's cpu, and nothing else.
        from serve.telemetry import SERIES
        for series in WORKER_SERIES.values():
            self.assertTrue(series.startswith("worker_"), series)
            self.assertIn(series[len("worker_"):], SERIES, series)



class RpcMonitorTests(unittest.TestCase):
    def setUp(self):
        self.hardware = {
            "gpu_util": 60.0, "gpu_mem_used": 8 * 1024**3, "gpu_mem_total": 24 * 1024**3,
            "gpu_temp": 70, "gpu_power": 250, "gpu_power_limit": 350,
            "gpu_pcie_rx_mb": 100, "gpu_pcie_tx_mb": 50,
            "cpu": 12.5, "ram_used": 10 * 1024**3, "ram_total": 64 * 1024**3,
            "disk_read_mb": 3.0, "disk_write_mb": 1.0,
        }
        self.info = {"rpc_stage": {"reported_at": 1000, "worker": {"hardware": {
            "gpu_monitor": {"name": "RTX 3090", "util": 40, "mem_used": 12 * 1024**3,
                            "mem_total": 24 * 1024**3, "temp": 60, "power": 200, "power_limit": 350,
                            "pcie_gen": 4, "pcie_gen_max": 4, "pcie_width": 16,
                            "pcie_rx_mb": 300, "pcie_tx_mb": 200, "sampled_at": 998.0,
                            # the worker host's own readings, from include/strata/rpc/worker_system_monitor.hpp
                            "cpu_pct": 37.5, "cores": 12, "threads": 24,
                            "cpu_name": "AMD Ryzen 9 5900X 12-Core Processor",
                            "ram_used": 20 * 1024**3, "ram_total": 32 * 1024**3,
                            "disk_read_mb": 4.25, "disk_write_mb": 0.75}
        }}}}

    def test_disabled_returns_unmodified_local_metrics(self):
        self.assertIs(add_remote_gpu(self.hardware, self.info, False, now=1000), self.hardware)

    def test_enabled_adds_second_gpu_and_aggregates(self):
        result = add_remote_gpu(self.hardware, self.info, True, now=1000,
                                hardware_static={"gpu_name": "RTX 5090", "gpu_vram_total_mib": 32768})
        self.assertEqual([(g["index"], g["source"]) for g in result["gpus"]],
                         [(0, "local"), (1, "rpc-worker")])
        self.assertEqual(result["gpus"][1]["name"], "RTX 3090")
        self.assertFalse(result["gpus"][1]["stale"])
        self.assertEqual(result["gpu_util"], 50)
        self.assertEqual(result["gpu_mem_used"], 20 * 1024**3)
        self.assertEqual(result["gpu_mem_total"], 48 * 1024**3)
        self.assertEqual(result["gpu_temp"], 70)
        self.assertEqual(result["gpu_power"], 450)
        self.assertEqual(result["gpu_pcie_rx_mb"], 400)

    def test_partial_and_old_samples_remain_explicit(self):
        remote = self.info["rpc_stage"]["worker"]["hardware"]["gpu_monitor"]
        remote["util"] = None
        remote["sampled_at"] = 970
        result = add_remote_gpu({}, self.info, True, now=1000)
        self.assertTrue(result["gpus"][1]["stale"])
        self.assertEqual(result["gpus"][1]["sample_age_s"], 30)
        self.assertIsNone(result["gpus"][1]["util"])
        self.assertNotIn("gpu_util", result)

    def test_missing_worker_telemetry_does_not_invent_gpu(self):
        self.assertEqual(add_remote_gpu(self.hardware, {}, True, now=1000), self.hardware)

    def test_worker_host_readings_land_on_its_row(self):
        result = add_remote_gpu(self.hardware, self.info, True, now=1000)
        row = worker_row(result)
        self.assertIsNotNone(row)
        self.assertEqual(row["cpu_pct"], 37.5)
        self.assertEqual((row["cores"], row["threads"]), (12, 24))
        self.assertEqual(row["cpu_name"], "AMD Ryzen 9 5900X 12-Core Processor")
        self.assertEqual(row["ram_used"], 20 * 1024**3)
        self.assertEqual((row["disk_read_mb"], row["disk_write_mb"]), (4.25, 0.75))

    def test_host_readings_stay_out_of_the_coordinator_totals(self):
        # A mean of two machines' CPU percent, or a sum of their RAM, answers nothing: the top-level
        # figures are the coordinator's, and the worker's live on its own row.
        result = add_remote_gpu(self.hardware, self.info, True, now=1000)
        self.assertEqual(result["cpu"], 12.5)
        self.assertEqual(result["ram_used"], 10 * 1024**3)
        self.assertEqual(result["disk_read_mb"], 3.0)
        self.assertNotIn("cpu_pct", result)

    def test_a_worker_without_host_readings_still_shows_its_gpu(self):
        for key in ("cpu_pct", "disk_read_mb", "disk_write_mb", "cpu_name", "cores", "threads",
                    "ram_used", "ram_total"):
            self.info["rpc_stage"]["worker"]["hardware"]["gpu_monitor"][key] = None
        result = add_remote_gpu(self.hardware, self.info, True, now=1000)
        row = worker_row(result)
        self.assertEqual(row["name"], "RTX 3090")
        self.assertIsNone(row["cpu_pct"])
        self.assertIsNone(row["disk_read_mb"])
        self.assertIsNone(row["cpu_name"])

    def test_no_row_without_the_projection(self):
        self.assertIsNone(worker_row(add_remote_gpu(self.hardware, self.info, False, now=1000)))
        self.assertIsNone(worker_row(self.hardware))

    def test_the_series_names_are_the_ones_the_page_reads(self):
        self.assertEqual(WORKER_SERIES,
                         {"cpu_pct": "worker_cpu", "disk_read_mb": "worker_disk_read_mb",
                          "ram_used": "worker_ram_used"})


if __name__ == "__main__":
    unittest.main()
