# S-017 — Balanced checkpoint, bounded three-arm `hostSeconds` comparison
(experimental design only — not yet run)

> **Status: PLAN ONLY, not launched.** This session drafted and committed the
> experimental design per the user's explicit request ("draft the spec and
> commit, we will launch the new spec in a new session"). No gem5 process has
> been started for this spec. Everything below is a plan to be executed by a
> follow-up session, which should update this file with actual results
> in-place (new `## N. Results` section) rather than opening a new spec.

## 1. Motivation

[S-012](./S-012-eventq-critical-path-instrumentation-design.md) §19.4
re-measured cross-domain lock-wait share at real hour-scale
(`MAX_TICKS=1.34e12`, the balanced checkpoint) and found it collapsed to
**<0.15% of `hostSeconds`** — down from §17.3's ~11% at the shorter
(`MAX_TICKS=2e9`) window. Locking is not the bottleneck at this scale. That
finding motivates going back to this project's actual headline question
(CLAUDE.md "Primary research goal" / "Measurement methodology: three-arm
comparison") with real numbers at the *same* hour-scale, instead of continuing
to refine the critical-path/lock-wait picture further.

That three-arm methodology (**Baseline** / **Current Serial** / **Current
Parallel**, see CLAUDE.md) has never actually been run cleanly at hour-scale
on this project's own balanced checkpoint:

- S-012 §18 got real hour-scale *parallel* throughput (~7.4×10⁸ tick/s
  steady-state) but no baseline/current-serial comparison, and no critpath
  CSVs (the `SIGTERM`-vs-`atexit` gap documented there).
- S-012 §19 got a real hour-scale *parallel* run with critpath tracing **on**
  — but §19.4 itself warns that `hostSeconds` measured with
  `EVENTQ_CRITPATH_TRACE=1` is not comparable to a tracing-off run (§19's own
  §5 argument, reiterated at line ~1004-1005 of that spec): the instrumentation
  changes the measured wall-clock cost, sometimes substantially (§19.5's
  `critPathBuffer` reallocation stalls). It also only ever covered the
  parallel arm, not baseline/current-serial.
- The `x86-threads3-roi-classic` checkpoint currently has its own three-arm
  job in flight (started 2026-07-18 13:56 UTC, unbounded/no `MAX_TICKS`, no
  `STAT_DUMP_PERIOD` — see the live-status check earlier this session), which
  will eventually answer this same question for that checkpoint, but on an
  unknown/unbounded timeline and for a different (non-balanced) workload.

This spec's goal: get real **baseline / current-serial / current-parallel**
`hostSeconds` at the *same* hour-scale bound S-012 §19 used
(`MAX_TICKS=1,340,000,000,000`), on the **balanced** checkpoint, with
**critpath tracing off** (so the numbers are directly comparable, per §19.4's
own caveat), then apply CLAUDE.md's three formulas:

- **Overhead ratio** = Current Serial / Baseline
- **Real speedup** = Baseline / Current Parallel
- **Internal speedup** = Current Serial / Current Parallel

## 2. Experimental design

### 2.1 Fixed parameters (same across all three arms)

| Parameter | Value | Why |
|---|---|---|
| Checkpoint | `/workspace/gem5-ckpt/x86-threads-balanced3-roi-classic` | The balanced 4-core workload from S-013, same one S-012 §17-19 already characterized — keeps this result comparable to the existing lock-wait/critpath findings it's following up on. |
| `MAX_TICKS` | `1340000000000` (1.34×10¹²) | Same bound as S-012 §19.1 (~45 min simulated time at this workload's rate) — reuses an already-load-bearing scale instead of picking a new one, and is known to reach a clean `m5 exit`-independent bounded stop (not the natural workload-end exit) within a tractable wall-clock budget. |
| `EVENTQ_CRITPATH_TRACE` | unset (off, default `0`) | Deliberately **off** — this run is a `hostSeconds` throughput comparison, not a critical-path analysis; S-012 §19.4 already showed tracing-on `hostSeconds` isn't comparable to tracing-off, so mixing the two would make the three arms' numbers inconsistent with each other in a new way. |
| `STAT_DUMP_PERIOD` | `2000000000` (2×10⁹ ticks) | Safety net / progress visibility, same value S-012 §19.1 used. Cheap without critpath tracing (plain `stats.txt` append, no CSV flush), gives ~670 periodic dumps to eyeball a throughput trend and to recover partial `hostSeconds` data if a run has to be killed before its natural bounded exit. |
| `SIM_QUANTUM_TICKS` | `6660` | This project's standing operating point since S-009 §27; keep it fixed so this isn't also a quantum experiment. |
| Board/cache config | script defaults (unchanged) | `docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py` is used unmodified for all three arms — CLAUDE.md's own note confirms the same script file works against a Baseline build's binary too (it only touches fork-added `Root` params inside `if PARALLEL_EVENTQ:`). |

### 2.2 Per-arm parameters and binaries

| Arm | Binary | `PARALLEL_EVENTQ` | `EVENTQ_BARRIER_MODE` | `HOST_PIN_CPUS` | `taskset --cpu-list` |
|---|---|---|---|---|---|
| **Baseline** | `/workspace/gem5-wt/baseline-prefork-13462eed1b/build/X86_MESI_Three_Level/gem5.opt` (pre-fork commit `13462eed1b`, already built — confirm this is still current before launching, see §2.4) | unset | unset | unset | `108` |
| **Current Serial** | `/workspace/gem5/build/X86_MESI_Three_Level/gem5.opt` (current `main`, already built — confirm current before launching) | `0` | unset | unset | `109` |
| **Current Parallel** | same binary as Current Serial | `1` | `spin` | `100,101,102,103,104,105,106,107` | `100-107` |

The parallel arm's 8-core pin (`100-107`) is the exact same core list S-012
§19.1 used for this checkpoint's 8 EventQueue domains (4 core-private
L1+L2 domains + shared L3 + 3 directory/DRAM domains) — reusing rather than
re-deriving that assignment.

### 2.3 Core reservation — must not collide with the live S-016-full job

At the time this plan was drafted, a separate, still-running (unbounded,
started 2026-07-18) three-arm job on the **`x86-threads3-roi-classic`**
checkpoint (`/tmp/s016-full/{baseline,current-serial,parallel-v2}`) holds
cores `54` (baseline), `55` (current-serial), and `92-99` (current-parallel,
8 domains) — all within CLAUDE.md's reserved isolated ranges
(`54-55,92-111`). Per CLAUDE.md and
[[feedback_core_pinning_policy]] (per-core exclusivity, check before
launching, not just before the first launch attempt ever): **before actually
launching this spec's runs**, re-verify with `ps`/`taskset -c` that `108`,
`109`, and `100-107` are still free and that the S-016-full job (or any other
job) hasn't drifted onto them — don't assume this document's snapshot is
still accurate by the time a follow-up session acts on it. `110-111` are
deliberately left unassigned as slack (e.g. for a monitoring script, or if an
extra core turns out to be needed) — they don't need to stay reserved for
this experiment specifically.

### 2.4 Pre-launch checklist for the follow-up session

1. Re-check core occupancy (§2.3) — `ps aux | grep gem5.opt` and cross-check
   `psr` via `/proc/<pid>/task/*/stat` the same way the S-016-full monitor
   script does, not just `taskset -c` (a core can be free of *pinned*
   processes but still show contention from something else).
2. Confirm both binaries in §2.2 are still up to date: `git -C
   /workspace/gem5-wt/baseline-prefork-13462eed1b log -1` should still show
   `13462eed1b` (that worktree is pinned to the pre-fork commit and should
   never move); `git -C /workspace/gem5 log -1` should be checked against the
   binary's mtime — if `main` has picked up any C++/SConscript changes since
   the binary was last built (mtime `2026-07-18 00:18` as of this writing),
   rebuild `build/X86_MESI_Three_Level/gem5.opt` first (`taskset` the `scons`
   job to the unreserved pool, not `100-111` — CLAUDE.md's build-tool-core
   rule). Pure `docs/`-only commits (like the two this session made) don't
   require a rebuild.
3. `rm -f ~/.cache/gem5/*.lock.lock` before the first launch of the session —
   S-015's memory note: a stale resource filelock from a prior killed run
   hangs every subsequent run silently at startup with no banner/output,
   easy to misread as a new hang.
4. Pick an output root under `/tmp` (16G tmpfs, ~10G free as of this
   writing) — e.g. `/tmp/s017-balanced-hostseconds/{baseline,current-serial,
   current-parallel}` — **not** `/workspace/shm/gem5/...`; unlike S-012 §19,
   this run has critpath tracing off so its `stats.txt`+log footprint is
   small (no ~124GB CSV problem to route around).
5. Launch all three with a generous `timeout` safety net (S-012 §19.1's
   precedent: `timeout <budget> taskset --cpu-list <cores> <binary> -d
   <outdir> docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`, env vars from
   §2.1/§2.2 prefixed). Suggest `timeout 10800` (3h) per arm as a starting
   budget — no prior tracing-off hostSeconds measurement exists for this
   exact bound to calibrate a tighter number against (§3's estimate below is
   rough). The `MAX_TICKS` bound itself should trigger each arm's own clean
   exit well before the timeout in the normal case; the timeout is a safety
   net, not the intended stop condition (S-012 §16.6 lesson: a
   `SIGTERM`-terminated run skips `atexit`-registered cleanup, though that
   only matters for critpath flush, which is off here — a `stats.txt`
   `STAT_DUMP_PERIOD` dump is unaffected either way per §18's finding).
6. Start a drift/liveness monitor analogous to the removed
   `s016-full`/`monitor_cpu.sh` (per-tid `psr` snapshot every few minutes,
   flag on any pinned tid leaving its expected core range) before or
   immediately after launch, not as an afterthought.

## 3. Rough wall-clock expectation (not a commitment)

No tracing-off `hostSeconds` number exists yet for this exact bound on this
checkpoint to anchor a real estimate. As a rough, explicitly-uncertain
planning number: S-012 §18's steady-state parallel throughput without
tracing (~7.4×10⁸ tick/s, same checkpoint) would put the **parallel** arm at
very roughly 1340000000000 / 7.4e8 ≈ 1,810s (~30 min) wall-clock if that
steady-state rate holds for the full window (§18 also found 17 unexplained
long-tail stalls worth ~12.4% of wall-time in a *tracing-on* run of similar
length — unknown whether those recur tracing-off). **Baseline** and
**current-serial** have no comparable prior tracing-off measurement at this
scale at all — S-009's short-window (`MAX_TICKS≈2e8`) numbers suggest
baseline and current-serial are the same order of magnitude as each other
(the whole point of the "overhead ratio" being close to but not exactly 1),
but scaling that ~6,700× up to 1.34e12 ticks by simple ratio is not something
this spec treats as reliable. Budget accordingly (§2.4 step 5's 3h-per-arm
timeout suggestion already reflects this uncertainty) and treat §2.4's
`timeout` values as adjustable once the first real run gives an actual rate.

## 4. Analysis plan (for the follow-up session, once all three `stats.txt` exist)

1. Confirm all three arms reached the `MAX_TICKS` bound cleanly (exit code 0,
   `finalTick` consistent with the bound, no assert/panic/segfault in any
   log) before trusting any `hostSeconds` number — a killed-by-timeout run's
   partial `hostSeconds` (from the last `STAT_DUMP_PERIOD` dump) is usable
   for a rough rate estimate but should be labeled as such, not reported
   as a completed-run number.
2. Pull final `hostSeconds` from each arm's `stats.txt` (S-012 §14.6's
   convention: use the run's own final cumulative value, not a
   tracing-affected one — moot here since tracing is off throughout, but
   keep the habit).
3. Also sanity-check functional consistency where comparable: baseline vs.
   current-serial `simInsts`/`finalTick` should match exactly (both are
   single-`EventQueue` serial runs of the identical simulated machine, modulo
   this fork's serial-mode-only lock overhead, which doesn't change
   simulated behavior — see CLAUDE.md's Current-Serial-vs-Baseline
   description). Current-parallel's `simInsts` should also match (this
   project's standing correctness bar since S-009 §27), given the same
   checkpoint/quantum has already been verified at this exact bound in
   S-012 §19 (parallel arm only, tracing-on) and at `MAX_TICKS=2e9` in
   S-013/S-012 §17 (all three... actually only serial+parallel, not
   baseline, were compared there — this would be the first time baseline
   is checked against this specific checkpoint at all, not just at this
   bound. Worth flagging explicitly if it does *not* match, rather than
   assuming it will.).
4. Compute and record all three CLAUDE.md formulas (Overhead ratio, Real
   speedup, Internal speedup), and compare Real speedup against this
   project's only prior real-speedup-adjacent numbers (S-009 §27's 0.91x,
   itself now caveated per S-014 as measured only in an under-covered short
   window — this would be the first real-speedup number at genuine hour
   scale on any checkpoint, so treat any large deviation from 0.91x as
   informative, not as evidence of a bug, unless something else (a
   crash/hang/incorrect `simInsts`) also goes wrong).
5. Per CLAUDE.md's working-style convention, write the actual numbers into
   this file (a new `## 5. Results` section) in the same session they're
   obtained, including if they're unfavorable to the project's speedup goal
   — don't leave them only in chat.
