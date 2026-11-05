#!/usr/bin/env python3
import json
import io
import tempfile
import threading
import time
import urllib.error
import urllib.request
import unittest

from codex_llama_proxy import (
    ProxyHandler,
    STATS_UI_HTML,
    StatsStore,
    ToolMapping,
    flatten_tools,
    rewrite_function_calls,
    request_model,
    request_model_from_query,
    transform_request,
    normalize_backend_metrics,
    _aggregate_metrics,
    _percentile,
    ProxyConfig,
    build_server,
)


class ProxyTransformTests(unittest.TestCase):
    def test_model_catalog_preserves_backend_vision_capability(self):
        raw = json.dumps({
            "data": [{"id": "vision-model", "architecture": {"input_modalities": ["text", "image", "video"]}}],
            "models": [{"name": "vision-model", "capabilities": ["completion", "multimodal"]}],
        }).encode()
        handler = object.__new__(ProxyHandler)
        patched = json.loads(handler._patch_models(raw))
        self.assertEqual(patched["data"][0]["input_modalities"], ["text", "image"])
        self.assertEqual(patched["models"][0]["input_modalities"], ["text", "image"])
        self.assertEqual(patched["data"][0]["architecture"]["input_modalities"], ["text", "image", "video"])

    def test_model_catalog_keeps_text_only_profiles_text_only(self):
        raw = json.dumps({
            "data": [{"id": "text-model", "architecture": {"input_modalities": ["text"]}}],
            "models": [{"name": "text-model", "capabilities": ["completion"]}],
        }).encode()
        handler = object.__new__(ProxyHandler)
        patched = json.loads(handler._patch_models(raw))
        self.assertEqual(patched["data"][0]["input_modalities"], ["text"])
        self.assertEqual(patched["models"][0]["input_modalities"], ["text"])

    def test_flatten_namespace(self):
        tools, mappings, dropped = flatten_tools([
            {
                "type": "namespace",
                "name": "mcp__filesystem__",
                "tools": [
                    {"type": "function", "name": "read_file", "description": "read", "parameters": {}}
                ],
            }
        ])
        self.assertEqual(dropped, [])
        self.assertEqual(tools[0]["name"], "mcp__filesystem__read_file")
        self.assertEqual(
            mappings["mcp__filesystem__read_file"],
            ToolMapping("mcp__filesystem__", "read_file"),
        )

    def test_drop_unsupported_tools(self):
        tools, _mappings, dropped = flatten_tools([
            {"type": "web_search"},
            {"type": "computer_use_preview"},
        ])
        self.assertEqual(tools, [])
        self.assertEqual(dropped, ["web_search", "computer_use_preview"])

    def test_nested_chat_function_spec(self):
        tools, _mappings, _dropped = flatten_tools([
            {
                "type": "namespace",
                "name": "mcp__demo__",
                "tools": [{"type": "function", "function": {"name": "echo", "parameters": {}}}],
            }
        ])
        self.assertEqual(
            tools,
            [{"type": "function", "name": "mcp__demo__echo", "parameters": {}}],
        )

    def test_collision_rejected(self):
        with self.assertRaises(ValueError):
            flatten_tools([
                {
                    "type": "namespace",
                    "name": "mcp__one__",
                    "tools": [{"type": "function", "name": "run"}],
                },
                {
                    "type": "namespace",
                    "name": "mcp__one",
                    "tools": [{"type": "function", "name": "run"}],
                },
            ])

    def test_response_call_restored(self):
        response = {
            "output": [
                {"type": "function_call", "name": "mcp__filesystem__read_file", "call_id": "call_1"}
            ]
        }
        rewritten = rewrite_function_calls(
            response,
            {"mcp__filesystem__read_file": ToolMapping("mcp__filesystem__", "read_file")},
        )
        self.assertEqual(rewritten["output"][0]["namespace"], "mcp__filesystem__")
        self.assertEqual(rewritten["output"][0]["name"], "read_file")

    def test_request_without_tools_is_unchanged(self):
        request = {"model": "qwen", "input": "hello"}
        transformed, mappings, dropped, _diagnostics = transform_request(request)
        self.assertEqual(transformed, request)
        self.assertEqual(mappings, {})
        self.assertEqual(dropped, [])

    def test_system_messages_are_folded_into_instructions(self):
        request = {
            "model": "qwen",
            "instructions": "top-level policy",
            "input": [
                {"role": "user", "content": "old"},
                {"role": "developer", "content": "policy"},
                {"role": "assistant", "content": "reply"},
                {"role": "system", "content": "rules"},
            ],
        }
        transformed, _mappings, _dropped, diagnostics = transform_request(request)
        self.assertEqual(
            [item["role"] for item in transformed["input"]],
            ["user", "assistant"],
        )
        self.assertEqual(transformed["instructions"], "top-level policy\n\npolicy\n\nrules")
        self.assertEqual(diagnostics["folded_instruction_items"], 2)

    def test_array_system_content_and_tool_history_are_preserved(self):
        request = {
            "model": "qwen",
            "input": [
                {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": "{}"},
                {"role": "system", "content": [{"type": "input_text", "text": "system rules"}]},
                {"type": "function_call_output", "call_id": "call_1", "output": "done"},
                {"role": "developer", "content": [{"type": "input_text", "text": "developer rules"}]},
                {"role": "user", "content": "continue"},
            ],
        }
        transformed, _mappings, dropped, diagnostics = transform_request(request)
        self.assertEqual(dropped, [])
        self.assertEqual(transformed["instructions"], "system rules\n\ndeveloper rules")
        self.assertEqual(diagnostics["folded_instruction_items"], 2)
        self.assertEqual(
            [item.get("type") or item.get("role") for item in transformed["input"]],
            ["function_call", "function_call_output", "user"],
        )

    def test_unsupported_reasoning_efforts_are_normalized(self):
        request = {
            "model": "qwen",
            "reasoning": {"effort": "high"},
            "reasoning_effort": "minimal",
            "chat_template_kwargs": {"reasoning_effort": "max"},
            "input": "continue",
        }
        transformed, _mappings, dropped, diagnostics = transform_request(request)
        self.assertEqual(dropped, [])
        self.assertEqual(transformed["reasoning"]["effort"], "medium")
        self.assertEqual(transformed["reasoning_effort"], "low")
        self.assertEqual(transformed["chat_template_kwargs"]["reasoning_effort"], "xhigh")
        self.assertEqual(diagnostics["normalized_reasoning_efforts"], 3)

    def test_codex_custom_tool_history_is_translated(self):
        raw_input = (
            "const result = await tools.exec_command({"
            "cmd: \"rg --files\", workdir: \"/workspace\""
            "});"
        )
        request = {
            "model": "qwen",
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call_1",
                    "name": "exec",
                    "input": raw_input,
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_1",
                    "output": [{"type": "input_text", "text": "done"}],
                },
            ],
        }
        transformed, _mappings, dropped, _diagnostics = transform_request(request)
        self.assertEqual(dropped, [])
        self.assertEqual(transformed["input"][0]["type"], "function_call")
        self.assertEqual(
            json.loads(transformed["input"][0]["arguments"]),
            {"input": raw_input},
        )
        self.assertEqual(transformed["input"][1]["type"], "function_call_output")
        self.assertEqual(transformed["input"][1]["output"], "done")

    def test_tool_outputs_are_normalized_to_text(self):
        request = {
            "model": "qwen",
            "input": [
                {"type": "function_call_output", "call_id": "a", "output": "plain"},
                {"type": "function_call_output", "call_id": "b", "output": [
                    {"type": "input_text", "text": "first"},
                    {"type": "input_text", "text": "second"},
                ]},
                {"type": "function_call_output", "call_id": "c", "output": [
                    {"type": "output_text", "text": "visible"},
                    {"type": "image", "url": "ignored-by-template"},
                ]},
                {"type": "function_call_output", "call_id": "d", "output": None},
            ],
        }
        transformed, _mappings, dropped, diagnostics = transform_request(request)
        self.assertEqual(dropped, [])
        outputs = [item["output"] for item in transformed["input"]]
        self.assertEqual(outputs[0], "plain")
        self.assertEqual(outputs[1], "first\nsecond")
        self.assertIn("visible", outputs[2])
        self.assertIn("image", outputs[2])
        self.assertEqual(outputs[3], "")
        self.assertEqual(diagnostics["normalized_tool_outputs"], 3)

    def test_responses_history_is_transformed_without_tool_mapping(self):
        request = {
            "model": "qwen",
            "input": [{
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [{"type": "input_text", "text": "done"}],
            }],
        }
        transformed, mappings, _dropped, _diagnostics = transform_request(request)
        self.assertEqual(mappings, {})
        self.assertEqual(transformed["input"][0]["output"], "done")

    def test_unsupported_history_is_dropped_and_diagnosed(self):
        request = {
            "model": "qwen",
            "input": [
                {"type": "web_search_call", "status": "completed"},
                {"type": "future_metadata", "value": "ignored"},
                {"role": "user", "content": "continue"},
            ],
        }
        transformed, _mappings, dropped, diagnostics = transform_request(request)
        self.assertEqual([item["role"] for item in transformed["input"]], ["user"])
        self.assertEqual(dropped, ["web_search_call", "future_metadata"])
        self.assertEqual(diagnostics["input_item_types"], [
            "web_search_call", "future_metadata", "<missing>"
        ])
        self.assertEqual(diagnostics["dropped_history_item_types"], dropped)
        self.assertEqual(diagnostics["normalized_history_item_count"], 1)

    def test_plaintext_compaction_is_normalized(self):
        transformed, _mappings, dropped, diagnostics = transform_request({
            "model": "qwen",
            "input": [
                {"type": "compaction", "summary": "Continue from the verified analysis."},
            ],
        })
        self.assertEqual(dropped, [])
        self.assertEqual(transformed["input"][0]["type"], "message")
        self.assertIn("Continue from the verified analysis.", transformed["input"][0]["content"])
        self.assertEqual(diagnostics["normalized_history_item_count"], 1)

    def test_encrypted_compaction_is_rejected(self):
        from codex_llama_proxy import CompactionError
        with self.assertRaises(CompactionError) as context:
            transform_request({
                "model": "qwen",
                "input": [{"type": "compaction", "encrypted_content": "opaque"}],
            })
        self.assertEqual(context.exception.code, "unsupported_encrypted_compaction")

    def test_empty_compaction_is_rejected(self):
        from codex_llama_proxy import CompactionError
        with self.assertRaises(CompactionError) as context:
            transform_request({"input": [{"type": "compaction", "summary": ""}]})
        self.assertEqual(context.exception.code, "invalid_compaction")

    def test_malformed_reasoning_is_dropped(self):
        request = {"model": "qwen", "input": [
            {"type": "reasoning", "content": [{"text": "private"}]},
            {"role": "user", "content": "next"},
        ]}
        transformed, _mappings, dropped, _diagnostics = transform_request(request)
        self.assertEqual(dropped, ["reasoning_without_content"])
        self.assertEqual(transformed["input"][0]["role"], "user")

    def test_model_metadata_is_patched_for_codex(self):
        raw = b'{"models":[{"name":"qwen"}],"data":[{"id":"qwen"}]}'
        patched = ProxyHandler._patch_models(None, raw)
        payload = json.loads(patched)
        self.assertEqual(payload["models"][0]["slug"], "qwen")
        self.assertEqual(payload["models"][0]["display_name"], "qwen")
        self.assertEqual(payload["data"][0]["slug"], "qwen")
        self.assertEqual(payload["data"][0]["display_name"], "qwen")

    def test_request_model_uses_model_or_unknown(self):
        self.assertEqual(request_model({"model": "muse"}), "muse")
        self.assertEqual(request_model({"model": "  muse  "}), "muse")
        self.assertEqual(request_model({}), "<unknown>")
        self.assertEqual(request_model(None), "<unknown>")
        self.assertEqual(request_model_from_query("client=demo&model=muse%2Dq8"), "muse-q8")
        self.assertEqual(request_model_from_query("client=demo"), "<unknown>")

    def test_stats_group_by_model_and_client_ip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StatsStore(f"{directory}/stats.jsonl")
            now = time.monotonic()
            store.record(
                path="/v1/responses", status=200, started=now, headers_at=now,
                finished=now + 1, stream=False, response_bytes=10,
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                tool_info={}, tool_call_ids=[], model="muse", client_ip="192.0.2.217",
            )
            summary = store.snapshot()
            self.assertEqual(summary["session"]["by_model"]["muse"]["requests"], 1)
            self.assertEqual(summary["session"]["by_client_ip"]["192.0.2.217"]["total_tokens"], 12)
            self.assertEqual(summary["lifetime"]["by_model"]["muse"]["output_tokens"], 2)

    def test_stats_group_by_endpoint_and_client_ip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StatsStore(f"{directory}/stats.jsonl")
            now = time.monotonic()
            store.record(
                path="/v1/chat/completions", method="POST", status=200,
                started=now, headers_at=now, finished=now + 1, stream=False,
                request_bytes=100, response_bytes=200,
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                tool_info={}, tool_call_ids=[], model="muse", client_ip="192.0.2.217",
            )
            summary = store.snapshot()
            endpoint = summary["session"]["by_endpoint"]["POST /v1/chat/completions"]
            client = summary["session"]["by_endpoint_client_ip"]["POST /v1/chat/completions"]["192.0.2.217"]
            self.assertEqual(endpoint["requests"], 1)
            self.assertEqual(endpoint["request_bytes"], 100)
            self.assertEqual(endpoint["response_bytes"], 200)
            self.assertEqual(client["input_tokens"], 7)
            self.assertEqual(client["output_tokens"], 3)

    def test_grouped_lifetime_state_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/stats.jsonl"
            store = StatsStore(path)
            now = time.monotonic()
            store.record(
                path="/v1/responses", status=200, started=now, headers_at=now,
                finished=now + 1, stream=False, response_bytes=1, usage=None,
                tool_info={}, tool_call_ids=[], model="muse", client_ip="10.0.0.2",
            )
            restored = StatsStore(path)
            self.assertEqual(restored.snapshot()["lifetime"]["by_model"]["muse"]["requests"], 1)
            self.assertEqual(restored.snapshot()["lifetime"]["by_client_ip"]["10.0.0.2"]["requests"], 1)

    def test_stats_ui_contains_refresh_and_group_views(self):
        self.assertIn("/stats", STATS_UI_HTML)
        self.assertIn("By model", STATS_UI_HTML)
        self.assertIn("By client IP", STATS_UI_HTML)
        self.assertIn("By endpoint", STATS_UI_HTML)
        self.assertIn("Endpoint by client IP", STATS_UI_HTML)
        self.assertIn("/stats/runtime", STATS_UI_HTML)
        self.assertIn("Stack offline", STATS_UI_HTML)
        self.assertIn("id=\"refresh\"", STATS_UI_HTML)

    def test_tool_call_statistics_and_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StatsStore(f"{directory}/stats.jsonl")
            now = time.monotonic()
            store.record(
                path="/v1/responses",
                status=200,
                started=now - 1.0,
                headers_at=now - 0.5,
                finished=now,
                stream=True,
                response_bytes=100,
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                tool_info={
                    "tool_declarations": 2,
                    "mcp_tool_declarations": 2,
                    "dropped_tool_types": 1,
                    "tool_result_ids": [],
                },
                tool_call_ids=["call_1"],
            )
            store.record(
                path="/v1/responses",
                status=200,
                started=now,
                headers_at=now,
                finished=now + 0.25,
                stream=False,
                response_bytes=100,
                usage={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                tool_info={"tool_result_ids": ["call_1"]},
                tool_call_ids=[],
            )
            summary = store.snapshot()
            self.assertEqual(summary["tool_calls"], 1)
            self.assertEqual(summary["tool_results"], 1)
            self.assertEqual(summary["tool_round_trips"], 1)
            self.assertGreater(summary["average_tool_round_trip_ms"], 0)

    def test_stats_persist_lifetime_and_reset_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/stats.jsonl"
            first = StatsStore(path)
            now = time.monotonic()
            first.record(
                path="/v1/responses", status=200, started=now, headers_at=now,
                finished=now + 1, stream=False, response_bytes=1,
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                tool_info={"dropped_tool_types": 1, "dropped_tool_names": ["web_search"],
                           "dropped_history_item_types": ["compaction"]},
                tool_call_ids=[],
            )
            second = StatsStore(path)
            summary = second.snapshot()
            self.assertEqual(summary["requests"], 0)
            self.assertEqual(summary["lifetime"]["counters"]["requests"], 1)
            self.assertEqual(summary["lifetime"]["dropped_tool_types"]["web_search"], 1)
            self.assertEqual(summary["lifetime"]["dropped_history_item_types"]["compaction"], 1)
            self.assertNotEqual(summary["session"]["id"], first.snapshot()["session"]["id"])

    def test_error_type_is_classified(self):
        from codex_llama_proxy import _error_type
        self.assertEqual(
            _error_type("upstream", 500, b'{"error":{"code":"bad_request"}}'),
            "upstream:500:bad_request",
        )

    def test_backend_metrics_separate_forwarded_evaluated_and_cached(self):
        metrics = normalize_backend_metrics(
            {"input_tokens": 100000, "output_tokens": 50},
            {"prompt_n": 10000, "cache_n": 90000, "prompt_ms": 500,
             "predicted_n": 50, "predicted_ms": 2500},
        )
        self.assertEqual(metrics["prompt_forwarded_tokens"], 100000)
        self.assertEqual(metrics["prompt_eval_tokens"], 10000)
        self.assertEqual(metrics["prompt_cached_tokens"], 90000)
        self.assertEqual(metrics["decode_tokens"], 49)
        self.assertEqual(metrics["prompt_eval_tok_s"], 20000)
        self.assertEqual(metrics["decode_tok_s"], 19.6)

    def test_metrics_missing_or_invalid_remain_unavailable(self):
        self.assertIsNone(normalize_backend_metrics(None, None)["prompt_cached_tokens"])
        normalized = normalize_backend_metrics(None, {"prompt_n": "bad", "cache_n": -2})
        self.assertIsNone(normalized["prompt_eval_tokens"])
        self.assertIsNone(normalized["prompt_cached_tokens"])
        self.assertIsNone(normalized["prompt_forwarded_tokens"])

    def test_stats_do_not_turn_unreported_backend_metrics_into_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StatsStore(f"{directory}/stats.jsonl")
            now = time.monotonic()
            store.record(
                path="/v1/responses", status=200, started=now,
                headers_at=now, finished=now + 1, stream=False,
                response_bytes=1, usage={"input_tokens": 80, "output_tokens": 4},
                timings=None, tool_info={}, tool_call_ids=[],
            )
            metrics = store.snapshot()["metrics"]
            self.assertEqual(metrics["prompt_forwarded_tokens"], 80)
            self.assertIsNone(metrics["prompt_eval_tokens"])
            self.assertIsNone(metrics["prompt_cached_tokens"])
            self.assertIsNone(metrics["decode_tokens"])
            self.assertIsNone(metrics["prompt_cache_reuse_pct"])
            self.assertIsNone(metrics["decode_weighted_tok_s"])

    def test_weighted_throughput_uses_total_tokens_over_total_time(self):
        events = [
            {"is_inference": True, "success": True, "decode_tokens": 100,
             "decode_ms": 10000, "prompt_eval_tokens": 0, "prompt_cached_tokens": 0},
            {"is_inference": True, "success": True, "decode_tokens": 100,
             "decode_ms": 2000, "prompt_eval_tokens": 0, "prompt_cached_tokens": 0},
        ]
        self.assertEqual(_aggregate_metrics(events, None)["decode_weighted_tok_s"], 16.67)
        self.assertEqual(_percentile([1, 2, 3, 4], 0.5), 2.5)

    def test_legacy_average_speed_alias_uses_weighted_backend_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StatsStore(f"{directory}/stats.jsonl")
            for tokens, duration_ms in ((100, 10000), (100, 2000)):
                now = time.monotonic()
                store.record(
                    path="/v1/responses", status=200, started=now - duration_ms / 1000,
                    headers_at=now, finished=now, stream=False, response_bytes=1,
                    usage={"output_tokens": tokens},
                    timings={"predicted_n": tokens, "predicted_ms": duration_ms},
                    tool_info={}, tool_call_ids=[],
                )
            snapshot = store.snapshot()
            self.assertEqual(snapshot["average_output_tokens_per_second"], 16.5)
            self.assertEqual(snapshot["metrics"]["decode_weighted_tok_s"], 16.5)

    def test_bounded_history_windows_and_non_inference_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StatsStore(f"{directory}/stats.jsonl", history_max=2, context_limit=200)
            for index in range(3):
                now = time.monotonic()
                store.record(
                    path="/v1/responses", status=200, started=now - 1,
                    headers_at=now - 0.5, finished=now, stream=True,
                    response_bytes=1, usage={"input_tokens": 100, "output_tokens": 5},
                    timings={"prompt_n": 20, "cache_n": 80, "prompt_ms": 10,
                             "predicted_n": 5, "predicted_ms": 500},
                    tool_info={}, tool_call_ids=[], model="m", client_ip="local",
                    ttft_ms=50,
                )
            now = time.monotonic()
            store.record(
                path="/health", status=200, started=now, headers_at=now,
                finished=now, stream=False, response_bytes=1, usage=None,
                tool_info={}, tool_call_ids=[], method="GET",
            )
            metrics = store.snapshot("session")["metrics"]
            self.assertEqual(metrics["inference_requests"], 3)
            self.assertEqual(metrics["http_requests"], 4)
            self.assertEqual(metrics["percentile_history_size"], 2)
            self.assertEqual(metrics["prompt_eval_tokens"], 60)
            self.assertEqual(metrics["prompt_cached_tokens"], 240)
            self.assertEqual(metrics["max_context_tokens"], 100)
            self.assertEqual(metrics["context_limit"], 200)
            self.assertEqual(store.snapshot("5m")["metrics"]["inference_requests"], 1)
            self.assertEqual(store.snapshot("all")["metrics"]["inference_requests"], 3)

    def test_stream_parser_captures_ttft_timings_and_preserves_sse(self):
        class FakeResponse:
            def __init__(self, chunks):
                self.chunks = iter(chunks)

            def read1(self, _size):
                return next(self.chunks, b"")

        class FakeConnection:
            def close(self):
                pass

        handler = ProxyHandler.__new__(ProxyHandler)
        handler.wfile = io.BytesIO()
        started = time.monotonic() - 0.02
        chunks = [
            b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n',
            b'data: {"type":"response.output_item.added","item":{"type":"function_call","call_id":"c1","name":"shell"}}\n\n',
            b'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"c1","name":"shell"}}\n\n',
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":4,"output_tokens":1},"timings":{"prompt_n":3,"cache_n":1,"prompt_ms":2,"predicted_n":1,"predicted_ms":10}}}\n\n',
        ]
        result = handler._stream_sse(FakeResponse(chunks), FakeConnection(), {}, started)
        usage, timings, _size, _ids, tools, ttft, cancelled, interrupted = result
        self.assertEqual(usage["input_tokens"], 4)
        self.assertEqual(timings["cache_n"], 1)
        self.assertGreaterEqual(ttft, 0)
        self.assertFalse(cancelled)
        self.assertFalse(interrupted)
        self.assertEqual(tools, {"shell": 1})
        self.assertEqual(handler.wfile.getvalue(), b"".join(chunks))

    def test_stats_endpoint_windows_json_text_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            server = build_server(
                "127.0.0.1", 0,
                ProxyConfig("127.0.0.1", 1, "", 1, 1, 1024, 4096),
                f"{directory}/stats.jsonl",
            )
            now = time.monotonic()
            server.stats.record(
                path="/v1/responses", status=200, started=now - 0.1,
                headers_at=now - 0.05, finished=now, stream=True,
                response_bytes=1, usage={"input_tokens": 10, "output_tokens": 2},
                timings={"prompt_n": 4, "cache_n": 6, "prompt_ms": 2,
                         "predicted_n": 2, "predicted_ms": 20},
                tool_info={}, tool_call_ids=[],
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}/stats"
            try:
                with urllib.request.urlopen(base + "?window=5m") as response:
                    data = json.loads(response.read())
                self.assertEqual(data["metrics"]["window"], "5m")
                self.assertEqual(data["metrics"]["prompt_cached_tokens"], 6)
                with urllib.request.urlopen(base + "?format=text&window=session") as response:
                    text = response.read().decode()
                self.assertIn("Prompt tokens", text)
                self.assertIn("Cache reuse: 60.0%", text)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base + "?window=bogus")
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
