import unittest

from serve.rpc_monitor import add_remote_gpu


class RpcMonitorTests(unittest.TestCase):
    def setUp(self):
        self.hardware = {
            "gpu_util": 60.0, "gpu_mem_used": 8 * 1024**3, "gpu_mem_total": 24 * 1024**3,
            "gpu_temp": 70, "gpu_power": 250, "gpu_power_limit": 350,
            "gpu_pcie_rx_mb": 100, "gpu_pcie_tx_mb": 50,
        }
        self.info = {"rpc_stage": {"reported_at": 1000, "worker": {"hardware": {
            "gpu_monitor": {"name": "RTX 3090", "util": 40, "mem_used": 12 * 1024**3,
                            "mem_total": 24 * 1024**3, "temp": 60, "power": 200, "power_limit": 350,
                            "pcie_gen": 4, "pcie_gen_max": 4, "pcie_width": 16,
                            "pcie_rx_mb": 300, "pcie_tx_mb": 200, "sampled_at": 998.0}
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


if __name__ == "__main__":
    unittest.main()
