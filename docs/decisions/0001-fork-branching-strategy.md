# ADR 0001 — Branching strategy for the parallel-EventQueue research fork

- **Status**: Accepted (2026-07-17)
- **Scope**: this fork's research work only (the parallel-`EventQueue` +
  shared-L2/L3 project). Upstream-contribution rules are unchanged; see the last
  section.

`docs/decisions/` is this fork's Architecture Decision Record (ADR) log. This is
record 0001 and establishes the format: short, dated, one decision per file,
numbered `NNNN-slug.md`, never rewritten after "Accepted" (supersede with a new
record instead).

## Context

Until now, all research — the entire S-001→S-015 arc — was committed directly
onto a single local `main` branch. The work is intrinsically a set of parallel
investigation threads:

- some **orthogonal** (e.g. S-010 `AddrRangeMap` race vs. S-013 balanced-workload
  checkpoint — disjoint code), and
- some **conflicting / dependent** in the same subsystem (S-009 → S-014 → S-015,
  all editing `src/mem/xbar.cc` / `src/mem/packet_queue.cc`).

Doing everything on one branch caused real friction already recorded in the
specs: half-finished experiments blocking clean builds (S-013 §8 had to
path-`git stash` its WIP to get a clean Step-3 build for comparison), and no
clean way to abandon a dead-end investigation (S-013 and S-015 were both negative
results that nonetheless live in the main-line history).

Relevant environment facts (re-probe each session — do not assume carry-over):

- Remotes: `origin = git@github.com:pkwxl/gem5-parq.git` (personal fork),
  `upstream = https://github.com/gem5/gem5.git`. `main` tracks `origin/main`.
- `/workspace/shm` is a large tmpfs (hundreds of GB). A full `build/<ISA>/` tree
  is tens of GB. tmpfs is RAM-backed and **volatile** — cleared on host reboot.
- Each investigation is one `docs/specs/S-NNN-slug.md` spec. Spec numbers are
  allocated globally and never backfilled (see `docs/specs/INDEX.md`).
  `docs/specs/INDEX.md` (its table + the "现状" narrative paragraph) is edited by
  *every* investigation — it is the guaranteed merge hotspot.

## Decision

### Remotes and trunk

- `origin` (the personal fork `pkwxl/gem5-parq`) is the research backup and
  integration home. `upstream` (`gem5/gem5`) is read-only.
- **`main` (tracking `origin/main`) is the research trunk** — analogous to how
  upstream uses `develop`. Keep it buildable and spec-consistent.
  **No more direct exploratory commits on `main`.** Only finished, merged
  investigations and number-claim rows (below) land there.
- **Pushing is manual.** Whoever wants a branch backed up runs `git push`
  themselves — nothing here auto-pushes (`git push *` is denied in
  `.claude/settings.json` by design). Push `main` (and any active `sNNN-*` branch
  worth backing up) to `origin` regularly; that is the only off-machine copy.

### One branch per investigation

Each S-NNN investigation is one branch `sNNN-slug`, matching its spec filename,
branched from `main`. Lifecycle:

1. **Claim the number on `main` first.** This is the real parallel race: two
   branches must not both grab the next S-NNN. Add the `INDEX.md` table row for
   the new number with status `进行中` and the branch name, commit it on `main`.
   That atomically reserves the number at the trunk and makes in-flight work
   visible. *Then* create the branch.
2. `git worktree add /workspace/gem5-wt/sNNN-slug -b sNNN-slug main`
   (see *Worktrees + tmpfs* for why a worktree, not a plain checkout).
3. Do the work: code plus `docs/specs/S-NNN-*.md`, committing in this project's
   established small-step style (`S-012 Step 1/2/3`, `S-015 §1a/§1b`).
4. On a documented conclusion (spec written, INDEX "现状" updated, memory updated),
   merge back to `main` with `--no-ff`, then `git worktree remove` +
   `git branch -d`. Push `main` by hand.

### Merge style: `--no-ff`, never squash

The commit log *is* a research record here — the granular
`S-012 Step 1/2/3` history is what lets a later session reconstruct what was
tried. Squashing destroys that. Always `git merge --no-ff`, subject line:

```
merge: S-NNN <slug> — <one-line outcome>
```

Record the outcome plainly, including negative results (per the project working
style in `CLAUDE.md`).

### Orthogonal vs. conflicting work

- **Orthogonal** investigations → independent branches off `main`, merged in any
  order. They touch disjoint code, so the only conflict is `INDEX.md`.
- **Conflicting / dependent** investigations in the same subsystem (the
  S-009 → S-014 → S-015 pattern) → branch the dependent one off its **parent
  branch**, or rebase it onto `main` after the parent merges. Do **not** run two
  truly parallel branches over the same files (`xbar.cc` / `packet_queue.cc`) —
  that just relocates the single-branch conflict into a merge conflict.

### Dead-end investigations

Negative results are common and valuable here (S-013, S-015). A dead-end branch's
spec + INDEX row still land on `main` as the record; the code branch itself need
not be merged. Force-deleting an abandoned `sNNN-*` branch (`git branch -D`) is
allowed without a prompt — the deny-list only protects the three permanent
branches (`main` / `stable` / `develop`) from force-delete, so investigation
branches can be cleaned up freely (recoverable via reflog if deleted in error).

### The `INDEX.md` hotspot

`docs/specs/INDEX.md` will conflict on nearly every parallel merge. Conventions to
keep that trivial:

- Spec files themselves never conflict — each branch adds its own new file.
- The spec **table is append-only**; a conflict there is a mechanical "keep both
  rows."
- Keep "现状" edits **small and localized**; resolve by taking both sides. The
  second branch to merge owns the resolution and never rewrites another branch's
  row.
- Merge **frequently** (short-lived branches) so divergence stays small.

### Worktrees + tmpfs

A `git worktree` per active investigation gives each one its own working directory
*and its own `build/` tree*, so concurrent investigations never fight over the
build dir or interrupt each other's long runs (switching branches in one checkout
would invalidate the 59 GB `build/` and kill in-flight experiments).

- Worktrees live under `/workspace/gem5-wt/<branch>/`.
- One-time setup: `mkdir -p /workspace/shm/gem5`. The `gem5/` namespace keeps our
  build trees separate so a co-tenant project sharing this tmpfs won't `rm` them.
- Each worktree's `build/` is a **symlink** into
  `/workspace/shm/gem5/<branch>/build/` (tmpfs). tmpfs is fast and volatile —
  builds regenerate, so losing them on reboot is fine. **Never** put anything you
  care about there: specs live in the git tree; checkpoints keep going to
  `-d /tmp/...` (see `CLAUDE.md`).
- Isolcpus discipline is unchanged (`CLAUDE.md`): `taskset` builds off the
  reserved cores; serial-arm timing on `54-55`, parallel-arm on `92-111`.

New-investigation recipe (copy-paste):

```sh
# 0. from the main worktree: claim S-NNN on main first
cd /workspace/gem5
$EDITOR docs/specs/INDEX.md          # add the S-NNN row, status 进行中, branch sNNN-slug
git commit -am "docs: claim S-NNN <slug> (进行中)"   # push by hand when convenient

# 1. create the investigation worktree
git worktree add /workspace/gem5-wt/sNNN-slug -b sNNN-slug main

# 2. back its build/ with namespaced tmpfs
mkdir -p /workspace/shm/gem5/sNNN-slug/build
ln -s /workspace/shm/gem5/sNNN-slug/build /workspace/gem5-wt/sNNN-slug/build

# 3. work in it; build off the reserved cores, e.g.
cd /workspace/gem5-wt/sNNN-slug
taskset -c 0-53,56-91 scons build/X86_MESI_Three_Level/gem5.opt -j<N>

# 4. when concluded, from the main worktree:
cd /workspace/gem5
git merge --no-ff sNNN-slug -m "merge: S-NNN <slug> — <outcome>"
git worktree remove /workspace/gem5-wt/sNNN-slug
git branch -d sNNN-slug
# push main by hand
```

## Consequences

- **Backup is now real but manual.** The single-point-of-failure (everything on
  one un-pushed local branch) is closed only as long as someone actually pushes.
  Push discipline is a human responsibility, not automated.
- **`INDEX.md` merge conflicts are expected**, on nearly every merge — mitigated,
  not eliminated, by the append-only + take-both conventions above.
- **Per-worktree builds consume RAM** (tmpfs). Fine given the current tmpfs size,
  but many simultaneous worktrees × tens of GB each is the limit to watch. Remove
  worktrees promptly when an investigation concludes.
- **tmpfs is volatile** — a host reboot wipes every worktree's `build/`. Rebuild;
  never store anything non-regenerable there.
- **`.claude/settings.json`**: `Bash(*)` permits `git worktree` / `branch` /
  `checkout` / `merge` without prompts. `git push *` stays denied (pushing is a
  deliberate manual step). The blanket `git branch -D *` deny was narrowed to
  `git branch -D main*` / `stable*` / `develop*` so investigation branches can be
  force-deleted during cleanup while the three permanent branches stay protected.

## Non-goals / unchanged: upstream contribution

This ADR governs *research* branching only. Anything intended for upstream gem5
still follows `CONTRIBUTING.md`: branch from `upstream/develop` (not `main`), keep
fork-research commits out of it, and open the PR against `gem5/gem5`'s `develop`.
The research lineage (`main` + `sNNN-*`) and the upstream lineage stay disjoint.
