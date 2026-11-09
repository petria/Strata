#!/usr/bin/env bash
set -euo pipefail
REMOTE=${1:-coordinator.example}
MODE=${2:-deploy}
case "$MODE" in prepare|activate|deploy) ;; *) echo 'Usage: deploy_custom_rpc_stack.sh [host] [prepare|activate|deploy]' >&2; exit 2;; esac
ROOT=$(git rev-parse --show-toplevel)
if [[ -n "$(git -C "$ROOT" status --porcelain)" ]]; then
  echo 'Commit or remove local changes before deployment.' >&2; exit 2
fi
REV=$(git -C "$ROOT" rev-parse HEAD)
REMOTE_BASE=/home/operator/.local/share/strata-rpc
RELEASE="$REMOTE_BASE/releases/$REV"
if [[ "$MODE" != activate ]]; then
  cmake -S "$ROOT" -B "$ROOT/build-v0212-sm86-rdma" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.3/bin/nvcc \
    -DCMAKE_CUDA_ARCHITECTURES=86 -DSTRATA_ENABLE_CUDA=ON -DSTRATA_ENABLE_STAGE_RPC=ON \
    -DSTRATA_ENABLE_RPC_RDMA=ON -DSTRATA_MMQ_KQUANTS=ON -DSTRATA_MMQ_TQ_OVERLAY=ON \
    -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_Q6K_EXPERTS=ON -DSTRATA_PORTABLE=OFF \
    -DSTRATA_PLE_FIXTURE_DIR="$ROOT/bench/micro"
  cmake --build "$ROOT/build-v0212-sm86-rdma" --target strata --parallel 6
  git -C "$ROOT" archive --format=tar "$REV" | ssh "$REMOTE" "mkdir -p '$RELEASE' && tar -xf - -C '$RELEASE'"
  ssh "$REMOTE" bash -s -- "$RELEASE" "$REMOTE_BASE" <<'BUILD'
set -euo pipefail
release=$1; base=$2; build="$release/build-v0212-sm120-rdma"; vision_build="$release/build-vision"
cmake -S "$release" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.3/bin/nvcc -DCMAKE_CUDA_ARCHITECTURES=120 \
  -DSTRATA_ENABLE_CUDA=ON -DSTRATA_ENABLE_STAGE_RPC=ON -DSTRATA_ENABLE_RPC_RDMA=ON \
  -DSTRATA_MMQ_KQUANTS=ON -DSTRATA_MMQ_TQ_OVERLAY=ON -DSTRATA_NATIVE_EXPERTS=ON \
  -DSTRATA_Q6K_EXPERTS=ON -DSTRATA_PORTABLE=OFF -DSTRATA_PLE_FIXTURE_DIR="$release/bench/micro"
cmake --build "$build" --target strata --parallel 6
cmake -S "$release/tools/vision" -B "$vision_build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DLLAMA_DIR="$build/_deps/strata_llamacpp-src" -DSTRATA_VISION_CUDA=OFF -DSTRATA_PORTABLE=OFF
cmake --build "$vision_build" --target strata-vision --parallel 6
install -D -m 755 "$vision_build/bin/strata-vision" "$release/engine/strata-vision"
test -x "$build/strata"; test -f "$release/data/expert-profile.bin"
python3 - "$release" <<'CLEAN'
from pathlib import Path
import shutil,sys
for p in Path(sys.argv[1]).rglob('.git'):
    if p.is_dir(): shutil.rmtree(p)
    else: p.unlink()
CLEAN
if [[ ! -x "$base/python-env/bin/python" ]]; then python3 -m venv "$base/python-env"; fi
"$base/python-env/bin/python" -m pip install -r "$release/requirements.txt"
BUILD
fi
if [[ "$MODE" == prepare ]]; then echo "Prepared $REV at $RELEASE (no inference services restarted)."; exit 0; fi
# Validate both binaries before touching the active stack.
test -x "$ROOT/build-v0212-sm86-rdma/strata"
ssh "$REMOTE" test -x "$RELEASE/build-v0212-sm120-rdma/strata"
ssh "$REMOTE" systemctl --user stop codex-llama-proxy.service strata-swift-iq3s-rpc-coordinator.service
systemctl --user stop strata-swift-iq3s-rpc-worker.service
ssh "$REMOTE" bash -s -- "$RELEASE" "$REMOTE_BASE" <<'ACTIVATE'
set -euo pipefail
release=$1; base=$2; stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/state/strata-stage-rpc/swift-iq3s-v0212" "$base/backups"
touch "$HOME/.local/state/strata-stage-rpc/swift-iq3s-v0212/coordinator.log"
for unit in strata-swift-iq3s-rpc-coordinator.service codex-llama-proxy.service; do
  if [[ -f "$HOME/.config/systemd/user/$unit" ]]; then cp -a "$HOME/.config/systemd/user/$unit" "$base/backups/$unit.$stamp"; fi
  cp "$release/packaging/rpc/systemd/$unit" "$HOME/.config/systemd/user/$unit"
done
ln -sfn "$release" "$base/current.next"; mv -Tf "$base/current.next" "$base/current"
systemctl --user daemon-reload
ACTIVATE
mkdir -p "$HOME/.local/state/strata-stage-rpc/swift-iq3s-v0212"
touch "$HOME/.local/state/strata-stage-rpc/swift-iq3s-v0212/worker.log"
install -D -m 644 "$ROOT/packaging/rpc/systemd/strata-swift-iq3s-rpc-worker.service" "$HOME/.config/systemd/user/strata-swift-iq3s-rpc-worker.service"
systemctl --user daemon-reload
systemctl --user start strata-swift-iq3s-rpc-worker.service
# Inspect the local listening socket without connecting: the worker accepts its one authenticated peer.
python3 - <<'WORKER_READY'
import subprocess,time
end=time.monotonic()+300
while time.monotonic()<end:
    status=subprocess.run(['systemctl','--user','is-active','--quiet','strata-swift-iq3s-rpc-worker.service'])
    listening=subprocess.check_output(['ss','-H','-ltn','sport = :50056'],text=True)
    if status.returncode==0 and listening.strip():
        print('Worker listener ready: 192.0.2.20:50056');break
    time.sleep(1)
else: raise SystemExit('Worker listener did not become ready; coordinator was not started.')
WORKER_READY
ssh "$REMOTE" systemctl --user start strata-swift-iq3s-rpc-coordinator.service
python3 - <<'READY'
import json,time,urllib.request
end=time.monotonic()+300
while time.monotonic()<end:
    try:
        h=json.load(urllib.request.urlopen('http://192.0.2.10:8080/health',timeout=5))
        if h.get('status')=='ok' and h.get('loaded'): print('Coordinator ready:',h);break
    except Exception: pass
    time.sleep(3)
else: raise SystemExit('Coordinator did not become ready; restore the saved paired runtime.')
READY
ssh "$REMOTE" systemctl --user start codex-llama-proxy.service
python3 - <<'READY'
import json,time,urllib.request
end=time.monotonic()+60
while time.monotonic()<end:
    try:
        m=json.load(urllib.request.urlopen('http://192.0.2.10:8081/v1/models',timeout=5))
        if m.get('data'): print('Proxy ready:',m['data'][0]['id']);break
    except Exception: pass
    time.sleep(2)
else: raise SystemExit('Proxy did not become ready.')
READY
if systemctl --user is-enabled --quiet strata-live-monitor-worker.service; then "$ROOT/tools/rpc/deploy_live_monitor.sh" "$REMOTE"; fi
systemctl --user is-active --quiet strata-swift-iq3s-rpc-worker.service
ssh "$REMOTE" systemctl --user is-active --quiet strata-swift-iq3s-rpc-coordinator.service codex-llama-proxy.service
echo "Activated Strata v0.2.12 RPC runtime $REV"
