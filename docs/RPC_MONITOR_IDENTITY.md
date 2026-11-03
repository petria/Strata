# RPC Stack card

The Monitor shows this card when RPC worker GPU telemetry is present. It lists
GPU0 and CPU0 for the coordinator, and GPU1 and CPU1 for the worker. It uses the
same subtitle style as the other Monitor cards.

The coordinator CPU name comes from normal server telemetry. The worker GPU
row's `cpu_name`, if provided, takes priority over deployment metadata.
Current workers do not report that field, so generate the optional CPU identity
module on the Linux worker:

```sh
python3 tools/rpc/write_monitor_identity.py \
  --coordinator-host coordinator.example --output /tmp/rpc-stack-identity.js
```

Copy the output to `serve/web/rpc-stack-identity.js` in the coordinator checkout.
The generated file is ignored by Git and contains only CPU model names read from
the hosts' `/proc/cpuinfo`. Regenerate it if either host changes. Refresh the page
after replacing it. No engine rebuild or restart is needed.

Without the module or native CPU metadata, the UI shows an unknown CPU label.
The module is requested only when the RPC card is visible. Keep this UI commit
separate from engine changes when rebasing onto upstream releases; this optional
static module uses the existing web asset route and adds no server API.
