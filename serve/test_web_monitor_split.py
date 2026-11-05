"""serve/test_web_monitor_split.py - the Monitor's CPU and Disk read cards split left/right, this host on
the left and the RPC worker on the right, like the Speed card.

Reads the page source for what must hold whatever the code looks like: the halves exist only for those two
cards, the worker's number is never mixed into this host's, and the curve the right half draws is named the
same in the page and in serve/rpc_monitor.py.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from serve.rpc_monitor import WORKER_SERIES

APP = Path(__file__).resolve().parent / "web" / "app.js"


class SplitCards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text(encoding="utf-8")

    def metrics_table(self) -> str:
        table = self.src[self.src.index("const METRICS = ["):]
        return table[:table.index("];")]

    def test_only_the_cpu_and_disk_cards_split(self):
        table = self.metrics_table()
        for key in ("cpu", "disk"):
            self.assertRegex(table, rf'\{{key: "{key}"[^}}]*split: true\}}')
        split_lines = [line for line in table.splitlines() if "split: true" in line]
        self.assertEqual(2, len(split_lines), "only the CPU and Disk read cards split")
        for line in split_lines:
            self.assertTrue('key: "cpu"' in line or 'key: "disk"' in line, line)

    def test_the_worker_half_starts_hidden_and_has_its_own_ids(self):
        for key in ("cpu", "disk"):
            for marker in (f'id="mw-${{m.key}}" hidden',           # the right half, off until a worker is there
                           f'id="mv-${{m.key}}-worker"', f'id="ms-${{m.key}}-worker"',   # its number and its sentence
                           f'id="sp-${{right}}"'):                 # its own curve, from the same id as a split card's
                self.assertTrue(marker in self.src, f"the page has no {marker}")
        self.assertIn('const right = m.key === "speed" ? "prefill" : `${m.key}-worker`;', self.src)
        self.assertIn('id="mv-prefill"', self.src)          # the Speed card keeps its own ids

    def test_the_halves_are_shown_by_the_workers_presence(self):
        self.assertRegex(self.src, r'const worker = \(hw\.gpus \|\| \[\]\)\.find\(\(g\) => g\.source === "rpc-worker"\)')
        for key in ("cpu", "disk"):
            self.assertRegex(self.src, rf'\$\("mw-{key}"\)\.hidden = !worker;')

    def test_this_hosts_number_stays_this_hosts(self):
        # The left half reads the coordinator's readings; the right half reads the worker's row. A mean of
        # two hosts' CPU percent answers nothing.
        self.assertRegex(self.src, r'setMetric\("cpu", hw\.cpu == null')
        self.assertRegex(self.src, r'setMetric\("cpu-worker", worker\.cpu_pct == null')
        self.assertRegex(self.src, r'setMetric\("disk", big \? fmt\(hw\.disk_read_mb / 1024')
        self.assertRegex(self.src, r'const wr = worker\.disk_read_mb, wbig = ')
        self.assertRegex(self.src, r'setMetric\("disk-worker", wr == null')
        # The worker's write rate reads the worker's row, as this host's reads the coordinator's.
        self.assertRegex(self.src, r'hw\.disk_write_mb[\s\S]{0,400}worker\.disk_write_mb')

    def test_the_worker_curve_comes_from_its_own_series(self):
        # The curves plot the worker's series, never this host's: a split card drawing h.cpu twice says
        # nothing about the second machine.
        plotted = set(re.findall(r'spark\("sp-([a-z]+)-worker", h\.([a-z_]+)', self.src))
        self.assertEqual({("cpu", "worker_cpu"), ("disk", "worker_disk_read_mb")}, plotted)
        self.assertIn('spark("sp-cpu", h.cpu, 100)', self.src)
        self.assertIn('spark("sp-disk", h.disk_read_mb)', self.src)

    def test_the_page_and_the_server_agree_on_the_series_names(self):
        # Every series the right halves plot is one serve/rpc_monitor.py records, and the page names no
        # series the server does not send.
        plotted = set(re.findall(r'spark\("sp-[a-z]+-worker", h\.([a-z_]+)', self.src))
        self.assertTrue(plotted, "the split cards plot no worker series")
        self.assertTrue(plotted <= set(WORKER_SERIES.values()),
                        f"the page plots a series the server does not record: {sorted(plotted - set(WORKER_SERIES.values()))}")

    def test_worker_subtitles_have_no_role_prefix_or_age_counter(self):
        self.assertNotIn("Coordinator ·", self.src)
        self.assertNotIn("Worker ·", self.src)
        self.assertNotIn("stale", self.src)
        self.assertNotIn("sample_age_s", self.src)
        self.assertNotIn("s ago", self.src)

    def test_the_rpc_stack_card_labels_the_worker_from_its_own_row(self):
        self.assertIn('["CPU1", worker.cpu_name', self.src)

    def test_rpc_stack_groups_cpu_and_gpu_rows_under_each_node(self):
        positions = [self.src.index(value) for value in (
            'title: "NODE #0"', '["CPU0", statics.cpu_name', '["GPU0", coordinator?.name',
            'title: "NODE #1"', '["CPU1", worker.cpu_name', '["GPU1", worker.name',
        )]
        self.assertEqual(sorted(positions), positions)
        self.assertIn('group.className = "rpc-node"', self.src)
        self.assertIn('heading.className = "rpc-node__heading"', self.src)


if __name__ == "__main__":
    unittest.main()
