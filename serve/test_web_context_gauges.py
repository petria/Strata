"""The Context-fill resource bars show both RPC hosts without changing single-host behavior."""
from __future__ import annotations

import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent / "web"


class ContextResourceGauges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.js = (WEB / "app.js").read_text(encoding="utf-8")
        cls.css = (WEB / "app.css").read_text(encoding="utf-8")

    def test_each_resource_keeps_single_host_markup_and_has_two_rpc_rows(self):
        for key in ("slots", "ram", "temp"):
            with self.subTest(resource=key):
                self.assertIn(f'id="{key}-single"', self.html)
                self.assertIn(f'id="{key}-rpc" hidden', self.html)
                for gpu in (0, 1):
                    self.assertIn(f'id="{key}-gpu{gpu}-label"', self.html)
                    self.assertIn(f'id="{key}-gpu{gpu}-text"', self.html)
                    self.assertIn(f'id="{key}-gpu{gpu}-bar"', self.html)
                    label = f"NODE #{gpu}"
                    self.assertIn(f'id="{key}-gpu{gpu}-label">{label}</span>', self.html)
                self.assertIn(f'$(`${{key}}-single`).hidden = false;', self.js)
                self.assertIn(f'$(`${{key}}-rpc`).hidden = true;', self.js)

    def test_rpc_rows_use_host_specific_resource_data(self):
        self.assertIn('const workerHardware = eng.rpc_stage && eng.rpc_stage.worker && eng.rpc_stage.worker.hardware || {};', self.js)
        self.assertIn('slots: eng.expert_slots, cacheMiB: eng.expert_cache_mib', self.js)
        self.assertIn('slots: workerHardware.expert_slots, cacheMiB: workerHardware.expert_cache_mib', self.js)
        self.assertIn('ramUsed: hw.ram_used, ramTotal: hw.ram_total', self.js)
        self.assertIn('ramUsed: worker.ram_used, ramTotal: worker.ram_total', self.js)
        self.assertIn('temp: gpu0.temp', self.js)
        self.assertIn('temp: worker.temp', self.js)
        self.assertIn('cacheBytes / totalVram', self.js)
        self.assertIn('100 * row.ramUsed / row.ramTotal', self.js)

    def test_missing_worker_values_are_handled_without_age_or_stale_labels(self):
        self.assertIn('expertParts.length ? expertParts.join(" · ") : "–"', self.js)
        self.assertIn('hasRam ? `${gb(row.ramUsed)} / ${gb(row.ramTotal, 0)} GB` : "–"', self.js)
        self.assertIn('row.temp == null ? "–"', self.js)
        self.assertNotIn("stale", self.js)
        self.assertNotIn("sample_age_s", self.js)
        self.assertNotIn("s ago", self.js)

    def test_context_and_recent_request_cards_stretch_to_equal_height(self):
        self.assertIn("align-items: stretch", self.css)
        self.assertIn(".monitor-row > .st-card { align-self: stretch; }", self.css)

    def test_resource_labels_match_rpc_node_labels_without_role_text(self):
        self.assertNotIn("Coordinator ·", self.js)
        self.assertNotIn("Worker ·", self.js)
        self.assertNotIn("Coordinator", self.html)
        self.assertNotIn("RPC worker", self.html)

    def test_all_context_resources_use_rpc_stack_node_labels(self):
        self.assertIn('label: "NODE #0", ramLabel: "NODE #0"', self.js)
        self.assertIn('label: "NODE #1", ramLabel: "NODE #1"', self.js)
        self.assertIn('$(`ram-${suffix}-label`).textContent = row.ramLabel;', self.js)


if __name__ == "__main__":
    unittest.main()
