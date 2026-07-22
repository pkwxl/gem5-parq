# S-018 — O3 CPU restore onto the balanced checkpoint (in progress)

**Status: builds and restores cleanly; smoke test surfaced a real
serial-vs-parallel stats divergence that is O3-specific (does NOT
reproduce with the unmodified TimingSimpleCPU script at the same tiny
window). Mechanism now identified in §6.2 -- an unsigned-Tick underflow
in `AvgStor::set()` from cross-domain `curTick()` skew; no fix applied
yet.** This session wrote
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

## 6.2 Reproduction after a bad-edit cleanup, and the actual mechanism

**Worktree cleanup first.** A later session left the worktree in a state
that could not build or run, and wrote a `TODO.md` planning around the
resulting symptoms as if they were real findings. For the record, none
of that `TODO.md`'s premises held:

- `x86_fs_mesi3_parallel_eventq_o3.py` had been edited (uncommitted) to
  replace the working restore path -- `obtain_resource("x86-ubuntu-24.04
  -boot-with-systemd", "5.0.0").get_parameters()` feeding `kernel`,
  `disk_image` **and `kernel_args`** -- with hardcoded
  `obtain_resource("x86-linux-kernel", "5.4.0")` /
  `("x86-ubuntu-24.04-img", "2024-01-01")` and **no `kernel_args`**. The
  `TypeError: set_kernel_disk_workload() missing 2 required positional
  arguments` that `TODO.md` records as "当前错误", and its
  highest-priority "resource version mismatch" blocker, were both
  artifacts of that half-finished edit. HEAD never asks for those
  resources. Dropping `kernel_args` would additionally have restored the
  checkpoint under different boot arguments than it was created with --
  a silent config divergence, worse than the loud `TypeError`.
- `src/base/stats/text.cc` had an edit containing a literal newline
  inside a string literal, i.e. the tree did not compile at all.
- `src/python/m5/{simulate.py,stats/__init__.py}` had a
  `postCheckpointRestore()` mechanism added on the theory that stats file
  streams go invalid across a checkpoint restore. There is no evidence
  for that: §6 already recorded both arms dumping stats normally, and the
  0-byte `stats.txt` files that motivated it came from runs reporting
  `Return code: -15` (SIGTERM) -- a killed process never reaches the exit
  dump. The driver `run_parallel_test.py` also silently dropped
  `SIM_QUANTUM_TICKS` (falling back to the script default of 300 rather
  than §6's 6660, ~18x more barrier syncs) and passed no `timeout=`.

All four files were reverted to HEAD; the discarded diff is preserved
outside the repo. `scons build/X86_MESI_Three_Level/gem5.opt` then
relinked cleanly (the binary had been stale).

**Reproduction.** Reran §6's protocol verbatim on the rebuilt binary
(`SIM_QUANTUM_TICKS=6660`, `MAX_TICKS=10000000`, serial `taskset -c 10`,
parallel `HOST_PIN_CPUS=11..18`). §6's numbers reproduce exactly:
`simInsts` 5481 / `simOps` 10892 identical across arms, `finalTick`
5303204840323, `hostSeconds` 1.15 serial vs 1.21 parallel -- i.e. the
parallel arm is still *slower* at this window. The `m_buf_msgs`
divergence also reproduces, again only in the parallel arm, this time on
four links (`int_links102.buffers1`, `int_links116.buffers1`,
`int_links128.buffers1`, `int_links79.buffers2`). Which links are
affected and how many keeps varying run to run, as §6.1 already noted.

**Mechanism.** The bogus values are not arbitrary. Every one observed so
far -- the four above plus both values recorded in §6 and §6.1 -- fits

    value = k * 2^64 / 10000001,   k in {1, 2, 3, 5}

with the denominator exactly 10000001 in all six cases. That denominator
is correct (the `Average` stat's elapsed-tick divisor for a 10M-tick
window), which **rules out §7's earlier leading candidate**: the problem
is not a wrong-domain `curTick()` in the denominator. The numerator has
overflowed by exactly k whole 2^64 units.

`m_buf_msgs` (`MessageBuffer.hh:299`) is a `statistics::Average`, backed
by `AvgStor` (`src/base/stats/storage.hh`), whose every update does:

```cpp
total += current * (curTick() - last);
last = curTick();
```

`Tick` is `uint64_t` and `last` is stamped by whichever domain last
touched the buffer. Under this project's relaxed cross-domain timing,
two domains sharing a `MessageBuffer` do not agree on `curTick()`: when
a domain that is *behind* the barrier enqueues or dequeues, `curTick() <
last`, and `curTick() - last` underflows to ~2^64 instead of a small
positive delta. It is then scaled by `current`, the buffer's occupancy at
that instant -- which is exactly the observed k. So k is not a corruption
count; it is how many messages were sitting in the buffer when a
backwards-in-time cross-domain update landed.

This is consistent with §7's framing that the bug is O3-*triggered* but
not O3-*caused*: `AvgStor` is shared upstream code, and TimingSimpleCPU's
concurrency-1 traffic simply never produced a cross-domain update on a
non-empty buffer at a lagging tick. It also puts this in the same family
as S-009 through S-016 (cross-domain reads of state stamped by another
domain's clock), and means a fix belongs in shared code, not in this
spec's own script.

Not yet decided: whether to fix this in `AvgStor` (clamp/skip when
`curTick() < last`), at the `MessageBuffer` call sites, or by giving
cross-domain-shared stats a domain-consistent tick source. Nothing has
been changed -- per `CLAUDE.md`'s working-style rule, that choice is a
checkpoint, not something to pick unattended.

## 7. Not done yet

- ~~Root cause of §6/§6.1's divergence~~ -- **traced in §6.2**: unsigned
  `Tick` underflow of `curTick() - last` in `AvgStor::set()`, driven by
  cross-domain `curTick()` skew, scaled by buffer occupancy. The
  hypothesis this bullet previously carried (a wrong-domain `curTick()`
  in the *denominator*) is wrong and has been retracted -- the
  denominator is exactly correct in all six observed cases. **No fix
  applied**; choosing where the fix belongs (`AvgStor` vs. the
  `MessageBuffer` call sites vs. a domain-consistent tick source for
  shared stats) is the open decision.
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
"Working style"): §6's divergence is now root-caused (§6.2), but
*fixing* it means touching shared upstream stats code (`AvgStor`) or the
cross-domain tick contract, which affects every stat in every past S-NNN
run, not just this spec's. That is the checkpoint -- it should be agreed
before any code changes, not pursued unattended.

---

**Previous**: [S-017: balanced-bounded three-arm hostSeconds](./S-017-balanced-bounded-three-arm-hostseconds.md)
**Return**: [INDEX.md](./INDEX.md)
