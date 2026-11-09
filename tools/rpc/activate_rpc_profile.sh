#!/usr/bin/env bash
# Switch the paired local/remote model profiles without rebuilding unchanged engine binaries.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
profile=${1:?Usage: activate_rpc_profile.sh PROFILE [--check|--activate]}
mode=${2:---check}
[[ "$profile" =~ ^[a-zA-Z0-9._-]+$ ]] || { echo 'Invalid profile name' >&2; exit 2; }
[[ "$mode" == --check || "$mode" == --activate ]] || { echo 'Expected --check or --activate' >&2; exit 2; }
remote=coordinator.example
runtime=/home/operator/.local/share/strata-rpc/current
state=/home/operator/.local/state/strata-stage-rpc/$profile
# Stage only profiles; they do not alter the compiled stage source identity.
ssh "$remote" mkdir -p "$runtime/profiles/rpc/$profile"
scp -q "$root/profiles/rpc/$profile/"*.json "$remote:$runtime/profiles/rpc/$profile/"
STRATA_ROOT="$root" MODEL_DATA_ROOT=/srv/storage/llm_stuff/strata-data TLS_DIR=/home/operator/.local/share/strata-stage-rpc/tls \
python3 - "$root" "$profile" <<'CHECK'
import sys,os,json,string
from pathlib import Path
root=Path(sys.argv[1]);profile=sys.argv[2]
w=json.loads((root/'profiles/rpc'/profile/'worker.json').read_text());a=[string.Template(v).substitute(os.environ) for v in w['args']]
for flag in ('--pack','--native','--ple-gguf','--expert-profile','--rpc-ca','--rpc-cert','--rpc-key'):
    p=Path(a[a.index(flag)+1]);assert p.exists(),str(p)
assert Path(string.Template(w['exe']).substitute(os.environ)).is_file()
assert (Path(a[a.index('--pack')+1])/'native_experts.txt').is_file()
manifest=root/'profiles/rpc'/profile/'model-manifest.json'
if manifest.exists():
    for item in json.loads(manifest.read_text())['files']:
        path=Path(a[a.index('--native')+1]).parent/item['name']
        assert path.stat().st_size==item['bytes'],str(path)
        verified=json.loads(path.with_suffix('.verified.json').read_text())
        assert verified['sha256']==item['sha256'] and verified['mtime_ns']==path.stat().st_mtime_ns,str(path)
print('Worker profile paths and verified model shards ready')
CHECK
ssh "$remote" bash -s -- "$runtime" "$profile" "$state" <<'CHECK_REMOTE'
set -euo pipefail
export STRATA_ROOT=$1 MODEL_DATA_ROOT=/srv/storage/llm_stuff/strata-2.0-baseline-data TLS_DIR=/home/operator/.local/share/strata-stage-rpc/tls STATE_DIR=$3
python3 - "$1" "$2" <<'PY'
import os,sys,json,string
from pathlib import Path
root=Path(sys.argv[1]);profile=sys.argv[2];d=json.loads((root/'profiles/rpc'/profile/'coordinator.json').read_text())
a=[string.Template(v).substitute(os.environ) for v in d['args']]
for flag in ('--pack','--native','--ple-gguf','--mtp','--expert-profile','--rpc-ca','--rpc-cert','--rpc-key'):
    p=Path(a[a.index(flag)+1]);assert p.exists(),str(p)
assert Path(string.Template(d['exe']).substitute(os.environ)).is_file()
manifest=root/'profiles/rpc'/profile/'model-manifest.json'
if manifest.exists():
    for item in json.loads(manifest.read_text())['files']:
        path=Path(a[a.index('--native')+1]).parent/item['name']
        assert path.stat().st_size==item['bytes'],str(path)
        verified=json.loads(path.with_suffix('.verified.json').read_text())
        assert verified['sha256']==item['sha256'] and verified['mtime_ns']==path.stat().st_mtime_ns,str(path)
print('Coordinator profile paths and verified model shards ready:',d['model_name'])
PY
CHECK_REMOTE
[[ "$mode" == --activate ]] || { echo 'Preflight complete; inference was not changed.'; exit 0; }
backup=/home/operator/.local/state/strata-stage-rpc/profile-backup-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$backup" "$state"
cp "$HOME/.config/systemd/user/strata-swift-iq3s-rpc-worker.service" "$backup/worker.service"
ssh "$remote" "mkdir -p '$backup' '$state'; cp ~/.config/systemd/user/strata-swift-iq3s-rpc-coordinator.service '$backup/coordinator.service'"
rollback() {
  echo "Profile activation failed; restoring saved units from $backup" >&2
  ssh "$remote" systemctl --user stop codex-llama-proxy.service strata-swift-iq3s-rpc-coordinator.service || true
  systemctl --user stop strata-swift-iq3s-rpc-worker.service || true
  cp "$backup/worker.service" "$HOME/.config/systemd/user/strata-swift-iq3s-rpc-worker.service"
  ssh "$remote" "cp '$backup/coordinator.service' ~/.config/systemd/user/strata-swift-iq3s-rpc-coordinator.service; systemctl --user daemon-reload" || true
  systemctl --user daemon-reload
  systemctl --user start strata-swift-iq3s-rpc-worker.service || true
  ssh "$remote" systemctl --user start strata-swift-iq3s-rpc-coordinator.service codex-llama-proxy.service || true
}
trap rollback ERR
ssh "$remote" systemctl --user stop codex-llama-proxy.service strata-swift-iq3s-rpc-coordinator.service
systemctl --user stop strata-swift-iq3s-rpc-worker.service
python3 - "$backup/worker.service" "$HOME/.config/systemd/user/strata-swift-iq3s-rpc-worker.service" "$profile" <<'UNIT'
from pathlib import Path
import re,sys
s=Path(sys.argv[1]).read_text();s=re.sub(r'^Description=.*$',f'Description=Strata custom {sys.argv[3]} RPC worker RTX 3090',s,flags=re.M);s=re.sub(r'profiles/rpc/[^/]+/worker.json',f'profiles/rpc/{sys.argv[3]}/worker.json',s);s=re.sub(r'strata-stage-rpc/[^/]+/worker.log',f'strata-stage-rpc/{sys.argv[3]}/worker.log',s)
Path(sys.argv[2]).write_text(s)
UNIT
touch "$state/worker.log"
ssh "$remote" bash -s -- "$backup" "$profile" "$state" <<'REMOTE_UNIT'
set -euo pipefail
python3 - "$1/coordinator.service" /home/operator/.config/systemd/user/strata-swift-iq3s-rpc-coordinator.service "$2" <<'PY'
from pathlib import Path
import re,sys
s=Path(sys.argv[1]).read_text();profile=sys.argv[3]
s=re.sub(r'^Description=.*$',f'Description=Strata custom {profile} RPC coordinator RTX 5090',s,flags=re.M)
s=re.sub(r'profiles/rpc/[^/]+/coordinator.json',f'profiles/rpc/{profile}/coordinator.json',s)
s=re.sub(r'(\.local/state/strata-stage-rpc/)[^/\s]+',lambda m:m[1]+profile,s)
Path(sys.argv[2]).write_text(s)
PY
touch "$3/coordinator.log"
systemctl --user daemon-reload
REMOTE_UNIT
systemctl --user daemon-reload
systemctl --user start strata-swift-iq3s-rpc-worker.service
python3 - <<'WAIT'
import subprocess,time
end=time.monotonic()+900
while time.monotonic()<end:
 restarts=int(subprocess.check_output(['systemctl','--user','show','strata-swift-iq3s-rpc-worker.service','-p','NRestarts','--value']))
 if restarts:raise SystemExit('Worker failed during loading; restoring previous profile')
 if subprocess.check_output(['ss','-H','-ltn','sport = :50056'],text=True).strip():print('Worker listener ready');break
 time.sleep(2)
else:raise SystemExit('Worker did not load within 15 minutes')
WAIT
ssh "$remote" systemctl --user start strata-swift-iq3s-rpc-coordinator.service
python3 - "$root" "$profile" <<'WAIT'
import json,time,urllib.request,sys
from pathlib import Path
expected=json.loads((Path(sys.argv[1])/'profiles/rpc'/sys.argv[2]/'coordinator.json').read_text())['model_name']
end=time.monotonic()+600
while time.monotonic()<end:
 try:
  h=json.load(urllib.request.urlopen('http://192.0.2.10:8080/health',timeout=5))
  if h.get('loaded') and h.get('model')==expected:print('Coordinator ready',h);break
 except Exception:pass
 time.sleep(3)
else:raise SystemExit('Coordinator did not load within 10 minutes')
WAIT
ssh "$remote" systemctl --user start codex-llama-proxy.service
python3 - <<'WAIT'
import urllib.request,json,time
end=time.monotonic()+60
while time.monotonic()<end:
 try:
  if json.load(urllib.request.urlopen('http://192.0.2.10:8081/v1/models',timeout=5)).get('data'):break
 except Exception:pass
 time.sleep(1)
else:raise SystemExit('Proxy did not become ready')
WAIT
trap - ERR
python3 - "$state" "$profile" "$backup" <<'RECORD'
from pathlib import Path
import json,sys,time
Path(sys.argv[1],'activation.json').write_text(json.dumps({'profile':sys.argv[2],'backup':sys.argv[3],'activated_at':time.time()},indent=2))
RECORD
echo "Activated $profile; previous units saved at $backup"
