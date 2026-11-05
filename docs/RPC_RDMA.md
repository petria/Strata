# Stage RPC over TCP or native RDMA

Stage RPC has three runtime modes. These are opt-in settings for the experimental
layer-stage path; normal local inference does not open an RPC listener.

| Mode | Selection | Data path |
| --- | --- | --- |
| Local inference | Omit `--rpc-stage` and `--rpc-listen` | No network stage session. |
| TCP, the RPC default | Select a stage role; omit `--rpc-transport` or use `tcp` | Mutual TLS protects both authentication and payloads. |
| Plain RDMA on an approved trusted link | Select a stage role and `--rpc-transport rdma` | Mutual TLS authenticates setup; native RoCE SEND/RECV carries unencrypted payloads. |

The runtime transport selector accepts only `tcp` and `rdma`. There is no encrypted
RDMA mode and no automatic fallback from RDMA to TCP. Both peers must select the
same transport. Use plain RDMA only where the operator has approved plaintext
payloads on the private link. This integration test was explicitly approved for
that link; it does not change any live inference service.

## Build and runtime settings

### Custom 3090 + 5090 stack

The custom Swift IQ3_S profile is stored in
[`profiles/rpc/swift-iq3s-v0210`](../profiles/rpc/swift-iq3s-v0210). Both hosts
use the same tracked worker/coordinator launcher in
[`tools/rpc/strata_stage_rpc.py`](../tools/rpc/strata_stage_rpc.py). The worker
runs from this checkout. The 5090 uses a generated runtime bundle made from this
checkout; the bundle has no Git metadata. Keep model packs, model weights and TLS
private keys in their existing external data/state directories.

The profile templates use `STRATA_ROOT`, `MODEL_DATA_ROOT`, `TLS_DIR`, and
`STATE_DIR` to resolve host paths. The worker service template is in
[`packaging/rpc/systemd`](../packaging/rpc/systemd). To install the worker unit,
copy its file to `~/.config/systemd/user/`, run `systemctl --user daemon-reload`,
then restart `strata-swift-iq3s-rpc-worker.service`. For the coordinator, prepare
a runtime bundle under `~/.local/share/strata-rpc/current`, install its unit on
the 5090, and keep the matching Python environment under
`~/.local/share/strata-rpc/python-env`. The active Swift IQ3_S profile uses a
CPU vision reader and the committed `data/expert-profile.bin` file.

From a clean, committed checkout, run
`tools/rpc/deploy_custom_rpc_stack.sh [ssh-host]` to archive the current commit,
build the coordinator engine and CPU vision reader on the 5090, install the
coordinator unit, switch the runtime symlink, restart both services, and wait for
the coordinator health check. Model files and TLS credentials are not copied.

Do not remove an older runtime checkout until its engine binary and the Python
server bundle have been copied to the runtime directory and the new service has
passed the health and RPC readiness checks.

Build stage RPC with `-DSTRATA_ENABLE_STAGE_RPC=ON`. Native RDMA additionally needs
`-DSTRATA_ENABLE_RPC_RDMA=ON`, Linux RDMA headers and libraries (`libibverbs` and
`librdmacm`), and OpenSSL. An RDMA request fails when the backend was not built.
TCP does not need the RDMA build option.

The worker uses `--rpc-listen IP:50056 --rpc-allow-peer PEER_IP`; the coordinator
uses `--rpc-stage WORKER_IP:50056`. Port 50056 here is an example private mTLS
bootstrap endpoint, not a second transport selector. Both roles still need the
normal stage model, prefix, native preset and role credential settings:
`--rpc-ca`, `--rpc-cert`, `--rpc-key`, `--rpc-model-id`, and `--rpc-timeout`.
The worker identity is `strata-stage-worker`; the coordinator identity is
`strata-stage-coordinator`. Keep private keys on their owning host.

For RDMA, append these settings to **both** roles:

```sh
--rpc-transport rdma --rpc-rdma-port 50058 --rpc-rdma-interface roce0
```

The RDMA data port defaults to 50058. Supply matching data ports on both peers.
The backend uses `roce0` when no interface is supplied. The interface option is a
constraint on native route selection, not an alternative TCP bind address.
RDMA port/interface overrides belong to RDMA mode.

The peers negotiate transport through TLS ALPN. Inside the authenticated TLS
connection they exchange a versioned RDMA proposal and a fresh 32-byte session
cookie. The cookie binds the data and credit QPs to that authenticated session;
it is not payload encryption. The data listener is made ready before the cookie
is sent to the client. TLS remains open for session liveness. A failure invalidates
the session; there is no transparent retry of stage mutations.

## Link and memory requirements

Both peers need an active IPv4 RoCE Ethernet interface, an RDMA-capable device,
and a working route to each other. The bootstrap connection's local IP must belong
to the selected active interface. The device must have an IPv4-mapped GID belonging
to that interface, and the CM-selected source GID must match its IPv4 address.
The backend validates these constraints rather than choosing a GID index by hand.

Useful read-only checks on each host:

```sh
ip -4 addr show roce0
ip route get PEER_IP
rdma link show
ibv_devinfo
ls /sys/class/infiniband/*/ports/*/gid_attrs/ndevs/
```

Check the matching `gids/`, `gid_attrs/types/`, and `gid_attrs/ndevs/` entries under
that port if CM route validation fails. Network MTU and RoCE configuration must
work end to end. The test harness does not reconfigure interfaces, GIDs, routing,
MTU, firewalls or services.

Stage 1 uses **host staging, not GPUDirect RDMA**. GPU data first reaches host
memory through the existing stage path. Native transfers copy through a bounded
registered host pool (4,198,400 bytes per connection in the tested implementation),
with 1 MiB data records and separate credit flow control. No CUDA device memory is
registered by this backend. Connection counters and client metrics report the
selected backend, encryption state, registered bytes, device, interface, records,
completions and waits. These transport tests do not establish model correctness,
GPU throughput, or end-to-end inference performance.

An authenticated worker may stay idle longer than the operation timeout. After
data starts arriving, normal bounded transfer deadlines apply. Client replies also
have finite deadlines. The integration harness adds outer process timeouts so a
broken idle test cannot leave its fixture running forever.

## Reproduce the no-GPU integration tests

From the repository root:

```sh
python3 tests/rpc/test_transport.py
python3 tools/rpc/test_rdma_connection.py --large-payloads
```

The harness compiles only `transport_fixture.cpp`, `transport.cpp`, `client.cpp`,
and `rdma_transport.cpp`, with `-DSTRATA_HAS_RPC_RDMA=1`, linking `-lssl -lcrypto
-libverbs -lrdmacm`. It loads no model or GPU runtime. It copies a temporary binary
to the remote host and removes its temporary files afterward.

Default test topology:

| Setting | Value |
| --- | --- |
| Local fixture server / allowed remote client | `192.0.2.20` / `192.0.2.10` |
| mTLS bootstrap / RDMA data | `50166` / `50168` |
| Interface | `roce0` |
| Remote SSH target | `operator@192.0.2.10` (hostname `coordinator.example`) |
| Existing SSH host key identity | `192.0.2.10` |
| TLS directory, both hosts | `/home/operator/.local/share/strata-stage-rpc/tls` |
| Per-operation timeout / idle interval | 5 seconds / 7 seconds |
| Outer client timeout | 35 seconds, with a shorter remote `timeout` wrapper |
| Persistent report | `/home/operator/AI-tools/bench/strata-rdma-20261026/transport-tests.json` |

The alias `coordinator.example` did not resolve in the validation environment. The
explicit peer IP uses the already known SSH host key for that same host. Override
`--remote`, `--host-key-alias`, IPs, ports, TLS directory and report path as needed;
see `--help`. An empty `--host-key-alias ''` uses the target's own known-host entry.
The native-only fixture owned by the other test task uses port 50068; this harness
does not touch it. Run only one copy of this harness per selected port pair.

Cases cover a 32 MiB + 31 byte roundtrip, a 31-byte roundtrip, a seven-second idle
interval, a TCP baseline, wrong client and server role certificates, both transport
mismatch directions, a nonexistent interface, and a mismatched RDMA data port.
Slow TLS tests drip a valid proposal or session cookie in individual writes that
each arrive inside the configured timeout, but whose total duration exceeds it.
The peer must reject the exchange near the absolute five-second bootstrap
deadline while the test keeps TLS open. These exercise the proposal read and
cookie read; they do not directly force the server's tiny cookie write to block.
`--close-repetitions` (default 20) repeats immediate server exit after the final
reply, alternating 31-byte and 32 MiB + 31-byte final payloads. In this mode the
fixture echoes the full payload on the final commit as well as the earlier call;
there is no added sleep or acknowledgment before server close.
The wrong server role can be rejected by certificate-purpose validation before
identity matching. Both are authentication failures. The harness creates a fresh
CA-signed wrong-client-role certificate in temporary files without altering the
existing credentials or CA serial file.

`--large-payloads` also tests a request of 512 MiB minus 16 bytes, whose status and
error-string envelope make the reply exactly the 512 MiB frame maximum, and a
512 MiB + 1 request rejected by the sender. These correctness cases use a separate
30-second operation timeout (`--large-payload-timeout`); normal, idle and slow-TLS
cases retain the configured five-second timeout. Every case records its operation
timeout and wall time, with an outer process deadline. An earlier 512 MiB case
exceeded five seconds during concurrent GPU builds; its failed report is retained
separately. Use `--only-case rdma-max-reply --large-payloads
--large-payload-timeout 5 --output PATH` for an isolated strict-deadline diagnosis.
Large cases require several GiB of available host memory. They do not test a malicious oversized RDMA wire header;
the existing TCP suite separately tests rejection of malformed and oversized
received headers before payload allocation.

The JSON report records compilation commands, source hashes, return codes, errors,
durations and both peers' transport metrics. A negative case passes only when both
fixtures fail and a relevant diagnostic appears; timeouts and SSH failures do not
count as expected rejection. Only processes started by this harness are killed.
No live GPU stack is stopped or restarted.

## Validation limits

Read the persistent report for the latest run; the presence of test code is not
proof a case passed. This suite exercises the actual `Connection` and `Client`
implementation on the two hosts. It does not run the production CLI, CMake GPU
build, stage worker/model handshake, GPU inference, concurrent callers, raw RDMA
protocol attacks, or production service deployment. Those gates remain separate.

## Live inference verification after benchmarking

`tools/rpc/verify_rdma_inference.py` is a separate verifier for an already running
endpoint. Run it **after** the parent's first RDMA replay arm, on instruction from
the parent: its requests warm and evict caches, so running it earlier would change
the benchmark's starting conditions. It never starts or stops an inference service.

```sh
python3 tools/rpc/verify_rdma_inference.py \
  --base-url http://192.0.2.10:8080 \
  --output /home/operator/AI-tools/bench/strata-rdma-20261026/inference-verification.json
```

It accepts either the server root or a URL ending in `/v1`. It discovers the model
from `/v1/models` unless `--model` is supplied. It reads an optional API key from
`STRATA_API_KEY` (override the environment variable's name with `--api-key-env`);
it does not write the key into the report.

The default cases are Responses text, a deterministic red 64x64 PNG data URL,
disconnect during observed partial prefill followed by a recovery request, and ten
turns interleaving five independent conversation histories. The cache check needs
an enabled cache with one to four parked slots to force eviction; it does not
change cache settings. It records local park/restore/eviction counts and worker
park/activation counts and checks that retained worker conversations remain
bounded. A missing observation is a failure, not a claimed pass. HTTP does not
expose individual worker handle release identities or independently attest image
row placement on a GPU; those remain explicit limits in its report.

Session-marker response mismatches are recorded separately as model-quality
findings. The verifier continues the cache exercise and reports cache-engine
checks independently; a model failing to repeat a marker alone is not proof of
cache corruption. `model_quality_passed` is separate from the API/cache-engine
`passed` result. Cumulative systemd cgroup CPU counters are sampled before and
after each case for the managed RDMA worker and coordinator. Override the unit
names and coordinator SSH target with `--cpu-worker-unit`,
`--cpu-coordinator-unit`, and `--cpu-coordinator-ssh`. Unavailable CPU counters
are reported as unavailable rather than zero.

Use `--cases text,image`, `--cancel-words`, `--cache-words`, and `--timeout` to
adjust an explicit run. Cancellation requires observing `0 < prompt_read <
prompt_total` while the request is still reading, then a server disconnect-cancel
counter increment and successful recovery. A prompt that finishes before this
observation does not prove cancellation during prefill. Run on an otherwise idle
endpoint. The verifier was prepared without sending any live inference requests;
its execution and results belong to the later authorized validation step.
