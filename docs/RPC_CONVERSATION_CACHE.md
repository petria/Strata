# Stage-RPC conversation parking A/B

This experimental dual-host implementation parks each conversation in two host-RAM snapshots: the coordinator keeps its suffix stage, and the worker keeps its prefix stage under an opaque handle. The coordinator owns eviction. A worker cache miss falls back to full prefill after both stages reset; a partial restore or transport failure is fatal rather than allowing divergent states.

The existing `--conversation-cache-mib`, `--conversation-cache-slots`, and `--conversation-cache-min-free-mib` options now work with stage RPC. The same MiB budget and slot count must be passed to **both** roles. The managed launcher does this with `STRATA_RPC_CACHE_MIB` (default `0`) and `STRATA_RPC_CACHE_SLOTS` (default `4`). It keeps the engine's 2,560 MiB free-memory floor. Snapshots are runtime-only; disk session save/load remains unsupported in stage RPC.

For the controlled test, use the managed launcher with the original GSQ-IQ3_S settings (`STRATA_RPC_CONTEXT=262144`, `STRATA_RPC_PREFIX_LAYERS=16`, `STRATA_RPC_CHUNK=2048`, `STRATA_RPC_WORKER_VRAM_RESERVE_MIB=4096`) and set `STRATA_RPC_CACHE_MIB` to `0`, then `8192`, then `0` across separate starts. Keep `STRATA_RPC_PROFILE=1` on both services. Do not run other inference clients during an arm.

`tools/rpc/cache_ab_replay.py fixture PATH` generates a static 12-request coding-agent fixture. Reuse **the same file** for each arm; `run --url $STRATA_RPC_COORDINATOR_URL --fixture PATH --output ARM.json --expected-cache-mib N` records each request and its server metrics. It refuses a busy server and unexpected cache setting. Compare `prompt_read`, `reused`, `prompt_ms`, zero-reuse request count, and RPC worker parks/activations/misses. A nonzero cache budget alone is not evidence of benefit: confirm that the worker actually parks and activates and that full-prefill requests fall.

For stage-balance profiling, `RPCMETRICS` includes cumulative `coordinator_prefill` counters from the coordinator's local CUDA stage. Each replay record also stores `stage_delta`: coordinator prompt-path time/tokens, worker `prefill`/checkpoint buckets, and transport bytes/timing. Difference these snapshots per request rather than comparing process-lifetime totals. Worker and transport profile durations use `host_elapsed_overlapping`; do not sum them as wall time or treat socket elapsed time as pure wire latency. Generate a longer agent-like fixture with `fixture PATH --max-tokens 64`; retain the 16-token default for the original cache A/B comparison so decode work does not dominate.

An exclusive split/chunk sweep uses the same fixture and settings, restarting both hosts between arms:

```bash
for split in 12 16 20; do
  STRATA_RPC_CACHE_MIB=8192 STRATA_RPC_CACHE_SLOTS=4 STRATA_RPC_CONTEXT=262144 \
    STRATA_RPC_PREFIX_LAYERS="$split" STRATA_RPC_CHUNK=2048 \
    scripts/strata_stage_rpc_profile.sh start gsq-iq3-s
  python3 tools/rpc/cache_ab_replay.py run --url $STRATA_RPC_COORDINATOR_URL \
    --fixture /path/to/fixed-fixture.json --output "split-${split}.json" --expected-cache-mib 8192
  scripts/strata_stage_rpc_profile.sh stop gsq-iq3-s
done
```

Then test chunks 1024/2048/4096 at the best split, changing one variable at a time. Wait for startup to finish and verify `/health` plus the loaded context before each replay. Preserve the deployed settings after the sweep; do not send other inference requests during an arm.

The RPC source fingerprint changes when source changes; rebuild both hosts from matching source before restarting. The worker accepts one authenticated coordinator and cannot reconnect or replay a failed mutation. Do not use this test on a shared serving stack.
