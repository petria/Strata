#!/usr/bin/env bash
set -euo pipefail

REMOTE=${1:-coordinator.example}
ROOT=$(git rev-parse --show-toplevel)
if [[ -n "$(git -C "$ROOT" status --porcelain)" ]]; then
  echo "Commit or remove local changes before deployment." >&2
  exit 2
fi
REV=$(git -C "$ROOT" rev-parse HEAD)
REMOTE_BASE=/home/operator/.local/share/strata-rpc
RELEASE="$REMOTE_BASE/releases/$REV"

git -C "$ROOT" archive --format=tar "$REV" | \
  ssh "$REMOTE" "mkdir -p '$RELEASE' && tar -xf - -C '$RELEASE'"

ssh "$REMOTE" bash -s -- "$RELEASE" "$REMOTE_BASE" <<'REMOTE_SCRIPT'
set -euo pipefail
release=$1
base=$2
build="$release/build-v0210-sm120-rdma"
vision_build="$release/build-vision"

cmake -S "$release" -B "$build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.3/bin/nvcc \
  -DCMAKE_CUDA_ARCHITECTURES=120 \
  -DSTRATA_ENABLE_CUDA=ON \
  -DSTRATA_ENABLE_STAGE_RPC=ON \
  -DSTRATA_ENABLE_RPC_RDMA=ON \
  -DSTRATA_MMQ_KQUANTS=ON \
  -DSTRATA_MMQ_TQ_OVERLAY=ON \
  -DSTRATA_NATIVE_EXPERTS=ON \
  -DSTRATA_Q6K_EXPERTS=ON \
  -DSTRATA_PORTABLE=OFF \
  -DSTRATA_PLE_FIXTURE_DIR="$release/bench/micro"
cmake --build "$build" --target strata --parallel 12

cmake -S "$release/tools/vision" -B "$vision_build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLAMA_DIR="$build/_deps/strata_llamacpp-src" \
  -DSTRATA_VISION_CUDA=OFF \
  -DSTRATA_PORTABLE=OFF
cmake --build "$vision_build" --target strata-vision --parallel 12
install -D -m 755 "$vision_build/bin/strata-vision" "$release/engine/strata-vision"
test -x "$build/strata"
test -f "$release/data/expert-profile.bin"

if [[ ! -x "$base/python-env/bin/python" ]]; then
  python3 -m venv "$base/python-env"
  "$base/python-env/bin/python" -m pip install -r "$release/requirements.txt"
fi

mkdir -p "$HOME/.config/systemd/user" "$base/backups"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
unit="$HOME/.config/systemd/user/strata-swift-iq3s-rpc-coordinator.service"
if [[ -f "$unit" ]]; then cp -a "$unit" "$base/backups/coordinator.service.$stamp"; fi
cp "$release/packaging/rpc/systemd/strata-swift-iq3s-rpc-coordinator.service" "$unit"
ln -sfn "$release" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
systemctl --user daemon-reload
REMOTE_SCRIPT

install -D -m 644 "$ROOT/packaging/rpc/systemd/strata-swift-iq3s-rpc-worker.service" \
  "$HOME/.config/systemd/user/strata-swift-iq3s-rpc-worker.service"
systemctl --user daemon-reload

ssh "$REMOTE" systemctl --user stop strata-swift-iq3s-rpc-coordinator.service
systemctl --user restart strata-swift-iq3s-rpc-worker.service
ssh "$REMOTE" systemctl --user restart strata-swift-iq3s-rpc-coordinator.service

python3 - <<'PY'
import json
import time
import urllib.request

deadline = time.monotonic() + 180
last_error = None
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen("http://192.0.2.10:8080/health", timeout=5) as response:
            health = json.load(response)
        if health.get("status") == "ok" and health.get("loaded"):
            print("Coordinator health:", json.dumps(health, sort_keys=True))
            break
        last_error = f"not ready: {health}"
    except Exception as exc:
        last_error = str(exc)
    time.sleep(3)
else:
    raise SystemExit(f"Coordinator health did not recover: {last_error}")
PY

ssh "$REMOTE" systemctl --user is-active --quiet strata-swift-iq3s-rpc-coordinator.service
systemctl --user is-active --quiet strata-swift-iq3s-rpc-worker.service
echo "Deployed Strata RPC runtime $REV to $REMOTE and restarted both services."
