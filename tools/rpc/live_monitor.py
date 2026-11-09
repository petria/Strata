#!/usr/bin/env python3
"""Opt-in, model-independent 1 Hz RPC worker telemetry. No inference socket access."""
import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SERIES = {"cpu_pct": "worker_cpu", "disk_read_mb": "worker_disk_read_mb", "ram_used": "worker_ram_used"}


class Worker:
    def __init__(self):
        from serve.telemetry import Telemetry
        self.sampler = Telemetry(extra=lambda: {"sampled_at": time.time()})

    def snapshot(self):
        s = self.sampler.snapshot()
        h, ident = s["now"], s["static"]
        gpu = {key.removeprefix("gpu_"): value for key, value in h.items() if key.startswith("gpu_")}
        gpu.update(name=ident.get("gpu_name"), sampled_at=h.get("sampled_at"),
                   cpu_pct=h.get("cpu"), cpu_name=ident.get("cpu_name"), cores=ident.get("cores"),
                   threads=ident.get("threads"))
        for key in ("ram_used", "ram_total", "disk_read_mb", "disk_write_mb"):
            gpu[key] = h.get(key)
        return {"gpu_monitor": gpu}

    def close(self):
        self.sampler.close()


class NetworkSampler:
    """Linux interface byte-counter deltas, divided by elapsed monotonic time."""
    def __init__(self, interface, root="/sys/class/net"):
        self.interface = interface
        self.path = Path(root) / interface / "statistics"
        self.previous = None

    def sample(self, now=None):
        now = time.monotonic() if now is None else now
        result = {"interface": self.interface, "down_kib_s": None, "up_kib_s": None,
                  "sampled_at": time.time()}
        try:
            rx = int((self.path / "rx_bytes").read_text())
            tx = int((self.path / "tx_bytes").read_text())
        except (OSError, ValueError):
            self.previous = None
            return result
        previous, self.previous = self.previous, (now, rx, tx)
        if previous and now > previous[0] and rx >= previous[1] and tx >= previous[2]:
            dt = now - previous[0]
            result.update(down_kib_s=(rx - previous[1]) / dt / 1024,
                          up_kib_s=(tx - previous[2]) / dt / 1024)
        return result


class Relay:
    def __init__(self, upstream, interface=None):
        self.upstream = upstream
        self.network = NetworkSampler(interface) if interface else None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.data = {}
        self.history = {series: deque(maxlen=60) for series in SERIES.values()}
        if self.network:
            self.history.update(network_down=deque(maxlen=60), network_up=deque(maxlen=60))
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                with urllib.request.urlopen(self.upstream, timeout=0.8) as response:
                    data = json.load(response)
                row = data["gpu_monitor"]
                fresh = 0 <= time.time() - float(row["sampled_at"]) < 3
                error = None
            except (OSError, ValueError, KeyError, TypeError) as exc:
                row, fresh, error = {}, False, type(exc).__name__
            network = self.network.sample() if self.network else None
            with self.lock:
                if network:
                    self.history["network_down"].append(network["down_kib_s"])
                    self.history["network_up"].append(network["up_kib_s"])
                for key, series in SERIES.items():
                    self.history[series].append(row.get(key) if fresh else None)
                self.data = {"gpu_monitor": row, "fresh": fresh, "error": error, "network": network,
                             "tick_at": time.time(), "history": {k: list(v) for k, v in self.history.items()}}
            self.stop.wait(max(0, 1 - (time.monotonic() - started)))

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)


def handler(source, origins):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            body = json.dumps(source.snapshot(), allow_nan=False).encode()
            self.send_response(200)
            origin = self.headers.get("Origin")
            if origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("worker", "relay"))
    p.add_argument("--listen", required=True)
    p.add_argument("--upstream")
    p.add_argument("--interface", help="coordinator network interface (relay only)")
    p.add_argument("--origin", action="append", default=[])
    a = p.parse_args()
    if a.mode == "relay" and not a.upstream:
        p.error("relay requires --upstream")
    source = Worker() if a.mode == "worker" else Relay(a.upstream, a.interface)
    host, port = a.listen.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), handler(source, a.origin))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        source.close()


if __name__ == "__main__":
    main()
