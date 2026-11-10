# RPC Stack card

The Monitor shows this card when RPC worker GPU telemetry is present. It lists
GPU0 and CPU0 for the coordinator, and GPU1 and CPU1 for the worker. It uses the
same subtitle style as the other Monitor cards.

The coordinator CPU name comes from normal server telemetry. The worker GPU
row's `cpu_name`, if provided, takes priority over deployment metadata.
A worker's own readings, including its CPU model, ride the RPC stats reply that
already carries its GPU temperature and PCIe link, so a current worker needs no
extra step. A worker built before those host readings reports nothing; generate
the optional CPU identity module on that worker instead:

```sh
python3 tools/rpc/write_monitor_identity.py \
  --coordinator-host $STRATA_RPC_REMOTE --output /tmp/rpc-stack-identity.js
```

Copy the output to `serve/web/rpc-stack-identity.js` in the coordinator checkout.
The generated file is ignored by Git and contains only CPU model names read from
the hosts' `/proc/cpuinfo`. Regenerate it if either host changes. Refresh the page
after replacing it. No engine rebuild or restart is needed.

Without the module or native CPU metadata, the UI shows an unknown CPU label.
The module is requested only when the RPC card is visible. Keep this UI commit
separate from engine changes when rebasing onto upstream releases; this optional
static module uses the existing web asset route and adds no server API.

## CPU and Disk read cards

The same reply carries the worker's CPU percent, disk read and write, RAM and CPU
model. When a worker is present, the Monitor's **CPU** and **Disk read** cards split
into two halves: this machine on the left, the worker on the right, the way the
Speed card splits Decode and Prefill. Each half keeps its own number and its own
curve, so the two hosts stay readable side by side. The left half's sentence names
the role (`Coordinator`) when a second half is showing.

The worker's readings arrive with a request's start and end lines, not once a
second, and the worker measures them once a second on its own machine. An idle
stack therefore shows an older reading, and the card says how old it is.

The worker's numbers are its own: nothing averages the two hosts' CPU percent,
because a mean of two machines answers nothing. A run with a single GPU keeps both
cards as they were.
