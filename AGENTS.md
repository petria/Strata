# Agent instructions for this Linux RPC fork

This branch is the experimental two-host CUDA RPC fork of Strata v0.2.13
(engine 0.1.59). The supported deployment here is **two Linux NVIDIA hosts**:
a local prefix worker and a coordinator reached with SSH. The reference hardware
is RTX 3090 + RTX 5090. Use [docs/RPC_LINUX_SETUP.md](docs/RPC_LINUX_SETUP.md) for
setup and [docs/RPC_ENVIRONMENT.md](docs/RPC_ENVIRONMENT.md) for configuration.

## Before changing a deployment

- Inspect the checkout, branch, worktree, installed user services, running binaries
  and `/health`. Treat saved endpoints and model names as potentially stale.
- The machine invoking the helpers is the worker. Access it locally. Use
  `STRATA_RPC_REMOTE` for the coordinator; never assume equal home directories,
  model mounts, usernames or environment variables on the two hosts.
- Do not stop, restart, replace, install or build over a live stack just to inspect
  it. Obtain authorization for deployment if the current task has not already
  authorized replacement. Do not reboot hosts as a side effect of setup.
- Keep machine settings in exported environment variables and local systemd
  overrides outside Git. The helpers do not load `.env` files. Never commit
  rendered profiles/units, private addresses or paths, credentials, or prompt logs.

## Setup workflow

1. Read the Linux setup guide completely. Check drivers, the CUDA compiler,
   build/Python dependencies, SSH and user systemd on both nodes. Do not infer the
   installed CUDA toolkit from `nvidia-smi`'s driver-capability banner.
2. Prepare the exact selected model, pack/tokenizer, MTP and optional vision
   artifacts. `setup.sh --no-start` can prepare source-setup assets; it does not
   build this fork's two-node RPC deployment. Do not silently select another model.
3. Provision a common CA certificate and separate worker/coordinator role
   certificates. Keep the CA signing key off both inference runtimes.
4. Export the environment settings, then render preview files outside the checkout.
   Read the resulting profiles and units. Model/profile names do not infer host paths.
5. Use `deploy_custom_rpc_stack.sh "$STRATA_RPC_REMOTE" prepare` to compile the
   same committed source revision for both GPUs and preflight the prepared pair.
   The new build must be prepared before its first activation. Templates in
   `packaging/rpc/systemd` must be rendered; do not install them verbatim.
6. When deployment is authorized, activate the prepared pair, then install the
   telemetry sidecars. Verify worker/coordinator/proxy service state, loaded model,
   context, listeners and recent logs. Send an inference request only when it is
   within the requested validation scope; a health check alone is not generation proof.

## Code and publication

- Keep stage RPC optional (`STRATA_ENABLE_STAGE_RPC=OFF` by default). Native RDMA
  additionally needs `STRATA_ENABLE_RPC_RDMA=ON`. Single-host inference stays available.
- Both peers must agree on source fingerprint, model identity, split, context, KV,
  speculation and transport. TCP RPC is mutual TLS; native RDMA authenticates setup
  with TLS and carries plaintext data on the configured trusted link. Neither mode
  is compatible with llama.cpp RPC. Current v1 supports one prefix worker.
- Read [CONTRIBUTING.md](CONTRIBUTING.md) before upstream issues or PRs. Agree large
  feature scope first; keep new features opt-in, changes focused and validation honest.
- For configuration changes, run `python3 -m unittest discover -s tools/rpc -p
  'test_*.py'`, `bash -n tools/rpc/*.sh`, `git diff --check`, and verify rendered units
  with systemd on their intended hosts. Test missing settings before side effects.
- For engine changes, run relevant engine tests/builds and report hardware execution
  separately. Do not claim CUDA, RDMA, image input or benchmark success from a mock
  or static inspection. Label checks passed, failed and not run accurately.
- `origin` is the public fork; `upstream` is the original project. Keep fork `main`
  as an upstream mirror. Publish named custom branches, never private backup refs or
  `git push --all`. The fork landing branch is `rpc-v0.2.13`.
- Rebase custom work onto an intentional upstream release with recovery copies
  outside Git. Audit the current tree and custom history for personal configuration
  before publication; use explicit force-with-lease expectations for a rewritten branch.
- Maintain README, this file and the Linux/environment guides with setup changes.
  Preserve upstream attribution and the license.
