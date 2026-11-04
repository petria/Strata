"""CPU-only checks for the RPC worker's host sampler (include/strata/rpc/worker_system_monitor.hpp).

No model, GPU, network download or root rights: the readings come from a procfs/sysfs tree built in a
temporary folder, which the fixture reads through STRATA_PROCFS_ROOT. One mode reads the machine's own
/proc, so a real kernel's file layout is covered too.
  python -m pytest tests/rpc/test_system_monitor.py     (skips when g++ is missing, as #112's tests do)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "rpc" / "system_monitor_fixture.cpp"

# 11 stats after major, minor and name: reads, merged, rbytes, rtime, writes, merged, wbytes, ...
DISKSTATS = (
    "   8     0 sda 1000 0 2048 100 500 0 4096 200 0 300 0\n"
    "   8     1 sda1 900 0 1024 90 400 0 2048 180 0 0 0\n"
    " 259     0 nvme0n1 50 0 512 10 25 0 256 5 0 20 0\n"
    "   7     0 loop0 8 0 16 1 4 0 8 1 0 2 0\n"
    " 259     1 nvme0n1p1 40 0 256 8 20 0 128 4 0 0 0\n"
    "   8    32 cciss/c0d0 10 0 64 2 5 0 32 1 0 3 0\n"
)
STAT = "cpu  100 10 50 700 25 5 5 0 0 0\n"
MEMINFO = "MemTotal:        1000000 kB\nMemFree:             80000 kB\nMemAvailable:     250000 kB\n"
CPUINFO = "\n".join(
    f"processor\t: {i}\nmodel name\t: AMD Ryzen 9 5900X 12-Core Processor\nvendor_id\t: AuthenticAMD\n"
    for i in range(4)
) + "\n"


def make_tree(base: Path, cpuinfo: str = CPUINFO) -> Path:
    """A procfs and sysfs shaped like Linux's, with four threads on two cores."""
    (base / "proc").mkdir(parents=True, exist_ok=True)
    (base / "proc" / "stat").write_text(STAT)
    (base / "proc" / "diskstats").write_text(DISKSTATS)
    (base / "proc" / "meminfo").write_text(MEMINFO)
    (base / "proc" / "cpuinfo").write_text(cpuinfo)
    for device in ("sda", "nvme0n1", "loop0", "cciss!c0d0"):
        (base / "sys" / "block" / device).mkdir(parents=True, exist_ok=True)
    # Two physical cores, each with an SMT sibling; the sibling shares its (package, core) pair.
    topology = [(0, 0), (0, 0), (0, 1), (0, 1)]
    for index, (package, core) in enumerate(topology):
        topo = base / "sys" / "devices" / "system" / "cpu" / f"cpu{index}" / "topology"
        topo.mkdir(parents=True, exist_ok=True)
        (topo / "physical_package_id").write_text(f"{package}\n")
        (topo / "core_id").write_text(f"{core}\n")
    for name in ("cpufreq", "cpuidle", "online"):        # not cpuN entries: must not count as threads
        (base / "sys" / "devices" / "system" / "cpu" / name).mkdir(parents=True, exist_ok=True)
    return base


class SystemMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("g++") is None:
            raise unittest.SkipTest("g++ is not installed")
        cls.tmp = tempfile.TemporaryDirectory(prefix="strata-system-monitor-")
        exe = Path(cls.tmp.name) / "system_monitor_fixture"
        subprocess.run(
            ["g++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-pthread", "-g", "-O1",
             "-I" + str(ROOT / "include"), str(FIXTURE), "-o", str(exe)],
            check=True, capture_output=True, text=True)
        cls.exe = exe

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_mode(self, mode: str, root: Path | None = None):
        env = dict(os.environ)
        env.pop("STRATA_PROCFS_ROOT", None)
        if root is not None:
            env["STRATA_PROCFS_ROOT"] = str(root)
        done = subprocess.run([str(self.exe), mode], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(0, done.returncode, f"{mode}: {done.stdout}{done.stderr}")

    def test_math_needs_no_files(self):
        self.run_mode("math")

    def test_fixture_tree(self):
        with tempfile.TemporaryDirectory(prefix="strata-procfs-") as tmp:
            self.run_mode("fixture", make_tree(Path(tmp)))

    def test_missing_procfs_stays_null(self):
        with tempfile.TemporaryDirectory(prefix="strata-procfs-empty-") as tmp:
            self.run_mode("empty", Path(tmp))

    def test_cpu_name_cannot_break_the_json(self):
        with tempfile.TemporaryDirectory(prefix="strata-procfs-quote-") as tmp:
            tree = make_tree(Path(tmp), cpuinfo='processor\t: 0\nmodel name\t: Bad "name" here\n')
            self.run_mode("quotes", tree)

    @unittest.skipUnless(sys.platform.startswith("linux"), "/proc is a Linux interface")
    def test_the_machines_own_procfs(self):
        self.run_mode("live")

if __name__ == "__main__":
    unittest.main()
