#!/usr/bin/env bash
# Optional UI telemetry sidecars. Leaves inference services and sockets running.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
remote=${1:-coordinator.example}
mkdir -p /home/operator/.config/systemd/user
cat > /home/operator/.config/systemd/user/strata-live-monitor-worker.service <<UNIT
[Unit]
Description=Strata independent RPC worker telemetry
After=network-online.target
[Service]
WorkingDirectory=$root
ExecStart=/usr/bin/python3 $root/tools/rpc/live_monitor.py worker --listen 192.0.2.20:50057
Restart=on-failure
RestartSec=3
LimitNOFILE=65536
[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable strata-live-monitor-worker.service
systemctl --user restart strata-live-monitor-worker.service
runtime=/home/operator/.local/share/strata-rpc/current
scp -q "$root/tools/rpc/live_monitor.py" "$remote:$runtime/tools/rpc/live_monitor.py"
scp -q "$root/serve/web/rpc-live-monitor.js" "$root/serve/web/app.js" "$remote:$runtime/serve/web/"
ssh "$remote" bash -s <<'REMOTE'
set -euo pipefail
mkdir -p /home/operator/.config/systemd/user
cat > /home/operator/.config/systemd/user/strata-live-monitor-relay.service <<'UNIT'
[Unit]
Description=Strata independent RPC telemetry relay
After=network-online.target
[Service]
WorkingDirectory=/home/operator/.local/share/strata-rpc/current
ExecStart=/usr/bin/python3 /home/operator/.local/share/strata-rpc/current/tools/rpc/live_monitor.py relay --listen 192.0.2.10:8086 --interface roce0 --upstream http://192.0.2.20:50057/metrics --origin http://192.0.2.10:8080 --origin http://192.0.2.10:8081 --origin http://coordinator.example:8080 --origin http://coordinator.example:8081 --origin http://coordinator.example:8080 --origin http://coordinator.example:8081
Restart=on-failure
RestartSec=3
[Install]
WantedBy=default.target
UNIT
cat > /home/operator/.local/share/strata-rpc/current/serve/web/rpc-live-config.js <<'JS'
// Optional deployment-specific telemetry URL. No model or prompt data passes here.
export default {url: `${location.protocol}//${location.hostname}:8086/metrics`};
JS
systemctl --user daemon-reload
systemctl --user enable strata-live-monitor-relay.service
systemctl --user restart strata-live-monitor-relay.service
REMOTE
