# Optional CUDA verifier doorbell publication

`STRATA_CUDA_DOORBELL_MODE=increment|store` selects the publisher when verifier
window graphs are captured. Unset means `increment` (the existing behavior).
Invalid values fail verifier initialization. Restart the engine to change mode.
The startup log records the selected mode. This option does not change HIP's
existing `STRATA_DOORBELL_STORE` option, RPC messages, or inference settings.

The store mode writes the known `(layer - first_owned_layer) * groups + group + 1`
step directly, with system fences around publication. The full-data path reuses
`doorbell_publish_value`. The residency-aware path uses a matching explicit-value
variant which still skips activation copying when all routed experts are resident
and still does not publish routing weights. The increment kernels are unchanged.

This is an experiment prompted by a captured 5090 verifier stall: the CPU last saw
sequence 11 but observed zero while waiting for 12 at layer 35. The trace does not
prove why that value appeared. A direct store avoids the GPU's mapped-host counter
read/modify/write; a successful short run alone is not proof of a fix.

## GPU fixture

```sh
/usr/local/cuda-13.3/bin/nvcc -std=c++20 -arch=sm_86 -Iinclude \
  tests/rpc/cuda_doorbell_fixture.cu src/kernels/cuda/elementwise.cu \
  -o /tmp/cuda-doorbell-fixture
timeout 240s /tmp/cuda-doorbell-fixture 10000
```

Use sm_120 on the 5090. Both modes, full-data/resident/missing-expert paths, and
one/two groups are checked with exact sequence values, payloads, canaries, CPU
acknowledgments and window resets. This synthetic test does not reproduce the
model's router/expert workload or establish long-run reliability.

## Controlled pilot

Build the same sources on both hosts and verify matching RPC source fingerprints.
Keep worker mode `increment` in both arms. Switch only the coordinator mode. Keep
all diagnostics and model/profile settings fixed, restart both stages before each
arm, and replay the same saved request bodies in order outside scored results.
Stop each arm at its first failure or after admitting requests for ten minutes;
an in-flight request is allowed to finish within its normal timeout.

Capture before restarting after a failure. Compare successful requests, generated
tokens, verifier windows and failure breadcrumbs, not just elapsed time. Two
failure-free arms are inconclusive; a baseline-only failure is evidence for a
longer confirmation run, not a reason to declare the issue fixed. Restore the
original configuration and diagnostics after the pilot. No permanent default
change is made by this experiment.
