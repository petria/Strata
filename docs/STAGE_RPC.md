# Experimental CUDA stage RPC for Strata 2.0

This is an engine-only port of the experimental two-host CUDA layer-stage
transport to Strata 2.0. It is opt-in, not upstream RPC support, and is not
compatible with llama.cpp's RPC protocol. The normal single-host engine path
remains the default. This port intentionally does not change Strata's HTTP
server, Python package, telemetry service, or web UI.

## Roles and execution model

The prefix worker owns layers `[0,K)`; the coordinator owns `[K,48)`, the
output head, MTP, sampling, and model front-end. `K` is a multiple of four
from 4 through 44. Both machines must have the same model artifacts and
verified model/profile identity; model weights are not fetched over RPC.
Linux CUDA is the supported backend for this experimental implementation.

Prefill sends token IDs and optional image-embedding rows to the prefix
worker. It returns FP32 residual rows. Decode transfers one speculative
window's IDs and the existing stage handoff; the coordinator verifies MTP,
chooses accepted tokens, and samples. The stages execute serially for decode;
this is layer partitioning, not tensor parallelism. Worker checkpoints are
opaque handles paired with coordinator checkpoints. Worker cache eviction
forces a safe full prefill rather than reuse of stale state. Conversation
cache parking/activation is supported by the engine path; disk session
save/restore remains unsupported with RPC v1.

TLS 1.3 with a private CA and role certificates is required. The worker binds
to its configured private-link address and admits the configured coordinator.
Messages have bounded sizes and finite transfer/handshake timeouts. A broken
or mismatched session is terminal: there is no automatic reconnect, local
fallback, or replay of a GPU mutation.

## Build

Build both machines from the same source revision and build configuration;
use CUDA architecture `86` for the RTX 3090 and `120` for the RTX 5090.
OpenSSL 3 development files are required.

```bash
cmake -S . -B build-rpc -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_ENABLE_CUDA=ON -DSTRATA_ENABLE_STAGE_RPC=ON \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.3/bin/nvcc \
  -DCMAKE_CUDA_ARCHITECTURES=86 -DSTRATA_BUILD_TESTS=OFF
cmake --build build-rpc --target strata -j12
```

The CMake option is OFF by default. The RPC handshake includes a source
fingerprint, model/profile identity, layer geometry, context/KV settings,
split, chunk, verification depth, vision, and RoPE settings. Binary hashes
will differ across GPU architectures; compare source fingerprints instead.

## Current v1 restrictions

Stage RPC rejects local multi-GPU/helper modes, batching, streamed or elastic
KV, disk sessions, and control vectors. It does not provide llama.cpp RPC,
Metal/HIP/SYCL support, distributed experts, multiple workers, or concurrent
sequence throughput. The coordinator remains the inference API endpoint;
this port does not add or modify an API/UI integration.

## Validation

```bash
python3 tests/rpc/test_transport.py
python3 -m unittest tests.rpc.test_speed
python3 -m unittest tests.rpc.test_cache_ab_replay
```

`test_transport.py` exercises the TLS framing and failure rules. The speed
and cache replay tests validate deterministic policy/replay behavior without
requiring a model. Actual two-host inference additionally requires matching
CUDA builds, model artifacts, TLS credentials, and an explicitly configured
worker/coordinator launch. Deployment commands and profile details remain in
the separate `llama-rpc` launcher repository documentation.
