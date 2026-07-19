# S-018 — O3 CPU restore onto the balanced checkpoint (in progress)

**Status: script implemented, not yet built or run.** This session wrote
`docs/refs/scripts/x86_fs_mesi3_parallel_eventq_o3.py` (an O3-CPU sibling
of `x86_fs_mesi3_parallel_eventq.py`) and bumped `RubySequencer.
max_outstanding_requests` to 32 on both scripts' shared restore path. No
build, boot, or restore run has happened yet.

## 1. Problem

Every ROI measurement so far in this project's history (S-007 through
S-017) uses `CPUTypes.TIMING` (`TimingSimpleCPU`) for the restore/ROI
core. The user pointed out this is the wrong CPU model for what this
project is trying to measure: `TimingSimpleCPU` issues one memory access
at a time and blocks the whole pipeline until it completes -- it has no
mechanism to have more than one memory request in flight per core. A
CPU model with effectively concurrency 1 per core cannot generate the
concurrent, overlapping cross-domain traffic that splitting the
`EventQueue` into per-domain host threads is meant to exploit; any
speedup (or lack of it) measured with `TimingSimpleCPU` says relatively
little about the parallel-EventQueue design's actual value proposition.

## 2. Scope decision: reuse the existing checkpoint, don't reboot

gem5 checkpoints (`m5.cpt` + `.pmem`/disk-cow files) capture only
architectural state -- registers, memory contents, device state -- not
CPU timing-model state. This project's existing checkpoint workflow
already relies on that: `x86_fs_classic_save_ckpt*.py` boots with
`CPUTypes.ATOMIC` (fast, functional-only) and checkpoints just before the
ROI; `x86_fs_mesi3_parallel_eventq.py` then restores that same checkpoint
into a completely different config (`MESIThreeLevelCacheHierarchy` +
`CPUTypes.TIMING`) for the actual measured run. Swapping the ROI CPU to
O3 is the same pattern, one more step removed -- restore the *same*
S-013 balanced checkpoint (`x86-threads-balanced3-roi-classic`) into an
O3 core config. No new multi-hour boot is needed, unlike S-013's own
checkpoint (which *did* need a new boot, because it changed the
benchmark binary baked into the guest disk image before the checkpoint
point -- that's a guest-content change, which O3 vs. Timing is not).

## 3. Outstanding-request audit (why 32, not just O3 by itself)

Before wiring in O3, checked whether anything else in the current
restore config would silently cap the concurrency O3 is supposed to
add. Two parameters govern this:

- `RubyController.number_of_TBEs` (`src/mem/ruby/slicc_interface/
  Controller.py:64`), default 256 -- per-controller (L1/L2/L3/directory)
  transaction table. Confirmed via the `MESI_Three_Level-L{0,1}cache.sm`
  SLICC source (`TBETable TBEs, ..., constructor="m_number_of_TBEs"`)
  that this is the parameter backing each controller's TBE table.
  Generous, not a realistic constraint at this core count.
- `RubyController.buffer_size`, default 0 (unbounded) -- message buffer
  depth between controllers. Not a constraint either.
- `RubySequencer.max_outstanding_requests` (`src/mem/ruby/system/
  Sequencer.py:96`), default **16** -- "max requests (incl. prefetches)
  outstanding" per core. This is the one real constraint: neither
  `mesi_three_level_cache_hierarchy.py` nor either
  `x86_fs_mesi3_parallel_eventq*.py` script has ever overridden it, so
  every core's `RubySequencer` (constructed in `.../caches/
  mesi_three_level/l1_cache.py`) has been running at this stdlib
  default the whole time this project has existed.

Cross-checked against O3's own queue sizing (`src/cpu/o3/
BaseO3CPU.py`): default `LQEntries` = `SQEntries` = 32 each. The
sequencer's cap of 16 is *tighter* than O3's LSQ -- so even with default
O3 settings, the sequencer would be the first thing to throttle
concurrent outstanding misses per core, undercutting the whole point of
switching to O3. Decision: raise `max_outstanding_requests` to 32,
matching the LSQ size, so O3's own queue occupancy (not an
un-examined stdlib default) is what limits concurrency. `TimingSimpleCPU`
never had more than one request in flight regardless of this cap, so
raising it has no effect on the existing Timing-CPU scripts/results this
project's history has already measured -- but changing the *shared*
`x86_fs_mesi3_parallel_eventq.py` (the Timing-CPU script used by S-009
through S-017) would still touch every past reproduction command, so the
bump was scoped to the new O3-only script rather than the original,
per this project's existing practice of copying scripts for new
variants (S-013 §4's `_balanced` checkpoint script) rather than editing
shared ones out from under earlier specs.

## 4. Implementation

New file `docs/refs/scripts/x86_fs_mesi3_parallel_eventq_o3.py`, a copy
of `x86_fs_mesi3_parallel_eventq.py` (unchanged: domain map, LINK_LATENCY,
SIM_QUANTUM_TICKS default, cache geometry, barrier/critpath knobs) with:

- `processor = SimpleProcessor(cpu_type=CPUTypes.O3, ...)` instead of
  `CPUTypes.TIMING`.
- A new `MAX_OUTSTANDING_REQUESTS = 32` module constant, applied in
  `ParallelX86Board._pre_instantiate()` right after the existing
  `LINK_LATENCY` loop and *before* the `if not PARALLEL_EVENTQ: return
  root` branch -- i.e. applied identically in both serial and parallel
  modes, same as `LINK_LATENCY`, so the two arms remain the same
  simulated machine (module docstring's existing "diff the two
  stats.txt" validation contract still holds).
- `[parallel-eventq]` log prefixes changed to `[parallel-eventq-o3]` to
  distinguish O3 runs from Timing-CPU runs in shared logs/history.

`CHECKPOINT_DIR` usage is unchanged -- pointing it at
`/workspace/gem5-ckpt/x86-threads-balanced3-roi-classic` (S-013's
checkpoint) is expected to just work, since the checkpoint itself never
encodes a CPU timing model. `python3 -m py_compile` passes; gem5-specific
imports (`m5`, `gem5.*`) aren't checkable outside `gem5.opt`'s embedded
interpreter, so this is a syntax check only, not a functional one.

## 5. Not done yet

- No build. `X86_MESI_Three_Level` needs (re)building in this worktree's
  tmpfs `build/` before anything can run; unclear yet whether O3 is
  already compiled into an existing build this worktree could reuse, or
  whether O3 is even gated by any Kconfig option in this build target
  (`build_opts/X86_MESI_Three_Level` doesn't obviously exclude it, but
  this hasn't been confirmed by an actual build attempt).
- No verification that O3 actually restores cleanly from a checkpoint
  that was saved via `CPUTypes.ATOMIC` and has only ever been restored
  into `CPUTypes.TIMING` before now -- O3's pipeline-state
  initialization on restore/takeover is a plausible new failure surface
  that Timing-CPU restores wouldn't have exercised.
- No short-window sanity run (the S-009 §27 / S-014 §8 style
  `MAX_TICKS`-bounded smoke test) to confirm the restore boots, runs,
  and exits cleanly before committing to any real-window measurement.
- No measurement of whether O3 + the 32-outstanding-request bump
  actually produces more concurrent cross-domain traffic than the
  Timing-CPU config did (e.g. via S-012's critical-path instrumentation)
  -- §3's reasoning is a plausibility argument from stdlib defaults, not
  an observed result yet.

Per this project's checkpoint-before-risky-step convention (`CLAUDE.md`
"Working style"), the build + first restore attempt is a new,
qualitatively riskier phase (first O3 use in this project, first restore
of an Atomic-booted checkpoint into anything other than Timing) and
should be checked in on before running, not started unattended.

---

**Previous**: [S-017: balanced-bounded three-arm hostSeconds](./S-017-balanced-bounded-three-arm-hostseconds.md)
**Return**: [INDEX.md](./INDEX.md)
