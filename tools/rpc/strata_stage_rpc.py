#!/usr/bin/env python3
"""Run either side of the local two-host Strata RPC profile."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import string
import sys


def _expand(value):
    if isinstance(value, str):
        expanded = string.Template(value).substitute(os.environ)
        if "$" in expanded:
            raise ValueError(f"unexpanded variable in RPC profile value: {value}")
        return expanded
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def load_profile(path: str) -> dict:
    profile = json.loads(Path(path).read_text(encoding="utf-8"))
    return _expand(profile)


def run_worker(config: str) -> None:
    profile = load_profile(config)
    env = dict(os.environ)
    env.update(profile.get("env", {}))
    env["CUDA_VISIBLE_DEVICES"] = "0"
    os.chdir(profile["cwd"])
    exe = profile["exe"]
    os.execve(exe, [exe, *profile["args"]], env)


def run_coordinator(config: str, state_dir: str, host: str, port: int) -> None:
    profile = load_profile(config)
    root = Path(os.environ["STRATA_ROOT"])
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    rendered = state / "coordinator.json"
    temporary = rendered.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    temporary.replace(rendered)
    env = dict(os.environ)
    env.update(profile.get("env", {}))
    os.chdir(profile["cwd"])
    os.execve(sys.executable,
              [sys.executable, str(root / "serve/server.py"), "--engine", "strata",
               "--config", str(rendered), "--host", host, "--port", str(port)], env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="role", required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--config", required=True)
    coordinator = commands.add_parser("coordinator")
    coordinator.add_argument("--config", required=True)
    coordinator.add_argument("--state-dir", required=True)
    coordinator.add_argument("--host", default="0.0.0.0")
    coordinator.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not os.environ.get("STRATA_ROOT"):
        parser.error("STRATA_ROOT must name the source checkout or deployed runtime bundle")
    if args.role == "worker":
        run_worker(args.config)
    else:
        run_coordinator(args.config, args.state_dir, args.host, args.port)


if __name__ == "__main__":
    main()
