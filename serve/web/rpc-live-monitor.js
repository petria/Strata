// Optional telemetry sidecar; no control or inference calls. The deployment supplies its URL.
export function mergeLiveWorker(metrics, sample, now = Date.now() / 1000) {
  const hw = metrics.hardware;
  if (!hw || !sample) return;
  if (sample.network) {
    const network = {...sample.network};
    if (now - network.sampled_at >= 3 || now < network.sampled_at) {
      network.down_kib_s = network.up_kib_s = null;
    }
    hw.network = network;
  }
  const rows = hw.gpus || [];
  const i = rows.findIndex((g) => g.source === "rpc-worker");
  if (i < 0) return;
  const live = sample.gpu_monitor || {};
  const fresh = sample.fresh && now - live.sampled_at >= 0 && now - live.sampled_at < 3;
  const row = {...rows[i], ...live, source: "rpc-worker", remote: true, stale: !fresh,
    sample_age_s: live.sampled_at ? now - live.sampled_at : null};
  if (!fresh) for (const k of ["util", "temp", "power", "pcie_rx_mb", "pcie_tx_mb", "cpu_pct", "ram_used", "disk_read_mb", "disk_write_mb"]) row[k] = null;
  rows[i] = row;
  metrics.history = {...metrics.history, ...sample.history};
  for (const [key, field, aggregate] of [
    ["gpu_util", "util", "mean"], ["gpu_temp", "temp", "max"],
    ["gpu_mem_used", "mem_used", "sum"], ["gpu_mem_total", "mem_total", "sum"],
    ["gpu_power", "power", "sum"], ["gpu_power_limit", "power_limit", "sum"],
    ["gpu_pcie_rx_mb", "pcie_rx_mb", "sum"], ["gpu_pcie_tx_mb", "pcie_tx_mb", "sum"]]) {
    const values = rows.map(g => g[field]).filter(v => typeof v === "number");
    const sum = values.reduce((a, b) => a + b, 0);
    hw[key] = !values.length ? null : aggregate === "max" ? Math.max(...values) : aggregate === "mean" ? sum / values.length : sum;
  }
}

export function startLiveWorker(url) {
  let sample = null;
  async function poll() {
    try {
      const response = await fetch(url, {cache: "no-store", signal: AbortSignal.timeout(1500)});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      sample = await response.json();
    } catch { /* Keep the timestamp: stale readings become gaps, never fabricated fresh values. */ }
    setTimeout(poll, 1000);
  }
  poll();
  return metrics => mergeLiveWorker(metrics, sample);
}
