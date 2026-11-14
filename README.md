# Strata RPC for two Linux NVIDIA hosts

This fork adds an optional network layer split to [Niko1221/Strata](http://github.com/Niko1221/Strata).
It lets a Linux **RTX 5090 coordinator** run inference together with a Linux
**RTX 3090 worker** on a second machine. The coordinator exposes the chat web UI
and API; the worker runs the model's prefix layers. This is an experimental fork,
based on **Strata v0.2.13 / engine 0.1.59**.

**Start with the [Linux setup guide](docs/RPC_LINUX_SETUP.md).**
Agents should read [AGENTS.md](AGENTS.md) before changing or deploying the stack.
This fork's deployment instructions cover Linux CUDA hosts only.

The fork's default branch is `rpc-v0.2.13`, which contains the RPC implementation
and these instructions. `main` is kept as a clean mirror of upstream. Upstream
release installers do not contain this fork's network RPC additions; build both
nodes from the same committed fork revision.

## What the fork adds

- One coordinator and one remote prefix worker, with model/settings/source identity checks.
- TCP RPC with mutual TLS, plus an optional native RDMA transport on a configured RoCE link.
- RPC conversation-cache parking and reuse, MTP on the coordinator, and remote hardware telemetry.
- A Monitor page that shows both nodes' GPUs, CPUs, memory, expert caches and history graphs.
- An optional Codex Responses compatibility proxy, with request statistics and tool-call recovery.
- Environment-configured deployment, profile activation and telemetry services for Linux user accounts.

Decode stages execute in sequence. This is a layer split, not tensor parallelism
or distributed expert routing. The two nodes need compatible model artifacts;
RPC does not serve model weight files to the worker. See the
[engine design and restrictions](docs/STAGE_RPC.md).

## Monitor preview

![Strata RPC Monitor showing an RTX 5090 coordinator and RTX 3090 worker, per-node hardware readings, request speeds and conversation cache](docs/media/strata-monitor-rpc.png)

The screenshot illustrates the UI changes on our two-node setup. Its displayed
speeds and memory readings are a snapshot, not a controlled benchmark or a promise
of performance on another installation.

## Reference profile

The default `swift-iq3s-v0212` profile keeps its historical name. It can be used
with the v0.2.13-based engine; the suffix is not the running release number.

| Setting | Reference configuration |
| --- | --- |
| Model | Swift Qwen3.8 Flash-Next GSQ-RCO IQ3_S |
| Coordinator | Linux, RTX 5090; output head, MTP, sampling and HTTP server |
| Worker | Linux, RTX 3090; first 24 of 48 layers, 12 CPU pool workers |
| Context / KV | 262,144 tokens / INT8 |
| Prefill / speculation | 2,048 / MTP window 4 |
| Conversation cache | 8 GiB, 4 parked slots |
| VRAM reserve | Worker 4,096 MiB; coordinator 1,000 MiB |
| Reasoning / vision | Low default / CPU vision reader on coordinator |
| Transport | TCP with mutual TLS by default |

Different models, free VRAM and RAM can require different profile settings.
The Q8 profile has separate mounted native-data requirements and a shard manifest;
it is not a drop-in replacement for the Swift data directory.

The optional [Gyro-L profile](docs/RPC_GYRO_L.md) uses AgentionAI's
`Qwen3.8-Flash-Next-Gyro-L-TQ3_0.gguf`. Its two-host loading and inference checks
passed at 64K, 128K and 256K context; this does not establish coding quality or
full-context performance.

## Setup and operation

1. Prepare Linux dependencies, SSH, model artifacts and TLS role credentials using
   the [setup guide](docs/RPC_LINUX_SETUP.md).
2. Export your own settings outside Git. The complete variable reference is in
   [RPC_ENVIRONMENT.md](docs/RPC_ENVIRONMENT.md); no `.env` file is loaded.
3. On the worker, from a clean committed checkout:

   ```sh
   # Check rendering before building. This writes private preview files only.
   python3 tools/rpc/stack_manage.py render --output "$HOME/.local/state/rpc-preview"
   # Build the same source revision for both GPUs, without starting inference.
   tools/rpc/deploy_custom_rpc_stack.sh "$STRATA_RPC_REMOTE" prepare
   # Start the prepared worker, coordinator and proxy.
   tools/rpc/deploy_custom_rpc_stack.sh "$STRATA_RPC_REMOTE" activate
   # Install the independent Monitor telemetry services.
   tools/rpc/deploy_live_monitor.sh "$STRATA_RPC_REMOTE"
   ```

4. Open `$STRATA_RPC_COORDINATOR_URL/` for chat and Monitor. Use
   `$STRATA_RPC_PROXY_URL/v1` for the custom agent proxy, or
   `$STRATA_RPC_COORDINATOR_URL/v1` for the direct Strata API.

`prepare` builds binaries and prepares the remote runtime; `activate` replaces the
managed inference services. For an existing prepared profile, use
`tools/rpc/activate_rpc_profile.sh swift-iq3s-v0212 --check` for a read-only asset
preflight. Do not run activation against an in-use stack without authorization.

## Further documentation

- [Linux setup from a fresh host pair](docs/RPC_LINUX_SETUP.md)
- [Environment variables, generated units and local settings](docs/RPC_ENVIRONMENT.md)
- [Stage RPC architecture and current restrictions](docs/STAGE_RPC.md)
- [TCP and native RDMA behavior](docs/RPC_RDMA.md)
- [Independent RPC Monitor telemetry](docs/RPC_LIVE_MONITOR.md)
- [Conversation cache](docs/RPC_CONVERSATION_CACHE.md)
- [Optional DFlash2 extension](docs/RPC_DFLASH2.md)
- [Maintaining the fork and rebasing onto upstream releases](docs/UPSTREAM_REBASE.md)
- [Contribution rules](CONTRIBUTING.md) and [MIT license](LICENSE)

Strata's engine, server and original UI are the work of the upstream project and
its contributors. For the original project's installation routes, model catalogue
and releases, see the [upstream README](http://github.com/Niko1221/Strata#readme).
