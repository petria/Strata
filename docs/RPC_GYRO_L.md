# Gyro-L on the Linux two-host RPC stack

This optional profile uses the v0.2.13-based fork with an RTX 5090 coordinator
and RTX 3090 prefix worker. Start with [RPC_LINUX_SETUP.md](RPC_LINUX_SETUP.md)
and export your own host settings using [RPC_ENVIRONMENT.md](RPC_ENVIRONMENT.md).
The profile is selected explicitly; the repository's default remains Swift IQ3_S.

## Model and preparation

Model card: http://huggingface.co/agentionai/Qwen3.8-Flash-Next-Gyro-GGUF

The pinned source is recorded in
[model-source.json](../profiles/rpc/gyro-l-v0213/model-source.json):

- Revision: `257c95704b87e99d12e4b1cbd65794837009d587`.
- File: `Qwen3.8-Flash-Next-Gyro-L-TQ3_0.gguf`, the full file with the PLE table.
- Bytes: `108323559808`.
- SHA-256: `5428a5646eaeaa56cc00a892f92e7546aeadb255233503969ad25cf48c7f359b`.

On each host, put this verified file in its model root under `models/gyro-l/`.
Keep adequate free disk space for the model and pack on both hosts. The model
card lists 50.21 GiB of active weights; the larger download also contains the
PLE table. The pack references experts in the original GGUF, so keep that file.

Prepare the pack once and copy it to the other host. Set `MODEL_DATA_ROOT` to
the data directory of the host doing the packing. If the tools cannot discover
llama.cpp's Python helpers, explicitly export `STRATA_GGUF_PY` pointing to an
existing `llama.cpp/gguf-py` directory. Use the Python environment with the
repository's requirements installed:

```sh
python tools/iq_pack.py \
  --gguf "$MODEL_DATA_ROOT/models/gyro-l/Qwen3.8-Flash-Next-Gyro-L-TQ3_0.gguf" \
  --out "$MODEL_DATA_ROOT/packs/gyro-l" --compat-bf16
```

Do not add `--experts-bin`: this profile reads the experts from the GGUF.
The compatibility conversion rounds some small projections to the engine's
supported forms; expert and PLE table bytes remain unchanged. The pack must
contain `hadamard.txt`, the v5 `native_experts.txt`, and `tokenizer/`.

Reuse the compatible Qwen Flash-Next MTP runtime at `mtp/rt/` and original
projector at `models/mmproj-Qwen3.8-Flash-Next-BF16.gguf` on the coordinator.
The projector runs on the CPU. These are additional assets, not downloaded by
the profile itself.

## Launch settings

```sh
export STRATA_RPC_PROFILE_NAME=gyro-l-v0213
tools/rpc/deploy_custom_rpc_stack.sh "$STRATA_RPC_REMOTE" prepare
# Once replacement is authorized:
tools/rpc/deploy_custom_rpc_stack.sh "$STRATA_RPC_REMOTE" activate
tools/rpc/deploy_live_monitor.sh "$STRATA_RPC_REMOTE"
```

Use distinct build directories when preparing alongside a running deployment.
The build helper enables the CUDA trellis MMQ overlay, including Gyro-L's
pair-trellis types 147–149. It builds the same committed source for both GPUs.

| Setting | Value |
| --- | --- |
| API model ID | `qwen3.8-flash-next-gyro-l-tq3_0-v0.2.13-rpc` |
| Context / KV | 262,144 / INT8 |
| Worker split / CPU pool | First 24 of 48 layers / 12 workers |
| Prefill | 2,048 |
| Speculation | MTP, 4 draft tokens, minimum probability 0.5 |
| Expert cache | Auto; resident RAM complement enabled |
| VRAM reserve | Worker 4,096 MiB; coordinator 1,000 MiB |
| Conversation cache | 8,192 MiB; 4 parked slots |
| Reasoning / vision | Low default / CPU projector |
| Sampling | Temperature 1; top-p 0.95; top-k 20; min-p 0.05 |
| Transport | Mutual-TLS TCP RPC |

The configured reserves leave approximately 50.87 GiB combined VRAM on the
tested cards, before runtime buffers, KV and MTP. Combined capacity is not a
shared allocation pool: the 24/24 split exceeds the worker's 20 GiB weight
budget. Its uncached experts therefore use CPU/RAM fallback.

## Validation on the reference pair

Checks completed on 2026-11-11:

- Full-file SHA-256 verification on both hosts.
- Matched SM86/SM120 builds, 58 RPC configuration tests, and CPU TQP checks
  on both CPUs (AVX2 on the worker; AVX2 and AVX-512 on the coordinator).
- Pack-load checks on both hosts: zero failures.
- A separate actual-weight CUDA dequantization check: 12 cases per GPU from
  layers 0, 23, 24 and 47, all matching the scalar format reference exactly.
  This checks decoding sampled weights, not every expert's complete output.
- Loaded models and successful short inference at 65,536, 131,072 and 262,144
  configured context. These were loading/smoke tests, not full-length prompts.
- Structured Responses API function call and tool-result round trip through
  the custom proxy, plus a successful image-input smoke test.

No coding-quality benchmark or sustained throughput comparison was run as part
of activation. Keep raw request logs and deployment-specific settings outside Git.
