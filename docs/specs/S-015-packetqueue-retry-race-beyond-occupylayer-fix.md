# S-015 — `PacketQueue::sendDeferredPacket` non-deterministic crash,
found while confirming the S-014 `occupyLayer` fix

**Status: crash reproduced (7 of 28 identical runs pre-fix); a first fix
attempt (§1c, extending `layerLock` to cover `Layer::releaseEvent`'s
callback) was implemented, built, and verified against -- **and FAILED
verification**: 3-4 crashes in the first 6 post-fix bare reruns, the
same order of magnitude as (if not higher than) the pre-fix ~15-50%
rate. The `layerLock`/`releaseEvent` gap identified in §1b is real
(confirmed by code inspection) but is now understood to be, at most, a
partial/secondary contributor -- **not** the dominant mechanism. §1c
identifies a second, more direct, and likely-dominant gap that the
first fix did not touch: `QueuedResponsePort::recvRespRetry()` /
`QueuedRequestPort::recvReqRetry()` (`qport.hh`, generic `PacketQueue`
port-framework code used far beyond `NoncoherentXBar`) can be invoked
**directly** by a cross-domain peer's own thread (e.g. a core domain
calling `RequestPort::sendRetryResp()` on itself, which calls straight
into `_responsePort->recvRespRetry()` -> `PacketQueue::retry()`) with
**no lock of any kind** -- this code has no awareness of `layerLock`
(a `BaseXBar`-only member) and never could, being generic
port/queue-framework code shared by every `QueuedRequestPort`/
`QueuedResponsePort` in the memory system. This matches the *original*,
broader framing of hypothesis (b) in §3 much better than the narrower
"just fix `Layer::releaseEvent`" diagnosis in §1b. **Not fixed. The
§1c-implemented `layerLock`-in-`releaseLayer()` change is left in place
(harmless, and correctly closes the gap it targets) but is not, by
itself, sufficient -- a `PacketQueue`-level lock (or equivalent) is now
the leading candidate, and needs a fresh user checkpoint before
attempting, being a second attempt after a failed first one and a
wider-blast-radius change (generic infrastructure, not
`NoncoherentXBar`-specific).** A later code-read (§6 step 4) found the
step-4 lock **cannot be implemented verbatim** -- `UncontendedMutex` is
non-recursive and the target methods call each other, so it
self-deadlocks even single-threaded, and holding it across
`sendTiming()` risks a `pqLock`<->`layerLock` order inversion; it needs
an unlocked-core/locked-wrapper restructuring plus a lock-ordering
proof. **The TSan A/B run to confirm the mechanism first (§6 step 5) is
now DONE and CONFIRMS the race, with TWO independent crash
reproductions**: standalone TSan runs (`diag2`, `diag4`) reproduced the
exact historical crash tick (`5305999323366`) and TSan reported data
races directly on `PacketQueue::retry()` and `deferredPacketReady()`
immediately before the assertion fired, plus races on `schedSendEvent()`/
`sendDeferredPacket()` and `transmitList`'s own size -- four different
pieces of the same `PacketQueue` instance's state, unsynchronized
across domains, exactly matching this spec's hypothesis and step 4's
proposed fix surface. A third standalone run (`diag3`) completed
cleanly with `finalTick`/`simInsts` matching the historical serial
reference exactly, serving as the correctness cross-check.
Standalone-run reproduction rate under TSan: ~67% (2/3), well above
the bare ~15% rate, as §1b's own prediction anticipated. Full detail:
§7 (note: §7.1's initial `halt_on_error` explanation for an earlier
anomaly was wrong and is corrected in §7.3 -- read §7.3-§7.4, not §7.1,
for the real account; §7.4 also flags a new, separate, TSan-build
launch-method hang unrelated to this bug). A **deferral-based
architectural alternative** (§6 step 5b) was recorded to weigh
against a 4th bolt-on lock before committing to fix #2. **§8 sketches
the step-4 restructure in full, with a lock-ordering proof -- and
corrects the "step 4 = complete fix surface" framing above: a `pqLock`
*alone* is INSUFFICIENT.** Part of the confirmed race is on
`sendEvent`'s scheduling, and gem5's `EventQueue` forbids cross-domain
`reschedule()` by assertion (`eventq.hh:844`), which no
`PacketQueue`-level lock can serialize against `serviceOne()`. The
robust fix is a **hybrid**: `pqLock` (a strict leaf lock) for
`transmitList`/`waitingOnRetry`, plus an owner-thread-only
`asyncInsert`-based handoff for `sendEvent` scheduling (a narrow,
structural piece of step 5b). **The hybrid-vs-full-5b design decision
is now SETTLED (§9, 2026-07-17, at the user's delegation): the §8
hybrid is the chosen fix; full 5b deferral is rejected as this bug's
fix (it cannot cover the confirmed `recvTimingResp`-path race without
a flow-control protocol redesign, since `recvTiming*` returns a
synchronous bool) and is retained only as a possible future
architecture investigation. **§10 (2026-07-17): the §8 hybrid is now
IMPLEMENTED on branch `s015-packetqueue-retry-race` (`src/mem/
packet_queue.{hh,cc}` + the cache override in `src/mem/cache/base.cc`),
builds clean, and passes the S-009 §27 short-window regression
byte-identical (serial vs spin) with no deadlock. The crash-confirmation
runs (§1a-style bare batch at MAX_TICKS=2e9 reaching the ~5.306e9
historical crash tick, and the §7 TSan A/B re-run) are the remaining
verification sub-phase — see §10.4.**

## 1. What happened

Per S-014 §8's step 3 (confirm the `occupyLayer` `crossDomainSnap()` fix
survives past the original crash point), ran the fixed build
(`build/X86_MESI_Three_Level/gem5.opt`, same session) at the same
operating point as S-009 §27 / S-014 §1 (`CHECKPOINT_DIR=
/workspace/gem5-ckpt/x86-threads3-roi-classic`, `SIM_QUANTUM_TICKS=6660`),
bumped from the previously-tested `MAX_TICKS=2e8` up to `MAX_TICKS=2e9`
(~3x past the original crash's ~646M-ticks-past-restore point, per the
user's explicit choice of "launch bounded past the old crash point").

- **Serial arm** (`taskset -c 54`, no `PARALLEL_EVENTQ`): completed
  cleanly every time run. `finalTick=5306177114066`,
  `simTicks=2000000000` (so restore tick = `5304177114066`),
  `simInsts=1475264`.
- **Parallel/spin arm** (`taskset -c 92`,
  `HOST_PIN_CPUS=92,93,94,95,96,97,98,99`,
  `EVENTQ_BARRIER_MODE=spin`), run **four times** with the identical
  command line:
  - **Run 1** (no debug flags): **crashed**.
    ```
    gem5.opt: src/mem/packet_queue.cc:219: virtual void
    gem5::PacketQueue::sendDeferredPacket(): Assertion
    `deferredPacketReady()' failed.
    Program aborted at tick 5305999323366
    ```
    Backtrace (libc signal handler, `abort()`):
    ```
    PacketQueue::retry()
    BaseXBar::Layer<RequestPort, ResponsePort>::retryWaiting()
    EventQueue::serviceOne()
    doSimLoop()
    ...
    ```
    Crash tick minus serial's restore tick: `5305999323366 -
    5304177114066` = **1,822,209,300 ticks past restore** (~1.82e9,
    ~91% of the way to the 2e9 bound, ~2.8x past the original S-014 §1
    crash point of ~646M ticks past restore).
  - **Run 2** (`--debug-start=5305999200000
    --debug-flags=PacketQueue,NoncoherentXBar,XBar`, to catch the last
    activity before the crash tick cheaply): **completed cleanly**,
    `finalTick`/`simInsts` identical to serial.
  - **Run 3, 4** (bare rerun, identical command to Run 1, no debug
    flags): **both completed cleanly**, `finalTick`/`simInsts` identical
    to serial.

**1 crash out of 4 identical invocations of the parallel/spin arm.** The
serial arm never crashed (expected: `inParallelMode` is false in serial
mode, so there is only one `EventQueue`/one host thread — no cross-domain
call can ever occur, and no genuine data race is possible either since
there's no second thread to race with).

## 1a. Repro-rate batch (S-014 §6 step 1 / this spec's §6 step 1,
16 more bare reruns, later session)

Ran 16 more sequential bare reruns of the exact same command line as §1's
parallel/spin arm (same checkpoint, `SIM_QUANTUM_TICKS=6660`,
`MAX_TICKS=2000000000`, `HOST_PIN_CPUS=92,93,94,95,96,97,98,99`,
`EVENTQ_BARRIER_MODE=spin`, `taskset -c 92`), each to a fresh `-d`
output dir, no debug flags, purely to get a better empirical
reproduction-rate estimate before committing to TSan (§6 step 1's
original suggestion). Wall-clock cost was cheap: every run (crash or
clean) finished in 43-48 seconds, so all 16 ran in well under 15
minutes total.

**Result: 2 crashes out of 16 runs**, combined with §1's original 1/4 =
**3 crashes out of 20 total runs (~15%)**:

| Run | Result | Exit | Crash tick | Notes |
|---|---|---|---|---|
| 1-12 | CLEAN | 0 | -- | `finalTick=5306177114066`, matches serial |
| 13 | **CRASH** | 134 | `5305999323366` | Identical assert/backtrace to §1 Run 1: `packet_queue.cc:219 assert(deferredPacketReady())`, via `PacketQueue::retry()` ← `BaseXBar::Layer<RequestPort,ResponsePort>::retryWaiting()` |
| 14-15 | CLEAN | 0 | -- | matches serial |
| 16 | **CRASH** | 134 | `5305999330026` | **Different assert**: `simulate.cc:394 assert(curTick() <= eventq->nextTick() && "event scheduled in the past")` in `doSimLoop` -- not the `PacketQueue` assert, but crashes in the same call context (parallel/spin arm, same operating point) |

**Crash-tick clustering (new finding, not previously noted in §1):**
Run 16's crash tick minus Run 13's crash tick = exactly `6660` --
**one `SIM_QUANTUM_TICKS`, precisely.** Both land at the same
~1.822e9-ticks-past-restore offset (~91% through the 2e9 window) as
§1 Run 1's original crash tick (`5305999323366` -- identical to Run 13's,
to the tick). Across all 3 observed crashes (this batch's 2 plus §1's
1), every crash tick falls within one quantum of the same point in
simulated time, despite the total sample spanning 20 independent runs
with no `MAX_TICKS` change between them.

This rules out "crashes at a random point across the 2e9-tick window"
and instead points to **one specific, deterministic event in the
simulated timeline** (some particular PIO/DMA transaction or guest
workload phase transition that always happens at ~this tick,
independent of run-to-run host scheduling variance) that triggers a
race whose *outcome* -- clean, `PacketQueue` assert, or `doSimLoop`
assert -- depends on non-deterministic host-thread interleaving at that
moment. This also means the two different asserts (Run 13 vs. Run 16)
are very likely **two symptoms of the same underlying race**, not two
separate bugs: once the race corrupts/desyncs one domain's view of
`curTick()` relative to a queued packet's tick, which specific
downstream assertion trips first plausibly depends on which code path
happens to run next, not on a different root cause. (Not confirmed by a
tracing tool -- inferred from the tick-clustering pattern alone, same
caveat as §3's hypothesis.)

No code changes made in this batch (measurement only, per the
task scope: "run 10-20 more bare reruns to estimate the reproduction
rate").

## 1b. Targeted `--debug-start` batch (this spec's §6 step 1 follow-up,
same session as §1a) -- hot object identified, third assert variant
found, and a concrete lock-coverage-gap hypothesis

Per the user's request ("try a narrow `--debug-start` around tick
5305999320000"), ran 8 more parallel/spin repros of the identical
operating point, with `--debug-start=5305999300000
--debug-flags=PacketQueue,XBar,DMA` (a window starting ~20,000-30,000
ticks before the known crash-tick cluster from §1a, cheap because the
trace only activates near the very end of the 2e9-tick run).

**Result: 4 crashes out of 8 runs (50%)** -- notably higher than §1a's
bare-rerun rate (15%). All 4 crash logs (runs 1, 3, 6, 7) are
**byte-for-byte identical** in their DPRINTF lead-up (module the
occasional garbled interleaving described below), down to the exact
tick values and packet addresses:

```
5305999321135: board.cache_hierarchy.ruby_system.l1_controllers3.sequencer.pio-response-port-RespPacketQueue: schedSendTiming for WriteResp address 2000000000003300 size 4 when 5305999321468 ord: 0
5305999323366: board.cache_hierarchy.ruby_system.l1_controllers0.sequencer.pio-response-port-RespPacketQueue: schedSendTiming for WriteResp address a000000000000000 size 4 when 5305999323699 ord: 0
5305999321468: board.iobus: recvTimingResp: src board.iobus.mem_side_port[17] WriteResp 0x2000000000003300
5305999321468: board.iobus.cpu_side_ports[8]-RespPacketQueue: schedSendTiming for WriteResp address 2000000000003300 size 4 when 5305999322367 ord: 0
5305999321468: board.iobus.respLayer8: The crossbar layer is now busy from tick 5305999321468 to 5305999323366
5305999323699: board.iobus: recvTimingResp: src board.iobus.mem_side_port[14] WriteResp 0xa000000000000000
5305999323699: board.iobus.cpu_side_ports[8]-RespPacketQueue: schedSendTiming for WriteResp address a000000000000000 size 4 when 5305999324365 ord: 0
5305999323366: board.cache_hierarchy.ruby_system.l1_controllers3.sequencer.response_ports1-RespPacketQueue: schedSendTiming for WriteResp address 2000000000003300 size 4 when 5305999323699 ord: 0
5305999323699: board.iobus.respLayer8: The crossbar layer is now busy from tick 5305999323699 to 5305999330026
src/sim/eventq.cc:223: panic: event not found!
Memory Usage: 4296816 KBytes
Program aborted at tick 5305999323366
```

**Hot object identified for the first time**: `board.iobus.respLayer8`
(the `RespLayer` guarding `board.iobus.cpu_side_ports[8]`) and its
associated `board.iobus.cpu_side_ports[8]-RespPacketQueue`. Two
`WriteResp` packets (from `mem_side_port[17]`, address
`0x2000000000003300`, and `mem_side_port[14]`, address
`0xa000000000000000`) converge on this exact same layer/queue within a
single busy-release cycle (`respLayer8` becomes busy at `321468`,
releases at `323366`, immediately becomes busy again at `323699`,
releases at `330026`). The crash always lands exactly at `323366` --
the tick `respLayer8`'s first busy period was scheduled to release.

**Third assert variant, not seen in §1/§1a**: `src/sim/eventq.cc:223
panic("event not found!")`, in `EventQueue::remove()`, reached via
`PacketQueue::schedSendEvent`'s inlined `reschedule()` call (backtrace
shows `schedSendEvent` calling `EventQueue::remove` directly -- an
optimized-build inlining artifact, not a sign `reschedule()`'s own body
was skipped). This is a **third distinct manifestation** of what looks
like the same underlying problem (alongside §1's
`PacketQueue::sendDeferredPacket`'s `deferredPacketReady()` assert and
§1a's `doSimLoop`'s "event scheduled in the past" assert) -- three
different assertions, three different call paths, all clustering at the
identical tick and object.

**Why this rules out a naive cross-domain TLS mismatch for this
particular manifestation**: `EventQueue::reschedule()` itself asserts
`event->queue == this` and `!inParallelMode || this == curEventQueue()`
before ever reaching `remove()` (`eventq.hh:841-844`) -- neither of
those fired (the reported failure is `remove()`'s own internal
`panic("event not found!")` at `eventq.cc:223`, a different message).
So the call that failed was executing on the *correct* domain's thread,
targeting the *correct* domain's queue -- the event's `scheduled()` flag
said true, `event->queue` matched `this`, but `remove()`'s internal
bin-list search (keyed off the event's own recorded `when`) couldn't
find it. That shape -- correct domain, internally inconsistent state --
points at concurrent unsynchronized mutation of the *same* Event/queue
data structures, not at a `curTick()`/TLS domain-mismatch of the kind
S-009/S-014 fixed.

**Root-cause candidate (read from source, not yet TSan-confirmed):**
`BaseXBar::layerLock` (`xbar.hh:360`, added S-009 §24) is taken around
the full body of exactly three `NoncoherentXBar` entry points:
`recvTimingReq` (`noncoherent_xbar.cc:104`), `recvTimingResp` (`:184`),
and `recvReqRetry` (`:244`) -- confirmed by grep, no other lock_guard on
`layerLock` exists anywhere in `xbar.cc`/`noncoherent_xbar.cc`. Its own
doc comment (`xbar.hh:352-359`) says it's "held for the full body of the
timing entry points" -- but `Layer::releaseEvent`'s callback chain
(`releaseLayer()` -> `retryWaiting()` -> `occupyLayer()` / `sendRetry()`
-> the peer's `PacketQueue::retry()`, `xbar.cc:272-327`) is **not** one
of those three entry points -- it's an `EventFunctionWrapper` scheduled
directly onto the crossbar's own domain's `EventQueue`
(`xbar.schedule(releaseEvent, until)`, inside `occupyLayer`) and fires
from `EventQueue::serviceOne()`'s own dispatch, entirely outside any
`NoncoherentXBar` method body -- so it **never acquires `layerLock`**,
despite mutating the exact same `Layer` state (`state`,
`waitingForLayer`, `occupancy`) and reaching into the exact same
downstream `PacketQueue` objects (`transmitList`, `sendEvent`,
`waitingOnRetry`) that the three locked entry points also touch.

`recvTimingReq` is reachable from a genuinely different host thread than
the crossbar's own domain (a core domain's synchronous cross-domain PIO
call, S-014 §7) -- while `releaseEvent`'s callback always executes on
the crossbar's own domain's thread (domain 0 here). **If `releaseEvent`
fires on domain 0's thread at the same real-world moment a core domain's
thread is inside `recvTimingReq` for a *different* port/layer on the
same crossbar, `layerLock` does nothing to protect them from each other
-- one side holds it, the other never asks for it.** This is a real,
structural coverage gap in the lock, not a hypothetical: `layerLock`'s
own comment describes it as covering "the timing entry points" and lists
none of `Layer`'s own internally-scheduled event callbacks, which is
exactly the shape of gap S-009 (`BridgeBase`) and S-014 (`occupyLayer`
itself) each already found once elsewhere in this same subsystem.

**This refines (does not just confirm) the §3 hypotheses below**:
neither hypothesis (a) ("`curTick()` didn't reflect the true domain
clock") nor the original framing of hypothesis (b) ("`PacketQueue`
itself was never given a lock") is quite right -- `PacketQueue` doesn't
need its *own* lock; the lock that should already cover it
(`layerLock`, held by the callers that route through
`NoncoherentXBar`) simply doesn't extend to the one call chain
(`releaseEvent` -> `retryWaiting` -> `sendRetry`/`occupyLayer`) that
also touches it.

**Methodology note (a gap in this batch, flagged rather than hidden)**:
the batch script kept only the last 200KB of *clean*-run logs (to save
disk), which discarded the very early part of the trace (right after
`--debug-start` activates) where the same lead-up sequence would have
appeared for comparison. This means clean runs were not directly
compared against crash runs at the point of divergence -- only the 4
crash logs' internal consistency (identical to each other) was
confirmed. Re-running with a narrower keep-window (or no truncation,
given the window is short) would be needed to see exactly where a clean
run's version of this sequence differs.

**Elevated crash rate under tracing (50% vs. 15% bare, §1a)** is
consistent with a genuine race whose window widens under perturbation --
DPRINTF I/O adds work to the hot path, which changes relative thread
timing and, empirically here, made the race easier to hit, not harder.
This is useful context for planning any future TSan run (TSan
instrumentation perturbs timing far more than tracing does, so if this
holds, a TSan build may show an even higher, more convenient
reproduction rate than 15-50%, per the pattern already seen in
S-009/S-010/S-011's successful TSan investigations).

No code changes made in this batch (measurement/tracing only).

## 1c. First fix attempt (`layerLock` in `releaseLayer()`) implemented,
built, and FAILED verification -- corrected/expanded root-cause analysis

Per the user's explicit request ("implement the layerLock fix and
re-run the repro batches to verify"), implemented §1b's fix: added
`std::lock_guard<UncontendedMutex> lock(xbar.layerLock);` as the first
statement of `BaseXBar::Layer<SrcType, DstType>::releaseLayer()`
(`xbar.cc`), so it's held for the same body `retryWaiting()` ->
`occupyLayer()`/`sendRetry()` -> the peer's `PacketQueue::retry()` runs
under, symmetric with the three `NoncoherentXBar` entry points that
already take this lock. Deliberately did **not** add locking inside
`retryWaiting()`/`occupyLayer()` themselves, since `retryWaiting()` is
also reached via `recvRetry()` <- `NoncoherentXBar::recvReqRetry()`,
which already holds this same (non-recursive) `UncontendedMutex` --
locking there too would self-deadlock on that path.

**Built clean** (`build/X86_MESI_Three_Level/gem5.opt`, `taskset -c
0-53,56-91`, off the reserved isolcpus ranges). **Short-window
regression** (S-009 §27 protocol, `MAX_TICKS=2e8`): clean, byte-identical
`stats.txt` between serial and parallel/spin arms, `simInsts=74062`
matching the reference exactly -- no behavior change in the
previously-validated window, as expected.

**Verification batch (bare reruns, same protocol as §1a) -- FAILED**:
of the first 6 post-fix runs, **3-4 crashed** (`CRASH_S015_OTHERASSERT`
at run 1, `CRASH_S015` at runs 2 and 4) -- a crash rate at least as high
as, arguably higher than, the pre-fix ~15% bare rate (§1a). **This fix
does not resolve the bug.** Reporting this immediately and plainly
rather than waiting for the full batch or a more flattering sample,
per this project's own convention.

**Why the fix didn't work -- a second, more direct gap, not touched by
§1b's fix**: re-examining the call chain for `board.iobus.
cpu_side_ports[8]-RespPacketQueue` (the hot object §1b identified) with
fresh eyes: `cpu_side_ports[8]` is one of `iobus`'s own
`QueuedResponsePort`s. When its own `RespPacketQueue` tries to send a
response to its peer (a core domain's `RequestPort`, e.g. a Ruby
Sequencer's PIO port) and that peer's `recvTimingResp()` returns false,
`cpu_side_ports[8]`'s own queue sets `waitingOnRetry = true`. Later,
when the **peer** (the core domain) decides it's ready, *it* calls
`RequestPort::sendRetryResp()` **on itself** (`port.hh:637`), which
calls `TimingRequestProtocol::sendRetryResp(_responsePort)`
(`timing.cc:72`), which calls `_responsePort->recvRespRetry()` directly
-- `_responsePort` here is `cpu_side_ports[8]` itself.
`QueuedResponsePort::recvRespRetry()` (`qport.hh:69`) is just
`{ respQueue.retry(); }` -- **no lock of any kind, anywhere in this
call chain.** This is generic port/queue-framework code
(`port.hh`/`qport.hh`/`packet_queue.cc`) with **no awareness that
`BaseXBar::layerLock` even exists** -- it can't take a lock it has no
reference to. Since the peer (core domain) decides on its own schedule
when to call `sendRetryResp()`, this executes on the **core domain's
own host thread** -- while `iobus`'s own domain-0 thread can
simultaneously be inside `NoncoherentXBar::recvTimingResp()` (which
*does* hold `layerLock`) calling `cpuSidePorts[cpu_side_port_id]->
schedTimingResp(...)`, mutating the exact same queue's `transmitList`/
`sendEvent`/`waitingOnRetry`. `layerLock` protects domain-0's own side
of this interaction, but the core domain's direct `recvRespRetry()`
call never asks for it -- so two different threads can still touch
the same `PacketQueue` state completely unsynchronized. This path is
independent of, and was entirely unaffected by, §1b/§1c's
`Layer::releaseEvent` fix -- it doesn't go through `Layer` at all.

**This refines the diagnosis again**: §1b's `layerLock`/`releaseEvent`
gap is real (confirmed by reading the code, and plausibly a genuine,
if apparently minor or non-dominant, contributor) but the
`recvRespRetry`/`recvReqRetry` gap identified here looks like the more
direct match for the observed crash, and was missed because it lives
in generic `qport.hh` port-framework code rather than
`NoncoherentXBar`/`BaseXBar`-specific code, so it didn't show up when
auditing `layerLock`'s three call sites in `noncoherent_xbar.cc`. This
is much closer to the **original, broader** framing of hypothesis (b)
in §3 ("`PacketQueue` itself... may never have gotten the same
cross-domain mutual-exclusion treatment") than §1b's narrower
"`Layer::releaseEvent` specifically" diagnosis -- the fix likely needs
to live at the `PacketQueue`/`QueuedResponsePort`/`QueuedRequestPort`
level itself (a lock acquired inside `retry()`, `schedSendTiming()`,
`schedSendEvent()`, and `sendDeferredPacket()`/`processSendEvent()`),
not solely at the `BaseXBar`/`Layer` level, since `recvRespRetry()`/
`recvReqRetry()` can never reach `layerLock` no matter where inside
`Layer` it's placed.

**Update (§7): first TSan A/B run in progress -- v1 (flawed, halted
early on TSan's own `halt_on_error` default) still caught a
TSan-CONFIRMED race on exactly this `PacketQueue::schedSendEvent()`/
`sendDeferredPacket()` cross-domain path (`packet_queue.cc:159`/`:232`,
via `QueuedResponsePort::schedTimingResp()` vs. domain 0's own
`processSendEvent()`) -- see §7.2. This is no longer just an inference
from source; a corrected (v2) full-window run is in progress to
determine whether it's the crash's actual mechanism.**

**Disposition of the §1c code change**: left in place, not reverted.
It correctly closes the gap it targets (`Layer::releaseEvent` running
unsynchronized against `NoncoherentXBar`'s three locked entry points),
causes no correctness or measurable performance regression (confirmed
by the clean short-window regression above), and may still be
contributing to safety on whichever fraction of crashes (if any) go
through that specific path -- it's simply not sufficient by itself.

**This is a second attempted fix that failed verification, on top of a
first attempt (S-014's `occupyLayer` fix) that succeeded** -- flagging
explicitly that this doesn't cast any doubt on S-014's fix (a different
bug, different mechanism, independently confirmed with 0 recurrences
across 20 runs); it's specific to S-015's own harder, non-deterministic
race.

No further code changes attempted in this update -- next fix attempt
(a `PacketQueue`-level lock) needs a fresh user checkpoint before
implementing, per `CLAUDE.md`'s convention, doubly so after a first
attempt already missed the mark.

## 2. Why this is a different bug from S-014's `occupyLayer` crash, not
a recurrence of it

- **Different file, different function, different assert.** S-014's
  crash was `assert(when >= getCurTick())` in `EventQueue::schedule`
  (`eventq.hh`), reached through `BaseXBar::Layer::occupyLayer`'s raw
  `xbar.schedule()`. This crash is `assert(deferredPacketReady())` in
  `PacketQueue::sendDeferredPacket()` (`packet_queue.cc:219`) — a
  completely different invariant (a different, generic packet-queue
  base class shared by essentially every `QueuedRequestPort`/
  `QueuedResponsePort` in the memory system, not specific to
  `BaseXBar`).
- **Different determinism class.** Every crash fixed in S-009/S-014 so
  far (`PacketQueue::schedSendEvent`, `BridgeBase::schedTimingReq/Resp`,
  `BaseXBar::Layer::occupyLayer`) was a **deterministic** consequence of
  quantum-grid arithmetic — same checkpoint + same build + same
  quantum always crashed at the same tick, every time (S-009 §27's and
  this session's own occupyLayer-fix regression check, S-014 §1's two
  independent checkpoints crashing at the same order of magnitude). This
  crash **reproduced in only 1 of 4 identical runs**, including one
  rerun that changed only the presence of `--debug-flags`/
  `--debug-start` (which perturbs host-thread timing by adding I/O and
  string-formatting work on the hot path) and still ran clean past the
  point where the crash previously fired, and completed to the exact
  same `finalTick` as serial. This is the signature of a genuine
  timing-dependent race, not a fixed arithmetic mistake -- consistent
  with the occupyLayer fix (§14 of S-014) being unrelated to and not a
  cause of this bug (the fix only changes a raw `schedule()` call's
  `until` argument deterministically; it doesn't introduce or remove any
  concurrency).
- **The crash is downstream of, not inside, the code S-014 §8 changed.**
  The backtrace shows `BaseXBar::Layer::retryWaiting()` calling into
  `PacketQueue::retry()` *before* `retryWaiting()` would reach its own
  (now-fixed) `occupyLayer(xbar.clockEdge())` call at the tail of the
  function (`xbar.cc:304`) -- the crash happens in a **sibling call
  inside the same function**, not in the code path this session
  modified.

## 3. Root-cause hypothesis (NOT confirmed -- flagging as a hypothesis,
not a finding)

Read `src/mem/packet_queue.cc`/`.hh` to understand the mechanism (no
TSan or other verification tool run against this yet, so treat this as
informed speculation, not established fact, unlike S-014 §2's root cause
which was confirmed by direct code inspection against a 100%-reproducible
crash):

`Layer<RequestPort, ResponsePort>` (i.e. `respLayers`, per
`noncoherent_xbar.cc:88`)'s `retryWaiting()` is reached **only** via
`releaseLayer()` (no code anywhere calls `respLayers[i]->recvRetry()`
directly for `NoncoherentXBar` -- confirmed by grep), which fires only
when `releaseEvent` (scheduled by `occupyLayer`, always on the crossbar's
own `EventQueue`) executes -- i.e. `retryWaiting()` for `respLayers`
always runs on the **xbar's own domain's host thread** (domain 0, per
S-014 §7). It calls `sendRetry(retryingPort)` where `retryingPort` is one
of the xbar's own `memSidePorts[j]` (a `RequestPort`); for `RespLayer`,
that's `retryingPort->sendRetryResp()`, which -- per gem5's request/
response port pairing convention -- notifies the **peer** connected to
`memSidePorts[j]` (the actual downstream device, e.g. an IDE controller,
PIT, RTC, or a `DMAController`/`DMASequencer`) via its
`recvRespRetry()` → that device's own `RespPacketQueue::retry()`
(matches the backtrace).

Per S-014 §7's domain map, every device attached to `iobus.mem_side_ports`
is nominally domain 0 (same domain as the xbar) -- so this call chain,
*on its own*, looks domain-local end-to-end and shouldn't have a
cross-domain tick mismatch of the kind S-014 fixed. But `deferredPacketReady()`
(`transmitList.front().tick <= curTick()`) failing means the packet at
the head of *that device's own* `transmitList` was inserted with a tick
value that hadn't arrived yet by the time `retry()` ran -- which is only
possible if either (a) `curTick()` at that exact moment did not actually
reflect domain 0's true clock (contradicting the domain-locality
argument above -- unresolved), or (b) the same device's `PacketQueue`
(`transmitList`/`waitingOnRetry`) was mutated concurrently from **two
different host threads** without synchronization: e.g. the device's own
PIO-response-generating code path (reachable via a *cross-domain*
synchronous call from a core-domain thread, the same kind of call S-009
§18/§23/§25 already found needed `pioLock`) racing against domain 0's own
thread running `retryWaiting()`/`retry()` on the identical queue at the
same time. **(b) would mean `PacketQueue` itself (`transmitList` +
`waitingOnRetry`, distinct from the `BaseXBar`-level `layerLock` and
`PioDevice`-level `pioLock` that S-009 already added) has never been
given equivalent cross-domain mutual-exclusion protection** -- but this
is a hypothesis inferred from the code shape, not confirmed by any
tracing tool. Which of (a)/(b) (or something else entirely) is actually
happening is unresolved.

## 4. Scope and severity (preliminary, not fully assessed)

- If hypothesis (b) is correct, this would be a **generic
  `PacketQueue` thread-safety gap**, not specific to `NoncoherentXBar`'s
  `respLayers` -- `PacketQueue`/`ReqPacketQueue`/`RespPacketQueue`/
  `SnoopRespPacketQueue` are used by essentially every classic
  `QueuedRequestPort`/`QueuedResponsePort` in the memory system, a much
  larger blast radius than `occupyLayer`'s four call chains (S-014 §7).
- Non-deterministic crashes are inherently harder to bound: "ran clean N
  times" is not proof of a fix or of absence, only of a probability
  --this project's own history bears this out directly (S-011 designed a
  probability-estimating stress test for a structurally similar
  non-deterministic race and the user chose not to run it, opting for a
  design fix instead, per S-011 §6-8).
- This blocks the same thing S-014 already blocked: any run of this
  operating point long enough to approach real steady-state/full-ROI
  behavior, now for a second, independent reason.

## 5. Not attempted here

- No fix written.
- No TSan run -- this project's standard tool for exactly this class of
  problem (non-deterministic cross-thread races), used successfully in
  S-009 §23/24, S-010, S-011. Given the 1-in-4 reproduction rate observed
  here, a TSan run (which serializes/instruments memory accesses and
  tends to perturb timing enough to change race windows, sometimes
  making races more or less likely to manifest) is the obvious next
  diagnostic step, but has not been run.
- No attempt to identify the exact device/`PacketQueue` instance
  involved -- the one run that might have shown it via `DPRINTF`
  (`PacketQueue,NoncoherentXBar,XBar` debug flags, `--debug-start` just
  before the crash tick) happened not to crash at all, so no additional
  object-identifying information was captured. Re-attempting this would
  need either a wider `--debug-start` window (cost: more overhead,
  itself might perturb timing enough to prevent the crash again) or
  multiple attempts.
- No repetition count beyond 4 runs -- true reproduction rate is not
  well estimated (could be much rarer or much more common than 25%; 4
  samples is not enough to say).
- This is squarely new, likely-needs-live-debugging territory (probably
  requiring a TSan investigation session, per this project's own
  established playbook) -- per `CLAUDE.md`'s explicit checkpoint
  convention, this needs the user's direction before continuing, not
  autonomous investigation.

## 6. Suggested next steps (updated after §1a/§1b/§1c; steps 4-6 not
started)

1. ~~Run a short non-TSan repetition batch (10-20 runs at this same
   operating point) purely to get a better empirical reproduction-rate
   estimate before committing to a TSan investigation.~~ **Done, §1a:
   16 more runs, 2 more crashes, combined estimate ~15% (3/20).**
2. ~~Targeted `--debug-start` DPRINTF pass around the known tick
   cluster.~~ **Done, §1b: 8 runs, 4 crashes (50%), identical DPRINTF
   lead-up every time. Identified the hot object
   (`board.iobus.respLayer8`/`cpu_side_ports[8]-RespPacketQueue`), a
   third assert variant (`EventQueue::remove`'s "event not found!"), and
   a first root-cause candidate: `layerLock` covers `recvTimingReq`/
   `recvTimingResp`/`recvReqRetry` but not `Layer::releaseEvent`'s
   callback chain.**
3. ~~Implement that fix and re-verify.~~ **Done, §1c: implemented,
   built, short-window regression clean -- but verification FAILED**
   (3-4 crashes in the first 6 post-fix bare reruns, same order of
   magnitude as pre-fix). **Root cause was incomplete**: §1c identifies
   a second, more direct, likely-dominant gap the first fix didn't
   touch -- `QueuedResponsePort::recvRespRetry()`/`QueuedRequestPort::
   recvReqRetry()` (`qport.hh`) can be called directly by a cross-domain
   peer with **no lock at all**, being generic port-framework code with
   no awareness of `BaseXBar::layerLock`. The §1c code change is kept
   (harmless, closes a real if apparently minor gap) but is not
   sufficient alone.
4. **Recommended next fix to attempt** (design, **now fully sketched
   with a lock-ordering proof in §8** -- read §8 for the concrete,
   corrected version; the paragraph below is the original, coarser
   framing and is partly superseded, since §8 shows a `pqLock` alone is
   insufficient for the `sendEvent`-scheduling half): a
   lock at the `PacketQueue`/`QueuedResponsePort`/`QueuedRequestPort`
   level itself -- e.g. an `UncontendedMutex` member of `PacketQueue`,
   acquired inside `retry()`, `schedSendTiming()`, `schedSendEvent()`,
   and `sendDeferredPacket()`/`processSendEvent()` -- since this is the
   only level both the `NoncoherentXBar`-locked entry points and the
   generic, xbar-unaware `recvRespRetry()`/`recvReqRetry()` path can
   share. This is a wider-blast-radius change than §1c (touches
   `packet_queue.hh`/`.cc`, used by every `QueuedRequestPort`/
   `QueuedResponsePort` in the classic memory system, not just this
   xbar) and comes after one failed fix attempt already -- needs a
   fresh, explicit user checkpoint before implementing, not a
   continuation of the previous go-ahead.

   **Design caveat found this session (code-read, blocks the verbatim
   fix):** the granularity is right -- both racing threads mutate the
   *same* `PacketQueue`, so a per-queue lock can serialize them where
   `layerLock` structurally cannot -- but the method list above **cannot
   be implemented as written**, for two concrete reasons:
   - `UncontendedMutex` is **non-recursive** (`base/uncontended_mutex.hh`
     -- a bare atomic flag + CV, no owner/recursion count; a thread that
     re-enters `lock()` blocks on its own held lock forever). The four
     listed methods **call each other on a single thread**:
     `schedSendTiming()` calls `schedSendEvent()` (`packet_queue.cc:152`),
     `sendDeferredPacket()` calls `schedSendEvent()` (`:237`), and there
     is a **documented reentrant path** (`:127-133` comment):
     `sendDeferredPacket -> sendTiming ->` peer recv -> new request ->
     `schedSendTiming` on the *same* queue. Locking all four with one
     non-recursive mutex **self-deadlocks even single-threaded.** The fix
     must therefore be restructured as a private *unlocked* core
     (`...Locked()` helpers) with thin locked public wrappers, so no
     locked method calls another locked method.
   - Even restructured, closing the race requires holding the lock across
     `sendDeferredPacket`'s mutate/send/re-mutate sequence, which
     includes `sendTiming()` -- and `sendTiming()` calls **cross-domain
     into the peer's `recvTiming*` handler**, which on other queues takes
     `layerLock`/`pioLock`. That yields `pqLock`-then-`layerLock` on one
     path and `layerLock`-then-`pqLock` (`recvReqRetry`) on another -- a
     **lock-order inversion across domains** that can convert the race
     crash into a deadlock hang. Any implementation must prove this
     ordering cannot invert (or release `pqLock` before `sendTiming`,
     which reopens the very window it's meant to close and so needs its
     own care). This is generic code, so the ordering proof has to hold
     for *every* classic `QueuedPort`, not just `iobus.cpu_side_ports[8]`.
5. **Confirm the mechanism with TSan before implementing fix #2, not
   after.** Fix #1 (§1c) was implemented on an inferred, tool-unconfirmed
   hypothesis and failed verification; this whole spec still flags the
   root cause as *inferred, not confirmed*. Follow the S-009 §23/24-style
   TSan A/B protocol targeting `PacketQueue::retry()`/`schedSendTiming()`
   specifically -- it names the *exact* pair of racing accesses, which is
   what decides whether a queue-local lock even suffices or whether the
   real fix is elsewhere (may need `MAX_TICKS` tuned down from 2e9, since
   TSan runs 1-2 orders of magnitude slower, per S-014 §3's caveat about
   the `MAX_TICKS=1.3e9` TSan window; the observed rate rise under lighter
   tracing overhead, §1b, suggests TSan's heavier instrumentation may make
   this easier to catch, not harder). **Status: a TSan A/B run was
   started in a separate session (2026-07-17); results not yet folded in
   here.**
5b. **Architectural alternative worth weighing before committing to a
   4th bolt-on lock** (design, not yet scoped): every fix in this family
   -- `pioLock` (S-009 §25), `layerLock` (S-009 §24), `occupyLayer` snap
   (S-014), `layerLock`-in-`releaseLayer` (§1c), and now a `PacketQueue`
   lock -- patches one more instance of the *same* structural cause: a
   core-domain host thread synchronously mutating iobus-domain objects.
   Each fix closes one gap and the next investigation finds the next gap
   (this is gap #4/#5 by that count). The parti-gem5-aligned alternative
   is to stop doing the cross-domain synchronous mutation at all: when
   `recvRespRetry()`/`recvReqRetry()` (or the analogous entry) is invoked
   by a thread that is **not** the queue's owning domain, **defer it onto
   the owning domain** (schedule it on that domain's `EventQueue`) instead
   of running it inline on the caller's thread. That would make every one
   of these `PacketQueue`/`Layer` objects single-threaded again and retire
   the whole bug family, rather than adding lock #4 and waiting for gap
   #5. Bigger change, and it must respect the relaxed-timing quantum
   model (the deferred retry lands at a barrier boundary, same as other
   cross-domain timing) -- but it addresses the cause, not the symptom.
   **Decision made (§9): NOT chosen as the S-015 fix.** Full deferral
   can't cover the confirmed race surface (the §7.2 race enters via
   bool-returning `recvTimingResp`, which can't be deferred without a
   flow-control redesign), so a `pqLock` is needed regardless — see §9
   for the full grounds. The §8 hybrid already incorporates this
   step's principle exactly where the `EventQueue` contract forces it
   (the B2 scheduling handoff). Kept on record as the shape of a
   possible future domain-boundary-channel architecture investigation
   (its own S-NNN), with §9.4's falsifiers as the trigger.
6. Once fixed (or if ruled out as a real bug), return to S-014 §6 step 3
   / §9 proper: confirm the *original* `occupyLayer` crash point is
   durably fixed over a genuinely long/unbounded run, now that this
   second crash is understood and out of the way.

No code changes made in this investigation.

## 7. First TSan A/B run: setup, a methodology miss (`halt_on_error`
default), and -- even from the truncated v1 run -- a TSan-CONFIRMED hit
on the exact `PacketQueue` race §1c hypothesized

Per §6 step 5, set up and ran the S-009 §24.5/§25.4-style extended TSan
A/B (serial×2 + spin×2, simultaneously, on disjoint reserved cores)
against the current source tree (includes the uncommitted §1c
`layerLock`-in-`releaseLayer()` change, kept in place per its
disposition note).

**Environment this session** (re-probed per CLAUDE.md/S-009 §24.2's own
lesson -- don't assume carry-over): favorable across the board --
ASLR already disabled, passwordless `sudo`, `setarch x86_64 -R` works,
and the **full** `isolcpus=54-55,92-111` range is reachable from inside
this sandbox instance (`cat /sys/fs/cgroup/cpuset.cpus.effective` =
`0-111`) -- unlike the S-009 §25.2 session, which found that range
walled off by the container's cgroup. Rebuilt
`build/X86_MESI_Three_Level_TSAN/gem5.opt` (the existing binary
predated the uncommitted `xbar.cc`/`xbar.hh` §1c fix by several hours),
clean build, smoke-tested against the actual checkpoint
(`MAX_TICKS=1e6`) before committing to the full run -- exited 0, no
ASLR/mapping issues.

**A/B layout** (four-way, simultaneous, disjoint reserved cores --
this session's isolcpus access allowed following the isolated-core
version of the S-009 protocol, not the degraded non-isolated fallback
S-009 §25.2 had to use): `serial1`→core 54, `serial2`→core 55,
`spin1`→core 92 + `HOST_PIN_CPUS=92-99`, `spin2`→core 100 +
`HOST_PIN_CPUS=100-107`. Same checkpoint/quantum/bound as every S-015
repro batch so far (`x86-threads3-roi-classic`, `SIM_QUANTUM_TICKS=6660`,
`MAX_TICKS=2e9`), chosen to reach past the known ~1.822e9-ticks-past-
restore crash-tick cluster (S-015 §1a).

### 7.1 Methodology miss, caught and reported immediately -- ORIGINAL
EXPLANATION IN THIS SECTION WAS WRONG, corrected in §7.3

Both `spin1`/`spin2` (v1, no `TSAN_OPTIONS` set) exited after only
**~5-6 minutes wall-clock**, exit code **66**, having logged only
**11** and **10** unique-`SUMMARY`-line race reports respectively --
while the two `serial` arms (no cross-domain threads, so no races
possible) were still running past 20+ minutes on the same operating
point. Checked the v1 logs end-to-end for any gem5-side crash marker
(`Program aborted`, `panic:`, `Assertion`) -- **none present**.

**This section originally (wrongly) attributed the early exit to
TSan's default `halt_on_error` behavior and re-ran `spin1`/`spin2` (v2)
with `TSAN_OPTIONS=halt_on_error=0` explicitly set as the fix.**
**That diagnosis was WRONG, disproven in the same session**: v2's
`spin1v2`/`spin2v2` exited **identically** to v1 -- same exit code 66,
same 11/10 report counts, same ~5-6 minute timing, same absence of any
crash marker -- **despite** `halt_on_error=0` being confirmed reaching
the process (verified via a plain-shell env-propagation check and via
`verbosity=1` diagnostics, §7.3). So `halt_on_error` was never the
actual mechanism; correcting this immediately per this project's own
convention rather than letting the wrong explanation stand. **The real
explanation, and what it means for interpreting `spin1`/`spin2`/
`spin1v2`/`spin2v2`'s results, is in §7.3** -- do not read this
section (7.1) as settled; it's kept for the record of what was tried
and why the initial diagnosis was rejected, not as the final account.

### 7.2 Even from the truncated v1 run: a TSan-CONFIRMED race directly
on the S-015 §1c-hypothesized path -- not just inferred from source
anymore

Despite v1 halting early, it still caught real races before dying, and
one of them is a direct, tool-confirmed hit on exactly the mechanism
§1c's post-mortem proposed as the *real*, still-unfixed gap (as opposed
to the `layerLock`/`releaseEvent` gap the first fix attempt targeted
and which turned out insufficient):

```
WARNING: ThreadSanitizer: data race (pid=307450)
  Read of size 1 at 0x7b4800015d58 by thread T1:
    #0 gem5::PacketQueue::schedSendEvent(unsigned long) src/mem/packet_queue.cc:159
    #1 gem5::PacketQueue::schedSendTiming(gem5::Packet*, unsigned long) src/mem/packet_queue.cc:152
    #2 gem5::QueuedResponsePort::schedTimingResp(gem5::Packet*, unsigned long) src/mem/qport.hh:95
    ... (core-domain thread's call chain)

  Previous write of size 1 at 0x7b4800015d58 by main thread:
    #0 gem5::PacketQueue::sendDeferredPacket() src/mem/packet_queue.cc:232
    #1 gem5::PacketQueue::processSendEvent() src/mem/packet_queue.cc:248
    ... (domain 0's own EventQueue::serviceOne() -> EventFunctionWrapper dispatch)

SUMMARY: ThreadSanitizer: data race src/mem/packet_queue.cc:159 in
gem5::PacketQueue::schedSendEvent(unsigned long)
```

Both accesses are 1-byte, and the exact field is now pinned down (not
just "almost certainly"): `:159` is `if (waitingOnRetry) {` in
`schedSendEvent`, and `:232` is `waitingOnRetry = !sendTiming(dp.pkt);`
in `sendDeferredPacket` -- so the raced byte is **`PacketQueue::
waitingOnRetry`** (a `bool`, `packet_queue.hh:113`), *not* an
`inTransmit` member (no such member exists -- verified by grep; an
earlier draft of this note misnamed it). This is significant, not
pedantic: `waitingOnRetry` is the queue's **flow-control flag** -- the
one piece of state the entire retry protocol hinges on. A torn/stale
read of it is a direct mechanism for every crash variant observed:
`retry()`'s `assert(waitingOnRetry)` (`:70`), `sendDeferredPacket()`'s
`assert(!waitingOnRetry)` (`:218`), and the `deferredPacketReady()`
assert all trip when two threads disagree about whether a send is
in flight.
**Thread T1** reaches this via a *core domain's* own thread calling
`QueuedResponsePort::schedTimingResp()` -- the same generic,
`layerLock`-unaware `qport.hh` call chain §1c's post-mortem identified
as the likely-dominant, still-unfixed gap. **The main thread** (domain
0, this board's `iobus` lives there) reaches the same byte via its own
`EventQueue::serviceOne()` dispatching `PacketQueue::processSendEvent()`
directly (no `NoncoherentXBar` frame in this particular stack, i.e. this
specific `PacketQueue` instance's own scheduled send-event, not routed
through a `Layer` at all). **Two different domains' threads mutating
the same `PacketQueue`'s internal state with zero synchronization,
exactly as hypothesized** -- this is now a TSan tool-finding, not just
an inference from reading `packet_queue.cc`/`qport.hh` source.

A second v1 report (immediately preceding this one in the log) shows
the same pattern one level up: a race on
`std::__cxx11::_List_base<PacketQueue::DeferredPacket,...>::_M_get_size()`
(`stl_list.h:480`), i.e. concurrent access to `transmitList`'s own size
bookkeeping -- consistent with the same two threads also touching the
`std::list` itself, not just the `inTransmit` flag.

**This does not yet confirm the exact mechanism behind the observed
crash ticks** (v1 didn't run anywhere near the ~1.822e9-tick cluster
before halting) -- but it is the first tool-confirmed evidence that the
generic `PacketQueue` cross-domain race §1c's post-mortem proposed is
real and reachable at this operating point, not merely plausible from
reading the source. The v2 corrected run (§7.1, in progress) is needed
to see whether the same/related races appear concentrated near the
known crash-tick cluster, and to get a fuller census (S-009 §24.5/25.4
style: tally report counts per category, distinguish this project's
known "accepted" background races -- `EventQueue::_curTick`,
`Event::Flags`/`setWhen`, both already seen in this v1 log and matching
S-009 §24.4's precedent exactly -- from anything new).

Not yet done: full-window (v2) results, a report-count census, any
determination of whether this race's *outcome* correlates with the
known crash-tick cluster. No fix implemented this section (measurement
only, consistent with §5/§6 scoping).

### 7.3 Correction to §7.1, and the actual strongest result of this
whole run: a standalone diagnostic reached the exact historical crash
tick, with TSan reporting races DIRECTLY on `PacketQueue::retry()` and
`deferredPacketReady()` immediately beforehand

**§7.1's `halt_on_error` theory is wrong** (disproven: v2 behaved
identically to v1 despite the flag). Investigated further by running a
single standalone spin invocation (`diag2`, same operating point,
`TSAN_OPTIONS=halt_on_error=0`, launched directly rather than through
the `serial1/serial2/spin1/spin2` wrapper script used for v1/v2) with
close monitoring rather than fire-and-forget. **This run did NOT exit
early** -- it ran to completion of its natural termination, and that
termination was the **real S-015 crash**, byte-identical to every prior
observation of it:

```
gem5.opt: src/mem/packet_queue.cc:219: virtual void
gem5::PacketQueue::sendDeferredPacket(): Assertion
`deferredPacketReady()' failed.
Program aborted at tick 5305999323366
```

`5305999323366` is **the exact same tick** as §1 Run 1's original crash
and §1a Run 13's crash -- the same deterministic-simulated-time cluster
this whole spec has tracked since §1a. `/tmp/s015-diag2/stats.txt` is
0 bytes, confirming the process died mid-run rather than completing
`MAX_TICKS=2e9` cleanly.

**New, load-bearing finding: two TSan race reports fired in the ~100
lines immediately preceding the assertion, on the exact two functions
the crash's own call chain involves:**

```
SUMMARY: ThreadSanitizer: data race src/mem/packet_queue.cc:70 in gem5::PacketQueue::retry()
SUMMARY: ThreadSanitizer: data race src/mem/packet_queue.hh:117 in gem5::PacketQueue::deferredPacketReady() const
gem5.opt: src/mem/packet_queue.cc:219: ... Assertion `deferredPacketReady()' failed.
```

This is the strongest evidence this investigation has produced so far
that the crash **is** a real data race, not a logic bug that merely
happens to be timing-sensitive: TSan independently flagged unsynchronized
concurrent access to `deferredPacketReady()`'s own state (the exact
predicate whose violation is the assertion) and to `retry()` (the
caller), in the same run, right before the predicate was observed
false. Combined with §7.2's earlier `schedSendEvent()`/
`sendDeferredPacket()` race (same `PacketQueue` instance's `inTransmit`
byte) and a `transmitList` `std::list`-size race, TSan has now flagged
races on **four different pieces of the same object's state**
(`inTransmit`, `transmitList`'s size, whatever `deferredPacketReady()`
reads, and whatever `retry()` touches) -- consistent with broad,
unsynchronized concurrent mutation of one `PacketQueue` instance's
entire internal state, not one narrow two-field race. This is now a
**tool-confirmed** root cause, not an inference from source.

**Secondary finding, worth recording but not this bug's mechanism**:
immediately after the assertion/abort, the log shows
`ThreadSanitizer: signal-unsafe call inside of a signal ... in malloc`
(twice) and `... in __interceptor_free`, then `Failed to execute
default signal handler!`. gem5's own `SIGABRT` handler
(`init_signals.cc`, prints "Program aborted..." and a backtrace) calls
`malloc`/`free` while running inside signal context, which TSan flags
as unsafe (real, but a pre-existing signal-handler-hygiene issue,
unrelated to cross-domain locking) -- and this interaction appears to
be **why the process exits via TSan's own `Die()` path (`exitcode=66`,
matching every other run's exit code) instead of the plain
signal-terminated `134`** that every non-TSan crash in this spec has
shown. This means **exit code 66 does not by itself distinguish
"crashed" from "didn't crash" under this TSan build** -- the log
content (presence/absence of `Program aborted`/`Assertion`/the
`packet_queue.cc:70`/`hh:117` races) is the only reliable signal,
`echo $?` is not.

**Open, NOT yet resolved: why did `spin1`/`spin2`/`spin1v2`/`spin2v2`
(the wrapper-script-launched runs, §7.1) all also exit 66 after the
same ~5-6 minutes / ~10-11 reports, but WITHOUT any of `diag2`'s
crash-identifying content** (`packet_queue.cc:70`, `packet_queue.hh:117`,
`packet_queue.cc:219`, `Program aborted`, or the signal-handler lines
-- confirmed absent by exhaustive grep across all four logs, including a
raw byte-level check of the last 500 bytes of `spin1v2.log` for
truncated/garbled text). Two live hypotheses, neither confirmed:
(a) these four runs are genuinely non-deterministic and simply didn't
happen to hit the crash this time (consistent with this bug's entire
history -- a bare rate of ~15%, §1a) and instead hit **some other**
TSan-fatal condition unrelated to S-015 that also produces exit 66 --
in which case the "always ~10-11 reports" consistency across
independent runs (spin1-configured: 11, 11 twice more counting diag2's
own eventual 11; spin2-configured: 10, 10) would need its own
explanation, since that's a lot of consistency for something claimed
non-deterministic; or (b) something about the nested
`nohup driver.sh -> run_spin() { ... } & -> wait` launch structure used
for `spin1/spin2/spin1v2/spin2v2` (vs. `diag2`'s flatter
`env ... command > log 2>&1 &` launch) changes output buffering or
process-group signal delivery enough to matter for whether TSan's
post-abort output survives. **Not resolved this session** -- flagging
plainly rather than papering over it. Two more standalone
(`diag2`-style, non-wrapper) runs (`diag3`, `diag4`) were launched to
get a slightly larger sample of the direct-launch method; results to
be folded in once they land.

**What this changes for §6 step 4/5**: the TSan A/B run's original
purpose (§6 step 5: "confirm the mechanism with TSan before
implementing fix #2") is **now satisfied** -- the race is real,
TSan-confirmed, and localized to `PacketQueue`'s own instance state
(`retry()`, `deferredPacketReady()`, `schedSendEvent()`,
`sendDeferredPacket()`, `transmitList`), matching §6 step 4's proposed
fix surface exactly. This doesn't resolve the step 4 design caveat
(lock-ordering / non-recursive-mutex self-deadlock risk) or the step
5b architectural-alternative question -- those are still open decisions
for the user -- but the empirical prerequisite ("is this actually a
race, and where") is no longer just inferred from source.

### 7.4 `diag3`/`diag4` results: a second independent crash
confirmation, a clean correctness cross-check, and a NEW (separate,
unrelated) finding -- the wrapper-launched TSan builds hang, including
in serial mode where no cross-domain race is even possible

**`diag4`** (standalone launch, same method as `diag2`, `HOST_PIN_CPUS=
100-107`): **crashed at the identical tick** `5305999323366`, with the
identical `packet_queue.cc:70`/`packet_queue.hh:117` TSan races
immediately preceding the assertion, byte-for-byte matching `diag2`'s
mechanism. This is now **two independent standalone runs**, on two
different core sets, both reproducing the same crash with the same
TSan-flagged races -- not a one-off.

**`diag3`** (standalone, `HOST_PIN_CPUS=92-99`, same as `diag2`'s
cores): **completed cleanly** -- `finalTick=5306177114066`,
`simInsts=1475264`, exactly matching the historical serial reference
(S-015 §1) and confirming, independently of the (hung, see below)
in-session serial-vs-spin A/B, that this TSan build's parallel/spin
arm is still **timing-neutral when it doesn't crash** -- no silent
correctness divergence, consistent with every prior non-TSan A/B in
this project.

**Standalone TSan reproduction rate so far: 2 crashes / 3 runs
(`diag2`, `diag4` crashed; `diag3` clean) -- ~67%**, well above the
bare ~15% (§1a) and even above the `--debug-start`-perturbed ~50%
(§1b), continuing the trend §1b predicted ("TSan's heavier
instrumentation may make this easier to catch, not harder"). Small
sample (n=3), so treat 67% as directional, not a precise rate -- but
directionally it strongly supports "TSan makes this bug easy to hit,"
useful context for anyone re-running this A/B in the future.

**New, separate finding: the wrapper-launched TSan serial arms
(`serial1`/`serial2`) hung** -- not slow, **actually stuck**: `ps`
showed a static `TIME` field across many checks spanning over an hour;
`/proc/<pid>/stat`'s `utime` field was **byte-identical across a
direct 5-second recheck** (zero CPU progress), the single thread
(`/proc/<pid>/task` confirmed only one) was parked in
`futex_wait_queue` (per `/proc/<pid>/wchan`), and `serial1.log`'s last
line was written at `01:28`, over an hour before the processes were
killed. Both had burned **~294 CPU-seconds** in an initial ~5-6 minute
active window (`utime=29375` clock ticks / 100Hz) before going
permanently idle -- i.e. they made real simulation progress for a
few minutes, then hung forever, not "ran very slowly the whole time."
**Killed both (`SIGTERM`, clean exit) after confirming zero progress**
rather than let them sit on the reserved serial cores indefinitely.

**This is important precisely because it's serial mode**: `inParallelMode`
is false with only one `EventQueue`/one host thread in serial mode, so
**no cross-domain race of any kind is possible** -- this hang cannot be
the S-015 bug or any variant of it. It must be either (a) a TSan-build-
specific issue unrelated to this investigation (e.g. a genuine
TSan-internal deadlock, rare but documented to happen in complex
codebases, or an interaction between TSan's instrumentation and some
gem5 primitive that assumes real wall-clock timing), or (b) -- and this
now looks more likely given §7.3's still-unresolved "wrapper vs.
standalone launch" puzzle -- **something about the specific
`nohup driver.sh -> function() { ... } & -> wait` launch structure**
used for every `serial1/serial2/spin1/spin2/spin1v2/spin2v2` run in
this section, as opposed to the flatter `env ... command > log 2>&1 &`
structure used for `diag2/diag3/diag4` (all three of which completed,
either via crash or clean exit, with no hangs). **A hang in serial
mode is strong new evidence for (b) over (a)** -- if the wrapper
launch method itself has some interaction (buffering, signal
delivery, process-group/session semantics under `nohup` + a
backgrounded shell function) that TSan-instrumented binaries are
sensitive to, that would explain both the serial hang here and
§7.3's unexplained early spin exits, without needing a second,
unrelated TSan bug. **Not confirmed, flagged for whoever next uses
this A/B protocol under TSan**: prefer the flat `env VAR=val ... cmd >
log 2>&1 &` launch form over a nested wrapper-script/function
structure when running TSan builds in this project.

**Practical bottom line for this exercise**: the correctness
cross-check that `serial1`/`serial2` were meant to provide is not lost
-- `diag3`'s clean, byte-matching completion already serves that
purpose (same `finalTick`/`simInsts` as the long-established serial
reference number). The wrapper-script hang is a new, real, but
*separate* problem from S-015 and does not weaken §7.3's root-cause
conclusion; it's recorded here so it isn't rediscovered from scratch
next time this project runs a TSan A/B.

**Total wall-clock for this whole TSan exercise**: session start to
`diag3`/`diag4` completion, ~2.5 hours elapsed (`01:22`-ish build
start to `~02:32` conclusion), most of it spent on setup, the
`halt_on_error` red herring, and the serial hang investigation rather
than the TSan runs' own compute time (each individual run: 5-15
minutes).

## 8. Step-4 restructure sketch + lock-ordering proof (design only, not
implemented) -- and a correction: pure locking is INSUFFICIENT for the
`sendEvent`-scheduling half of the race

Before writing any code, sketched the §6 step-4 fix concretely and read
the actual `EventQueue` scheduling contract to ground the lock-ordering
proof. This surfaced a correction to an earlier framing in this spec (and
to a claim made mid-session that "an object-level `PacketQueue` lock is
the complete fix surface"): **a `pqLock` alone cannot fix the whole bug**,
because part of the confirmed race is on `sendEvent`'s *scheduling*, and
gem5's `EventQueue` forbids cross-domain rescheduling by assertion, not by
a lock a `PacketQueue` could share.

### 8.1 The decisive EventQueue facts (read from `src/sim/eventq.hh/.cc`)

- `EventQueue::schedule()` on a **foreign** domain routes through
  `asyncInsert()` (`eventq.hh:797` -> `eventq.cc:459`), which locks
  `async_queue_mutex`, pushes to `async_queue`, unlocks; the owner later
  drains it via `handleAsyncInsertions()` (`eventq.cc:467`, calls only
  `insert`, never `remove`). This is the **one** cross-domain-safe
  scheduling primitive, and it can only *add* an event, never move one
  earlier.
- `EventQueue::reschedule()` (`eventq.hh:839`) and `deschedule()` (`:815`)
  both `assert(!inParallelMode || this == curEventQueue())` -- **owner
  thread only** -- and manipulate the bin structure (`remove()`+`insert()`)
  with no async path.
- But `PacketQueue::schedSendEvent()` calls `em.reschedule(&sendEvent,
  when)` on its "move-earlier" branch (`packet_queue.cc:199`), and
  `schedSendEvent` is reachable cross-domain (the confirmed §7.2 T1 path:
  a core-domain thread through `schedTimingResp`). **A foreign thread
  calling `reschedule` does `remove()` on a queue it doesn't own, racing
  the owner's `serviceOne()` -- this is precisely the §1b `eventq.cc:223`
  "event not found!" and §1a "event scheduled in the past" crash
  variants.** No `PacketQueue`-level lock can serialize this against
  `serviceOne()`, which is generic `EventQueue` code that will never take
  `pqLock`.

**Consequence**: the robust fix is necessarily a *hybrid* -- `pqLock` for
the data members (`transmitList`/`waitingOnRetry`), plus a structural
owner-thread-only handoff for `sendEvent` scheduling (a narrow piece of
the §6 step-5b idea). The step-4/step-5b framing in §6 was a false binary
for the scheduling half.

### 8.2 State model

Add to `PacketQueue`: `mutable UncontendedMutex pqLock;` (a strict leaf
lock), `bool sending = false;` (send-in-progress guard, under `pqLock`),
and for the cross-domain scheduling handoff an `EventFunctionWrapper
crossWakeEvent` (owner runs it) plus `std::atomic<bool> wakePending`.

**The invariant the whole proof rests on:** `pqLock` is held *only* around
manipulation of `transmitList`, `waitingOnRetry`, and `sending`. It is
released before **every** call that leaves the object -- `sendTiming()`,
`em.schedule()`, `em.reschedule()`, `signalDrainDone()`, DPRINTF. It never
nests with itself or any other lock.

### 8.3 Data half -- `pqLock` as a leaf, with unlocked cores

Public methods become thin locked wrappers over unlocked cores, so no
locked method calls another locked method (defeats the non-recursive
self-deadlock the §6-step-4-verbatim list would hit). `retry()`:

```cpp
void PacketQueue::retry() {
    {   std::lock_guard<UncontendedMutex> l(pqLock);
        assert(waitingOnRetry);
        waitingOnRetry = false;
    }
    sendDeferredPacket();          // pqLock NOT held across this
}
```

### 8.4 The consistency window -- why `sending` is needed

`sendDeferredPacket` must not hold `pqLock` across `sendTiming()` (leaf
rule; also what lets the documented x86-PTW reentrant `schedSendTiming` on
the *same* queue, `packet_queue.cc:127-133`, take `pqLock` cleanly instead
of self-deadlocking). Dropping the lock across the send opens a window
where another thread sees the packet popped but `waitingOnRetry` still
false. The `sending` flag closes it -- a concurrent `scheduleSend` treats
`sending` exactly like `waitingOnRetry` (don't schedule; the in-flight
send settles it). This is the `inTransmit`-style flag §7.2's draft
mis-guessed at -- it belongs in the *fix*, not the diagnosis.

```cpp
void PacketQueue::sendDeferredPacket() {
    DeferredPacket dp;
    {   std::lock_guard<UncontendedMutex> l(pqLock);
        assert(!waitingOnRetry);
        assert(deferredPacketReady());
        dp = transmitList.front();
        transmitList.pop_front();
        sending = true;
    }
    bool ok = sendTiming(dp.pkt);          // cross-domain, NO lock held
    Tick next;
    {   std::lock_guard<UncontendedMutex> l(pqLock);
        sending = false;
        waitingOnRetry = !ok;
        if (!ok) transmitList.emplace_front(dp);
        next = ok ? deferredPacketReadyTime() : MaxTick;
    }
    if (ok) scheduleSend(next);            // pqLock NOT held
}
```

### 8.5 Scheduling half -- owner-thread-only, with a cross-domain handoff

```cpp
void PacketQueue::scheduleSend(Tick when) {
    { std::lock_guard<UncontendedMutex> l(pqLock);
      if (waitingOnRetry || sending) return; }        // snapshot gate
    if (when == MaxTick) { /* drain check, unchanged */ return; }
    when = std::max(when, curTick() + 1);

    if (!inParallelMode || curEventQueue() == em.eventQueue()) {
        // B1 same-domain: legal to touch sendEvent directly (as today)
        if (!sendEvent.scheduled())        em.schedule(&sendEvent, when);
        else if (when < sendEvent.when())  em.reschedule(&sendEvent, when);
    } else {
        // B2 cross-domain: MUST NOT touch sendEvent.scheduled()/reschedule
        when = snapToQuantum(when);        // existing grid-anchored snap
        if (!wakePending.exchange(true))   // at most one outstanding
            em.schedule(&crossWakeEvent, when);  // -> asyncInsert(), safe
    }
}

// runs on the OWNER thread only (crossWakeEvent fired from its own queue)
void PacketQueue::ownerReschedule() {
    wakePending.store(false);
    scheduleSend(deferredPacketReadyTime());   // now same-domain -> B1
}
```

The foreign thread never touches `sendEvent`; it posts `crossWakeEvent`
via the only cross-domain-safe primitive (`asyncInsert`), and the owner
does the actual `reschedule` on its own thread where it's legal.
Idempotent: a spurious wake just recomputes; `wakePending` collapses
bursts. One extra wake-hop of latency, snapped to a quantum boundary --
timing-consistent with the relaxed model, same philosophy as the existing
`schedSendEvent` snap.

### 8.6 Lock-ordering proof

Locks in play: `pqLock` (per queue), `layerLock` (per xbar), `pioLock`
(per device), `async_queue_mutex`/`service_mutex` (per `EventQueue`),
other queues' `pqLock`. A deadlock requires a cycle in the "thread holds X
while acquiring Y" graph.

1. **`pqLock` has no outgoing edges.** By the leaf rule it is released
   before every call that leaves `PacketQueue`; the only work under it is
   `std::list` ops and scalar flag reads/writes -- none acquire a lock. So
   no thread ever holds `pqLock` while acquiring `layerLock`, `pioLock`,
   `async_queue_mutex`, `service_mutex`, or another `pqLock`.
2. **Incoming edges are harmless.** Others acquire `pqLock` while holding
   their lock -- e.g. `NoncoherentXBar::recvReqRetry` holds `layerLock` ->
   `reqQueue.retry()` -> `pqLock` (edge `layerLock -> pqLock`). Since
   `pqLock` has no outgoing edge, no such edge can close a cycle.
3. **`async_queue_mutex` stays a leaf too:** `asyncInsert()` locks/pushes/
   unlocks with no nested acquire, and we never call `em.schedule()` while
   holding `pqLock`, so no `pqLock -> async_queue_mutex` edge exists.
4. The pre-existing graph (`layerLock`/`pioLock`/EventQueue mutexes) was
   acyclic before this change and is unchanged. Adding a vertex (`pqLock`)
   with only incoming edges cannot create a cycle. QED.

**Deadlock-free by construction**, and it holds regardless of what the
peer's `recvTiming*` does -- precisely because `pqLock` never spans the
send.

### 8.7 Coverage against the confirmed TSan races (§7)

| Confirmed race | Closed by |
|---|---|
| `schedSendEvent:159` read vs `sendDeferredPacket:232` write of `waitingOnRetry` | `pqLock` (8.3/8.4) |
| `transmitList` size / list ops | `pqLock` |
| `retry()` / `deferredPacketReady()` | `pqLock` |
| `eventq.cc:223` reschedule / "event not found!" | **B2 handoff (8.5)** -- foreign thread stops calling `reschedule`; *not* lockable |

The last row is the point of the whole sketch: a pure step-4 `pqLock`
leaves that race live. The hybrid (8.3-8.5) is what closes all four.

### 8.8 Status and open items

- **Not implemented.** This is a design sketch only; no code written.
- **Still needs, before/at implementation:** (a) confirm `em.eventQueue()`
  is the right "owning domain" handle to compare against `curEventQueue()`
  in `scheduleSend` (the existing `schedSendEvent` snap already uses
  exactly this comparison, `packet_queue.cc:186`, so it's consistent); (b)
  decide whether `crossWakeEvent`/`wakePending` live in `PacketQueue` base
  or only where cross-domain sends actually occur (base is simpler, costs
  one `EventFunctionWrapper`+atomic per queue); (c) `drain()`/`MaxTick`
  drain-signalling path must also respect `sending` so a drain can't
  complete mid-send; (d) verify no subclass override of
  `sendDeferredPacket` (the cache's request queue overrides it) breaks the
  `sending`-guard contract -- that override must adopt the same
  lock/guard discipline or be audited as same-domain-only.
- **Verification plan when built:** rerun the §7 TSan A/B (expect the four
  race signatures in the table to disappear, `diag2`/`diag4` repro to go
  clean) + the §1a bare-repro batch (expect 0/20) + the S-009 §27
  short-window byte-identical `stats.txt` regression.

## 9. DECISION (2026-07-17): the §8 hybrid is chosen as the S-015 fix;
full §6-step-5b deferral is REJECTED as this bug's fix (recorded as a
future architecture direction, not discarded)

The user delegated the open hybrid-vs-defer design decision ("settle
it"); this section records the decision and its grounds so the next
session can implement without re-litigating. Every code fact cited was
re-verified against the tree this session, not carried over from
memory.

### 9.1 The decision

Implement the fix as sketched in §8: a strict-leaf `pqLock` over
`PacketQueue`'s data members (`transmitList`, `waitingOnRetry`, a new
`sending` guard), restructured as unlocked cores + thin locked
wrappers, **plus** the owner-thread-only `sendEvent` scheduling handoff
(`crossWakeEvent` via `asyncInsert`, §8.5). Do **not** pursue §6 step
5b's full defer-to-owning-domain redesign as the S-015 fix.

### 9.2 Grounds

1. **Full deferral cannot cover the confirmed race surface without a
   flow-control protocol redesign — so the lock is needed either way.**
   The entries that *can* be deferred as-is are the void-returning ones
   (`recvRespRetry`/`recvReqRetry`, `qport.hh:69`/`:121`). But the
   TSan-confirmed §7.2 T1 race enters through
   `NoncoherentXBar::recvTimingResp` → `cpuSidePorts[id]->
   schedTimingResp(...)` (`noncoherent_xbar.cc:225`) running on a
   core-domain thread — and `recvTimingReq`/`recvTimingResp` return a
   **synchronous flow-control `bool`** (`port.hh:255`, `:454`) that the
   caller consumes inline. Deferring those means either always-accept
   (unbounded buffering — a real protocol change, not a fix) or a
   proper inter-domain channel with its own backpressure (see ground
   2). Deferring *only* the retries leaves the confirmed
   `schedTimingResp` race live, so `pqLock` is required regardless.
   The actual menu was never "lock vs. defer" — it was "hybrid" vs.
   "hybrid *plus also* defer the retries", and the extra deferral adds
   timing perturbation without closing any additional confirmed race.

2. **The version of 5b that would genuinely retire the bug family is a
   domain-boundary message-passing architecture** — defer at the
   *source* of every cross-domain call, with an explicit inter-domain
   channel handling flow control (parti-gem5's actual design). That is
   a new investigation (its own S-NNN), not a bug fix: it invalidates
   the byte-identical-`stats.txt` regression baseline every validation
   in this project rests on, requires re-quantifying timing accuracy
   from scratch, and is disproportionate for what is (in this topology)
   rare PIO/interrupt traffic — all while the project's real open
   question (path 3, getting past 0.91x) is untouched. If the family
   persists after this fix (see 9.4), that investigation is the right
   response — as architecture, with its own spec, not as gap-#6
   patching.

3. **The hybrid does not repeat the gap-#N failure mode that motivated
   5b.** Every prior partial fix locked *callers* — three
   `NoncoherentXBar` entry points (S-009 §24), `PioDevice` entries
   (S-009 §25), `releaseLayer()` (§1c) — and each time the next gap was
   a caller chain that couldn't see (or never asked for) the lock.
   `pqLock` lives **inside the raced object**: every chain that mutates
   `transmitList`/`waitingOnRetry` does so through `PacketQueue`'s own
   methods, so the "unaware caller" failure mode is structurally gone
   *for this object's state*. And the one operation an object-level
   lock cannot fix — a foreign thread calling `em.reschedule()`
   (owner-thread-only by assertion, `eventq.hh:844`) — is exactly where
   §8 already applies 5b's principle (the B2 handoff). The hybrid is
   not "lock #4 waiting for gap #5"; it is "lock the state itself, and
   defer the one operation that is un-lockable."

4. **Timing/validation asymmetry.** The B2 handoff posts
   `crossWakeEvent` at the same grid-snapped tick today's cross-domain
   `schedSendEvent` snap already computes (`packet_queue.cc:186-199`),
   so the hybrid is *expected* stats-neutral in the validated S-009 §27
   window (must still be verified, §8.8). Full retry-deferral moves
   retry delivery to quantum boundaries → changes stats everywhere and
   forfeits the byte-identical regression protocol.

5. **Deadlock safety is already proven for the hybrid** (§8.6 leaf-lock
   proof), and its known implementation caveats are catalogued (§8.8);
   none is a blocker.

### 9.3 Costs accepted, with eyes open

- One uncontended `compare_exchange_strong` per queue operation on
  **every** classic `PacketQueue` in **every** config — including
  pure-classic-memory configs where these queues sit on the cache hot
  path even in serial mode (`UncontendedMutex`'s fast path,
  `base/uncontended_mutex.hh`). If a serial-mode perf regression is
  measurable, gate the lock on `inParallelMode` at implementation time
  — decide by measuring, not by guessing.
- Added state-machine complexity: the `sending` flag, its drain-path
  interaction, and the cache's `sendDeferredPacket` override audit
  (§8.8 items c/d) are now mandatory implementation work, not
  optional hardening.

### 9.4 Falsifiers — conditions under which this decision gets revisited

- The prototype cannot hold `stats.txt` byte-identical in the S-009
  §27 short window (i.e. the B2 handoff turns out not to be
  timing-neutral where it must be), **or**
- post-fix TSan finds the family continuing in a *fifth* object
  outside `PacketQueue` (another cross-domain-mutated structure with
  its own unaware callers).

Either of those means stop patching and open the domain-boundary
channel investigation (ground 2's architecture) as its own spec,
rather than writing a gap-#6 fix.

### 9.5 Scope note

This settles the *design*. Implementation (per §8, with §8.8's open
items) and its verification (§8.8's plan: TSan A/B re-run expecting the
four §8.7 signatures to disappear, §1a-style bare batch expecting 0/20,
S-009 §27 byte-identical short-window regression) are the next
sub-phase and were not started in the deciding session.

## 10. Implementation of the §8 hybrid (2026-07-17, branch `s015-packetqueue-retry-race`)

Implemented per the §9 decision and the §8 sketch. Branch created off
`main` with its own worktree/tmpfs per ADR 0001 (the S-015 number was
already claimed, so no new INDEX row).

### 10.1 What changed

`src/mem/packet_queue.{hh,cc}`:
- New state (§8.2): `mutable UncontendedMutex pqLock` (strict leaf) and
  `bool sending` (protected, so the cache override can use them);
  `EventFunctionWrapper crossWakeEvent` + `std::atomic<bool> wakePending`
  and a private `processCrossWake()` for the B2 handoff.
- `retry()` — thin locked wrapper: clears `waitingOnRetry` under `pqLock`,
  then calls `sendDeferredPacket()` with the lock released (§8.3).
- `schedSendTiming()` — the `transmitList` insert (and the 1024-packet
  sanity check) now run under `pqLock`; `schedSendEvent()` is called after
  the lock is released, and only on the emplace-front branch as before.
- `schedSendEvent()` — restructured into §8.5: a `pqLock`-guarded snapshot
  gate (`waitingOnRetry || sending` → hold off), then the MaxTick/drain
  branch (reads `transmitList.empty() && !sending` under lock), then the
  B1 same-domain path (touches `sendEvent` directly, owner-thread-only) vs
  the B2 cross-domain path (snap to quantum + `crossWakeEvent` via
  `asyncInsert`, guarded by `wakePending.exchange`).
- `processCrossWake()` — owner-thread callback: clears `wakePending`,
  reads `deferredPacketReadyTime()` under lock, calls `schedSendEvent()`
  (now on the B1 path).
- `sendDeferredPacket()` — the §8.4 pop-under-lock / send-unlocked /
  settle-under-lock structure with the `sending` guard; `schedSendEvent()`
  called only on success, lock released.
- `processSendEvent()` — dropped the redundant unlocked `assert(!
  waitingOnRetry)` (would be a TSan-flagged read vs `retry()`);
  `sendDeferredPacket()` asserts it under lock.
- `drain()` — now returns Drained only if `transmitList.empty() &&
  !sending` under lock (§8.8 item c).

`src/mem/cache/base.cc` — `BaseCache::CacheReqPacketQueue::
sendDeferredPacket()` (§8.8 item d): adopts the same discipline —
`waitingOnRetry` read/written under `pqLock`, `sending` held only around
the outbound `entry->sendPacket(cache)`. `sending` is deliberately NOT
set across `checkConflictingSnoop()`, which itself calls
`schedSendEvent()` (that would no-op under the `sending` gate).

### 10.2 Design points worth noting for a future reader

- The query methods that only *read* `transmitList` (`checkConflict`,
  `trySatisfyFunctional`, `size`, the public `deferredPacketReadyTime`,
  `deferredPacketReady`) are intentionally left unlocked: they are called
  from within `pqLock`-held regions (`sendDeferredPacket`,
  `processCrossWake`) and `UncontendedMutex` is non-recursive, so locking
  them would self-deadlock. This matches §8.2's "lock only the mutations"
  invariant. They were not in the §8.7 confirmed-race set; if post-fix
  TSan flags one, that is the §9.4 falsifier-2 condition, to be recorded,
  not silently patched.
- Lock is a strict leaf (§8.6): every outbound call (`sendTiming`,
  `em.schedule/reschedule`, `signalDrainDone`, DPRINTF) is made with
  `pqLock` released, so `pqLock` has no outgoing edges and cannot be in a
  deadlock cycle.

### 10.3 Verification done so far — PASS (short window)

S-009 §27 short-window regression, this build
(`build/X86_MESI_Three_Level/gem5.opt`, checkpoint
`x86-threads3-roi-classic`, `SIM_QUANTUM_TICKS=6660`,
`MAX_TICKS=2e8`):
- serial (`taskset -c 54`): clean, `simInsts=74062` (matches the S-009
  §27 reference).
- parallel/spin (`taskset -c 92-99`, `HOST_PIN_CPUS=92,93,…,99`): clean,
  `simInsts=74062`, **no deadlock** (the primary risk of a locking
  change), and `stats.txt` **byte-identical** to serial (excluding
  `host*` lines).

This confirms the fix is a no-op in the already-validated window and
that the B2 handoff is timing-neutral there — i.e. §9.4 falsifier-1 is
NOT triggered. It does **not** yet confirm the fix stops the crash: the
2e8 window ends at tick ≈5.3046e9, short of the ≈5.306e9 historical
crash cluster.

(Operational note for the next runner: the config script's
`HOST_PIN_CPUS` is comma-separated and `int()`-parsed per element —
pass `92,93,…,99`, not the range `92-99`, or it aborts with a
`ValueError` before simulating.)

### 10.4 Verification still to do (the crash-confirmation sub-phase)

Per the §8.8 / §9.5 plan, still outstanding (qualitatively riskier —
long runs, possible live debugging — so gated on a checkpoint per the
project working style):
- **§1a-style bare-repro batch at `MAX_TICKS=2e9`** (reaches the
  ≈5.306e9 crash tick): expect **0 crashes / 20 runs** (pre-fix rate was
  3/20 ≈ 15%).
- **§7 TSan A/B re-run** on `build/X86_MESI_Three_Level_TSAN/gem5.opt`:
  expect the four §8.7 race signatures to disappear and the `diag2`/
  `diag4` standalone repro to go clean (use the flat launch form, not the
  wrapper script — §7.4; set `TSAN_OPTIONS=halt_on_error=0`).

---

**Related**: [S-014](./S-014-occupylayer-crossdomain-crash-beyond-tested-window.md)
(where this was found, while confirming §8's fix), [S-009 §18-25](./S-009-raise-fs-quantum-past-iobus-edge-design.md)
(the `layerLock`/`pioLock` precedent), [S-011](./S-011-consumer-lock-owner-race-audit.md)
(prior non-deterministic-race investigation in this project, including
the "designed a stress test, user chose not to run it" precedent)
**Return**: [INDEX.md](./INDEX.md)
