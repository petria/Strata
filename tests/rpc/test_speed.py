"""CPU-only speed-policy regression tests. No model, GPU or network downloads."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SpeedTests(unittest.TestCase):
    def test_cpp_policies(self):
        with tempfile.TemporaryDirectory(prefix="strata-speed-cpu-") as tmp:
            exe = Path(tmp) / "fixture"
            flags = ["-std=c++20", "-Wall", "-Wextra", "-Werror", "-pthread", "-g", "-O1"]
            if os.environ.get("STRATA_SPEED_SANITIZE") == "1":
                flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
            subprocess.run(["g++", *flags, "-I" + str(ROOT / "include"),
                            str(ROOT / "tests/rpc/speed_fixture.cpp"), str(ROOT / "src/rpc/transport.cpp"),
                            "-lssl", "-lcrypto", "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True, timeout=60)


if __name__ == "__main__":
    unittest.main()
