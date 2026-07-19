# S-018 — O3 CPU restore onto the balanced checkpoint (in progress)

**Status: builds and restores cleanly; smoke test surfaced a real
serial-vs-parallel stats divergence that is O3-specific (does NOT
reproduce with the unmodified TimingSimpleCPU script at the same tiny
window) -- root cause not yet found, see §6.** This session wrote
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

## 5. Build

`taskset --cpu-list 0-53,56-91 scons build/X86_MESI_Three_Level/gem5.opt
-j 88` (constrained to the unreserved host cores per `CLAUDE.md`, since a
different session's long-running S-016/S-017-family three-arm job was
live on the isolated cores at the time -- confirmed via `taskset -pc` on
its PIDs, pinned to 54/55/92, before starting this build). Clean full
build from an empty tmpfs `build/`, no errors -- O3 is compiled into
`X86_MESI_Three_Level` with no Kconfig changes needed, confirming §5's
open question from the pre-build version of this section.

## 6. Smoke test: restore works, but surfaced a real O3-specific stats divergence

Protocol: `CHECKPOINT_DIR=x86-threads-balanced3-roi-classic`,
`SIM_QUANTUM_TICKS=6660`, `MAX_TICKS=10000000` (10M ticks -- deliberately
tiny, just to test "does it restore and run at all" before spending any
real wall-clock time), serial (`PARALLEL_EVENTQ=0`, `taskset -c 10`) and
parallel (`PARALLEL_EVENTQ=1`, `HOST_PIN_CPUS=11..18`) arms, both against
the new `x86_fs_mesi3_parallel_eventq_o3.py`.

**Both arms restore and exit cleanly, no crash.** `hostSeconds` ~1.15s
(serial) / ~1.19s (parallel); both hit the `MAX_TICKS` bound and dumped
stats normally. `simInsts`/`simOps` match exactly between the two arms
(5481/10892). In this tiny window only core 2 (of 4) committed any
instructions in either arm -- consistent with S-013 §9's finding that
short windows on this checkpoint mostly cover guest thread-startup
serialization, not a new problem. This answers §5's (renumbered from the
pre-build version) main open question: **O3 does restore correctly from
an Atomic-booted checkpoint that has only ever been restored into Timing
before** -- no new failure surface there.

**But a full `diff` of the two `stats.txt` (excluding `host*` lines)
found a real divergence**, not present anywhere else in either file:

```
< board.cache_hierarchy.ruby_system.network.int_links102.buffers1.m_buf_msgs     0.109890
> board.cache_hierarchy.ruby_system.network.int_links102.buffers1.m_buf_msgs 1844674222903.647949
< board.cache_hierarchy.ruby_system.network.int_links104.buffers0.m_buf_msgs     0.056610
> board.cache_hierarchy.ruby_system.network.int_links104.buffers0.m_buf_msgs 3689348445807.124512
```

(`<` = serial, `>` = parallel.) `m_buf_msgs` (`src/mem/ruby/network/
MessageBuffer.hh:299`) is a `statistics::Average` -- a time-weighted
average of buffer occupancy, incremented/decremented at message
enqueue/dequeue (`MessageBuffer.cc:316,361`) and integrated against
elapsed ticks at dump time. The two bogus parallel-arm values are ~1.8e12
and ~3.7e12 -- roughly a 2x multiple of each other, and in the same
order of magnitude as `finalTick` (5.3e12), which smells like a
tick-arithmetic bug (e.g. sampling `curTick()` from the wrong domain's
`EventQueue` when computing the elapsed-time denominator) rather than
random corruption, but this is a first impression from the numbers, not
a traced root cause.

**Control test, to isolate scope**: reran the exact same protocol
(same checkpoint, same `MAX_TICKS`/`SIM_QUANTUM_TICKS`, same host pins)
against the **unmodified** `x86_fs_mesi3_parallel_eventq.py`
(`CPUTypes.TIMING`, no `max_outstanding_requests` override) on the same
freshly-built binary. Result: **`diff` of the two stats.txt is empty --
byte-for-byte identical, zero divergent lines.** This rules out "short
windows on this checkpoint are just generally prone to this stat glitch,
nobody tested small windows before" -- the exact same tiny-window
protocol is clean on Timing and dirty on O3. The divergence is specific
to something in the O3 script: the O3 CPU model itself, the
`max_outstanding_requests=32` bump, or (most likely) the combination
producing coherence/message traffic shaped differently enough to hit an
existing gap that Timing-CPU's low concurrency never reached. Not yet
isolated which.

**Isolation test, to separate O3 from the concurrency bump**: reran the
identical protocol against a scratch copy of the O3 script
(`/tmp/s018-o3-default16.py`, not committed) with
`MAX_OUTSTANDING_REQUESTS` changed from 32 back to the stdlib default
(16) -- everything else (O3 CPU, checkpoint, quantum, host pins)
unchanged. Result: **the divergence still reproduces**, ruling out the
`max_outstanding_requests` bump as the cause -- this is O3 itself (or
O3's interaction with the existing default-16 sequencer cap), not
something the §3 tuning introduced. Two details worth noting rather than
smoothing over: the bogus values are **not the same as the 32-request
run** (`int_links102.buffers1` reads 9223371114517.761719 here vs.
1844674222903.647949 at 32 requests), and this run has **three**
divergent lines instead of two (`int_links79.buffers2` newly appears,
value 3689348445807.183105 -- suspiciously close to but not identical
to the 32-request run's `int_links104.buffers0` value,
3689348445807.124512). The bogus values scale with something about the
run rather than being a fixed sentinel/constant, and which specific
buffers are affected isn't stable across configs either -- both facts
argue for tracing the actual tick-arithmetic rather than guessing further
from the numbers.

## 7. Not done yet

- Root cause of §6/§6.1's divergence -- not traced, despite ruling out
  the concurrency bump. Leading candidate now: an existing cross-domain
  tick-sampling gap in `statistics::Average`/`MessageBuffer` that only
  O3's traffic pattern (rather than TimingSimpleCPU's) reaches, in the
  same family as this project's long history of cross-domain-read bugs
  (S-009 through S-016) -- consistent with the bug being O3-triggered but
  not O3-caused, i.e. latent in shared `xbar.cc`/`MessageBuffer` code
  this whole project rather than newly introduced, just never triggered
  by Timing-CPU's concurrency-1 access pattern before. Not confirmed --
  worth keeping in mind before assuming it's O3-script-local, since a
  fix would then belong in shared code rather than this spec's own
  script.
- No real-window run of any kind yet -- everything so far is the 10M-tick
  smoke test. S-013 §9's own lesson (a 2e8-tick window was still
  boot-phase) suggests this smoke window is nowhere near representative;
  no conclusions about O3 exposing more parallel-EventQueue benefit
  should be drawn until §6's divergence is understood (a stats
  correctness bug undermines trusting any throughput number from this
  config) and a real window is run.
- No measurement of whether O3 + the concurrency bump actually produces
  more concurrent cross-domain traffic than Timing-CPU did (e.g. via
  S-012's critical-path instrumentation) -- still just a plausibility
  argument from stdlib defaults (§3), now additionally clouded by not
  yet knowing whether §6's divergence affects the traffic-shape metrics
  that would answer this.

Per this project's checkpoint-before-risky-step convention (`CLAUDE.md`
"Working style"), root-causing §6's divergence is new, unexplored
territory (first real bug found in this project's first-ever O3 use, no
existing design doc or fix pattern obviously covers it) and should be
checked in on before diving in further, not pursued unattended.

---

**Previous**: [S-017: balanced-bounded three-arm hostSeconds](./S-017-balanced-bounded-three-arm-hostseconds.md)
**Return**: [INDEX.md](./INDEX.md)
