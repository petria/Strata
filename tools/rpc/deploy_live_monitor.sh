#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
remote=${1:-${STRATA_RPC_REMOTE:-}}
args=()
[[ -z "$remote" ]] || args+=(--remote "$remote")
exec python3 "$root/tools/rpc/stack_manage.py" monitor "${args[@]}"
