# Rebasing the RPC fork onto an upstream release

This runbook updates the RPC integration branch to a specific Strata release
while preserving the upstream project and fork histories. It covers source work
only. Building deployment binaries, publishing rewritten branches, and changing
the live stack are separate actions that need explicit authorization.

## Branch and remote roles

- `upstream` is `Niko1221/Strata`, the original project.
- `origin` is `petria/Strata`, the public fork.
- `main` on the fork is an upstream mirror. Keep RPC changes on a named integration
  branch such as `rpc-v0.2.13`; never merge RPC commits into the mirror branch.
- Choose and record an exact upstream release tag, such as `v0.2.14`. Do not use
  a moving `main`, `latest`, or an unverified version string as the rebase base.

## 1. Inspect and preserve the current state

Run from the repository and record the current branch, commit, remotes, and worktree:

```sh
git status --short --branch
git branch --show-current
git rev-parse HEAD
git remote -v
git log --graph --oneline --decorate --all -40
```

Confirm that `upstream` and `origin` point to the repositories above. Fetch refs
without changing the checked-out files, then choose the intended release tag:

```sh
git fetch upstream --tags --prune
git fetch origin --prune
git tag --list 'v*' --sort=-version:refname | head -20
git show --no-patch --format=fuller v0.2.14
git merge-base --is-ancestor v0.2.14 upstream/main
```

Replace `v0.2.14` below with the tag actually selected. Confirm that the tag is
reachable from the intended upstream history and record its commit. Review the
upstream release notes and the changes since the previous base, especially files
that overlap RPC changes.

Do not discard, reset, or stash changes just to make the checkout clean. If the
worktree contains edits or untracked files, leave it untouched and perform the
rebase in a separate worktree made from the integration branch. Before rewriting
history, create a recovery bundle outside the repository and verify it:

```sh
mkdir -p "$HOME/.local/state"
backup_dir="$(mktemp -d "$HOME/.local/state/strata-rebase.XXXXXX")"
git bundle create "$backup_dir/pre-rebase.bundle" rpc-v0.2.13
git bundle verify "$backup_dir/pre-rebase.bundle"
```

Keep that bundle until the rebased branch has passed review. Never push recovery
refs or backup bundles to the public fork.

## 2. Identify the old base and RPC commits

Use the previous upstream release tag as the old base when it is an ancestor of
the integration branch. Verify this before rebasing:

```sh
git merge-base --is-ancestor v0.2.13 rpc-v0.2.13
git log --oneline --decorate v0.2.13..rpc-v0.2.13
git diff --stat v0.2.13...rpc-v0.2.13
```

Update `v0.2.13` and `rpc-v0.2.13` to the actual old base and integration branch.
If the old tag is not an ancestor, the branch contains merge commits, or the custom
commit range is unclear, stop and inspect the graph to identify the exact fork point.
Do not guess a boundary or replay upstream commits as RPC patches.

## 3. Rebase on an isolated branch

Keep the existing checkout and integration branch as recovery points. Create a
separate worktree and a new candidate branch, then replay only the RPC commits
after the verified old release base:

```sh
git worktree add --detach /path/outside/repository/strata-rpc-rebase rpc-v0.2.13
git -C /path/outside/repository/strata-rpc-rebase switch -c rpc-v0.2.14
git -C /path/outside/repository/strata-rpc-rebase \
  rebase --onto v0.2.14 v0.2.13 rpc-v0.2.14
```

Use real absolute paths and the verified branch/tag names. If a rebase stops on a
conflict, inspect the current patch and both versions of the affected code. Resolve
each conflict narrowly, preserving upstream fixes and the fork's optional RPC
behavior. Do not choose `ours` or `theirs` for whole files without reviewing the
result, and do not skip a commit to make the rebase complete. If the history has
meaningful merge commits, decide whether to use `--rebase-merges` after reviewing
the graph.

## 4. Review and validate the rebased source

After the rebase, verify the branch ancestry, commits and complete diff against the
new base:

```sh
git -C /path/outside/repository/strata-rpc-rebase status --short --branch
git -C /path/outside/repository/strata-rpc-rebase log --graph --oneline --decorate -40
git -C /path/outside/repository/strata-rpc-rebase diff --stat v0.2.14...rpc-v0.2.14
git -C /path/outside/repository/strata-rpc-rebase diff --check v0.2.14...rpc-v0.2.14
```

Review the full diff, not just conflict resolutions. Confirm that:

- ordinary single-host inference still works and stage RPC remains opt-in;
- TCP RPC and optional RDMA build/configuration paths remain distinct;
- both peers still enforce compatible source, model, split, context, KV,
  speculation, and transport settings;
- deployment templates remain templates and contain no machine-specific values;
- version references, setup instructions, and upstream attribution are current.

Follow the test and build requirements in `AGENTS.md`. Build the same candidate
commit for both supported CUDA architectures (SM86 and SM120), compare source
fingerprints, and run focused unit/integration tests. Report source/build checks
separately from hardware execution. Do not claim runtime, RDMA, vision, or benchmark
validation unless that exact check was run and its result recorded.

## 5. Report and hand off

Summarize the exact old and new upstream tags/commits, the RPC commit range replayed,
conflicts and resolutions, changed files, and checks grouped as passed, failed, or
not run. List remaining risks. Keep the original integration branch and recovery
bundle until the candidate is accepted.

Source rebase completion does not authorize publication or deployment. After review
and explicit authorization, publish only the named integration branch. If updating
an already-published branch whose history changed, use a lease-checked force push
with the expected old remote commit; never use `--force`, `--all`, or rewrite the
upstream-mirror `main` branch. Activate rebuilt binaries only in a separate,
explicitly authorized deployment step.
