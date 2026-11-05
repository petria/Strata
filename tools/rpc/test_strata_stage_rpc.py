import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import strata_stage_rpc as rpc


class RpcStageRunnerTest(unittest.TestCase):
    def test_load_profile_expands_paths_and_nested_values(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "profile.json"
            config.write_text(json.dumps({"exe": "$STRATA_ROOT/bin/strata", "args": ["$TLS_DIR/ca.crt"]}))
            with mock.patch.dict(os.environ, {"STRATA_ROOT": "/srv/strata", "TLS_DIR": "/state/tls"}):
                self.assertEqual(rpc.load_profile(str(config)),
                                 {"exe": "/srv/strata/bin/strata", "args": ["/state/tls/ca.crt"]})

    def test_missing_profile_variable_is_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "profile.json"
            config.write_text('{"exe":"$NOT_CONFIGURED/bin/strata"}')
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(KeyError):
                    rpc.load_profile(str(config))

    def test_worker_execs_profile_with_expected_gpu_visibility(self):
        profile = {"exe": "/bundle/strata", "cwd": "/bundle", "args": ["--serve"],
                   "env": {"STRATA_CUDA_DOORBELL_MODE": "increment"}}
        with mock.patch.object(rpc, "load_profile", return_value=profile), \
             mock.patch.object(rpc.os, "chdir") as chdir, \
             mock.patch.object(rpc.os, "execve") as execve:
            rpc.run_worker("unused.json")
        chdir.assert_called_once_with("/bundle")
        args, env = execve.call_args.args[1:]
        self.assertEqual(args, ["/bundle/strata", "--serve"])
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "0")
        self.assertEqual(env["STRATA_CUDA_DOORBELL_MODE"], "increment")

    def test_coordinator_renders_config_then_execs_server(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "bundle"
            state = Path(temp) / "state"
            root.mkdir()
            profile = {"cwd": str(root), "env": {"STRATA_CUDA_DOORBELL_MODE": "store"}}
            with mock.patch.object(rpc, "load_profile", return_value=profile), \
                 mock.patch.dict(os.environ, {"STRATA_ROOT": str(root)}), \
                 mock.patch.object(rpc.os, "chdir") as chdir, \
                 mock.patch.object(rpc.os, "execve") as execve:
                rpc.run_coordinator("unused.json", str(state), "0.0.0.0", 8080)
            rendered = json.loads((state / "coordinator.json").read_text())
            self.assertEqual(rendered, profile)
            chdir.assert_called_once_with(str(root))
            executable, args, env = execve.call_args.args
            self.assertEqual(executable, os.sys.executable)
            self.assertEqual(args[1], str(root / "serve/server.py"))
            self.assertEqual(args[-2:], ["--port", "8080"])
            self.assertEqual(env["STRATA_CUDA_DOORBELL_MODE"], "store")


if __name__ == "__main__":
    unittest.main()
