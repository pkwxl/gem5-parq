# Measure FS parallel-vs-serial speedup at the current operating point

## Context

The project's goal is real wall-clock speedup over the serial single-EventQueue
simulator. S-007 §12.6 measured FS **spin vs cv** (1.41x) on a 2e8-tick bounded
window, but never measured **parallel vs serial** — the number the goal is
actually defined by. SE-side we know 0.90x (near parity, S-007 §12.3); FS-side
the serial arm of the same window has simply never been run. The user asked for
exactly this: "we should know the speedup vs serial at the current position."

Two new facts feed this session:
- **The FS full-ROI serial reference finished**: `/tmp/fs3-restore-serial/stats.txt`
  has simTicks=210050075139858, hostSeconds=16851.86, simInsts=3278585986. This
  closes the "serial reference TBD" item from S-006/S-007 and must be banked
  into the docs before it gets lost (/tmp).
- The FS script header states the quantum is capped at 300 ticks by the
  **classic iobus (IOXBar) edge = 1 board cycle = 333 ticks @3GHz**, while the
  Ruby links would legally allow ll=20 × 333 ≈ 6667 ticks. That 22x quantum gap
  is the designated next lever (S-004 §9.3 path 1), but it is *not* this
  session's implementation work — this session produces the measurement that
  tells us how big the remaining gap actually is.

## Phase 0 — housekeeping (fast)

1. **Commit the pending doc-split working tree** (all changes are the
   monolithic-doc → `docs/specs/S-NNN` split): staged deletion of
   `docs/specs/parallel-eventq-lockfree-l2-design.md`, untracked `docs/specs/`
   (INDEX.md + S-001..S-007), updated section references in code comments
   (`src/arch/x86/tlb.hh`, `src/mem/page_table.hh`, `src/mem/ruby/common/Consumer.hh`,
   `src/mem/ruby/network/MessageBuffer.{cc,hh}`, `src/mem/ruby/system/RubySystem.cc`,
   `src/mem/ruby/system/Sequencer.hh`, `src/sim/process.hh`), config-script
   docstrings (`configs/deprecated/example/se.py`, `docs/refs/scripts/x86_fs_mesi*_parallel_eventq.py`),
   and the CLAUDE.md "Primary research goal" section. One or two `docs:`-tagged
   commits (code-comment refs + configs can ride along; no behavior changes).
2. **Delete `build/X86_MESI_Three_Level/gem5.opt.serialref`** (954MB) — the
   serial reference run that held its inode has exited (no gem5 processes
   running).
3. **Copy the full-ROI serial reference stats** out of `/tmp` (e.g.
   key lines into the doc written in Phase 2; optionally stash stats.txt under
   the scratchpad or docs/refs if the user wants the raw file kept).

## Phase 1 — the measurement (the point of this session)

FS bounded-window A/B, **serial vs parallel-spin**, same binary
(`build/X86_MESI_Three_Level/gem5.opt`, already has spin + pythonDump fix),
same checkpoint (`CHECKPOINT_DIR=/workspace/gem5-ckpt/x86-threads3-roi-classic`),
same window as 12.6 so results compose with the existing cv/spin numbers:

- Common: `MAX_TICKS=200000000`, script defaults `LINK_LATENCY=20`,
  `SIM_QUANTUM_TICKS=300`, driver
  `docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`.
- Serial arm: `PARALLEL_EVENTQ=0`, pinned to one of the same cores
  (`HOST_PIN_CPUS=8` — single queue, queue 0 gets pinned) for fairness.
- Parallel arm: `PARALLEL_EVENTQ=1 HOST_PIN_CPUS=8,9,...,15
  EVENTQ_BARRIER_MODE=spin`.
- **Hard time budget (user requirement): the whole experiment ≤ 15 min.**
  Expected total is ~5–10 min. The serial arm's duration is the only unknown
  (spin is ~40s, cv ~57s from 12.6). So: run **one serial probe first** at
  MAX_TICKS=2e8. If it finishes in ≤ ~2 min, keep the 2e8 window (composes
  directly with 12.6's cv/spin medians). If it runs long, kill it at the
  2-min mark, **shrink the window** (1e8 or 5e7) and run BOTH arms fresh at
  the smaller window — speedup is a ratio, so composability with 12.6 is
  nice-to-have, not required (the spin arm is cheap to re-run at any window).
  Window sizing keeps every individual run under ~2 min and the full set of
  runs inside the 15-min cap.
- **2 interleaved rounds** (AB AB), not 3 — enough to catch host-load drift
  for a ratio this coarse; median/mean of 2 per arm. Distinct `-d /tmp/...`
  outdirs per run.
- Do NOT send SIGUSR1/SIGUSR2 to the parallel runs (S-007 §14).

Correctness read-outs alongside the timing:
- Parallel simInsts should reproduce 74062 (12.6's value). Serial simInsts on
  the same window: if it differs from 74062, that is a real finding (quantifies
  the Q=300 relaxation against true serial timing) — record it plainly either way.

Then the gap arithmetic (S-004 §9.3 style, now with a measured serial W):
- speedup = W_serial / W_spin at Q=300;
- project what raising Q to the Ruby-legal ~6600 (iobus edge fixed) would give:
  barrier count drops 22x (6.67e5 → ~3e4 syncs), spin cost ~3µs/quantum;
- state whether the >1x / 3x goal is reachable from measured numbers with
  path-1 (bigger Q) alone or needs path-3 (more work per domain) too.

## Phase 2 — write it down (same session, per project working style)

- New **`docs/specs/S-008-fs-serial-vs-parallel-current-position.md`** (INDEX
  numbering convention: next free number, own file even though it completes
  12.6's table): the A/B table (serial / cv / spin / hybrid — cv+hybrid medians
  imported from 12.6), the simInsts comparison, the banked full-ROI serial
  reference numbers, and the gap arithmetic.
- Add the S-008 row to `docs/specs/INDEX.md` and update its 现状 paragraph
  (serial-reference TBD item now resolved; current measured speedup vs serial).
- Update memory (`parallel-eventq-l2-project-status.md`) with the outcome.
- Commit docs.

## What comes after (not this session — checkpoint with the user first)

The measurement decides the framing, but the next implementation lever is
already identified: **S-009, raise the FS quantum ceiling past the classic
iobus edge** (extend the 11.5 grid-anchored snap / 8.8-style kick to the
CPU↔iobus classic cross-domain edge), unlocking Q≈6600. Per the project's
collaboration style this is new-architecture territory → offer a checkpoint
and a design writeup before implementing.

## Verification

- Phase 0: `git status` clean afterwards except intended untracked leftovers;
  serial ref numbers appear verbatim in S-008.
- Phase 1: every run exits at MAX_TICK with a stats.txt; parallel arms show
  ~8 threads / >300% CPU; medians computed from 3 rounds; simInsts recorded
  for every run.
- Phase 2: INDEX table renders with S-008 row; links resolve; committed on
  `main` with `docs:`/`configs,docs:` tags per convention.
