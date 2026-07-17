# S-014 — `BaseXBar::Layer::occupyLayer` cross-domain crash beyond any
previously-tested window

**Status: crash reproduced and root-caused at the source level; call-site
audit (§7) done; fix implemented and compiled (§8) -- `crossDomainSnap()`
wrapped inside `Layer::occupyLayer()` itself, covering all four call
chains per the user's explicit choice of the broad option; short-window
regression check (same protocol as S-009 §27) is clean (byte-identical
`stats.txt`, no crash). **Not yet done: re-running the actual unbounded-
window reproduction to confirm the fix survives past the original crash
point** (§8, awaiting a scope/duration check-in before launching a
possibly-long run) -- so this bug is not yet confirmed *fixed* in the
scenario that found it, only shown not to regress the short window.
Discovered as a side effect of S-013, but it is NOT about S-013's
four-core-balance question — it's a correctness gap in the grid-anchored
snap fix S-009 shipped, and it directly qualifies S-009's "Q=6660, 0.91x
speedup, validated" conclusion.** This surfaced because the
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

## 7. Call-site audit (step 1 of §6, read-only, this session)

Per S-009 §18/§23's precedent, audited every path that reaches
`Layer::occupyLayer` for cross-domain reachability, using this project's
*actual* instantiated topology (`x86_fs_mesi3_parallel_eventq.py` +
`MESIThreeLevelCacheHierarchy`), not just the generic `BaseXBar` class
hierarchy. No code was changed; this is groundwork for whichever of steps
2-4 the user greenlights next.

**Finding: `CoherentXBar` is not instantiated at all in this project's
topology.** Ruby (`MESIThreeLevelCacheHierarchy`) never builds a
`CoherentXBar`/`L2XBar`/`SystemXBar` -- confirmed by grepping
`src/python/gem5/components/cachehierarchies/ruby/*.py` for `XBar` (no
hits). The only `BaseXBar` subclass this project's binary ever builds is
`IOXBar` (`= NoncoherentXBar`, `src/mem/XBar.py:209`), used as
`X86Board.iobus` (`x86_board.py:119`). So for the purposes of this fork's
actual operating point, only `NoncoherentXBar`'s three call chains matter;
`CoherentXBar`'s four `occupyLayer` call sites
(`coherent_xbar.cc:213/322/352/652/684`) are dead code here (still
theoretically reachable if a future config adds a classic coherent xbar
anywhere, but not exercised by anything this project runs today).

**`occupyLayer` has three call chains, all ultimately in `xbar.cc`:**

| Call chain | Layer | Reachable how (in this topology) | Cross-domain? |
|---|---|---|---|
| `recvTimingReq` → `succeededTiming` (`noncoherent_xbar.cc:171`) | `reqLayers` | `iobus.cpu_side_ports` ← `pci_host.up_request_port()` ← per-core Ruby `Sequencer`/RubyPort PIO forwarding (`Pc.py:109`, `x86_board.py:143-145`) | **Yes -- confirmed.** This is the exact call chain the crash backtrace shows (§1-2): the calling thread is whichever core domain (i+1) issued the PIO request; `iobus` itself sits on domain 0 (`eventq_index` never set for it in `_pre_instantiate`, so it keeps the default 0). Matches S-009's already-fixed `RubyPort.cc` PIO-forwarding edge -- same source domain, different downstream scheduling point that was missed. |
| `recvTimingReq` → `failedTiming` (`noncoherent_xbar.cc:159-160`) | `reqLayers` | Same call chain as above, taken when `memSidePorts[...]->sendTimingReq(pkt)` returns false (peer busy) instead of true | **Yes, same hazard, not yet observed in a crash.** It's the failure branch of the identical cross-domain call stack -- `clockEdge(Cycles(1))` is computed by the same wrong-domain calling thread before `occupyLayer` schedules onto `iobus`'s domain-0 queue. No structural reason this branch is safer than the success branch; it just wasn't the branch taken in the run that crashed. |
| `recvTimingResp` → `succeededTiming` (`noncoherent_xbar.cc:231`) | `respLayers` | `iobus.mem_side_ports` ← `pci_host`/south-bridge devices (IDE, PIT, RTC, serial, floppy, PCI config) -- all domain 0 per this project's domain map (`x86_fs_mesi3_parallel_eventq.py` docstring: "domain 0: DMA controllers + every device + uncore leftovers") | **No, domain-local in this topology.** Both the calling device and `iobus` are domain 0 -- same thread, so `curTick()`/`clockEdge()` agree. Not at risk *as currently wired*; would become at risk only if some mem-side device were ever moved off domain 0. |
| `recvReqRetry` → `recvRetry` → `retryWaiting` → `occupyLayer(xbar.clockEdge())` (`xbar.cc:304`) | `reqLayers` | Same mem-side devices as above (domain 0) calling back to report a retry | **No, domain-local in this topology, same reasoning as `recvTimingResp`.** Note this call site is structurally different from the other two: it computes `until` from `xbar.clockEdge()` *inside* `Layer::retryWaiting()` rather than from a value the caller passed in -- but that doesn't change the hazard shape, since `clockEdge()`/`curTick()` still resolve via whatever `EventQueue` the *executing* thread is currently bound to (thread-local `curEventQueue()`), not necessarily the domain that owns `xbar`. If this call chain is ever reached from a cross-domain caller in some future topology, it has the identical bug. |

**Conclusion for this project's current operating point (Q=6660,
3-core... 4-core MESI_Three_Level FS):** exactly one of the four
`occupyLayer` call chains needs the fix to stop the observed crash
(`recvTimingReq` → `succeededTiming`, `reqLayers`), and one more shares
its exact hazard on an untaken branch (`recvTimingReq` → `failedTiming`,
same `reqLayers`). The other two (`respLayers` success path, `recvRetry`
path) are safe *only because* every device this board attaches to
`iobus.mem_side_ports` happens to live in domain 0 -- that's an artifact
of this project's specific domain assignment (`_pre_instantiate` in
`x86_fs_mesi3_parallel_eventq.py`), not a structural guarantee in
`xbar.cc`/`noncoherent_xbar.cc` itself.

**Implication for where a fix should live (not yet decided, flagging for
whichever step comes next):** patching only the two `reqLayers` call
sites in `NoncoherentXBar::recvTimingReq` would stop today's crash and
match this project's actual reachability, but would repeat the exact
mistake S-009 made twice already (§2-3 above) -- fixing at individual
call sites that happen to be reachable *today* rather than at the shared
low-level scheduling point (`Layer::occupyLayer` itself, `xbar.cc:165`,
which every call chain funnels through). Wrapping the fix inside
`occupyLayer` once would cover all four chains (including the two that
are currently domain-local but not structurally guaranteed to stay that
way) and the still-unaudited `CoherentXBar` chains for free, at the cost
of a redundant snap on the two paths that don't need it today. This
tradeoff -- narrow/precise vs. broad/future-proof -- is a design decision
for step 2, not resolved by this audit.

No code changes made in this investigation (audit only, per user's
explicit scope decision this session -- steps 2-4 of §6 not started).

## 8. Fix implemented (§6 steps 2, this session, user explicitly requested
"wrap the fix inside occupyLayer itself, cover all four chains")

Per the §7 tradeoff, the user chose the broad option: wrap the
grid-anchored snap inside `Layer::occupyLayer()` itself rather than
patching only the two call sites confirmed reachable today. This covers
all four call chains (§7's table) plus the unaudited `CoherentXBar` chains,
at the cost of a redundant (harmless) snap check on the two paths that
don't need it in this project's current topology.

**Change** (`src/mem/xbar.hh`, `src/mem/xbar.cc`): added
`Layer<SrcType, DstType>::crossDomainSnap(Tick until) const`, same
grid-anchored pattern as `BridgeBase::crossDomainSnap()`
(`bridge.cc:57-68`) and `PacketQueue::schedSendEvent`
(`packet_queue.cc:186-192`) -- `if (inParallelMode && curEventQueue() !=
xbar.eventQueue())`, snap `until` up to the next `simQuantumStart +
k*simQuantum` boundary. `occupyLayer()` now calls
`until = crossDomainSnap(until);` immediately before
`xbar.schedule(releaseEvent, until)`; the `occupancy` stat accumulation
still uses the (now-snapped) `until` value, no change to that line's
shape. Added `#include "base/intmath.hh"` to `xbar.cc` for `divCeil`
(bridge.cc already includes it for the same reason).

**Compiled**: `build/X86_MESI_Three_Level/gem5.opt`, `taskset -c
0-53,56-91` (kept off the reserved `54-55`/`92-111` isolcpus ranges per
`CLAUDE.md`) -- clean build, no warnings from the new code.

**Quick regression check (this session)**: re-ran the exact S-009 §27
short-window protocol (same checkpoint
`x86-threads3-roi-classic`, `SIM_QUANTUM_TICKS=6660`, `MAX_TICKS=2e8`,
serial on `taskset -c 54`, parallel/spin on `taskset -c 92` with
`HOST_PIN_CPUS=92,93,94,95,96,97,98,99`) against the new build, one round
each. Result: no assert/abort/panic in either arm, `simInsts=74062` in
both (matches the S-009 §27 reference number exactly), and the two
`stats.txt` (host* fields excluded) are **byte-identical** to each other.
This confirms the fix is a no-op within the previously-validated short
window -- exactly the expected outcome, since `crossDomainSnap` should
only ever change behavior when a cross-domain call's `until` has actually
drifted behind the target domain's clock, which this window never
triggers (that's the whole reason S-014 needed a *longer* window to find
the bug in the first place).

**Not yet done**: the actual confirmation this bug fix targets --
re-running the unbounded-window reproduction from §1 (no `MAX_TICKS`
cap, same checkpoints) to see whether it now runs past the previous
crash point (~646M ticks past restore) instead of asserting -- has
**not** been run this session. That run's duration is unknown (the
crash previously hit well before any guest-side `m5 exit`, but there is
no data yet on how much further execution now proceeds, or whether it
reaches the benchmark's actual end); per `CLAUDE.md`'s checkpoint
convention this is exactly the kind of open-ended, possibly-long,
first-of-its-kind verification step to confirm scope/duration
expectations on before launching, rather than starting it unprompted.
Also still not done: auditing/testing the `CoherentXBar` call sites
(dead code in this project's topology, per §7, so lower priority), and
reassessing the "0.91x, validated" conclusion's wording (§6 step 4).

## 9. Confirmation run (§6 step 3, this session) -- original crash does
not recur, but a second, unrelated bug (S-015) now blocks a full
unbounded confirmation

Per the user's explicit choice ("launch bounded past the old crash
point," not a fully unbounded run), re-ran the same operating point as
§1 (`CHECKPOINT_DIR=x86-threads3-roi-classic`, `SIM_QUANTUM_TICKS=6660`,
`EVENTQ_BARRIER_MODE=spin`, `HOST_PIN_CPUS=92,93,94,95,96,97,98,99`)
against the fixed build, bumped to `MAX_TICKS=2e9` (~3x past the
original ~646M-ticks-past-restore crash point).

- **Serial arm**: completed cleanly, `finalTick=5306177114066`,
  `simInsts=1475264`.
- **Parallel/spin arm, 4 runs**: **0 of 4 recurrences of this bug's
  crash** (`assert(when >= getCurTick())` in `EventQueue::schedule`).
  All 4 either completed cleanly to the same `finalTick` as serial, or
  crashed with a **different** assert entirely, in a different function
  (`PacketQueue::sendDeferredPacket`'s `assert(deferredPacketReady())`)
  -- a new, non-deterministic bug, unrelated to this fix, written up
  separately as [S-015](./S-015-packetqueue-retry-race-beyond-occupylayer-fix.md).

This is a good sign for the `crossDomainSnap()` fix itself (§8) -- the
exact crash it targeted did not come back even ~3x past where it
previously fired 100% of the time -- but it is **not** the full
confirmation §6 step 3 called for. A "clean run to completion" can no
longer be treated as sufficient proof at this operating point, since a
second, independent race (S-015) can now crash the same run for an
unrelated reason. Full confirmation of this fix (a genuinely long or
unbounded run with **zero** crashes of *any* kind) is blocked until
S-015 is understood/resolved -- see S-015 §6 step 4, which loops back to
this exact task.

**Batch update (later session, S-015 repro-rate investigation)**: a
follow-up batch of 16 more identical parallel/spin runs (S-015 §1a) added
2 more S-015-class crashes (bringing the combined sample to 3 crashes in
20 runs) and **zero** additional recurrences of this bug's original
assert -- still 0 crashes of *this* specific bug across all 20 runs
tested so far, reinforcing that the `crossDomainSnap()` fix itself is
holding up; S-015 remains the sole blocker to calling this fix fully
confirmed.

## 10. §6 step 3 confirmed -- via S-015 §11.3's batch, not a fresh rerun

S-015 was fixed (§11, tolerate-spurious-retry change, commit
`c693e9777d`) and merged to `main` (`--no-ff`, `1bacbb0a7c`). Checking
which commit this fix built on: S-015's fix commits (`c815c78fec`,
`c693e9777d`) are both descendants of this spec's own fix commit
(`a3e325afb5`, §8's `crossDomainSnap()` in `occupyLayer`) -- i.e. every
build S-015 tested after its own fix already contained §8's fix too.

That means S-015 §11.3's crash-confirmation batch -- same operating
point as this spec's §9 (`x86-threads3-roi-classic`,
`SIM_QUANTUM_TICKS=6660`, `MAX_TICKS=2e9`, spin arm, `HOST_PIN_CPUS=
92-99`), run 20 times -- **is** the §6 step 3 confirmation this spec was
waiting on, not a separate result that merely clears the way for one.
Outcome: **20/20 clean, 0 crashes of any kind** (neither this bug's
original `assert(when >= getCurTick())` nor S-015's), identical
`simInsts=1475264`/`finalTick=5306177114066` across all 20 runs. S-015
§11.3 says so explicitly: "This unblocks the S-014 confirmation."

Decision this session (user-confirmed): treat that batch as the
confirmation rather than re-running an identical experiment on the same
code for a second data point. **§6 step 3 is done.**

**Still open from §6**: step 4 (reassess whether S-009's "0.91x,
validated" conclusion needs a formal caveat given the long-window crash
history this spec and S-015 uncovered -- unrelated to whether the fix
works, a documentation-scoping call for the user) and the `CoherentXBar`
call-site audit (§7 -- dead code in this project's current topology, so
low priority, still unaudited/untested).

---

**Related**: [S-009 §19-27](./S-009-raise-fs-quantum-past-iobus-edge-design.md),
[S-013](./S-013-balanced-four-core-workload-checkpoint.md) (where this was
found), [S-015](./S-015-packetqueue-retry-race-beyond-occupylayer-fix.md)
(the new bug blocking full confirmation of §8's fix)
**Return**: [INDEX.md](./INDEX.md)
