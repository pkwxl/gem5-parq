# S-013 — Balanced four-core workload + new checkpoint (in progress)

**Status: new benchmark + new checkpoint built and verified against
S-012's critpath protocol -- but the fix did NOT balance the four cores.**
The imbalance persists at essentially the same magnitude, just on a
different domain (domain 3 instead of domain 1). §7 reports this plainly,
and a determinism re-run (same checkpoint, repeated) shows domain 3 wins
reproducibly (~51-53% both times) rather than by chance -- ruling out a
pure per-run-chaos explanation, but leaving the actual structural cause
unidentified. §8 lists next-step options; none taken yet, pending a
decision. This is a direct follow-on to
[S-012](./S-012-eventq-critical-path-instrumentation-design.md) §13.5's
open question ("why is domain 1 / core 0 specifically the last-arriver"),
filed as its own spec per the `INDEX.md` numbering convention rather than
appended to S-012's body.

## 1. Problem

S-012 Step 3 turned on critical-path tracing over the checkpoint
`/workspace/gem5-ckpt/x86-threads3-roi-classic` (the `threads 200000 1`
benchmark) and found domain 1 (core 0's private L1+L2+local APIC) is the
last-arriving domain at 50.4%/47.5% of quantum barriers (pass 1/pass 2) --
10-20x more often than domains 2/3/4 (cores 1-3), and its `eventCount` when
last is ~27x theirs (S-012 §13.4-13.5). This refuted S-012 §2.3's original
guess that shared L3/directory domains would dominate, and S-012 explicitly
left "why domain 1 specifically" unanswered as out of its scope.

That imbalance makes this checkpoint unsuitable as a control baseline for
S-012 Step 4/5 (lock-wait instrumentation) or for the "outlet 3" (put more
work per domain) design work that motivated S-012 in the first place: any
future critical-path data measured on it will keep showing "core 0 is the
bottleneck" regardless of what outlet-3 change is tried, because the
bottleneck is baked into the benchmark's shape, not the coherence topology.

## 2. Root cause (read from source, no new instrumentation needed)

`tests/test-progs/threads/src/threads.cpp` (the benchmark baked into the
current checkpoint, via `x86_fs_classic_save_ckpt.py`):

- `main()` serially allocates and initializes all three arrays
  (`num_values` = 200,000 elements) **before** spawning any thread.
- It spawns only `cpus - 1` worker threads for `array_add`; the **last**
  thread's share is run directly by the main thread itself (comment: "-1
  is required for this to work in SE mode" -- a leftover SE-mode
  constraint that no longer applies now that the project is FS-mode-only,
  S-006).
- After `join()`, it serially validates all 200,000 elements on the main
  thread.
- There is no explicit thread-to-vCPU affinity anywhere -- which guest
  core ends up hosting which `array_add` share (and which domain that maps
  to) is left entirely to the guest scheduler.

Only `array_add` is actually parallel (50,000 elements/thread with
`chunk_size=1`). Whichever core hosts the process's original (non-spawned)
thread context -- overwhelmingly likely core 0, since that's where the
process starts and where thread creation happens -- ends up doing the two
big serial loops (400,000 element-ops total) *plus* its own 50,000-element
share = 450,000 element-ops, versus 50,000 for each of the other three
threads. That ~9x ratio in raw element-level work is the same order of
magnitude as the ~10-27x skew S-012 §13.4-13.5 measured, and fully explains
it without needing a coherence-topology explanation. This is a
workload-shape artifact of the benchmark, not a finding about the parallel
EventQueue design.

## 3. Fix: `threads_balanced.cpp`

New file `docs/refs/scripts/threads_balanced.cpp` (kept as a sibling of
the existing `docs/refs/scripts/work.c` scratch benchmark -- deliberately
not touching `tests/test-progs/threads/`, which is upstream gem5 test
fixture territory, not this fork's scratch space).

Same CLI (`threads_balanced [num_values] [chunk_size]`), same
`chunk_size=1` default (worst-case false-sharing, element-interleaved --
kept identical to the current checkpoint so the *only* variable being
changed is workload shape, not sharing intensity or size), but:

- All three phases -- init (`a[i]=i; b[i]=num_values-i; c[i]=0`),
  compute (`array_add`), and validate (`c[i]==num_values`) -- use the
  *same* block-cyclic partition (`for_each_chunk`, factored out of the
  original `array_add`'s loop shape) so each thread only ever touches its
  own index range across all three phases. No new inter-thread
  synchronization is needed between phases: a thread's own init always
  precedes its own compute/validate because they're sequential in that
  thread's code, and no thread ever reads another thread's partition.
- All `cpus` threads (not `cpus - 1` + main) are spawned as real workers;
  the main thread only spawns/joins/prints, doing negligible work. This
  drops the SE-mode-only workaround since S-006 moved the whole project to
  FS mode.
- Each worker's first action is `pthread_setaffinity_np` pinning itself to
  guest CPU `tid` (`<pthread.h>`/`<sched.h>`, `CPU_ZERO`/`CPU_SET`),
  matching the domain map's domain-`(i+1)` = core-`i` convention (S-012
  §2.3/§13.1) -- this makes the domain<->core mapping deterministic
  instead of left to guest scheduler placement, removing a second source
  of run-to-run noise on top of the serial-phase fix.
- Validation count uses `std::atomic<int> num_valid` (`fetch_add` of each
  thread's local tally), avoiding a serial reduction.

**Native sanity check** (host build, not through gem5, before spending any
gem5 boot time): built with `g++ -std=c++11 -pthread -O2 -o
threads_balanced threads_balanced.cpp`, run with `taskset -c 0,1,2,3
./threads_balanced 200000 1` -> `Validating...Success!`, exit 0. Confirms
the partition/pinning/atomic-counter logic is correct before committing to
the hours-long checkpoint boot.

**Static binary** for staging into the guest, built with the exact
existing recipe (`x86_fs_mesi3_save_ckpt.py`'s documented flags): `g++ -o
threads_balanced.static threads_balanced.cpp -pthread -std=c++11 -static
-static-libgcc -static-libstdc++ -O2`. Also confirmed
`Validating...Success!` natively. Kept at
`/workspace/gem5-bin/threads_balanced.static` -- a new sibling directory
to `/workspace/gem5-ckpt`/`/workspace/gem5-resources` for this project's
compiled scratch artifacts, deliberately outside the git tree (this repo's
`docs/refs/scripts/` convention only tracks source, e.g. `work.c` has no
committed compiled `work` binary either).

## 4. New checkpoint

New save-checkpoint script `docs/refs/scripts/x86_fs_classic_save_ckpt_balanced.py`,
a copy of `x86_fs_classic_save_ckpt.py` (same classic NoCache + Atomic
method, same `NUM_CORES=4`/`DualChannelDDR4_2400` board config -- must
still match the restore config) changing only:

- `STATIC_BIN` default -> `/workspace/gem5-bin/threads_balanced.static`.
- `CKPT_DIR` default -> `/workspace/gem5-ckpt/x86-threads-balanced3-roi-classic`.
- `BENCH_NVAL`/`BENCH_CHUNK` defaults unchanged (`200000`/`1`).

Launched in the background (this sandbox's copy of the same classic
NoCache Atomic boot that produced the current checkpoint took ~5.5h
wall-clock per S-006 §11.1, not the script comment's estimated "minutes" --
same expectation applies here, no reason to assume this run will be
faster):

```
GEM5_RESOURCE_DIR=/workspace/gem5-resources \
CKPT_DIR=/workspace/gem5-ckpt/x86-threads-balanced3-roi-classic \
./build/X86/gem5.opt -d <scratch-outdir> \
    docs/refs/scripts/x86_fs_classic_save_ckpt_balanced.py
```

Started 2026-07-16 15:58:32, finished 2026-07-16 21:40:43 (host time) --
**~5h42m wall-clock**, consistent with (slightly longer than) S-006 §11.1's
~5.5h precedent for this same boot method; not the faster run one might
hope for, no surprise given it's the same classic-NoCache-Atomic full
Ubuntu+systemd boot. Exited cleanly at the pre-ROI `m5_hypercall`
instruction (the scripted `m5 exit`) and wrote the checkpoint:

```
/workspace/gem5-ckpt/x86-threads-balanced3-roi-classic/
  m5.cpt                                    383,190 bytes
  board.physmem.store0.pmem              86,913,776 bytes
  board.pc.south_bridge.ide.disks.image.cow 3,623,384 bytes
```

Same file set as the original `x86-threads3-roi-classic` (no `ruby_system`
section, as expected for classic NoCache -- S-006 §11.8's load-bearing
reason this checkpoint style is restorable into the parallel Ruby config
at all).

## 5. Known open item: uncommitted Step-4-shaped WIP in the working tree

While preparing this work, `git status` showed uncommitted modifications
to exactly the files S-012 §4.1 designs for **Step 4** (lock tagging):
`src/base/uncontended_mutex.hh`, `src/base/critpath_trace.{hh,cc}`,
`src/mem/xbar.hh`, `src/dev/io_device.hh`, `src/mem/physical.hh`,
`src/mem/ruby/common/Consumer.hh`, `src/sim/simulate.cc`, `src/sim/root.cc`,
`src/sim/Root.py` -- none of this is committed (`HEAD` is still
`cb91748e71`, S-012 Step 3), and S-012's own status line still says Step 4
"not yet done". File mtimes show these edits (15:46) predate the currently
built `build/X86_MESI_Three_Level/gem5.opt` (15:53), so **that binary
already includes this uncommitted, unverified WIP** -- it has not had the
TSan/correctness regression S-012 §9 requires before Step 4's changes can
be trusted.

This doesn't block anything in §1-4 above (those only use
`build/X86/gem5.opt`, a different, older build unaffected by this WIP).
It does matter for §6 (verification): using the current
`X86_MESI_Three_Level` build would mean the new checkpoint's critpath
histogram is measured against unverified Step-4 code rather than the clean
Step-3 baseline S-012 §13.4 was measured on, contaminating the comparison.
**Not resolved yet -- needs a decision (most likely: build
`X86_MESI_Three_Level` from the clean committed `cb91748e71` state, e.g.
via `git worktree` or `git stash`, rather than reusing the current binary)
before running §6.**

## 6. Resolving §5: clean build for the verification run

Rather than risk losing the uncommitted Step-4 WIP (or rebuilding it away
by accident), the WIP was moved aside with a *path-scoped* `git stash push
-- <files>` (only the specific files S-012 §4.1 touches, not the whole
tree -- this session's own new/edited files were left alone), confirmed
`git diff HEAD` was empty for those files (working tree == committed Step-3
state `cb91748e71`), then `X86_MESI_Three_Level` was rebuilt from that
clean state (`taskset --cpu-list 0-53,56-91 scons ... -j32`, constrained to
the unreserved host cores per `CLAUDE.md`). The rebuild triggered a large
recompile (`eventq.hh`-adjacent headers touch much of the tree) but
finished cleanly with only the usual missing-optional-library warnings.
The WIP was then restored with `git stash pop`.

One more provenance issue surfaced during this: `docs/refs/scripts/
x86_fs_mesi3_parallel_eventq.py` (not stashed initially, since it isn't a
compiled file) unconditionally sets `root.critpath_trace_reserve =
EVENTQ_CRITPATH_RESERVE` -- a WIP-only `Root` param that a clean-Step-3
`Root.py` doesn't declare, which would have raised an `AttributeError`
against the freshly-rebuilt clean binary. Stashed and restored this one
file the same way, scoped to just this run.

`docs/refs/scripts/critpath_aggregate.py`'s WIP additions (Step 4's
lock-wait summary) were left in place unstashed -- they only add handling
for `kind == "lockwait"` CSV rows and degrade gracefully ("no lockwait
records found") when a trace has none, which is exactly what a clean-Step-3
run produces, so they don't affect the pass-1/pass-2 barrier histogram this
verification needed.

## 7. Verification run: the fix did NOT balance the four cores

Ran the exact S-012 §13.2-13.4 operating point (clean-rebuilt
`X86_MESI_Three_Level/gem5.opt`, `docs/refs/scripts/
x86_fs_mesi3_parallel_eventq.py` at its clean committed state,
`SIM_QUANTUM_TICKS=6660`, `EVENTQ_BARRIER_MODE=spin`,
`HOST_PIN_CPUS=92,93,94,95,96,97,98,99`, `MAX_TICKS=2e8`,
`EVENTQ_CRITPATH_TRACE=1`), swapping only
`CHECKPOINT_DIR=/workspace/gem5-ckpt/x86-threads-balanced3-roi-classic`.
Exit 0, no assert/panic/segfault, all 8 `critpath-domain*.csv` produced.
`docs/refs/scripts/critpath_aggregate.py` output:

```
=== barrier pass 1 (quantum-local events done) -- 30030 quanta ===
  domain   last-arriver count    share   avg eventCount when last
       3                15375    51.2%                       26.9
       5                 4508    15.0%                        6.5
       6                 3227    10.7%                        4.4
       7                 2639     8.8%                        4.8
       4                 1289     4.3%                        1.0
       2                 1144     3.8%                        1.0
       1                  962     3.2%                        1.0
       0                  886     3.0%                        1.0

=== barrier pass 2 (global event body (domain 0 only)) -- 30030 quanta ===
  domain   last-arriver count    share   avg eventCount when last
       3                13546    45.1%                        0.0
       5                 4595    15.3%                        0.0
       ...
```

This is essentially the **same shape** as S-012 §13.4's original result
(one core-private domain dominant at ~50%, ~27x `eventCount` when last,
the rest at low single digits) -- **just relabeled**: domain 3 (core 2)
now, not domain 1 (core 0). Parallelizing all three phases and pinning
each thread to its vCPU did not remove the imbalance; it moved which
domain the imbalance lands on. This directly contradicts §3's design intent
and must be reported as such, not glossed over.

**A clue from the time-series, not just the aggregate**: bucketing domain
3's pass-1 rows into deciles by tick order shows the skew is not constant
through the window --

```
decile 0: avg_eventCount=12.7 times_last=1404/3003 (46.8%)
decile 1: avg_eventCount= 8.4 times_last=1019/3003 (33.9%)
decile 2: avg_eventCount=11.5 times_last=1280/3003 (42.6%)
decile 3: avg_eventCount=12.3 times_last=1361/3003 (45.3%)
decile 4: avg_eventCount=12.6 times_last=1385/3003 (46.1%)
decile 5: avg_eventCount= 7.7 times_last= 966/3003 (32.2%)
decile 6: avg_eventCount=11.0 times_last=1256/3003 (41.8%)
decile 7: avg_eventCount=13.0 times_last=1410/3003 (47.0%)
decile 8: avg_eventCount=27.2 times_last=2660/3003 (88.6%)
decile 9: avg_eventCount=26.8 times_last=2634/3003 (87.7%)
```

Domain 3 is already the *most frequent* last-arriver from the very start
(consistently the plurality across deciles 0-7, ~33-47%, well above the
~25% four-way-even baseline), but it isn't overwhelming until the final
20% of the window, where it jumps to ~88% and `eventCount` roughly
doubles. This pattern -- a persistent early lean that snowballs into
near-total dominance later -- is not what "four symmetric partitions of
equal-sized independent work" should produce if the partition itself were
the only variable in play (each thread's fill/add/validate only ever
touches its own index range, confirmed by construction in §3).

**Determinism check (done, cheap -- re-ran the identical experiment,
same checkpoint, no new boot)**: repeated the exact same verification run
against the same checkpoint. Domain 3 won again, at a near-identical
share: 52.6% vs. the first run's 51.2% (pass 1), `eventCount` 26.2 vs.
26.9. This **rules out a pure per-run chaos/coin-flip explanation** --
if the dominance were the product of a butterfly-effect race with no
structural cause, repeating the identical experiment should have had a
real chance of landing on a different domain, or at least a visibly
different share. Getting the same domain at essentially the same
percentage twice in a row points to something **structural and
reproducible** about this specific checkpoint + `threads_balanced` binary
+ guest-state combination -- not necessarily `chunk_size`-driven
coherence chaos as originally guessed in the first draft of this
hypothesis. What that structural cause actually is remains open: candidates
include the guest scheduler consistently favoring/disfavoring a specific
vCPU for reasons independent of this benchmark (kernel housekeeping,
ksoftirqd/RCU placement, whatever state the classic-NoCache-Atomic boot
left each vCPU in at the checkpoint point), or `pthread_setaffinity_np`
silently not taking effect for one specific thread in this guest kernel
build (its return value is not checked in `threads_balanced.cpp`, §3).
Distinguishing these needs guest-side diagnostics (e.g. instrumenting
`threads_balanced` to print which `sched_getcpu()` each thread actually
lands on, and/or checking `/proc/interrupts` at the checkpoint's guest
state) that go beyond gem5-side critical-path tracing -- not done here.

## 8. Status and open decision

This spec's original goal -- a checkpoint where the critical-path trace
shows all four cores roughly equally busy -- **has not been achieved**.
The new checkpoint and benchmark are real artifacts (correct, reproducibly
built, verified not to crash) but they do not solve the problem motivating
them, and the determinism check (§7) narrowed down *what kind* of problem
is left: not chaos, something structural and reproducible, cause not yet
identified. Further diagnosis needs guest-side visibility (e.g.
instrumenting `threads_balanced` to print `sched_getcpu()` per thread, or
checking the `pthread_setaffinity_np` return value it currently ignores)
that this session didn't build. This is exactly the kind of new, harder,
not-yet-understood sub-problem `CLAUDE.md` says to stop and check in on
rather than keep iterating on alone. Options for how to proceed, roughly
in order of effort:

1. Add a cheap diagnostic print to `threads_balanced.cpp` (each thread
   prints its `tid` and `sched_getcpu()`/affinity-call return value to
   stdout/console) and rebuild+re-checkpoint (~5-6h boot again) to see
   directly whether the pinning took effect and which vCPU each thread
   actually ran on -- would conclusively confirm or rule out the
   "affinity call silently failed" candidate from §7.
2. Try a `chunk_size` other than 1 in a new checkpoint (another ~5-6h
   boot) -- less likely to be the answer now that determinism is
   confirmed (a coherence-traffic-intensity explanation doesn't obviously
   predict the *same* domain winning twice), but still cheap to rule out.
3. Deprioritize achieving perfect four-core balance for now and move
   straight to S-012 Step 4 (lock-wait instrumentation) using the
   *existing* `x86-threads3-roi-classic` checkpoint, treating "one
   specific core reproducibly dominates" as a documented property of this
   experimental setup rather than something that must be fixed before
   Step 4 can proceed -- Step 4's lock-wait data may even help explain
   *why* domain 3 (or domain 1, on the original checkpoint) dominates,
   which would fold this open question back into Step 4 rather than
   needing a separate investigation.

No further action taken pending this decision.

## 9. Follow-up: extending the window hit a bigger, unrelated bug

The user asked whether the `MAX_TICKS=2e8` window (§7) was simply too
short to reach real steady-state behavior. Checking `simInsts` confirmed
it: only 74,588 total instructions executed in that window (1.49-1.52s
host time) -- nowhere near enough to complete the benchmark's ~600,000
element-ops across 4 threads (even at a generous 5-10 instructions per
element, that's 3-6M+ instructions before counting thread-creation
overhead). The window was almost certainly still inside guest
thread/kernel startup, not the parallel array computation at all -- which
means neither this session's domain-3 result nor S-012's original
domain-1 result can be confidently attributed to the benchmark's parallel
phase either; both may be artifacts of startup-phase scheduling.

Re-running with the tick cap removed (unbounded, run to the guest's own
`m5 exit`) to test this **crashed** -- a real
`assert(when >= getCurTick())` failure in `BaseXBar::Layer::occupyLayer`,
confirmed to also crash on the *original* (non-balanced) checkpoint at a
similar tick count, i.e. independent of anything in this spec's benchmark
work. This turned out to be a previously-undiscovered gap in S-009's
grid-anchored snap fix's coverage, with real implications for the
project's validated "Q=6660, 0.91x speedup" conclusion (S-009 §27's own
validation window was the same undersized `MAX_TICKS=2e8`). Full
writeup, root cause, and severity assessment: **[S-014](./S-014-occupylayer-crossdomain-crash-beyond-tested-window.md)**
-- not this spec's topic, filed separately per the numbering convention,
but noted here since it blocks any further attempt to get a longer,
more-representative critical-path window for the four-core-balance
question this spec is about. No further verification of S-013's own
question is possible until S-014 is resolved or worked around.

---

**Previous**: [S-012: EventQueue critical-path instrumentation design](./S-012-eventq-critical-path-instrumentation-design.md)
**Return**: [INDEX.md](./INDEX.md)
