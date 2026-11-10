#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
profile=${1:?Usage: activate_rpc_profile.sh PROFILE [--check|--activate]}
mode=${2:---check}
case "$mode" in --check) command=check;; --activate) command=activate;; *) echo 'Expected --check or --activate' >&2; exit 2;; esac
exec python3 "$root/tools/rpc/stack_manage.py" "$command" --profile "$profile"
