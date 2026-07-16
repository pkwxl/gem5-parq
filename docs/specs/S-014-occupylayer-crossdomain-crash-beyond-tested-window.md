# S-014 — `BaseXBar::Layer::occupyLayer` cross-domain crash beyond any
previously-tested window

**Status: crash reproduced and root-caused at the source level; not fixed,
not yet scoped. Discovered as a side effect of S-013, but it is NOT about
S-013's four-core-balance question — it's a correctness gap in the
grid-anchored snap fix S-009 shipped, and it directly qualifies S-009's
"Q=6660, 0.91x speedup, validated" conclusion.** This surfaced because the
user asked, correctly, whether S-012/S-013's critical-path runs used too
few instructions and should be extended to cover more of the ROI
(`MAX_TICKS=2e8` only reaches ~74,588 instructions, essentially still
inside guest thread/kernel startup, per S-013 §7's realization) --
extending the window immediately hit this.

## 1. What happened

Re-ran the S-013 balanced checkpoint (`x86-threads-balanced3-roi-classic`)
with the `MAX_TICKS` cap removed entirely (unbounded -- run until the
guest's own `m5 exit` at the end of the benchmark script), same clean
Step-3 build and operating point as S-012 §13/S-013 §6-7 otherwise
(`SIM_QUANTUM_TICKS=6660`, `EVENTQ_BARRIER_MODE=spin`,
`HOST_PIN_CPUS=92,93,94,95,96,97,98,99`). It crashed:

```
gem5.opt: src/sim/eventq.hh:784: void gem5::EventQueue::schedule(gem5::Event*, gem5::Tick, bool): Assertion `when >= getCurTick()' failed.
Program aborted at tick 5303840987196
```

Backtrace (child domain thread, not domain 0):

```
BaseXBar::Layer<ResponsePort, RequestPort>::occupyLayer
NoncoherentXBar::recvTimingReq
ReqPacketQueue::sendTiming
(EventQueue::serviceOne dispatch)
EventQueue::serviceOne
doSimLoop
SimulatorThreads::runUntilLocalExit (child-thread lambda)
```

**Re-ran the same unbounded window against the *original*
`x86-threads3-roi-classic` checkpoint** (not the S-013 balanced one) to
check whether this was specific to the new benchmark -- **it crashed too**,
same assert, identical backtrace, at tick `5304839812039` (same order of
magnitude past restore as the balanced run's crash). **This rules out any
connection to S-013's benchmark changes** -- it's a property of the
Q=6660 parallel-EventQueue operating point itself once run long enough,
independent of which checkpoint or workload is staged.

Neither crashed run produced any `critpath-domain*.csv` output --
`abort()` does not run `atexit` handlers or reach the child threads'
normal loop-exit flush point, so `critPathFlush()` never ran. No
instruction-count-at-crash data is available from these runs; the tick
values above come only from the abort message itself and the balanced
checkpoint's earlier truncated-window CSV (restore tick `5303194846983`,
giving a crash roughly 646M ticks -- about 3.2x -- past the previously-
tested `MAX_TICKS=2e8` boundary). The original checkpoint's own restore
tick wasn't independently re-measured this session; it's very likely close
to the same value (near-identical boot sequence up to the staged binary),
but that's an inference, not a re-confirmed number -- flagged here rather
than stated as fact.

## 2. Root cause (read from source, matches the crash exactly)

`BaseXBar::Layer<SrcType, DstType>::occupyLayer` (`src/mem/xbar.cc:165`):

```cpp
void BaseXBar::Layer<SrcType, DstType>::occupyLayer(Tick until)
{
    assert(state == BUSY);
    assert(until != 0);
    xbar.schedule(releaseEvent, until);   // <-- raw schedule, no snap
    occupancy += until - curTick();
}
```

Called from `NoncoherentXBar::recvTimingReq` (`src/mem/noncoherent_xbar.cc:171`,
via `Layer::succeededTiming`), where `until` (`packetFinishTime`) is
computed a few lines earlier:

```cpp
Tick packetFinishTime = clockEdge(Cycles(1)) + pkt->payloadDelay;
...
reqLayers[mem_side_port_id]->succeededTiming(packetFinishTime);
```

`clockEdge()` resolves relative to whatever `curTick()` the **calling**
thread sees at the moment `recvTimingReq` executes -- and `recvTimingReq`
is reached via a synchronous cross-domain port call (`sendTimingReq`),
so the calling thread can be a different domain's thread than the one
that owns this crossbar's `EventQueue`. If the caller's clock frame is
behind the target queue's already-advanced `curTick()` by the time the
call lands, `until < target_queue.curTick()` and
`EventQueue::schedule()`'s `assert(when >= getCurTick())` fires --
**this is the exact same hazard class S-009 fixed in
`PacketQueue::schedSendEvent`** (grid-anchored `crossDomainSnap()`,
S-009 §19.2/§26.1) and in `BridgeBase`'s `schedTimingReq`/`schedTimingResp`
(S-009 §26.5) -- but `occupyLayer`'s direct `xbar.schedule(releaseEvent,
until)` call was **never wrapped with that snap**. Confirmed by reading
both files: `occupyLayer` lives in `src/mem/xbar.cc`, entirely separate
from `schedSendEvent` in `src/mem/packet_queue.cc` -- S-009's "one change
covers all call sites" claim (§19.3, itself already noted in INDEX.md as
not holding for `BridgeBase`) doesn't hold for this call site either.

This is a **third gap** in the grid-anchored snap's coverage, after
`PacketQueue::schedSendEvent` (fixed, §19.2/§26.1) and `BridgeBase`
(fixed, §26.5) -- `BaseXBar::Layer::occupyLayer` was missed by both the
original design and the later `BridgeBase` audit.

## 3. Why this wasn't caught before

S-009 §27 (`docs/specs/S-009-raise-fs-quantum-past-iobus-edge-design.md:900`)
validated the Q=6660 step -- the basis for the project's headline "0.91x
speedup, matches projection" result (§27.4) -- using
**`MAX_TICKS=2e8`, the same window S-013 §7 found only reaches ~74,588
instructions**, i.e. still inside guest thread/kernel startup, nowhere
near a representative slice of real workload execution. Every later
validation this project has done at Q=6660 (S-010's TSan A/B, S-011's TSan
A/B, S-012's Step 1-3 regressions) reused this same small window or the
`MAX_TICKS=1.3e9` TSan-build window (which runs ~1-2 orders of magnitude
slower under TSan instrumentation, so 1.3e9 ticks there corresponds to far
fewer *wall-clock-normalized* guest instructions than 1.3e9 ticks would at
full speed) -- none of the prior byte-identical-`stats.txt` A/B validations
were long enough, at full simulation speed, to reach this crash.

**This means the "Q=6660 validated" claim needs a caveat it doesn't
currently have**: it was validated within a window that never left guest
boot/thread-startup, not across anything resembling a real workload's
steady-state execution. Whether the 0.91x number itself would hold up
over a longer, crash-free run is now an open question, not a settled one.

## 4. Scope and severity

- **Not** a S-013 (four-core-balance) issue -- confirmed checkpoint-
  independent (§1).
- **Is** a real correctness bug in the multi-EventQueue mechanism itself,
  in the exact subsystem (IOXBar cross-domain PIO forwarding) S-009 spent
  the most effort hardening.
- **Blocks** any critical-path analysis (S-012 Step 3/4/5) or "outlet 3"
  design work that needs a run longer than ~2e8 ticks past restore --
  which is very likely necessary to see genuine steady-state behavior
  rather than boot noise (S-013 §7's finding).
- **Blocks** ever running this operating point to a full, real ROI
  (thousands of times longer than 2e8 ticks) without hitting this crash
  first.

## 5. Not attempted here

No fix was written or attempted. The `occupyLayer` call site could
plausibly take the same `crossDomainSnap()` treatment as
`PacketQueue::schedSendEvent`/`BridgeBase` (§2), but:

- Other `BaseXBar::Layer::occupyLayer` call sites (`CoherentXBar`, and the
  two other calls in `xbar.cc` at lines 224/304, one of which look
  domain-local rather than cross-domain) haven't been audited -- unclear
  yet whether all of them need the fix or just the cross-domain-reachable
  ones, mirroring the exact kind of call-site-by-call-site audit S-009
  §18/§23 did for the original `PacketQueue`/`BaseXBar` locking work.
- No TSan or extended-window regression protocol has been designed for
  this yet.
- This is squarely new, likely-needs-live-debugging territory in the
  core sync mechanism -- per `CLAUDE.md`'s explicit guidance, this needs a
  checkpoint with the user before starting, not autonomous continuation.

## 6. Suggested next steps (not started, for discussion)

1. Audit every `Layer::occupyLayer` call site across all `BaseXBar`
   subclasses (`CoherentXBar`, `NoncoherentXBar`, others) for cross-domain
   reachability, the same way S-009 §18/§23 audited the locking gaps.
2. Apply `crossDomainSnap()` (or equivalent) to the cross-domain-reachable
   ones, following the exact pattern already used for
   `PacketQueue::schedSendEvent`/`BridgeBase`.
3. Re-run this same unbounded-window reproduction after the fix to confirm
   it survives past the previous crash point; then decide how much longer
   a window is actually needed to reach steady-state before trusting any
   critical-path or speedup number derived from it.
4. Reassess whether S-009's "0.91x, validated" conclusion needs a formal
   caveat added given §3 above, independent of whether this bug gets fixed
   -- that's a documentation/conclusion-scoping question the user may want
   handled separately from the code fix.

No code changes made in this investigation.

---

**Related**: [S-009 §19-27](./S-009-raise-fs-quantum-past-iobus-edge-design.md),
[S-013](./S-013-balanced-four-core-workload-checkpoint.md) (where this was
found)
**Return**: [INDEX.md](./INDEX.md)
