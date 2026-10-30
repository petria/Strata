"""Project a worker's RPC hardware snapshot into the web Monitor's GPU list."""

import time


def add_remote_gpu(hardware: dict, engine_info: dict, enabled: bool, now: float | None = None,
                   hardware_static: dict | None = None) -> dict:
    """Return a Monitor hardware view with the RPC worker shown as an additional GPU.

    Local telemetry and device placement are untouched. The worker's snapshot is already carried by RPCMETRICS;
    its own timestamp is kept so the UI can identify an old sample.
    """
    if not enabled:
        return hardware
    rpc = engine_info.get("rpc_stage") if isinstance(engine_info, dict) else None
    worker = rpc.get("worker") if isinstance(rpc, dict) else None
    snapshot = worker.get("hardware") if isinstance(worker, dict) else None
    remote = snapshot.get("gpu_monitor") if isinstance(snapshot, dict) else None
    if not isinstance(remote, dict):
        return hardware

    now = time.time() if now is None else now
    result = dict(hardware)
    local_rows = list(result.get("gpus") or [])
    if not local_rows:
        static = hardware_static or {}
        local_rows = [{
            "index": 0,
            "name": static.get("gpu_name") or "Local GPU",
            "util": result.get("gpu_util"),
            "mem_used": result.get("gpu_mem_used"),
            "mem_total": result.get("gpu_mem_total") or ((static.get("gpu_vram_total_mib") or 0) * 1048576 or None),
            "temp": result.get("gpu_temp"),
            "power": result.get("gpu_power"),
            "power_limit": result.get("gpu_power_limit"),
            "pcie_gen": result.get("gpu_pcie_gen"),
            "pcie_gen_max": result.get("gpu_pcie_gen_max"),
            "pcie_width": result.get("gpu_pcie_width"),
            "pcie_rx_mb": result.get("gpu_pcie_rx_mb"),
            "pcie_tx_mb": result.get("gpu_pcie_tx_mb"),
        }]

    local_rows = [dict(row, remote=False, source="local") for row in local_rows]
    remote_index = max((int(row.get("index", i)) for i, row in enumerate(local_rows)), default=-1) + 1
    worker_sample_at = remote.get("sampled_at")
    try:
        age = max(0.0, now - float(worker_sample_at))
    except (TypeError, ValueError):
        age = None
    row = {key: remote.get(key) for key in (
        "name", "util", "mem_used", "mem_total", "temp", "power", "power_limit",
        "pcie_gen", "pcie_gen_max", "pcie_width", "pcie_rx_mb", "pcie_tx_mb",
    )}
    row.update(index=remote_index, remote=True, source="rpc-worker", sampled_at=worker_sample_at,
               sample_age_s=round(age, 1) if age is not None else None, stale=age is None or age > 10)
    result["gpus"] = local_rows + [row]

    def values(key):
        return [item[key] for item in result["gpus"] if isinstance(item.get(key), (int, float))]

    for key in ("gpu_mem_used", "gpu_mem_total", "gpu_power", "gpu_power_limit", "gpu_pcie_rx_mb", "gpu_pcie_tx_mb"):
        v = values("mem_used" if key == "gpu_mem_used" else
                   "mem_total" if key == "gpu_mem_total" else key.removeprefix("gpu_"))
        if v:
            result[key] = sum(v)
    v = values("util")
    if v:
        result["gpu_util"] = sum(v) / len(v)
    v = values("temp")
    if v:
        result["gpu_temp"] = max(v)
    return result
