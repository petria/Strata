#!/usr/bin/env python3
"""Small Codex Responses -> llama-server compatibility proxy.

The proxy is intentionally dependency-free.  Codex owns MCP execution; this
process only flattens Codex namespace tool declarations into ordinary
Responses function tools and restores namespace metadata on model tool calls.
"""

from __future__ import annotations

import argparse
import copy
from collections import deque
import http.client
import json
import logging
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


LOG = logging.getLogger("codex-llama-proxy")
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
FORWARDED_HEADERS = {
    "accept",
    "authorization",
    "content-type",
    "openai-beta",
    "openai-organization",
    "openai-project",
    "user-agent",
    "x-request-id",
}
FORWARDED_RESPONSE_HEADERS = {
    "content-encoding",
    "etag",
    "location",
    "retry-after",
    "vary",
    "www-authenticate",
    "x-request-id",
    "openai-processing-ms",
}


class CompactionError(ValueError):
    """A compaction record cannot be represented by the local backend."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolMapping:
    namespace: str
    name: str


@dataclass(frozen=True)
class ProxyConfig:
    upstream_host: str
    upstream_port: int
    upstream_prefix: str
    connect_timeout: float
    read_timeout: float
    max_body_bytes: int
    context_limit: int | None = None


def _find_timings(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        timings = value.get("timings")
        if isinstance(timings, dict):
            return timings
        for item in value.values():
            found = _find_timings(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_timings(item)
            if found is not None:
                return found
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return number
    return None


def normalize_backend_metrics(
    usage: dict[str, Any] | None,
    timings: dict[str, Any] | None,
) -> dict[str, int | float | None]:
    """Normalize llama.cpp timing/usage fields without guessing missing data."""
    usage = usage or {}
    timings = timings or {}
    prompt_eval = _number(timings.get("prompt_n"))
    prompt_cached = _number(timings.get("cache_n"))
    forwarded = _number(usage.get("input_tokens", usage.get("prompt_tokens")))
    if forwarded is None and prompt_eval is not None and prompt_cached is not None:
        forwarded = prompt_eval + prompt_cached
    decode_tokens = _number(timings.get("predicted_n"))
    generated_tokens = decode_tokens
    if generated_tokens is None:
        generated_tokens = _number(usage.get("output_tokens", usage.get("completion_tokens")))
    if decode_tokens is not None:
        # llama.cpp's native decode rate excludes the first token, which is
        # sampled from the final prompt logits without a decode step.
        decode_tokens = max(decode_tokens - 1, 0)
    prompt_ms = _number(timings.get("prompt_ms"))
    decode_ms = _number(timings.get("predicted_ms"))
    prompt_tps = _number(timings.get("prompt_per_second"))
    decode_tps = _number(timings.get("predicted_per_second"))
    if prompt_tps is None and prompt_eval is not None and prompt_ms:
        prompt_tps = prompt_eval * 1000 / prompt_ms
    if decode_tps is None and decode_tokens is not None and decode_ms:
        decode_tps = decode_tokens * 1000 / decode_ms
    return {
        "prompt_forwarded_tokens": int(forwarded) if forwarded is not None else None,
        "prompt_eval_tokens": int(prompt_eval) if prompt_eval is not None else None,
        "prompt_cached_tokens": int(prompt_cached) if prompt_cached is not None else None,
        "generated_tokens": int(generated_tokens) if generated_tokens is not None else None,
        "prompt_eval_ms": prompt_ms,
        "prompt_eval_tok_s": prompt_tps,
        "decode_tokens": int(decode_tokens) if decode_tokens is not None else None,
        "decode_ms": decode_ms,
        "decode_tok_s": decode_tps,
    }


def _request_type(method: str, path: str) -> str:
    route = path.rstrip("/") or "/"
    if route in {"/health", "/props", "/slots"} or route.endswith("/health"):
        return "health_status"
    if route in {"/stats", "/stats/ui", "/stats/runtime"}:
        return "stats"
    if route in {"/v1/models", "/models"}:
        return "models"
    inference_routes = {
        "/v1/responses": "responses_inference",
        "/responses": "responses_inference",
        "/v1/chat/completions": "chat_completions_inference",
        "/v1/completions": "completions_inference",
        "/completion": "completions_inference",
        "/v1/embeddings": "embeddings_inference",
    }
    return inference_routes.get(route, "other")


def _percentile(values: list[float], percentile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _find_usage(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        usage = value.get("usage")
        if isinstance(usage, dict):
            return usage
        for item in value.values():
            found = _find_usage(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_usage(item)
            if found is not None:
                return found
    return None


def _inc_type(counter: dict[str, int], value: str, amount: int = 1) -> None:
    counter[value] = counter.get(value, 0) + amount


def _new_counters() -> dict[str, Any]:
    return {
        "requests": 0, "responses": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "request_bytes": 0, "response_bytes": 0,
        "request_ms": 0.0, "output_tokens_per_second": 0.0,
        "tool_declarations": 0, "mcp_tool_declarations": 0,
        "dropped_tool_types": 0, "dropped_history_item_types": 0,
        "tool_calls": 0, "tool_results": 0,
        "tool_round_trip_ms": 0.0, "tool_round_trips": 0,
    }


def _new_metric_totals() -> dict[str, Any]:
    return {
        "http_requests": 0, "http_successful": 0, "http_failed": 0,
        "inference_requests": 0, "successful": 0, "failed": 0, "cancelled": 0,
        "prompt_forwarded_tokens": 0, "prompt_eval_tokens": 0,
        "prompt_cached_tokens": 0, "decode_tokens": 0, "generated_tokens": 0,
        "prompt_forwarded_reports": 0, "prompt_eval_reports": 0,
        "prompt_cache_reports": 0, "generated_token_reports": 0,
        "prompt_timing_reports": 0, "decode_timing_reports": 0,
        "prompt_eval_ms": 0.0, "decode_ms": 0.0,
        "context_tokens_sum": 0, "context_requests_with_context": 0,
        "max_context_tokens": None, "max_context_utilization_pct": None,
        "requests_over_75pct": 0, "requests_over_90pct": 0,
        "tool_calls": 0, "retries": 0,
    }


def _aggregate_metrics(events: list[dict[str, Any]], context_limit: int | None) -> dict[str, Any]:
    result = _new_metric_totals()
    samples: dict[str, list[float]] = {
        "ttft_ms": [], "total_request_ms": [], "prompt_eval_tok_s": [],
        "decode_tok_s": [], "context_tokens": [], "context_utilization_pct": [],
    }
    tool_names: dict[str, int] = {}
    error_categories: dict[str, int] = {}
    for event in events:
        result["http_requests"] += 1
        result["http_successful" if event.get("success") else "http_failed"] += 1
        if not event.get("is_inference"):
            continue
        result["inference_requests"] += 1
        result["successful" if event.get("success") else "failed"] += 1
        result["cancelled"] += int(bool(event.get("cancelled")))
        result["retries"] += int(event.get("retry_count") or 0)
        if not event.get("success"):
            category = event.get("error_category") or "unknown"
            error_categories[category] = error_categories.get(category, 0) + 1
        for key in ("prompt_forwarded_tokens", "prompt_eval_tokens", "prompt_cached_tokens", "decode_tokens", "generated_tokens", "prompt_eval_ms", "decode_ms", "tool_calls"):
            value = event.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] += value
        for metric_key, report_key in (
            ("prompt_forwarded_tokens", "prompt_forwarded_reports"),
            ("prompt_eval_tokens", "prompt_eval_reports"),
            ("prompt_cached_tokens", "prompt_cache_reports"),
            ("generated_tokens", "generated_token_reports"),
        ):
            if isinstance(event.get(metric_key), (int, float)):
                result[report_key] += 1
        if event.get("prompt_eval_tokens") is not None and event.get("prompt_eval_ms") is not None:
            result["prompt_timing_reports"] += 1
        if event.get("decode_tokens") is not None and event.get("decode_ms") is not None:
            result["decode_timing_reports"] += 1
        for name, count in (event.get("tool_call_counts") or {}).items():
            tool_names[name] = tool_names.get(name, 0) + count
        context_value = event.get("context_tokens")
        utilization = event.get("context_utilization_pct")
        if isinstance(context_value, int):
            result["context_tokens_sum"] += context_value
            result["context_requests_with_context"] += 1
            result["max_context_tokens"] = max(result["max_context_tokens"] or 0, context_value)
        if isinstance(utilization, (int, float)):
            result["max_context_utilization_pct"] = max(result["max_context_utilization_pct"] or 0, utilization)
            result["requests_over_75pct"] += int(utilization > 75)
            result["requests_over_90pct"] += int(utilization > 90)
        for name in samples:
            value = event.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                samples[name].append(float(value))

    prompt_eval_tps = result["prompt_eval_tokens"] * 1000 / result["prompt_eval_ms"] if result["prompt_timing_reports"] and result["prompt_eval_ms"] else None
    decode_tps = result["decode_tokens"] * 1000 / result["decode_ms"] if result["decode_timing_reports"] and result["decode_ms"] else (0.0 if result["decode_timing_reports"] else None)
    percentiles = {
        name: {f"p{int(p * 100)}": _percentile(values, p) for p in (0.5, 0.9, 0.95, 0.99)}
        for name, values in samples.items()
    }
    result.update({
        "prompt_forwarded_tokens": result["prompt_forwarded_tokens"] if result["prompt_forwarded_reports"] else None,
        "prompt_eval_tokens": result["prompt_eval_tokens"] if result["prompt_eval_reports"] else None,
        "prompt_cached_tokens": result["prompt_cached_tokens"] if result["prompt_cache_reports"] else None,
        "generated_tokens": result["generated_tokens"] if result["generated_token_reports"] else None,
        "decode_tokens": result["decode_tokens"] if result["decode_timing_reports"] else None,
        "prompt_cache_reuse_pct": (
            round(result["prompt_cached_tokens"] * 100 / (result["prompt_cached_tokens"] + result["prompt_eval_tokens"]), 2)
            if result["prompt_cache_reports"] and result["prompt_eval_reports"] and result["prompt_cached_tokens"] + result["prompt_eval_tokens"] else None
        ),
        "prompt_eval_weighted_tok_s": round(prompt_eval_tps, 2) if prompt_eval_tps is not None else None,
        "decode_weighted_tok_s": round(decode_tps, 2) if decode_tps is not None else None,
        "context_limit": context_limit,
        "average_context_tokens": round(result["context_tokens_sum"] / result["context_requests_with_context"]) if result["context_requests_with_context"] else None,
        "tool_call_counts": tool_names,
        "errors_by_category": error_categories,
        "percentiles": percentiles,
    })
    return result


def _finalize_metric_totals(
    totals: dict[str, Any],
    context_limit: int | None,
    tool_call_counts: dict[str, int],
    errors_by_category: dict[str, int],
    percentiles: dict[str, Any],
    percentile_scope: str,
) -> dict[str, Any]:
    result = dict(totals)
    prompt_total = result["prompt_cached_tokens"] + result["prompt_eval_tokens"]
    for metric_key, report_key in (
        ("prompt_forwarded_tokens", "prompt_forwarded_reports"),
        ("prompt_eval_tokens", "prompt_eval_reports"),
        ("prompt_cached_tokens", "prompt_cache_reports"),
        ("generated_tokens", "generated_token_reports"),
        ("decode_tokens", "decode_timing_reports"),
    ):
        if not result[report_key]:
            result[metric_key] = None
    result.update({
        "prompt_cache_reuse_pct": round(totals["prompt_cached_tokens"] * 100 / prompt_total, 2) if totals["prompt_cache_reports"] and totals["prompt_eval_reports"] and prompt_total else None,
        "prompt_eval_weighted_tok_s": round(totals["prompt_eval_tokens"] * 1000 / totals["prompt_eval_ms"], 2) if totals["prompt_timing_reports"] and totals["prompt_eval_ms"] else None,
        "decode_weighted_tok_s": round(totals["decode_tokens"] * 1000 / totals["decode_ms"], 2) if totals["decode_timing_reports"] and totals["decode_ms"] else (0.0 if totals["decode_timing_reports"] else None),
        "context_limit": context_limit,
        "average_context_tokens": round(result["context_tokens_sum"] / result["context_requests_with_context"]) if result["context_requests_with_context"] else None,
        "tool_call_counts": dict(tool_call_counts),
        "errors_by_category": dict(errors_by_category),
        "percentiles": percentiles,
        "percentile_history_scope": percentile_scope,
    })
    return result


def _new_group() -> dict[str, Any]:
    return _new_counters()


def _group_totals(groups: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {name: dict(counters) for name, counters in groups.items()}


class StatsStore:
    """Append request records and maintain persistent lifetime/session totals."""

    def __init__(self, path: str, history_max: int = 1000, context_limit: int | None = None):
        self.path = Path(path).expanduser()
        self.state_path = self.path.with_name(self.path.stem + ".state.json")
        self._lock = threading.Lock()
        self._history_max = max(history_max, 1)
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=self._history_max)
        self._context_limit = context_limit
        self._lifetime_metrics = _new_metric_totals()
        self._session_metrics = _new_metric_totals()
        self._lifetime_tool_call_counts: dict[str, int] = {}
        self._session_tool_call_counts: dict[str, int] = {}
        self._lifetime_inference_errors: dict[str, int] = {}
        self._session_inference_errors: dict[str, int] = {}
        self._pending_tool_calls: dict[str, float] = {}
        self._session_started_at = time.time()
        self._session_id = uuid.uuid4().hex
        self._summary = _new_counters()
        self._session_by_model: dict[str, dict[str, Any]] = {}
        self._session_by_client_ip: dict[str, dict[str, Any]] = {}
        self._session_by_endpoint: dict[str, dict[str, Any]] = {}
        self._session_by_endpoint_client_ip: dict[str, dict[str, dict[str, Any]]] = {}
        self._session_dropped_tools: dict[str, int] = {}
        self._session_dropped_history: dict[str, int] = {}
        self._session_errors: dict[str, int] = {}
        self._lifetime = _new_counters()
        self._lifetime_by_model: dict[str, dict[str, Any]] = {}
        self._lifetime_by_client_ip: dict[str, dict[str, Any]] = {}
        self._lifetime_by_endpoint: dict[str, dict[str, Any]] = {}
        self._lifetime_by_endpoint_client_ip: dict[str, dict[str, dict[str, Any]]] = {}
        self._lifetime_dropped_tools: dict[str, int] = {}
        self._lifetime_dropped_history: dict[str, int] = {}
        self._lifetime_errors: dict[str, int] = {}
        self._lifetime_started_at = time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            LOG.warning("could not create stats directory %s: %s", self.path.parent, exc)
        self._load_state()

    def _load_state(self) -> None:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._lifetime.update({k: v for k, v in state.get("counters", {}).items() if k in self._lifetime})
            self._lifetime_dropped_tools = dict(state.get("dropped_tool_types", {}))
            self._lifetime_dropped_history = dict(state.get("dropped_history_item_types", {}))
            self._lifetime_errors = dict(state.get("errors_by_type", {}))
            self._lifetime_by_model = {
                str(name): dict(counters)
                for name, counters in state.get("by_model", {}).items()
                if isinstance(counters, dict)
            }
            self._lifetime_by_client_ip = {
                str(name): dict(counters)
                for name, counters in state.get("by_client_ip", {}).items()
                if isinstance(counters, dict)
            }
            self._lifetime_by_endpoint = {
                str(name): dict(counters)
                for name, counters in state.get("by_endpoint", {}).items()
                if isinstance(counters, dict)
            }
            self._lifetime_by_endpoint_client_ip = {
                str(endpoint): {
                    str(client): dict(counters)
                    for client, counters in clients.items()
                    if isinstance(counters, dict)
                }
                for endpoint, clients in state.get("by_endpoint_client_ip", {}).items()
                if isinstance(clients, dict)
            }
            self._lifetime_started_at = state.get("started_at", self._lifetime_started_at)
            lifetime_metrics = state.get("metrics", {})
            if isinstance(lifetime_metrics, dict):
                self._lifetime_metrics.update({k: v for k, v in lifetime_metrics.items() if k in self._lifetime_metrics})
            self._lifetime_tool_call_counts = {
                str(name): int(count) for name, count in state.get("tool_call_counts", {}).items()
                if isinstance(count, int) and count >= 0
            }
            self._lifetime_inference_errors = {
                str(name): int(count) for name, count in state.get("inference_errors_by_category", {}).items()
                if isinstance(count, int) and count >= 0
            }
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            LOG.warning("could not load stats state %s: %s", self.state_path, exc)

    def _persist_state(self) -> None:
        state = {
            "schema_version": 3,
            "started_at": self._lifetime_started_at,
            "metrics": self._lifetime_metrics,
            "tool_call_counts": self._lifetime_tool_call_counts,
            "inference_errors_by_category": self._lifetime_inference_errors,
            "counters": self._lifetime,
            "dropped_tool_types": self._lifetime_dropped_tools,
            "dropped_history_item_types": self._lifetime_dropped_history,
            "errors_by_type": self._lifetime_errors,
            "by_model": self._lifetime_by_model,
            "by_client_ip": self._lifetime_by_client_ip,
            "by_endpoint": self._lifetime_by_endpoint,
            "by_endpoint_client_ip": self._lifetime_by_endpoint_client_ip,
        }
        temporary = self.state_path.with_name(self.state_path.name + f".{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.state_path)
        except OSError as exc:
            LOG.warning("could not persist stats state %s: %s", self.state_path, exc)
            try:
                temporary.unlink()
            except OSError:
                pass

    def record(
        self,
        *,
        path: str,
        status: int,
        started: float,
        headers_at: float,
        finished: float,
        stream: bool,
        response_bytes: int,
        usage: dict[str, Any] | None,
        tool_info: dict[str, Any] | None,
        tool_call_ids: list[str],
        error_type: str | None = None,
        model: str | None = None,
        client_ip: str | None = None,
        method: str = "POST",
        request_bytes: int = 0,
        timings: dict[str, Any] | None = None,
        ttft_ms: float | None = None,
        request_type: str | None = None,
        cancelled: bool = False,
        retry_count: int = 0,
        tool_call_counts: dict[str, int] | None = None,
        context_limit: int | None = None,
        error_category: str | None = None,
    ) -> None:
        input_tokens = _numeric_usage(usage, "input_tokens")
        output_tokens = _numeric_usage(usage, "output_tokens")
        total_tokens = _numeric_usage(usage, "total_tokens")
        request_seconds = max(finished - started, 0.0)
        normalized = normalize_backend_metrics(usage, timings)
        decode_seconds = max(finished - headers_at, 0.0) if stream else request_seconds
        output_tps = normalized["decode_tok_s"]
        if output_tps is None:
            output_tps = output_tokens / decode_seconds if output_tokens and decode_seconds else None
        request_type = request_type or _request_type(method, path)
        is_inference = request_type.endswith("_inference")
        if error_category is None and error_type:
            if cancelled:
                error_category = "client_cancelled"
            elif error_type.startswith("local:"):
                error_category = "client_error"
            elif error_type.startswith("upstream:") and status == 502:
                error_category = "backend_connection_error"
            elif error_type.startswith("upstream:") and status >= 400:
                error_category = "backend_http_error"
            else:
                error_category = "proxy_internal_error"
        context_limit = context_limit or self._context_limit
        context_tokens = normalized["prompt_forwarded_tokens"]
        context_utilization = (
            context_tokens * 100 / context_limit
            if context_tokens is not None and context_limit else None
        )
        tool_info = tool_info or {}
        tool_result_ids = tool_info.get("tool_result_ids", [])
        if not isinstance(tool_result_ids, list):
            tool_result_ids = []
        tool_round_trip_ms = 0.0
        tool_round_trips = 0
        event = {
            "timestamp": time.time(),
            "request_id": uuid.uuid4().hex,
            "proxy_session_id": self._session_id,
            "method": method.upper(),
            "path": path,
            "model": model or "<unknown>",
            "client_ip": client_ip or "<unknown>",
            "status": status,
            "request_type": request_type,
            "is_inference": is_inference,
            "success": status < 400 and not cancelled,
            "cancelled": cancelled,
            "retry_count": max(retry_count, 0),
            "stream": stream,
            "request_bytes": max(request_bytes, 0),
            "response_bytes": response_bytes,
            "request_ms": round(request_seconds * 1000, 1),
            "time_to_headers_ms": round(max(headers_at - started, 0.0) * 1000, 1),
            "output_window_ms": round(decode_seconds * 1000, 1),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "output_tokens_per_second": round(output_tps, 2) if output_tps is not None else None,
            **normalized,
            "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
            "total_request_ms": round(request_seconds * 1000, 1),
            "context_tokens": context_tokens,
            "context_limit": context_limit,
            "context_utilization_pct": round(context_utilization, 2) if context_utilization is not None else None,
            "tool_declarations": tool_info.get("tool_declarations", 0),
            "mcp_tool_declarations": tool_info.get("mcp_tool_declarations", 0),
            "dropped_tool_types": tool_info.get("dropped_tool_types", 0),
            "tool_calls": len(tool_call_ids) if tool_call_ids else sum((tool_call_counts or {}).values()),
            "tool_call_counts": tool_call_counts or {},
            "tool_results": len(tool_result_ids),
            "input_item_types": tool_info.get("input_item_types", []),
            "dropped_history_item_types": tool_info.get("dropped_history_item_types", []),
            "normalized_history_item_count": tool_info.get("normalized_history_item_count", 0),
            "normalized_tool_outputs": tool_info.get("normalized_tool_outputs", 0),
            "folded_instruction_items": tool_info.get("folded_instruction_items", 0),
            "error_type": error_type,
            "error_category": error_category,
        }
        with self._lock:
            now = finished
            for call_id in tool_result_ids:
                call_started = self._pending_tool_calls.pop(call_id, None)
                if call_started is not None:
                    tool_round_trip_ms += max(now - call_started, 0.0) * 1000
                    tool_round_trips += 1
            for call_id in tool_call_ids:
                self._pending_tool_calls[call_id] = now
            tool_round_trip_ms = round(tool_round_trip_ms, 1)
            event["tool_round_trip_ms"] = tool_round_trip_ms
            event["tool_round_trips"] = tool_round_trips
            self._recent_events.append(event)
            self._accumulate_metrics(self._session_metrics, event)
            self._accumulate_metrics(self._lifetime_metrics, event)
            if is_inference:
                _merge_counts(self._session_tool_call_counts, event["tool_call_counts"])
                _merge_counts(self._lifetime_tool_call_counts, event["tool_call_counts"])
                if error_type:
                    _inc_type(self._session_inference_errors, error_category or "unknown")
                    _inc_type(self._lifetime_inference_errors, error_category or "unknown")
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            except OSError as exc:
                LOG.warning("could not write stats file %s: %s", self.path, exc)
            self._summary["requests"] += 1
            if path == "/v1/responses":
                self._summary["responses"] += 1
            if status >= 400:
                self._summary["errors"] += 1
            self._summary["input_tokens"] += input_tokens
            self._summary["output_tokens"] += output_tokens
            self._summary["total_tokens"] += total_tokens
            self._summary["request_ms"] += event["request_ms"]
            if output_tps is not None:
                self._summary["output_tokens_per_second"] += output_tps
            self._summary["tool_declarations"] += event["tool_declarations"]
            self._summary["mcp_tool_declarations"] += event["mcp_tool_declarations"]
            self._summary["dropped_tool_types"] += event["dropped_tool_types"]
            self._summary["dropped_history_item_types"] += len(event["dropped_history_item_types"])
            self._summary["tool_calls"] += event["tool_calls"]
            self._summary["tool_results"] += event["tool_results"]
            self._summary["tool_round_trip_ms"] += tool_round_trip_ms
            self._summary["tool_round_trips"] += tool_round_trips
            self._add_totals(self._lifetime, event)
            session_model = self._session_by_model.setdefault(event["model"], _new_group())
            session_client = self._session_by_client_ip.setdefault(event["client_ip"], _new_group())
            lifetime_model = self._lifetime_by_model.setdefault(event["model"], _new_group())
            lifetime_client = self._lifetime_by_client_ip.setdefault(event["client_ip"], _new_group())
            endpoint = f"{event['method']} {event['path']}"
            session_endpoint = self._session_by_endpoint.setdefault(endpoint, _new_group())
            lifetime_endpoint = self._lifetime_by_endpoint.setdefault(endpoint, _new_group())
            session_endpoint_clients = self._session_by_endpoint_client_ip.setdefault(endpoint, {})
            lifetime_endpoint_clients = self._lifetime_by_endpoint_client_ip.setdefault(endpoint, {})
            session_endpoint_client = session_endpoint_clients.setdefault(event["client_ip"], _new_group())
            lifetime_endpoint_client = lifetime_endpoint_clients.setdefault(event["client_ip"], _new_group())
            self._add_totals(session_model, event)
            self._add_totals(session_client, event)
            self._add_totals(lifetime_model, event)
            self._add_totals(lifetime_client, event)
            self._add_totals(session_endpoint, event)
            self._add_totals(lifetime_endpoint, event)
            self._add_totals(session_endpoint_client, event)
            self._add_totals(lifetime_endpoint_client, event)
            for value in tool_info.get("dropped_tool_names", []):
                _inc_type(self._session_dropped_tools, value)
                _inc_type(self._lifetime_dropped_tools, value)
            for value in event["dropped_history_item_types"]:
                _inc_type(self._session_dropped_history, value)
                _inc_type(self._lifetime_dropped_history, value)
            if error_type:
                _inc_type(self._session_errors, error_type)
                _inc_type(self._lifetime_errors, error_type)
            self._persist_state()

        if is_inference:
            LOG.info("inference_stats %s", json.dumps({
                key: event.get(key) for key in (
                    "request_type", "status", "context_tokens", "prompt_eval_tokens",
                    "prompt_cached_tokens", "decode_tokens", "prompt_eval_tok_s",
                    "decode_tok_s", "ttft_ms", "total_request_ms", "tool_calls",
                    "tool_call_counts", "error_type", "cancelled",
                )
            }, separators=(",", ":")))

    @staticmethod
    def _accumulate_metrics(target: dict[str, Any], event: dict[str, Any]) -> None:
        target["http_requests"] += 1
        target["http_successful" if event.get("success") else "http_failed"] += 1
        if not event.get("is_inference"):
            return
        target["inference_requests"] += 1
        target["successful" if event.get("success") else "failed"] += 1
        target["cancelled"] += int(bool(event.get("cancelled")))
        target["retries"] += int(event.get("retry_count") or 0)
        context_tokens = event.get("context_tokens")
        utilization = event.get("context_utilization_pct")
        if isinstance(context_tokens, int):
            target["context_tokens_sum"] += context_tokens
            target["context_requests_with_context"] += 1
            target["max_context_tokens"] = max(target["max_context_tokens"] or 0, context_tokens)
        if isinstance(utilization, (int, float)):
            target["max_context_utilization_pct"] = max(target["max_context_utilization_pct"] or 0, utilization)
            target["requests_over_75pct"] += int(utilization > 75)
            target["requests_over_90pct"] += int(utilization > 90)
        for key in ("prompt_forwarded_tokens", "prompt_eval_tokens", "prompt_cached_tokens", "decode_tokens", "generated_tokens", "prompt_eval_ms", "decode_ms", "tool_calls"):
            value = event.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                target[key] += value
        for metric_key, report_key in (
            ("prompt_forwarded_tokens", "prompt_forwarded_reports"),
            ("prompt_eval_tokens", "prompt_eval_reports"),
            ("prompt_cached_tokens", "prompt_cache_reports"),
            ("generated_tokens", "generated_token_reports"),
        ):
            if isinstance(event.get(metric_key), (int, float)):
                target[report_key] += 1
        if event.get("prompt_eval_tokens") is not None and event.get("prompt_eval_ms") is not None:
            target["prompt_timing_reports"] += 1
        if event.get("decode_tokens") is not None and event.get("decode_ms") is not None:
            target["decode_timing_reports"] += 1

    @staticmethod
    def _add_totals(target: dict[str, Any], event: dict[str, Any]) -> None:
        for key, value in _new_counters().items():
            target.setdefault(key, value)
        target["requests"] += 1
        if event["path"] == "/v1/responses": target["responses"] += 1
        if event["status"] >= 400: target["errors"] += 1
        for key in ("input_tokens", "output_tokens", "total_tokens", "request_bytes", "response_bytes", "request_ms",
                    "tool_declarations", "mcp_tool_declarations", "tool_calls",
                    "tool_results", "tool_round_trip_ms", "tool_round_trips"):
            target[key] += event[key]
        target["dropped_tool_types"] += event["dropped_tool_types"]
        target["dropped_history_item_types"] += len(event["dropped_history_item_types"])
        if event["output_tokens_per_second"] is not None:
            target["output_tokens_per_second"] += event["output_tokens_per_second"]

    def set_context_limit(self, context_limit: int | None) -> None:
        if isinstance(context_limit, int) and context_limit > 0:
            with self._lock:
                self._context_limit = context_limit

    def snapshot(self, window: str = "session") -> dict[str, Any]:
        with self._lock:
            result = dict(self._summary)
            session = dict(self._summary)
            lifetime = dict(self._lifetime)
            session_by_model = _group_totals(self._session_by_model)
            session_by_client_ip = _group_totals(self._session_by_client_ip)
            lifetime_by_model = _group_totals(self._lifetime_by_model)
            lifetime_by_client_ip = _group_totals(self._lifetime_by_client_ip)
            session_by_endpoint = _group_totals(self._session_by_endpoint)
            lifetime_by_endpoint = _group_totals(self._lifetime_by_endpoint)
            session_by_endpoint_client_ip = {
                endpoint: _group_totals(clients)
                for endpoint, clients in self._session_by_endpoint_client_ip.items()
            }
            lifetime_by_endpoint_client_ip = {
                endpoint: _group_totals(clients)
                for endpoint, clients in self._lifetime_by_endpoint_client_ip.items()
            }
            recent_events = list(self._recent_events)
            lifetime_metrics = dict(self._lifetime_metrics)
            session_metrics = dict(self._session_metrics)
            lifetime_tool_call_counts = dict(self._lifetime_tool_call_counts)
            session_tool_call_counts = dict(self._session_tool_call_counts)
            lifetime_errors = dict(self._lifetime_inference_errors)
            session_errors = dict(self._session_inference_errors)
            context_limit = self._context_limit
        responses = result["responses"]
        result["stats_file"] = str(self.path)
        result["state_file"] = str(self.state_path)
        result["schema_version"] = 3
        result["session"] = {"id": self._session_id, "started_at": self._session_started_at,
                              "counters": session, "dropped_tool_types": dict(self._session_dropped_tools),
                              "dropped_history_item_types": dict(self._session_dropped_history),
                              "errors_by_type": dict(self._session_errors),
                              "by_model": session_by_model,
                              "by_client_ip": session_by_client_ip,
                              "by_endpoint": session_by_endpoint,
                              "by_endpoint_client_ip": session_by_endpoint_client_ip}
        result["lifetime"] = {"started_at": self._lifetime_started_at, "counters": lifetime,
                               "dropped_tool_types": dict(self._lifetime_dropped_tools),
                               "dropped_history_item_types": dict(self._lifetime_dropped_history),
                               "errors_by_type": dict(self._lifetime_errors),
                               "by_model": lifetime_by_model,
                               "by_client_ip": lifetime_by_client_ip,
                               "by_endpoint": lifetime_by_endpoint,
                               "by_endpoint_client_ip": lifetime_by_endpoint_client_ip}
        result["dropped_tool_type_counts"] = dict(self._session_dropped_tools)
        result["dropped_history_item_type_counts"] = dict(self._session_dropped_history)
        result["errors_by_type"] = dict(self._session_errors)
        result["average_request_ms"] = round(result["request_ms"] / result["requests"], 1) if result["requests"] else 0.0
        result["average_output_tokens_per_second"] = round(
            result["output_tokens_per_second"] / responses, 2
        ) if responses else 0.0
        result["average_tool_round_trip_ms"] = round(
            result["tool_round_trip_ms"] / result["tool_round_trips"], 1
        ) if result["tool_round_trips"] else 0.0
        window_seconds = {"5m": 300, "15m": 900, "60m": 3600}
        if window in window_seconds:
            cutoff = time.time() - window_seconds[window]
            selected = [event for event in recent_events if event.get("timestamp", 0) >= cutoff]
            metrics = _aggregate_metrics(selected, context_limit)
            metrics["window_may_be_truncated"] = len(recent_events) == self._history_max
            metrics["percentile_history_scope"] = "bounded recent-request history"
        elif window == "session":
            metrics = _finalize_metric_totals(
                session_metrics, context_limit, session_tool_call_counts, session_errors,
                _aggregate_metrics(recent_events, context_limit)["percentiles"],
                "bounded current-process history",
            )
            metrics["window_may_be_truncated"] = False
        elif window == "all":
            metrics = _finalize_metric_totals(
                lifetime_metrics, context_limit, lifetime_tool_call_counts, lifetime_errors,
                _aggregate_metrics(recent_events, context_limit)["percentiles"],
                "bounded current-process history",
            )
            metrics["window_may_be_truncated"] = False
        else:
            raise ValueError("window must be one of: 5m, 15m, 60m, session, all")
        metrics["window"] = window
        metrics["history_limit"] = self._history_max
        metrics["percentile_history_size"] = len(recent_events)
        result["metrics"] = metrics
        # Preserve this public field as a compatibility alias, but make it the
        # correctly weighted rate whenever native timing totals are available.
        if metrics.get("decode_weighted_tok_s") is not None:
            result["average_output_tokens_per_second"] = metrics["decode_weighted_tok_s"]
        return result


def _numeric_usage(usage: dict[str, Any] | None, key: str) -> int:
    if not usage:
        return 0
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    value = next((usage.get(name) for name in aliases.get(key, (key,)) if name in usage), 0)
    if key == "total_tokens" and not value:
        value = _numeric_usage(usage, "input_tokens") + _numeric_usage(usage, "output_tokens")
    return value if isinstance(value, int) and value >= 0 else 0


def _collect_call_ids(value: Any, item_type: str) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if value.get("type") == item_type and isinstance(value.get("call_id"), str):
            found.append(value["call_id"])
        for item in value.values():
            found.extend(_collect_call_ids(item, item_type))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_call_ids(item, item_type))
    return found


def _collect_tool_call_counts(value: Any) -> dict[str, int]:
    counts: dict[str, int] = {}

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if item.get("type") == "function_call":
                name = item.get("name")
                if isinstance(name, str) and name:
                    counts[name] = counts.get(name, 0) + 1
            elif isinstance(item.get("function"), dict):
                name = item["function"].get("name")
                if isinstance(name, str) and name:
                    counts[name] = counts.get(name, 0) + 1
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return counts


def _merge_counts(target: dict[str, int], incoming: dict[str, int]) -> None:
    for name, count in incoming.items():
        target[name] = target.get(name, 0) + count


def _has_generated_output(value: Any) -> bool:
    if isinstance(value, dict):
        event_type = value.get("type")
        if isinstance(event_type, str) and event_type.endswith(".delta"):
            delta = value.get("delta")
            if isinstance(delta, str) and delta:
                return True
        choices = value.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta", {})
                if isinstance(delta, dict) and any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls")):
                    return True
        return any(_has_generated_output(child) for key, child in value.items() if key not in {"usage", "timings"})
    if isinstance(value, list):
        return any(_has_generated_output(child) for child in value)
    return False


def _render_stats_text(snapshot: dict[str, Any]) -> str:
    metrics = snapshot["metrics"]
    def fmt(value: Any, suffix: str = "") -> str:
        return "N/A" if value is None else f"{value}{suffix}"
    lines = [
        f"Window: {metrics['window']}",
        "",
        "Requests",
        f"  HTTP requests: {metrics['http_requests']}",
        f"  Inference requests: {metrics['inference_requests']}",
        f"  Successful / failed: {metrics['successful']} / {metrics['failed']}",
        f"  Cancelled / retries: {metrics['cancelled']} / {metrics['retries']}",
        "",
        "Prompt tokens",
        f"  Forwarded: {metrics['prompt_forwarded_tokens']}",
        f"  Evaluated: {metrics['prompt_eval_tokens']}",
        f"  Cached: {metrics['prompt_cached_tokens']}",
        f"  Cache reuse: {fmt(metrics['prompt_cache_reuse_pct'], '%')}",
        f"  Context max / limit: {fmt(metrics['max_context_tokens'])} / {fmt(metrics['context_limit'])}",
        f"  Peak utilization: {fmt(metrics['max_context_utilization_pct'], '%')}",
        "",
        "Performance",
        f"  Generated output tokens: {metrics['generated_tokens']}",
        f"  Timed decode steps: {metrics['decode_tokens']}",
        f"  Prompt eval weighted: {fmt(metrics['prompt_eval_weighted_tok_s'], ' tok/s')}",
        f"  Decode weighted: {fmt(metrics['decode_weighted_tok_s'], ' tok/s')}",
        f"  TTFT p50 / p95: {fmt(metrics['percentiles']['ttft_ms']['p50'], ' ms')} / {fmt(metrics['percentiles']['ttft_ms']['p95'], ' ms')}",
        f"  Request p50 / p95: {fmt(metrics['percentiles']['total_request_ms']['p50'], ' ms')} / {fmt(metrics['percentiles']['total_request_ms']['p95'], ' ms')}",
        f"  Tool calls: {metrics['tool_calls']}",
        f"  Tool counts: {json.dumps(metrics['tool_call_counts'], sort_keys=True)}",
        f"  Errors by category: {json.dumps(metrics['errors_by_category'], sort_keys=True)}",
        f"  Percentiles use {metrics['percentile_history_size']} recent requests (limit {metrics['history_limit']}).",
    ]
    return "\n".join(lines) + "\n"


def _request_tool_stats(
    original: dict[str, Any],
    transformed: dict[str, Any],
    mappings: dict[str, ToolMapping],
    dropped: list[str],
    history_diagnostics: dict[str, Any],
) -> dict[str, Any]:
    declarations = transformed.get("tools", [])
    return {
        "tool_declarations": len(declarations) if isinstance(declarations, list) else 0,
        "mcp_tool_declarations": len(mappings),
        "dropped_tool_types": history_diagnostics.get("dropped_tool_types", 0),
        "dropped_tool_names": history_diagnostics.get("dropped_tool_names", []),
        "input_item_types": history_diagnostics.get("input_item_types", []),
        "dropped_history_item_types": history_diagnostics.get("dropped_history_item_types", []),
        "normalized_history_item_count": history_diagnostics.get("normalized_history_item_count", 0),
        "normalized_tool_outputs": history_diagnostics.get("normalized_tool_outputs", 0),
        "folded_instruction_items": history_diagnostics.get("folded_instruction_items", 0),
        "tool_result_ids": _collect_call_ids(original.get("input"), "function_call_output"),
    }


def _error_type(source: str, status: int, raw: bytes | None = None, fallback: str = "unknown") -> str:
    code = None
    if raw:
        try:
            error = json.loads(raw).get("error", {})
            if isinstance(error, dict): code = error.get("code") or error.get("type")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
    code = str(code or fallback).lower()
    code = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in code)
    return f"{source}:{status}:{code}"


def default_stats_file() -> str:
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return str(Path(state_home) / "codex-llama-proxy" / "stats.jsonl")
    return str(Path.home() / ".local" / "state" / "codex-llama-proxy" / "stats.jsonl")


def request_model(payload: Any) -> str:
    if isinstance(payload, dict):
        model = payload.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return "<unknown>"


def request_model_from_query(query: str) -> str:
    values = parse_qs(query).get("model", [])
    return values[0].strip() if values and values[0].strip() else "<unknown>"


STATS_UI_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex llama proxy statistics</title>
<style>
:root { color-scheme: dark; font-family: system-ui, sans-serif; }
body { margin: 0; background: #111827; color: #e5e7eb; }
main { max-width: 1400px; margin: 0 auto; padding: 24px; }
h1 { margin-top: 0; }
button { border: 0; border-radius: 6px; padding: 8px 14px; background: #2563eb; color: white; cursor: pointer; }
button:hover { background: #1d4ed8; }
.status { color: #9ca3af; margin-left: 12px; }
.cards { display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0; }
.card { min-width: 130px; padding: 12px; border: 1px solid #374151; border-radius: 8px; background: #1f2937; }
.card strong { display: block; font-size: 1.35rem; margin-top: 4px; }
.section { margin-top: 28px; }
.tables { display: grid; grid-template-columns: repeat(auto-fit, minmax(520px, 1fr)); gap: 20px; }
table { width: 100%; border-collapse: collapse; background: #1f2937; }
th, td { padding: 8px 10px; border-bottom: 1px solid #374151; text-align: right; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; max-width: 360px; overflow: hidden; text-overflow: ellipsis; }
th { color: #9ca3af; font-weight: 600; }
.error { color: #fca5a5; }
@media (max-width: 700px) { main { padding: 14px; } .tables { grid-template-columns: 1fr; } table { font-size: .8rem; } }
</style>
</head>
<body>
<main>
<h1>Codex llama proxy statistics</h1>
<button id="refresh">Refresh</button><span class="status" id="status">Not loaded</span>
<section class="section"><h2>Running llama.cpp stack</h2><div class="card" id="runtime-status">Checking...</div><pre id="runtime-details"></pre></section>
<section class="section"><h2>Current session</h2><div class="cards" id="session-cards"></div><div class="tables"><div><h3>By model</h3><div id="session-model"></div></div><div><h3>By client IP</h3><div id="session-client"></div></div><div><h3>By endpoint</h3><div id="session-endpoint"></div></div><div><h3>Endpoint by client IP</h3><div id="session-endpoint-client"></div></div></div></section>
<section class="section"><h2>Inference performance</h2><pre id="metrics-details">Loading...</pre></section>
<section class="section"><h2>Lifetime</h2><div class="cards" id="lifetime-cards"></div><div class="tables"><div><h3>By model</h3><div id="lifetime-model"></div></div><div><h3>By client IP</h3><div id="lifetime-client"></div></div><div><h3>By endpoint</h3><div id="lifetime-endpoint"></div></div><div><h3>Endpoint by client IP</h3><div id="lifetime-endpoint-client"></div></div></div></section>
</main>
<script>
const fields = [['requests','Requests'],['errors','Errors'],['input_tokens','Input tokens'],['output_tokens','Output tokens'],['total_tokens','Total tokens'],['tool_calls','Tool calls']];
const number = value => Number(value || 0).toLocaleString();
function cards(target, counters) {
  target.replaceChildren(...fields.map(([key, label]) => { const el = document.createElement('div'); el.className = 'card'; el.textContent = label; const strong = document.createElement('strong'); strong.textContent = number(counters[key]); el.appendChild(strong); return el; }));
}
function table(target, groups) {
  const table = document.createElement('table');
  const headers = ['Group','Requests','Errors','Input','Output','Total','Avg ms','Tools'];
  const head = document.createElement('tr'); headers.forEach(value => { const cell = document.createElement('th'); cell.textContent = value; head.appendChild(cell); });
  const thead = document.createElement('thead'); thead.appendChild(head); table.appendChild(thead);
  const body = document.createElement('tbody');
  Object.entries(groups || {}).sort((a,b) => (b[1].requests || 0) - (a[1].requests || 0)).forEach(([name, item]) => {
    const row = document.createElement('tr'); const values = [name, item.requests, item.errors, item.input_tokens, item.output_tokens, item.total_tokens, item.requests ? (item.request_ms / item.requests).toFixed(1) : '0.0', item.tool_calls];
    values.forEach((value, index) => { const cell = document.createElement('td'); cell.textContent = index === 0 ? value : number(value); if (index === 2 && Number(value)) cell.className = 'error'; row.appendChild(cell); }); body.appendChild(row);
  });
  table.appendChild(body); target.replaceChildren(table);
}
function endpointClientTable(target, groups) {
  const flattened = {};
  Object.entries(groups || {}).forEach(([endpoint, clients]) => Object.entries(clients || {}).forEach(([client, item]) => { flattened[endpoint + ' @ ' + client] = item; }));
  table(target, flattened);
}
async function refresh() {
  const status = document.getElementById('status'); status.textContent = 'Loading...';
  try {
    const response = await fetch('/stats', { cache: 'no-store' }); if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    cards(document.getElementById('session-cards'), data.session.counters); cards(document.getElementById('lifetime-cards'), data.lifetime.counters);
    const metrics = data.metrics || {}; const p = metrics.percentiles || {};
    document.getElementById('metrics-details').textContent = [
      'Requests (' + (metrics.window || 'session') + '): HTTP ' + number(metrics.http_requests) + ', inference ' + number(metrics.inference_requests) + ', succeeded ' + number(metrics.successful) + ', failed ' + number(metrics.failed),
      'Prompt tokens: forwarded ' + number(metrics.prompt_forwarded_tokens) + ', evaluated ' + number(metrics.prompt_eval_tokens) + ', cached ' + number(metrics.prompt_cached_tokens) + ' (' + (metrics.prompt_cache_reuse_pct ?? 'N/A') + '% reuse)',
      'Weighted rates: prompt ' + (metrics.prompt_eval_weighted_tok_s ?? 'N/A') + ' tok/s, decode ' + (metrics.decode_weighted_tok_s ?? 'N/A') + ' tok/s',
      'TTFT p50/p95: ' + (p.ttft_ms?.p50 ?? 'N/A') + '/' + (p.ttft_ms?.p95 ?? 'N/A') + ' ms; request p50/p95: ' + (p.total_request_ms?.p50 ?? 'N/A') + '/' + (p.total_request_ms?.p95 ?? 'N/A') + ' ms',
      'Context max/limit: ' + (metrics.max_context_tokens ?? 'N/A') + '/' + (metrics.context_limit ?? 'N/A') + '; tools ' + number(metrics.tool_calls) + '; errors ' + JSON.stringify(metrics.errors_by_category || {})
    ].join('\\n');
    table(document.getElementById('session-model'), data.session.by_model); table(document.getElementById('session-client'), data.session.by_client_ip);
    table(document.getElementById('lifetime-model'), data.lifetime.by_model); table(document.getElementById('lifetime-client'), data.lifetime.by_client_ip);
    table(document.getElementById('session-endpoint'), data.session.by_endpoint); endpointClientTable(document.getElementById('session-endpoint-client'), data.session.by_endpoint_client_ip);
    table(document.getElementById('lifetime-endpoint'), data.lifetime.by_endpoint); endpointClientTable(document.getElementById('lifetime-endpoint-client'), data.lifetime.by_endpoint_client_ip);
    status.textContent = 'Updated ' + new Date().toLocaleString();
  } catch (error) { status.textContent = 'Load failed: ' + error; }
  await refreshRuntime();
}
async function refreshRuntime() {
  const status = document.getElementById('runtime-status'); const details = document.getElementById('runtime-details');
  try {
    const response = await fetch('/stats/runtime', { cache: 'no-store' });
    if (!response.ok) throw new Error('offline');
    const data = await response.json(); const params = (data.default_generation_settings || {}).params || {};
    const model = (data.model_alias || data.model_path || '<unknown>').split('/').pop();
    status.textContent = 'Online'; status.className = 'card';
    details.textContent = [
      'Model: ' + model,
      'Context: ' + number((data.default_generation_settings || {}).n_ctx),
      'Training context: ' + number((data.model_meta || {}).n_ctx_train || data.n_ctx_train || 0),
      'Quantization: ' + (data.model_ftype || (data.model_meta || {}).ftype || 'unknown'),
      'Slots: ' + number(data.total_slots),
      'Temperature: ' + (params.temperature ?? 'unknown'),
      'Top-p: ' + (params.top_p ?? 'unknown'),
      'Top-k: ' + (params.top_k ?? 'unknown'),
      'Build: ' + ((data.build_info || {}).build_number || (data.build_info || {}).version || 'unknown')
    ].join('\\n');
  } catch (error) {
    status.textContent = 'Stack offline'; status.className = 'card error'; details.textContent = '';
  }
}
document.getElementById('refresh').addEventListener('click', refresh); refresh();
</script>
</body>
</html>
"""


def flatten_name(namespace: str, tool_name: str) -> str:
    """Return Codex's canonical flat MCP name."""
    namespace = namespace.rstrip("_")
    if not namespace:
        return tool_name
    return f"{namespace}__{tool_name}"


def _function_spec(tool: dict[str, Any]) -> dict[str, Any] | None:
    """Accept Responses-style and Chat-Completions-style nested function specs."""
    if tool.get("type") != "function":
        return None
    if isinstance(tool.get("function"), dict):
        spec = copy.deepcopy(tool["function"])
        for key in ("strict",):
            if key in tool and key not in spec:
                spec[key] = tool[key]
        return spec
    return copy.deepcopy(tool)


def flatten_tools(tools: Any) -> tuple[list[dict[str, Any]], dict[str, ToolMapping], list[str]]:
    """Flatten namespace tools and report dropped unsupported tool types."""
    if not isinstance(tools, list):
        raise ValueError("'tools' must be an array")

    flattened: list[dict[str, Any]] = []
    mappings: dict[str, ToolMapping] = {}
    dropped: list[str] = []

    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("each tool must be an object")

        tool_type = tool.get("type")
        if tool_type == "function":
            flattened.append(copy.deepcopy(tool))
            continue

        if tool_type != "namespace":
            dropped.append(str(tool_type or "<missing>"))
            continue

        namespace = tool.get("name")
        nested = tool.get("tools")
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("namespace tool requires a non-empty name")
        if not isinstance(nested, list):
            raise ValueError("namespace tool requires a tools array")

        for nested_tool in nested:
            if not isinstance(nested_tool, dict):
                raise ValueError("namespace entries must be objects")
            spec = _function_spec(nested_tool)
            if spec is None:
                dropped.append(str(nested_tool.get("type") or "<missing>"))
                continue
            original_name = spec.get("name")
            if not isinstance(original_name, str) or not original_name:
                raise ValueError("namespace function requires a non-empty name")

            flat_name = flatten_name(namespace, original_name)
            previous = mappings.get(flat_name)
            if previous is not None and previous != ToolMapping(namespace, original_name):
                raise ValueError(f"flattened tool name collision: {flat_name}")

            spec["type"] = "function"
            spec["name"] = flat_name
            mappings[flat_name] = ToolMapping(namespace, original_name)
            flattened.append(spec)

    return flattened, mappings, dropped


def transform_request(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, ToolMapping], list[str], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    transformed = copy.deepcopy(payload)
    mappings: dict[str, ToolMapping] = {}
    dropped: list[str] = []
    tool_dropped: list[str] = []
    history_dropped, history_diagnostics = _normalize_history_items(transformed)
    dropped.extend(history_dropped)
    history_diagnostics["folded_instruction_items"] = _fold_instruction_messages(transformed)
    history_diagnostics["normalized_reasoning_efforts"] = _normalize_reasoning_efforts(transformed)
    if "tools" in transformed:
        flat_tools, mappings, tool_dropped = flatten_tools(transformed["tools"])
        dropped.extend(tool_dropped)
        if flat_tools:
            transformed["tools"] = flat_tools
        else:
            transformed.pop("tools", None)
    history_diagnostics["dropped_tool_types"] = len(tool_dropped)
    history_diagnostics["dropped_tool_names"] = tool_dropped
    return transformed, mappings, dropped, history_diagnostics


def _tool_output_as_text(value: Any) -> str:
    """Convert Responses tool output content to llama.cpp-compatible text."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [_tool_output_as_text(part) for part in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _normalize_function_call_output(item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    converted = copy.deepcopy(item)
    converted["type"] = "function_call_output"
    was_non_text = not isinstance(converted.get("output"), str)
    converted["output"] = _tool_output_as_text(converted.get("output"))
    return converted, was_non_text


def _normalize_history_items(payload: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Translate Codex history items that llama-server names differently."""
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return [], {"input_item_types": [], "dropped_history_item_types": [], "normalized_history_item_count": 0}

    normalized: list[Any] = []
    dropped: list[str] = []
    input_item_types: list[str] = []
    normalized_tool_outputs = 0
    unsupported = {
        "web_search_call",
        "web_search_call_output",
        "computer_call",
        "computer_call_output",
        "mcp_tool_call_end",
    }
    for item in input_items:
        if not isinstance(item, dict):
            normalized.append(item)
            continue

        item_type = item.get("type")
        type_name = str(item_type or "<missing>")
        input_item_types.append(type_name)
        if item_type == "custom_tool_call":
            converted = copy.deepcopy(item)
            converted["type"] = "function_call"
            if "arguments" not in converted:
                raw_input = converted.pop("input", "")
                # llama.cpp parses function_call.arguments as JSON. Codex
                # custom tools carry opaque input, often JavaScript, so wrap
                # it in an object while preserving it verbatim.
                converted["arguments"] = json.dumps(
                    {"input": raw_input},
                    separators=(",", ":"),
                )
            normalized.append(converted)
            continue

        if item_type == "custom_tool_call_output":
            converted, changed = _normalize_function_call_output(item)
            normalized_tool_outputs += int(changed)
            normalized.append(converted)
            continue

        if item_type == "compaction":
            normalized.append(_normalize_compaction(item))
            continue

        if item_type in unsupported:
            dropped.append(str(item_type))
            continue

        # Codex may send MCP execution records that are not representable in
        # llama.cpp's chat-message converter. Preserve records that already
        # have the standard function-call shape; discard metadata-only forms.
        if item_type in {"mcp_call", "mcp_tool_call"}:
            if all(isinstance(item.get(key), str) for key in ("call_id", "name")) and (
                isinstance(item.get("arguments"), str) or isinstance(item.get("input"), str)
            ):
                converted = copy.deepcopy(item)
                converted["type"] = "function_call"
                if "arguments" not in converted:
                    converted["arguments"] = json.dumps({"input": converted.pop("input")}, separators=(",", ":"))
                normalized.append(converted)
            else:
                dropped.append(type_name)
            continue

        if item_type in {"mcp_call_output", "mcp_tool_call_output"}:
            if isinstance(item.get("call_id"), str) and ("output" in item):
                converted, changed = _normalize_function_call_output(item)
                normalized_tool_outputs += int(changed)
                normalized.append(converted)
            else:
                dropped.append(type_name)
            continue

        # llama-server's Responses converter requires reasoning.content to be
        # a non-empty array. Older OpenAI sessions can contain encrypted-only
        # reasoning summaries, which have no representable local equivalent.
        if item_type == "reasoning" and (
            not isinstance(item.get("summary"), list)
            or not isinstance(item.get("content"), list)
            or not item["content"]
            or not isinstance(item["content"][0], dict)
            or not isinstance(item["content"][0].get("text"), str)
        ):
            dropped.append("reasoning_without_content")
            continue

        if item_type == "function_call_output":
            item, changed = _normalize_function_call_output(item)
            normalized_tool_outputs += int(changed)

        accepted = (
            item_type in {"message", "function_call", "function_call_output", "reasoning"}
            or item.get("role") in {"user", "system", "developer"}
        )
        if item.get("role") == "assistant" and item_type in {None, "message"} and (
            isinstance(item.get("content"), (str, list))
        ):
            item = copy.deepcopy(item)
            item["type"] = "message"
            normalized.append(item)
            continue
        if not accepted:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "system", "developer"} and isinstance(content, (str, list)):
                normalized.append(item)
            else:
                dropped.append(type_name)
            continue

        normalized.append(item)

    payload["input"] = normalized
    return dropped, {
        "input_item_types": input_item_types,
        "dropped_history_item_types": dropped,
        "normalized_history_item_count": len(normalized),
        "normalized_tool_outputs": normalized_tool_outputs,
    }


def _normalize_compaction(item: dict[str, Any]) -> dict[str, Any]:
    """Convert plaintext Codex compaction data to a local user message.

    OpenAI-origin sessions may contain only encrypted_content. The local
    proxy has no key material, so forwarding or silently dropping it would
    corrupt the continuation context.
    """
    if item.get("encrypted_content") is not None:
        raise CompactionError(
            "unsupported_encrypted_compaction",
            "encrypted-only compaction cannot be decrypted by the local proxy",
        )

    value: Any = item.get("summary")
    if value is None:
        value = item.get("content")
    if value is None:
        value = item.get("replacement_history")

    if isinstance(value, str) and value.strip():
        text = value
    elif isinstance(value, list):
        parts: list[str] = []
        for entry in value:
            if isinstance(entry, str):
                parts.append(entry)
            elif isinstance(entry, dict):
                text_value = entry.get("text")
                if isinstance(text_value, str):
                    parts.append(text_value)
                elif isinstance(entry.get("content"), str):
                    parts.append(entry["content"])
        text = "\n".join(part for part in parts if part.strip())
    else:
        text = ""

    if not text.strip():
        raise CompactionError("invalid_compaction", "compaction has no plaintext summary")
    return {
        "type": "message",
        "role": "user",
        "content": "[Conversation compaction summary]\n" + text,
    }


def _fold_instruction_messages(payload: dict[str, Any]) -> int:
    """Fold system/developer history into top-level Responses instructions.

    Qwen templates can reject a system message once a conversation has begun.
    llama.cpp accepts top-level ``instructions``, but its Responses converter
    does not reliably place history system/developer items before tool and
    assistant records. Folding them keeps ``input`` template-safe.
    """
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return 0

    instruction_parts: list[str] = []
    existing_instructions = payload.get("instructions")
    if existing_instructions is not None:
        existing_text = _tool_output_as_text(existing_instructions).strip()
        if existing_text:
            instruction_parts.append(existing_text)

    conversation_items: list[Any] = []
    for item in input_items:
        if isinstance(item, dict) and item.get("role") in {"system", "developer"}:
            content = _tool_output_as_text(item.get("content")).strip()
            if content:
                instruction_parts.append(content)
        else:
            conversation_items.append(item)

    folded_count = len(input_items) - len(conversation_items)
    if not folded_count:
        return 0

    payload["input"] = conversation_items
    if instruction_parts:
        payload["instructions"] = "\n\n".join(instruction_parts)
    else:
        payload.pop("instructions", None)
    return folded_count


def _normalize_reasoning_efforts(payload: dict[str, Any]) -> int:
    """Translate Codex effort names unsupported by the Atomic chat template."""
    replacements = {
        "minimal": "low",
        "high": "medium",
        "max": "xhigh",
    }
    normalized = 0

    def normalize(container: dict[str, Any], key: str) -> None:
        nonlocal normalized
        value = container.get(key)
        if not isinstance(value, str):
            return
        replacement = replacements.get(value.lower())
        if replacement is not None and replacement != value:
            container[key] = replacement
            normalized += 1

    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        normalize(reasoning, "effort")
    normalize(payload, "reasoning_effort")
    template_kwargs = payload.get("chat_template_kwargs")
    if isinstance(template_kwargs, dict):
        normalize(template_kwargs, "reasoning_effort")
    return normalized


def rewrite_function_calls(value: Any, mappings: dict[str, ToolMapping]) -> Any:
    """Restore namespace metadata on Responses function_call objects."""
    if isinstance(value, list):
        return [rewrite_function_calls(item, mappings) for item in value]
    if not isinstance(value, dict):
        return value

    result = {key: rewrite_function_calls(item, mappings) for key, item in value.items()}
    if result.get("type") == "function_call":
        flat_name = result.get("name")
        mapping = mappings.get(flat_name)
        if mapping is not None:
            result["namespace"] = mapping.namespace
            result["name"] = mapping.name
    return result


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "CodexLlamaProxy/1.0"

    @property
    def proxy_config(self) -> ProxyConfig:
        return self.server.proxy_config  # type: ignore[attr-defined]

    @property
    def stats(self) -> StatsStore:
        return self.server.stats  # type: ignore[attr-defined]

    @property
    def client_ip(self) -> str:
        value = self.client_address[0] if self.client_address else ""
        return value or "<unknown>"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/stats":
            query = parse_qs(parsed.query)
            window = query.get("window", ["session"])[0]
            output_format = query.get("format", ["json"])[0]
            try:
                snapshot = self.stats.snapshot(window)
            except ValueError as exc:
                self._json_error(400, "invalid_stats_query", str(exc))
                return
            if output_format == "text":
                body = _render_stats_text(snapshot).encode("utf-8")
                self._send_bytes(200, "text/plain; charset=utf-8", body)
                return
            if output_format != "json":
                self._json_error(400, "invalid_stats_query", "format must be json or text")
                return
            body = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
            self._send_bytes(200, "application/json", body)
            return
        if path == "/stats/ui":
            self._send_bytes(200, "text/html; charset=utf-8", STATS_UI_HTML.encode("utf-8"))
            return
        if path == "/stats/runtime":
            self._runtime()
            return
        self._forward(
            self.path,
            None,
            None,
            method="GET",
            model=request_model_from_query(parsed.query),
            client_ip=self.client_ip,
        )

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        started = time.monotonic()
        client_ip = self.client_ip
        model = "<unknown>"
        try:
            body = self._read_body()
            try:
                payload = json.loads(body)
                model = request_model(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                payload = None
            if path in ("/v1/responses", "/responses"):
                if payload is None:
                    raise ValueError("Responses request body must be valid JSON")
                transformed, mappings, dropped, history_diagnostics = transform_request(payload)
            else:
                transformed, mappings, dropped, history_diagnostics = payload, {}, [], {}
        except CompactionError as exc:
            finished = time.monotonic()
            error_type = f"local:400:{exc.code}"
            self.stats.record(
                path=path, status=400, started=started, headers_at=finished,
                finished=finished, stream=False, response_bytes=0, usage=None,
                tool_info=None, tool_call_ids=[], error_type=error_type,
                model=model, client_ip=client_ip,
                method="POST", request_bytes=0,
            )
            self._json_error(400, exc.code, str(exc))
            return
        except (ValueError, json.JSONDecodeError) as exc:
            finished = time.monotonic()
            self.stats.record(
                path=path, status=400, started=started, headers_at=finished,
                finished=finished, stream=False, response_bytes=0, usage=None,
                tool_info=None, tool_call_ids=[], error_type="local:400:invalid_request",
                model=model, client_ip=client_ip,
                method="POST", request_bytes=0,
            )
            self._json_error(400, "invalid_request", str(exc))
            return

        if path in ("/v1/responses", "/responses") and dropped:
            LOG.warning("dropped unsupported Responses tool types: %s", ", ".join(dropped))

        outgoing = (
            json.dumps(transformed, separators=(",", ":")).encode("utf-8")
            if path in ("/v1/responses", "/responses")
            else body
        )
        self._forward(
            self.path,
            outgoing,
            mappings,
            _request_tool_stats(payload, transformed, mappings, dropped, history_diagnostics)
            if path in ("/v1/responses", "/responses") else None,
            method="POST",
            model=model,
            client_ip=client_ip,
            received_at=started,
        )

    def do_DELETE(self) -> None:
        parsed = urlsplit(self.path)
        body = self._read_body() if self.headers.get("Content-Length") else None
        model = request_model_from_query(parsed.query)
        if body:
            try:
                model = request_model(json.loads(body))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                pass
        self._forward(
            self.path,
            body,
            None,
            method="DELETE",
            model=model,
            client_ip=self.client_ip,
        )

    def _health(self) -> None:
        started = time.monotonic()
        try:
            response, connection = self._upstream("GET", "/health", None)
            body = response.read()
            status = response.status
            connection.close()
            self._send_bytes(status, response.getheader("Content-Type", "application/json"), body)
        except OSError as exc:
            finished = time.monotonic()
            self.stats.record(
                path="/health", status=503, started=started, headers_at=finished,
                finished=finished, stream=False, response_bytes=0, usage=None,
                tool_info=None, tool_call_ids=[], error_type="upstream:503:upstream_unavailable",
                client_ip=self.client_ip,
            )
            self._json_error(503, "upstream_unavailable", str(exc))

    def _runtime(self) -> None:
        try:
            response, connection = self._upstream("GET", "/props", None)
            body = response.read()
            content_type = response.getheader("Content-Type", "application/json")
            status = response.status
            connection.close()
            if status < 400:
                try:
                    payload = json.loads(body)
                    generation = payload.get("default_generation_settings", {})
                    if isinstance(generation, dict):
                        self.stats.set_context_limit(generation.get("n_ctx"))
                    if not isinstance(generation, dict) or not generation.get("n_ctx"):
                        self.stats.set_context_limit(payload.get("n_ctx"))
                    models_response, models_connection = self._upstream("GET", "/v1/models", None)
                    models_body = models_response.read()
                    models_connection.close()
                    if models_response.status < 400:
                        models_payload = json.loads(models_body)
                        models = models_payload.get("data", [])
                        if isinstance(models, list) and models and isinstance(models[0], dict):
                            payload["model_meta"] = models[0].get("meta", {})
                            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                except (UnicodeDecodeError, json.JSONDecodeError, OSError, AttributeError, TypeError):
                    pass
            self._send_bytes(status, content_type, body, response)
        except OSError as exc:
            self._json_error(503, "stack_offline", str(exc))

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > self.proxy_config.max_body_bytes:
            raise ValueError("request body is too large")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("incomplete request body")
        return body

    def _forward(
        self,
        path: str,
        body: bytes | None,
        mappings: dict[str, ToolMapping] | None,
        tool_info: dict[str, Any] | None = None,
        method: str = "POST",
        model: str | None = None,
        client_ip: str | None = None,
        received_at: float | None = None,
    ) -> None:
        started = received_at if received_at is not None else time.monotonic()
        route = urlsplit(path).path
        upstream_path = f"{self.proxy_config.upstream_prefix}{path}"
        request_bytes = len(body or b"")
        try:
            response, connection = self._upstream(method, upstream_path, body)
        except OSError as exc:
            finished = time.monotonic()
            LOG.warning(
                "upstream unavailable for %s %s:%s%s: %s",
                method,
                self.proxy_config.upstream_host,
                self.proxy_config.upstream_port,
                upstream_path,
                exc,
            )
            self.stats.record(
                path=route,
                status=502,
                started=started,
                headers_at=finished,
                finished=finished,
                stream=False,
                response_bytes=0,
                usage=None,
                tool_info=tool_info,
                tool_call_ids=[],
                error_type="upstream:502:upstream_unavailable",
                model=model,
                client_ip=client_ip or self.client_ip,
                method=method,
                request_bytes=request_bytes,
                request_type=_request_type(method, route),
                error_category="backend_timeout" if isinstance(exc, TimeoutError) else "backend_connection_error",
            )
            self._json_error(502, "upstream_unavailable", str(exc))
            return

        headers_at = time.monotonic()
        content_type = response.getheader("Content-Type", "application/json")
        is_stream = "text/event-stream" in content_type.lower()
        if is_stream:
            self._send_headers(response.status, content_type, response)
            usage, timings, response_bytes, tool_call_ids, tool_call_counts, ttft_ms, cancelled, interrupted = self._stream_sse(
                response, connection, mappings or {}, started
            )
            error_type = None
            if response.status >= 400:
                error_type = _error_type("upstream", response.status)
            elif interrupted:
                error_type = "stream:502:stream_interrupted"
            elif cancelled:
                error_type = "client:499:client_cancelled"
            self.stats.record(
                path=route,
                status=response.status,
                started=started,
                headers_at=headers_at,
                finished=time.monotonic(),
                stream=True,
                response_bytes=response_bytes,
                usage=usage,
                timings=timings,
                tool_info=tool_info,
                tool_call_ids=tool_call_ids,
                tool_call_counts=tool_call_counts,
                ttft_ms=ttft_ms,
                cancelled=cancelled,
                error_type=error_type,
                error_category=("backend_http_error" if response.status >= 400 else "stream_interrupted" if interrupted else "client_cancelled" if cancelled else None),
                model=model,
                client_ip=client_ip or self.client_ip,
                method=method,
                request_bytes=request_bytes,
                request_type=_request_type(method, route),
            )
            return

        raw = response.read()
        connection.close()
        if response.status >= 400:
            try:
                detail = raw.decode("utf-8", errors="replace")[:1000]
            except Exception:
                detail = "<undecodable response>"
            LOG.warning(
                "upstream returned HTTP %s for %s: %s",
                response.status,
                upstream_path,
                detail,
            )
        decoded: Any = None
        usage: dict[str, Any] | None = None
        timings: dict[str, Any] | None = None
        tool_call_ids: list[str] = []
        tool_call_counts: dict[str, int] = {}
        try:
            decoded = json.loads(raw)
            usage = _find_usage(decoded)
            timings = _find_timings(decoded)
            tool_call_ids = _collect_call_ids(decoded, "function_call")
            tool_call_counts = _collect_tool_call_counts(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        if mappings:
            try:
                if decoded is None:
                    decoded = json.loads(raw)
                raw = json.dumps(
                    rewrite_function_calls(decoded, mappings),
                    separators=(",", ":"),
                ).encode("utf-8")
            except (UnicodeDecodeError, json.JSONDecodeError):
                LOG.warning("upstream returned non-JSON response; passing it through")

        if route in ("/v1/models", "/models"):
            raw = self._patch_models(raw)
        self._send_bytes(response.status, content_type, raw, response)
        self.stats.record(
            path=route,
            status=response.status,
            started=started,
            headers_at=headers_at,
            finished=time.monotonic(),
            stream=False,
            response_bytes=len(raw),
            usage=usage,
            timings=timings,
            tool_info=tool_info,
            tool_call_ids=tool_call_ids,
            tool_call_counts=tool_call_counts,
            error_type=_error_type("upstream", response.status, raw) if response.status >= 400 else None,
            error_category="backend_http_error" if response.status >= 400 else None,
            model=model,
            client_ip=client_ip or self.client_ip,
            method=method,
            request_bytes=request_bytes,
            request_type=_request_type(method, route),
        )

    def _upstream(
        self,
        method: str,
        path: str,
        body: bytes | None,
    ) -> tuple[http.client.HTTPResponse, http.client.HTTPConnection]:
        connection = http.client.HTTPConnection(
            self.proxy_config.upstream_host,
            self.proxy_config.upstream_port,
            timeout=self.proxy_config.connect_timeout,
        )
        # Establish the TCP connection with the short connect timeout, then
        # allow the long read timeout for prompt evaluation and first token.
        # With a 256K context, llama-server may legitimately spend minutes
        # before it sends the response headers.
        connection.connect()
        if connection.sock is not None:
            connection.sock.settimeout(self.proxy_config.read_timeout)
        headers: dict[str, str] = {"Accept-Encoding": "identity"}
        ui_asset_request = method.upper() == "GET" and (
            path in {"/", "/index.html", "/manifest.webmanifest", "/sw.js", "/build.json"}
            or path.startswith("/_app/")
            or path.startswith("/favicon")
        )
        if ui_asset_request:
            accept_encoding = self.headers.get("Accept-Encoding")
            if accept_encoding:
                headers["Accept-Encoding"] = accept_encoding
        for key, value in self.headers.items():
            key_lower = key.lower()
            if key_lower in FORWARDED_HEADERS and key_lower not in HOP_BY_HOP:
                headers[key] = value
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response._rbufsize = 0  # type: ignore[attr-defined]
        response.fp.raw._sock.settimeout(self.proxy_config.read_timeout)  # type: ignore[union-attr]
        return response, connection

    def _stream_sse(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        mappings: dict[str, ToolMapping],
        started: float,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int, list[str], dict[str, int], float | None, bool, bool]:
        usage: dict[str, Any] | None = None
        timings: dict[str, Any] | None = None
        response_bytes = 0
        tool_call_ids: list[str] = []
        tool_call_counts: dict[str, int] = {}
        seen_call_ids: set[str] = set()
        ttft_ms: float | None = None
        cancelled = False
        interrupted = False
        completed = False
        try:
            pending = b""
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    ending = b"\n"
                    if line.endswith(b"\r"):
                        line = line[:-1]
                        ending = b"\r\n"
                    if line.startswith(b"data:"):
                        prefix, data = line[:5], line[5:].lstrip()
                        if data == b"[DONE]":
                            completed = True
                        elif data:
                            try:
                                parsed = json.loads(data)
                                usage = _find_usage(parsed) or usage
                                timings = _find_timings(parsed) or timings
                                for call_id in _collect_call_ids(parsed, "function_call"):
                                    if call_id not in seen_call_ids:
                                        seen_call_ids.add(call_id)
                                        tool_call_ids.append(call_id)
                                if ttft_ms is None and _has_generated_output(parsed):
                                    ttft_ms = max(time.monotonic() - started, 0.0) * 1000
                                event_type = parsed.get("type") if isinstance(parsed, dict) else None
                                if event_type == "response.output_item.added" or event_type is None:
                                    _merge_counts(tool_call_counts, _collect_tool_call_counts(parsed))
                                elif event_type == "response.completed" and not tool_call_counts:
                                    _merge_counts(tool_call_counts, _collect_tool_call_counts(parsed))
                                if event_type in {"response.completed", "message_stop"}:
                                    completed = True
                                if mappings:
                                    data = json.dumps(
                                        rewrite_function_calls(parsed, mappings),
                                        separators=(",", ":"),
                                    ).encode("utf-8")
                                    line = prefix + b" " + data
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                pass
                    output = line + ending
                    response_bytes += len(output)
                    self.wfile.write(output)
                    self.wfile.flush()
            if pending:
                response_bytes += len(pending)
                self.wfile.write(pending)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            cancelled = True
            LOG.info("client disconnected during streaming response")
        except OSError as exc:
            interrupted = True
            LOG.warning("backend stream interrupted: %s", exc)
        finally:
            connection.close()
        if not completed and not cancelled and not interrupted:
            interrupted = True
        return usage, timings, response_bytes, tool_call_ids, tool_call_counts, ttft_ms, cancelled, interrupted

    def _patch_models(self, raw: bytes) -> bytes:
        try:
            payload = json.loads(raw)
            backend_models = payload.get("data", [])
            backend_by_id = {
                entry["id"]: entry for entry in backend_models
                if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            } if isinstance(backend_models, list) else {}
            for collection_name in ("data", "models"):
                collection = payload.get(collection_name, [])
                if not isinstance(collection, list):
                    continue
                for model in collection:
                    if isinstance(model, dict) and "slug" not in model:
                        model["slug"] = model.get("id") or model.get("model") or model.get("name", "")
                    if isinstance(model, dict) and "display_name" not in model:
                        model["display_name"] = (
                            model.get("name")
                            or model.get("model")
                            or model.get("id")
                            or model.get("slug", "")
                        )
                    # Codex 0.154+ requires this field while refreshing its
                    # model catalog.  A llama.cpp server exposes runtime
                    # reasoning behavior through /props, but its /v1/models
                    # response does not include Codex's native metadata.
                    # This profile runs with reasoning disabled, so advertise
                    # an explicit empty set instead of making Codex fall back
                    # to unrelated hosted-model metadata.
                    if isinstance(model, dict):
                        model.setdefault("supported_reasoning_levels", [])
                        model.setdefault("default_reasoning_level", "low")
                        model.setdefault("shell_type", "unified_exec")
                        model.setdefault("supports_parallel_tool_calls", True)
                        model.setdefault("supports_reasoning_summaries", False)
                        # Preserve the live backend's image capability rather
                        # than advertising every profile as text-only to Codex.
                        model_id = model.get("id") or model.get("model") or model.get("name")
                        backend = backend_by_id.get(model_id, {}) if isinstance(model_id, str) else {}
                        architecture = model.get("architecture") or backend.get("architecture")
                        modalities = model.get("input_modalities")
                        if not isinstance(modalities, list) or not modalities:
                            modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
                        if not isinstance(modalities, list) or not modalities:
                            capabilities = model.get("capabilities")
                            has_vision = isinstance(capabilities, list) and "vision" in capabilities
                            modalities = ["text", "image"] if has_vision else ["text"]
                        # Codex consumes text/image metadata; leave the complete
                        # llama.cpp architecture (including video) untouched.
                        model["input_modalities"] = [m for m in modalities if m in ("text", "image")] or ["text"]
                        model.setdefault("visibility", "list")
                        model.setdefault("supported_in_api", True)
                        model.setdefault("priority", 0)
                        model.setdefault("truncation_policy", {"mode": "tokens", "limit": 10000})
                        model.setdefault("support_verbosity", False)
                        model.setdefault("default_verbosity", "low")
                        model.setdefault("base_instructions", "")
                        model.setdefault("experimental_supported_tools", [])
            return json.dumps(payload, separators=(",", ":")).encode("utf-8")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            return raw

    def _send_headers(
        self,
        status: int,
        content_type: str,
        response: http.client.HTTPResponse | None = None,
        content_length: int | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        if response is not None:
            for key in FORWARDED_RESPONSE_HEADERS:
                value = response.getheader(key)
                if value:
                    self.send_header(key, value)
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        if response is not None:
            # The compatibility headers above are included in the generic
            # response allowlist; avoid forwarding hop-by-hop headers.
            pass
        self.end_headers()

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes,
        response: http.client.HTTPResponse | None = None,
    ) -> None:
        self._send_headers(status, content_type, response, len(body))
        self.wfile.write(body)
        self.wfile.flush()

    def _json_error(self, status: int, code: str, message: str) -> None:
        body = json.dumps({"error": {"type": code, "message": message}}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("client disconnected while sending error response")


def build_server(
    listen_host: str,
    listen_port: int,
    config: ProxyConfig,
    stats_file: str,
    history_max: int = 1000,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((listen_host, listen_port), ProxyHandler)
    server.proxy_config = config  # type: ignore[attr-defined]
    server.stats = StatsStore(stats_file, history_max, config.context_limit)  # type: ignore[attr-defined]
    return server


def reset_stats(stats_file: str) -> None:
    state_path = Path(stats_file).expanduser().with_name(Path(stats_file).expanduser().stem + ".state.json")
    try:
        state_path.unlink()
        LOG.info("removed persistent stats state %s", state_path)
    except FileNotFoundError:
        LOG.info("persistent stats state does not exist: %s", state_path)
    except OSError as exc:
        raise SystemExit(f"could not reset stats state {state_path}: {exc}") from exc


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:8081", help="proxy listen address")
    parser.add_argument("--upstream", default=os.environ.get("STRATA_RPC_COORDINATOR_URL", "http://127.0.0.1:8080"), help="llama-server URL")
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=900.0)
    parser.add_argument("--max-body-mib", type=int, default=32)
    parser.add_argument(
        "--stats-history-max", type=int,
        default=int(os.environ.get("CODEX_PROXY_STATS_HISTORY_MAX", "1000")),
        help="maximum recent request records retained for window statistics",
    )
    parser.add_argument(
        "--context-limit", type=int,
        default=int(os.environ["CODEX_PROXY_CONTEXT_LIMIT"]) if os.environ.get("CODEX_PROXY_CONTEXT_LIMIT") else None,
        help="known llama.cpp context limit; /stats/runtime also learns it from /props",
    )
    parser.add_argument("--stats-file", default=default_stats_file(), help="JSONL request statistics file")
    parser.add_argument("--reset-stats", action="store_true", help="clear persistent totals and exit")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.reset_stats:
        reset_stats(args.stats_file)
        return 0
    listen_host, listen_port_text = args.listen.rsplit(":", 1)
    upstream = urlsplit(args.upstream)
    if upstream.scheme != "http" or not upstream.hostname:
        raise SystemExit("--upstream must be an http:// URL")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = ProxyConfig(
        upstream_host=upstream.hostname,
        upstream_port=upstream.port or 80,
        upstream_prefix=upstream.path.rstrip("/"),
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        max_body_bytes=args.max_body_mib * 1024 * 1024,
        context_limit=args.context_limit,
    )
    server = build_server(listen_host, int(listen_port_text), config, args.stats_file, args.stats_history_max)

    def stop(_signum: int, _frame: Any) -> None:
        # shutdown() must run from a thread other than serve_forever(); a
        # signal handler executes on the serving thread and would otherwise
        # wait on itself.
        threading.Thread(target=server.shutdown, name="proxy-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("listening on http://%s:%s -> %s", listen_host, listen_port_text, args.upstream)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
