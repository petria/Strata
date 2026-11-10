#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
remote=${1:-${STRATA_RPC_REMOTE:-}}
mode=${2:-deploy}
case "$mode" in prepare|deploy) ;; activate) mode=deploy-activate;; *) echo 'Expected prepare, activate, or deploy' >&2; exit 2;; esac
args=()
[[ -z "$remote" ]] || args+=(--remote "$remote")
exec python3 "$root/tools/rpc/stack_manage.py" "$mode" "${args[@]}"
