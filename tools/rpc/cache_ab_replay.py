#!/usr/bin/env python3
"""Generate and replay a fixed, interleaved coding-agent workload against an exclusive Strata server.

Generate once; reuse the resulting JSON for every arm. This deliberately ignores model
outputs, so the exact request bodies do not depend on the model's sampling path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_turns_bench import TOOLS, turns  # noqa: E402


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def make_fixture(path: Path, max_tokens: int = 16) -> None:
    selected = ("server-option", "expert-cache", "cuda-kernel")
    sessions = {name: [] for name in selected}
    for tag, messages in turns():
        name, _, step = tag.rpartition(".")
        if name in sessions and int(step) < 4:
            sessions[name].append((tag, messages))
    assert all(len(steps) == 4 for steps in sessions.values())
    fixture = []
    for turn in range(4):
        for name in selected:
            tag, messages = sessions[name][turn]
            fixture.append({"tag": tag, "body": {"messages": messages, "tools": TOOLS,
                "max_tokens": max_tokens, "temperature": 0, "seed": 1234,
                "reasoning_effort": "low", "chat_template_kwargs": {"enable_thinking": False}}})
    with path.open("x", encoding="utf-8") as out:
        json.dump(fixture, out, ensure_ascii=False, separators=(",", ":"))
        out.write("\n")
    print(f"Wrote {len(fixture)} fixed requests: {path}")


def rpc_stage_delta(before: dict, after: dict) -> dict:
    """Subtract cumulative stage snapshots; elapsed buckets can overlap across pipeline threads."""
    old = before.get("engine", {}).get("rpc_stage") or {}
    new = after.get("engine", {}).get("rpc_stage") or {}
    old_pf = old.get("coordinator_prefill") or {}
    new_pf = new.get("coordinator_prefill") or {}
    old_worker = ((old.get("worker") or {}).get("hardware") or {})
    new_worker = ((new.get("worker") or {}).get("hardware") or {})
    old_costs = (old_worker.get("profile") or {}).get("costs") or {}
    new_costs = (new_worker.get("profile") or {}).get("costs") or {}
    old_transport = ((old.get("client") or {}).get("profile") or {}).get("costs") or {}
    new_transport = ((new.get("client") or {}).get("profile") or {}).get("costs") or {}

    def diff(a: dict, b: dict, key: str) -> float:
        return float(b.get(key, 0)) - float(a.get(key, 0))

    return {
        "coordinator_prefill": {key: diff(old_pf, new_pf, key) for key in
                                ("tokens", "chunks", "ms_total", "ms_experts_host", "experts_streamed",
                                 "experts_dma", "experts_resident", "experts_cpu")},
        "worker_profile": {name: {"calls": diff(old_costs.get(name, {}), new_costs.get(name, {}), "calls"),
                                   "ms": diff(old_costs.get(name, {}), new_costs.get(name, {}), "ms"),
                                   "bytes": diff(old_costs.get(name, {}), new_costs.get(name, {}), "bytes")}
                           for name in ("prefill", "checkpoint_capture", "checkpoint_restore")},
        "coordinator_transport": {name: {"calls": diff(old_transport.get(name, {}), new_transport.get(name, {}), "calls"),
                                         "ms": diff(old_transport.get(name, {}), new_transport.get(name, {}), "ms"),
                                         "bytes": diff(old_transport.get(name, {}), new_transport.get(name, {}), "bytes")}
                                  for name in ("socket_read", "socket_write")},
        "timing_kind": ((new.get("worker") or {}).get("hardware", {}).get("profile") or {}).get("timing_kind"),
    }


def response_fingerprint(answer: dict) -> str:
    """Hash user-visible assistant output while ignoring server-generated tool-call IDs."""
    semantic = []
    for choice in answer.get("choices", []):
        message = choice.get("message") or {}
        semantic.append({"finish_reason": choice.get("finish_reason"),
                         "role": message.get("role"), "content": message.get("content"),
                         "refusal": message.get("refusal"),
                         "tool_calls": [{"name": (call.get("function") or {}).get("name"),
                                         "arguments": (call.get("function") or {}).get("arguments")}
                                        for call in message.get("tool_calls", [])]})
    return hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def run(url: str, fixture_path: Path, output_path: Path, expected_mib: int) -> None:
    raw = fixture_path.read_bytes()
    fixture = json.loads(raw)
    url = url.rstrip("/")
    before = fetch(url + "/metrics?requests=all")
    assert before["live"]["state"] == "idle" and before["live"].get("queued", 0) == 0, "server busy"
    assert before["engine"]["conversation_cache_mib"] == expected_mib, "wrong cache arm"
    model = before["engine"]["model"]
    records = []
    stage_before = before
    previous = before["totals"]["requests"]
    for item in fixture:
        body = {**item["body"], "model": model}
        request = urllib.request.Request(url + "/v1/chat/completions",
            data=json.dumps(body, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json"})
        start = time.monotonic()
        with urllib.request.urlopen(request, timeout=600) as response:
            answer = json.load(response)
        assert answer.get("choices"), f"no answer: {item['tag']}"
        after = fetch(url + "/metrics?requests=all")
        assert after["totals"]["requests"] == previous + 1, "concurrent/missing request"
        assert after["live"]["state"] == "idle", "server not idle after request"
        row = after["requests"][0]
        response_hash = response_fingerprint(answer)
        records.append({"tag": item["tag"], "wall_s": time.monotonic() - start,
                        "response_id": answer.get("id"), "response_sha256": response_hash, "metrics": row,
                        "stage_delta": rpc_stage_delta(stage_before, after)})
        stage_before = after
        previous += 1
        print(f"{item['tag']}: read={row['prompt_read']} reused={row['reused']} "
              f"prompt_ms={row['prompt_ms']:.1f}", flush=True)
    final = fetch(url + "/metrics?requests=all")
    result = {"fixture_sha256": hashlib.sha256(raw).hexdigest(), "before": before,
              "records": records, "final": final}
    with output_path.open("x", encoding="utf-8") as out:
        json.dump(result, out, indent=2)
        out.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    generate = sub.add_parser("fixture")
    generate.add_argument("path", type=Path)
    generate.add_argument("--max-tokens", type=int, default=16,
                          help="fixed completion length (default: 16 for cache/prefill comparisons)")
    replay = sub.add_parser("run")
    replay.add_argument("--url", required=True)
    replay.add_argument("--fixture", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--expected-cache-mib", type=int, required=True)
    args = parser.parse_args()
    if args.action == "fixture":
        if not 1 <= args.max_tokens <= 4096:
            raise SystemExit("--max-tokens must be in 1..4096")
        make_fixture(args.path, args.max_tokens)
    else: run(args.url, args.fixture, args.output, args.expected_cache_mib)


if __name__ == "__main__":
    main()
