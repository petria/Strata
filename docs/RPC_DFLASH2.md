# DFlash2 with stage RPC

This optional extension keeps DFlash2 on the coordinator. The worker runs only
its normal prefix layers and returns their DFlash2 hidden-state samples alongside
the existing handoff. It works through the selected TCP or RDMA transport.
Normal inference and MTP do not capture or transfer these samples.

On both roles, pass:

```sh
--rpc-dflash2-id PACK_SHA256 --rpc-dflash2-taps 5,15,25,35,45
```

Use the actual SHA-256 of the prepared drafter pack. The coordinator also needs
upstream's explicit `--draft dflash2`, `--draft-model PACK`,
`--draft-fallback none`, and a draft vocabulary. The worker must not load the
drafter or MTP. Both roles need the same speculative window size (at most eight)
and the usual matching model, context, KV, split, cache and TLS settings.
The coordinator checks that the declared tap layers match its loaded drafter.
The handshake includes the drafter identity, mode, tap layout and wire revision;
different settings are refused rather than silently running another drafter.

Samples are FP32, packed in position/row/layer/embedding order with explicit
geometry and a version. The receiver validates them against the operation that
requested them. Prompt requests specify the first position needed by the
drafter's 2,048-row context window. Earlier chunks return no sample rows.
Verification returns only the prefix stage's samples; the coordinator combines
them with its suffix stage before updating the drafter. Only committed rows
reach DFlash2's ring. Upstream's ring log restores reused and parked lineages;
prefetched prompt samples discarded by cancellation are never applied.

This remains layer-stage RPC, not the upstream local all-cards split engine.
Use `split_engine: off` in the server config and choose the drafter explicitly
while comparing releases. RPC lookup windows remain bounded by the eight-row
stage handoff, including when v0.2.10 would enable longer local lookup windows.

CPU-only wire checks:

```sh
python3 -m unittest tests.rpc.test_dflash2_taps
```

Real inference, cancellation, cache reuse and performance must additionally be
validated with matched builds and model files on both hosts. CPU wire tests
alone are not a correctness or speed claim.
