"""Offline checks for the fixed multi-conversation cache A/B fixture."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/rpc"))
from cache_ab_replay import make_fixture, response_fingerprint, rpc_stage_delta  # noqa: E402


class CacheAbFixture(unittest.TestCase):
    def test_round_robin_is_fixed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.json"
            make_fixture(path)
            first = path.read_bytes()
            rows = json.loads(first)
            self.assertEqual(len(rows), 12)
            self.assertEqual([r["tag"].split(".")[0] for r in rows],
                             ["server-option", "expert-cache", "cuda-kernel"] * 4)
            self.assertTrue(all(r["body"]["max_tokens"] == 16 for r in rows))
            make_fixture(Path(temporary) / "again.json")
            self.assertEqual(first, (Path(temporary) / "again.json").read_bytes())

    def test_longer_fixture_uses_requested_completion_length(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.json"
            make_fixture(path, max_tokens=64)
            rows = json.loads(path.read_text())
            self.assertEqual(len(rows), 12)
            self.assertTrue(all(r["body"]["max_tokens"] == 64 for r in rows))

    def test_stage_delta_differences_cumulative_profiles(self):
        def snapshot(t, ms, socket_ms):
            return {"engine": {"rpc_stage": {
                "coordinator_prefill": {"tokens": t, "chunks": 2, "ms_total": ms,
                                         "ms_experts_host": 3, "experts_streamed": 4,
                                         "experts_dma": 2, "experts_resident": 5, "experts_cpu": 0},
                "worker": {"hardware": {"profile": {"timing_kind": "host_elapsed_overlapping",
                    "costs": {"prefill": {"calls": 6, "ms": ms + 5, "bytes": 20},
                              "checkpoint_capture": {"calls": 1, "ms": 2, "bytes": 0},
                              "checkpoint_restore": {"calls": 0, "ms": 0, "bytes": 0}}}}},
                "client": {"profile": {"costs": {"socket_read": {"calls": 3, "ms": socket_ms,
                                                                      "bytes": 30}}}}}}}
        delta = rpc_stage_delta(snapshot(100, 50, 40), snapshot(160, 75, 62))
        self.assertEqual(delta["coordinator_prefill"]["tokens"], 60)
        self.assertEqual(delta["coordinator_prefill"]["ms_total"], 25)
        self.assertEqual(delta["worker_profile"]["prefill"]["ms"], 25)
        self.assertEqual(delta["coordinator_transport"]["socket_read"]["bytes"], 0)
        self.assertEqual(delta["timing_kind"], "host_elapsed_overlapping")

    def test_response_fingerprint_ignores_transport_generated_tool_ids(self):
        a = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant",
             "content": None, "tool_calls": [{"id": "call-a", "type": "function",
             "function": {"name": "read_file", "arguments": "{\\\"path\\\":\\\"README.md\\\"}"}}]}}]}
        b = json.loads(json.dumps(a).replace("call-a", "call-b"))
        self.assertEqual(response_fingerprint(a), response_fingerprint(b))
        b["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{}"
        self.assertNotEqual(response_fingerprint(a), response_fingerprint(b))


if __name__ == "__main__":
    unittest.main()
