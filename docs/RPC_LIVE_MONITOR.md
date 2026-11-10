# Independent RPC worker Monitor readings

The optional live Monitor uses separate services. The worker samples hardware once per second with
Strata's existing `serve.telemetry.Telemetry` sampler. The coordinator relay collects those readings
once per second over the dedicated 10.* link. Neither service opens the inference RPC connection.
The relay keeps 60 samples, including null gaps for failed or stale samples.

Enable or update the services from the 5900x host as petria:

```sh
tools/rpc/deploy_live_monitor.sh "$STRATA_RPC_REMOTE"
```

This installs persistent user units and updates static web files without restarting inference:

- Worker: `strata-live-monitor-worker.service`, bound to `$STRATA_RPC_WORKER_MONITOR_BIND`.
- Relay: `strata-live-monitor-relay.service`, bound to `$STRATA_RPC_MONITOR_BIND`.
- Relay reads `http://${STRATA_RPC_WORKER_MONITOR_BIND}/metrics`; the route exposes hardware only.
- The relay permits browser reads from the configured coordinator UI origins on ports 8080/8081.

The deployment generates `serve/web/rpc-live-config.js` in the coordinator's runtime bundle.
The optional `serve/web/rpc-live-monitor.js` module polls its URL and replaces only the RPC worker
hardware row and history. The relay also samples coordinator interface `roce0` from Linux byte
counters using monotonic elapsed time, exposing raw receive/transmit rates in KiB/s and 60-point network histories. The Network card
displays these as Read/Write in MB/s (binary conversion, matching the Disk read card).
These counters include all traffic on that interface, not just inference. Change `--interface` in the
relay unit to monitor another interface. Single-host runs and runs without the config retain the existing path.
Metrics JSON from the original Strata `/metrics` endpoint still has request-boundary worker data;
the independent data is available at `$STRATA_RPC_MONITOR_URL` and merged in the browser.
A page with selected dashboard text intentionally pauses display updates to preserve copying.

Reload existing browser tabs after deployment. `tools/rpc/deploy_custom_rpc_stack.sh` reinstalls the
sidecars and generated config on subsequent bundle deployments if the worker unit is enabled.
To disable this feature, disable both sidecar units and remove the generated config, then reload.

Validation: `python3 -m unittest discover -s tools/rpc -p test_live_monitor.py` checks that the relay's
history ticks without inference requests and records gaps when its source stops providing fresh data.
