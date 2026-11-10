# RPC deployment environment

The optional RPC deployment tools read exported environment variables. They do not
load `.env` files, run configuration as shell code, or assume both nodes have the
same home, model paths, or SSH environment. Keep real values in your own shell
configuration and systemd overrides outside the repository. Do not commit rendered
units, profiles, endpoint configuration or logs.

Run the helpers from any directory. The worker is the local host and the
coordinator is reached through the explicit SSH alias. The profile names select
model settings, not machine paths. When building a matched pair, `WORKER_ROOT`
must name the checkout running the helper so the archive and worker sources match. `STRATA_RPC_PROFILE_NAME` defaults to
`swift-iq3s-v0212`; `unsloth-q8-v0212` selects the Q8 profile.

## Required settings

All names below have the `STRATA_RPC_` prefix:

| Suffix | Meaning |
| --- | --- |
| `REMOTE` | Coordinator SSH alias or user@host; no password |
| `COORDINATOR_HOME` | Absolute home directory on the coordinator |
| `WORKER_MODEL_ROOT`, `COORDINATOR_MODEL_ROOT` | Local model-data roots on each node |
| `WORKER_BIND` | Worker inference HOST:PORT, shared by both roles |
| `WORKER_PEER` | Allowed coordinator transport IP or hostname |
| `COORDINATOR_URL`, `PROXY_URL` | Reachable base URLs, without `/v1` |
| `WORKER_MONITOR_BIND`, `MONITOR_BIND` | Worker and coordinator telemetry HOST:PORT |
| `MONITOR_URL` | Browser-reachable telemetry URL including `/metrics` |
| `INTERFACE` | Coordinator network interface sampled by the monitor |

Example exports below use documentation addresses; replace them with your own:

```sh
export STRATA_RPC_REMOTE=coordinator.example
export STRATA_RPC_COORDINATOR_HOME=/home/operator
export STRATA_RPC_WORKER_MODEL_ROOT=/srv/models/worker
export STRATA_RPC_COORDINATOR_MODEL_ROOT=/srv/models/coordinator
export STRATA_RPC_WORKER_BIND=192.0.2.20:50056
export STRATA_RPC_WORKER_PEER=192.0.2.10
export STRATA_RPC_COORDINATOR_BIND=0.0.0.0:8080
export STRATA_RPC_COORDINATOR_URL=http://192.0.2.10:8080
export STRATA_RPC_PROXY_BIND=192.0.2.10:8081
export STRATA_RPC_PROXY_URL=http://192.0.2.10:8081
export STRATA_RPC_WORKER_MONITOR_BIND=192.0.2.20:50057
export STRATA_RPC_MONITOR_BIND=192.0.2.10:8086
export STRATA_RPC_MONITOR_URL=http://192.0.2.10:8086/metrics
export STRATA_RPC_INTERFACE=eth1
```

## Optional settings and defaults

| Suffix | Default / purpose |
| --- | --- |
| `WORKER_ROOT` | Checkout discovered from the helper's location |
| `REMOTE_BASE` | Coordinator home + `.local/share/strata-rpc` |
| `COORDINATOR_ROOT` | Remote base + `/current`; runtime symlink managed by deployment |
| `WORKER_NATIVE_ROOT`, `COORDINATOR_NATIVE_ROOT` | Corresponding model root; override when dense/native shards are mounted elsewhere, including Q8 |
| `WORKER_TLS_DIR`, `COORDINATOR_TLS_DIR` | Corresponding home + `.local/share/strata-stage-rpc/tls` |
| `WORKER_STATE_ROOT`, `COORDINATOR_STATE_ROOT` | Corresponding home + `.local/state/strata-stage-rpc`; profile name is appended |
| `WORKER_UNIT_DIR`, `COORDINATOR_UNIT_DIR` | Corresponding home + `.config/systemd/user` |
| `WORKER_BUILD_DIR`, `COORDINATOR_BUILD_DIR` | Relative directories `build-rpc-worker`, `build-rpc-coordinator`; absolute paths and `..` are rejected |
| `WORKER_PYTHON` | Interpreter running the helper |
| `COORDINATOR_PYTHON` | Remote base + `/python-env/bin/python` |
| `WORKER_CUDA_ROOT`, `COORDINATOR_CUDA_ROOT` | `/usr/local/cuda` |
| `WORKER_CUDA_COMPILER`, `COORDINATOR_CUDA_COMPILER` | Corresponding CUDA root + `/bin/nvcc` |
| `WORKER_CUDA_ARCH`, `COORDINATOR_CUDA_ARCH` | `86`, `120`; set for other GPUs |
| `COORDINATOR_BIND`, `PROXY_BIND` | `127.0.0.1:8080`, `127.0.0.1:8081`; explicitly select LAN access |
| `MONITOR_ORIGINS` | JSON array of browser origins; defaults to coordinator and proxy URLs. Each becomes a separate `--origin` argument |
| `BUILD_JOBS` | `6` |
| `VISION_EXE` | Existing compatible vision-reader executable on the coordinator, copied into the new release before switching it |
| `VISION_LLAMA_DIR` | Alternative to `VISION_EXE`: explicit llama.cpp source directory for building the CPU vision reader |

Deployment requires one of `VISION_EXE` or `VISION_LLAMA_DIR`. It does not assume
that the engine's CMake build fetched a llama.cpp checkout. TLS private keys stay
on their owning host. Standard OS paths and model-relative filenames are portable
constants, not personal deployment paths.

The renderer supplies the profile variables `STRATA_ROOT`, `MODEL_DATA_ROOT`,
`NATIVE_DATA_ROOT`, `TLS_DIR`, `STATE_DIR`, `RPC_ENGINE`, `RPC_STAGE_ADDRESS`,
`RPC_ALLOWED_PEER`, `RPC_CUDA_BIN` and `RPC_CUDA_LIB`. Generated service units carry
these as `Environment=` directives; no shell startup file or automatic SSH
environment forwarding is required to run a service.

## Render, check and deploy

```sh
# Render privately; no SSH or service operations.
python3 tools/rpc/stack_manage.py render --output "$HOME/.local/state/rpc-preview"
# Validate profile assets on both hosts; no files or services are changed.
tools/rpc/activate_rpc_profile.sh swift-iq3s-v0212 --check
# Build a committed source revision without restarting inference.
tools/rpc/deploy_custom_rpc_stack.sh "$STRATA_RPC_REMOTE" prepare
# Activate the prepared pair, with saved units and rollback on a startup error.
tools/rpc/deploy_custom_rpc_stack.sh "$STRATA_RPC_REMOTE" activate
# Or build and activate in one operation.
tools/rpc/deploy_custom_rpc_stack.sh "$STRATA_RPC_REMOTE" deploy
# Switch models using already-built engine binaries.
tools/rpc/activate_rpc_profile.sh unsloth-q8-v0212 --activate
# Install/update independent telemetry; restarts the telemetry sidecars only.
tools/rpc/deploy_live_monitor.sh "$STRATA_RPC_REMOTE"
```

The local controller sends validated settings and rendered files as JSON on SSH
stdin. It does not put them into shell assignments. Service argv values are quoted
and escaped for systemd, including whitespace, dollar signs and percent signs.
Generated profiles are stored under each host's private state directory. Installed
units are backed up before replacement. A failed activation restores those units
and the previous coordinator runtime symlink.

## Standalone RDMA and inference tools

`test_rdma_connection.py` accepts flags or `RPC_TEST_REMOTE`,
`RPC_TEST_HOST_KEY_ALIAS` (optional), `RPC_TEST_LOCAL_IP`, `RPC_TEST_PEER_IP`,
`RPC_TEST_INTERFACE`, `TLS_DIR`, and `RPC_TEST_OUTPUT` (default `transport-tests.json`
in the working directory). Required settings are checked before allocating test
resources. Ports remain overrideable with `--port` and `--rdma-port`.

`verify_rdma_inference.py` requires explicit `--base-url` and `--output`. Its
optional coordinator CPU snapshot uses `--cpu-coordinator-ssh` or
`STRATA_RPC_REMOTE`; without either, that snapshot is skipped. The proxy's
standalone `--upstream` defaults to `STRATA_RPC_COORDINATOR_URL`, or loopback when
that variable is unset. The identity writer requires `--coordinator-host` and
`--output` explicitly.
