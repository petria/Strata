# Set up the Linux two-host RPC stack

This guide describes the fork's RTX 3090 worker + RTX 5090 coordinator setup.
Commands use generic examples. Supply your own hostnames, directories and network
addresses through the [environment configuration](RPC_ENVIRONMENT.md). It does
not require a particular Linux username. Run the controller from the worker;
connect to the coordinator through its SSH alias.

## 1. Prepare both Linux hosts

Use Linux x86-64 with a working NVIDIA driver and CUDA toolkit on each host.
The reference build uses CUDA 13.3, SM86 on the 3090, and SM120 on the 5090.
Choose a toolkit supported by each installed driver and host compiler. Driver
installation and reboots are separate administrative tasks, not performed by
these deployment helpers.

On Debian/Ubuntu, install the build and Python prerequisites on both hosts:

```sh
sudo apt-get update
sudo apt-get install -y git build-essential cmake ninja-build pkg-config \
  libssl-dev libibverbs-dev librdmacm-dev openssl \
  python3 python3-venv python3-pip rsync curl
nvidia-smi
/usr/local/cuda/bin/nvcc --version
cmake --version
systemctl --user status
```

Set the per-host `*_CUDA_ROOT` or `*_CUDA_COMPILER` variables if the toolkit is
elsewhere. The helper builds with RDMA support even when using TCP, so the RDMA
development packages are required. Native RDMA additionally needs configured
hardware, interfaces and routes; it is not necessary for the initial TCP setup.

The source requires Python 3.10 or newer; `requirements.txt` selects compatible
package pins by Python version. CMake must satisfy this checkout's minimum version.
If your distribution's build tools are too old, use `requirements-build.txt` in
an activated build environment and make those tools available to the coordinator's
noninteractive SSH session too. Source setup can also select a suitable Python
runtime; inspect its output before choosing `*_PYTHON` overrides.

Configure SSH keys and host-key verification for the coordinator alias. Check
this before deployment, substituting your alias:

```sh
ssh -o BatchMode=yes coordinator.example 'hostname; systemctl --user status'
```

The helpers do not configure networking or firewalls. Permit the configured API,
proxy and telemetry ports only to intended clients. Permit the worker's inference
and telemetry ports from the coordinator over the selected private link. The
worker IP allowlist and role certificates must match that coordinator.
For unattended user services after logout/reboot, the administrator can enable
lingering on each host: `sudo loginctl enable-linger "$USER"`.

## 2. Clone and prepare the selected model

On the worker, clone the RPC branch into a location you own:

```sh
git clone --branch rpc-v0.2.13 http://github.com/petria/Strata.git
cd Strata
```

Prepare model assets on each host using a source checkout of the same fork.
For a fresh Swift IQ3_S installation, run this on each node, choosing that node's
local data directory:

```sh
./setup.sh --family swift --model IQ3_S --data-dir /srv/strata-data \
  --context 262144 --kv int8 --mtp on --vision cpu --no-start --yes
```

This command downloads/prepares large model artifacts and a standalone engine;
`--no-start` prevents starting inference. The standalone engine is not the RPC
build. The deployment step later compiles the matched custom engines. If model
files are already prepared, reuse them after checking their layout and identity
instead of repeating downloads. Check disk, RAM and VRAM before preparing data;
file sizes and free memory, rather than swap reservation alone, determine whether
the selected model/context can run.

For `swift-iq3s-v0212`, each `*_MODEL_ROOT` must contain:

```text
models/swift-IQ3_S/Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf
models/swift-IQ3_S/Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00002-of-00002.gguf
packs/swift-iq3_s/                 # includes native_experts.txt and tokenizer/
mtp/rt/                          # coordinator MTP runtime files
models/mmproj-Swift-Qwen3.8-Flash-Next-BF16.gguf  # coordinator CPU vision
```

The coordinator needs a compatible `strata-vision` executable: set
`STRATA_RPC_VISION_EXE` to its existing absolute path on that host, or set
`STRATA_RPC_VISION_LLAMA_DIR` to the llama.cpp source directory used by the source
setup so deployment builds the CPU reader. Setup prints where its components and
model files were installed. Do not assume the RPC engine build fetched a vision
dependency into its CMake build directory.

Use identical model revisions and compatible packs on both nodes. Copy prepared
assets with your usual file-transfer tools or provision a suitable mount on each
host. RPC transfers inference state, not model weight files. The Q8 profile's
`*_NATIVE_ROOT` override and per-shard verification manifest need their own
preparation; the Swift command above does not prepare Q8.

## 3. Provision mutual TLS credentials

Generate a private CA and two different role certificates in a secure
administrative directory, with restrictive permissions. The following example
uses OpenSSL 3; keep the CA signing key in that directory, outside inference
runtime directories and outside Git:

```sh
umask 077
TLS_ADMIN_DIR=$(mktemp -d)
openssl req -x509 -newkey rsa:3072 -nodes -days 3650 \
  -keyout "$TLS_ADMIN_DIR/ca.key" -out "$TLS_ADMIN_DIR/ca.crt" \
  -subj /CN=strata-rpc-ca \
  -addext basicConstraints=critical,CA:TRUE \
  -addext keyUsage=critical,keyCertSign,cRLSign

for role in strata-stage-worker strata-stage-coordinator; do
  case "$role" in
    strata-stage-worker) purpose=serverAuth ;;
    strata-stage-coordinator) purpose=clientAuth ;;
  esac
  openssl req -new -newkey rsa:3072 -nodes \
    -keyout "$TLS_ADMIN_DIR/$role.key" -out "$TLS_ADMIN_DIR/$role.csr" \
    -subj "/CN=$role"
  printf 'subjectAltName=DNS:%s\nextendedKeyUsage=%s\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n' \
    "$role" "$purpose" > "$TLS_ADMIN_DIR/$role.ext"
  openssl x509 -req -in "$TLS_ADMIN_DIR/$role.csr" \
    -CA "$TLS_ADMIN_DIR/ca.crt" -CAkey "$TLS_ADMIN_DIR/ca.key" \
    -CAcreateserial -days 365 -extfile "$TLS_ADMIN_DIR/$role.ext" \
    -out "$TLS_ADMIN_DIR/$role.crt"
done
```

Provision only these files through a trusted transfer method:

| Host directory | Files |
| --- | --- |
| Worker `STRATA_RPC_WORKER_TLS_DIR` | `ca.crt`, `strata-stage-worker.crt`, `strata-stage-worker.key` |
| Coordinator `STRATA_RPC_COORDINATOR_TLS_DIR` | `ca.crt`, `strata-stage-coordinator.crt`, `strata-stage-coordinator.key` |

Use mode 700 for the directories and 600 for private keys. The directory defaults
are each host's own `$HOME/.local/share/strata-stage-rpc/tls`. Keep `ca.key` off
both inference runtimes. Retain it securely for renewal; role certificates in
this example expire after one year. The DNS role identities must remain exactly
`strata-stage-worker` and `strata-stage-coordinator` because the transport checks
them, independently of host IP addresses.

## 4. Export the private configuration

Use the complete exports and variable reference in [RPC_ENVIRONMENT.md](RPC_ENVIRONMENT.md).
Store real values in your local shell configuration outside the checkout. A file
of shell `export` statements can be sourced explicitly; no helper loads a `.env`
file. The renderer will place each host's required environment in its generated
systemd unit, so SSH does not need to forward the caller's environment.

In addition to the required endpoints and data roots, set a vision source and
verify the CUDA/build settings. The defaults compile SM86 for the worker and
SM120 for the coordinator. For the reference profile, keep the supplied model,
context, KV, split and speculation settings aligned between roles. Do not
increase one peer's context or split independently.

Render a preview on the worker before deploying:

```sh
python3 tools/rpc/stack_manage.py render --output "$HOME/.local/state/rpc-preview"
```

Inspect the two profile JSON files and service files there. They contain private
machine settings and must stay outside Git. The service files committed under
`packaging/rpc/systemd` are templates, not installable units.

## 5. Build the matched pair, then activate it

Run from a clean, committed worker checkout. Building requires enough RAM and
disk on both hosts; it does not start inference:

```sh
tools/rpc/deploy_custom_rpc_stack.sh "$STRATA_RPC_REMOTE" prepare
```

Preparation compiles the worker locally and archives that exact commit into a
coordinator release directory. It compiles the SM120 engine there, prepares the
vision reader/Python runtime, and validates model paths on both hosts. The first
prepare must succeed before `activate`; a fresh checkout's `--check` can otherwise
fail because its RPC executable does not exist yet.

Activation replaces managed services and can interrupt active clients. Once
that action is authorized:

```sh
tools/rpc/deploy_custom_rpc_stack.sh "$STRATA_RPC_REMOTE" activate
tools/rpc/deploy_live_monitor.sh "$STRATA_RPC_REMOTE"
```

The helper starts the worker listener first, then the coordinator model, then the
proxy. It saves old units privately and attempts rollback if startup fails.
Telemetry sidecars are independent services; they keep collecting readings while
inference is idle. Their browser origins and endpoints come from your exports.

## 6. Verify and connect clients

On the worker:

```sh
systemctl --user status strata-swift-iq3s-rpc-worker.service
curl --fail "$STRATA_RPC_COORDINATOR_URL/health"
curl --fail "$STRATA_RPC_PROXY_URL/v1/models"
```

On the coordinator, inspect `strata-swift-iq3s-rpc-coordinator.service` and
`codex-llama-proxy.service` with `systemctl --user status`. The health response must
report the selected loaded model and expected context. A health response verifies
loading, not answer quality; send a small explicit chat/tool request when you are
ready to validate inference.

Open `$STRATA_RPC_COORDINATOR_URL/` for the chat and Monitor UI. Point Codex or
another OpenAI-compatible agent to `$STRATA_RPC_PROXY_URL/v1`; direct Strata
clients can use `$STRATA_RPC_COORDINATOR_URL/v1`. This repository includes the
proxy, not the separate `dual_codex_wsl.sh` client launcher. Configure any existing
launcher with the selected proxy URL/model/context according to its own docs.

The shipped reference profile has `api_key: none`. Use LAN access deliberately:
private-link TLS protects node-to-node RPC, not the separate public-facing HTTP
API. Apply your intended API authentication/access policy before exposing it
beyond trusted clients.

For diagnosis, read generated units and the private state directories configured
by `*_STATE_ROOT`; engine and coordinator logs are there, and the proxy counter
file is selected with `PROXY_STATS`. Do not publish raw prompt logs or generated
configuration. Use the [RDMA guide](RPC_RDMA.md) only after the TCP deployment is
working; RDMA is optional and is never selected automatically.
