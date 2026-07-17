# S-016: Unbounded serial-mode run hangs — root cause found:
`NoncoherentXBar::recvReqRetry` holds `layerLock` across a synchronous
call chain that re-enters it; fix not yet written

> **Status: OPEN, root cause found by code reading (2026-07-17, continued
> same day), fix not yet written or verified.** Discovered by accident
> while checking on another session's live confirmation run for
> S-014/S-015. §2's original four reproductions were read as ruling out
> all of this fork's S-009-S-015 cross-domain locks — **that conclusion
> was wrong** (§3a): the "pre-S-009" bisection commit was mislabeled and
> actually included two of them (`layerLock`/`pioLock` from
> `94a0365951`, and the `AddrRangeMap` lock from `3f493c8838`). A clean
> two-point bisection resolved this: the genuine last lock-free commit
> (`796f662040`) does **not** hang (10 minutes monitored, zero stalls,
> `utime` climbing the whole time, log printing well past the
> `interrupts.cc:530` line every prior reproduction froze at — §3b);
> commit `94a0365951` itself (`layerLock`+`pioLock`, no `AddrRangeMap`
> lock yet) **does** hang, same signature, confirmed by two stall
> checkpoints (§3d). Reading `94a0365951`'s code end to end (§6) then
> found a complete, self-contained mechanism: `NoncoherentXBar::
> recvReqRetry` holds `layerLock` across `retryWaiting`→`sendRetry`→a
> peer's `PacketQueue::sendDeferredPacket`, which stock gem5's own
> (unmodified) comment already documents as re-entrant ("sending of the
> packet in some cases causes a new packet to be enqueued... leading to
> a new response") — the second, nested `recvTimingReq` call tries to
> re-acquire the same non-reentrant `UncontendedMutex`, self-deadlocking
> on the futex. No cross-domain execution needed; structurally the same
> class of bug S-014/S-015 already fixed in `packet_queue.cc`, just not
> yet applied to `layerLock`. High confidence from code reading, not yet
> confirmed by a live backtrace (`ptrace_scope=1` still blocks
> `gdb`/`strace`) or by writing/testing a fix. **This is a bug in this
> fork's own S-009 `layerLock`/`pioLock` change, not a pre-existing
> stock-gem5 bug** — §5's original hypothesis is superseded on that
> point, though the underlying `UncontendedMutex`-reentrancy mechanism it
> describes was correct in kind, just misattributed to a stock lock
> rather than this fork's own (§6).

## 1. What happened

This session was asked to check on a job "another session" had started:
a full, unbounded (no `MAX_TICKS`) serial-vs-spin A/B on plain
post-merge `main` (`build/X86_MESI_Three_Level/gem5.opt`, no TSan),
launched via flat `taskset -c 54 ...` against the checkpoint this whole
project line has used (`x86-threads3-roi-classic`). The spin arm was
healthy (7 threads at ~100% CPU). The serial arm (PID 457814) was
**hung**: single thread, `utime` frozen at ~37.7 CPU-seconds after 3.5
hours wall-clock, `wchan=futex_wait_queue`, `stats.txt` still 0 bytes,
log's last line `src/arch/x86/interrupts.cc:530: hack: Assuming logical
destinations are 1 << id.` — i.e. frozen very early, before any
simulated tick was counted.

This is the identical signature `S-015 §7.4/§12.2` documented for TSan
builds. Full narrative of how this was found and diagnosed (read-only
`/proc` inspection; `gdb`/`strace` unavailable, sandbox
`ptrace_scope=1`) is in
[S-015 §13](./S-015-packetqueue-retry-race-beyond-occupylayer-fix.md#13-the-serial-mode-hang-is-not-tsan-specific-and-not-caused-by-s-015--found-by-chance-while-checking-on-another-sessions-live-run-this-session-2026-07-17);
this document consolidates the findings and continues past where that
section left off, since the topic clearly diverged from S-015's own
(the `PacketQueue` retry race) once TSan and S-015's code were both
ruled out.

## 2. Four independent reproductions, all the same signature

| # | What changed | Build | Checkpoint | `utime` at freeze | Result |
|---|---|---|---|---|---|
| 1 | (original, other session) | post-S-015 `main` | `x86-threads3-roi-classic` | ~37.7s | hung |
| 2 | commit `a3e325afb5` (S-014 fix + early S-015 `layerLock` attempt, **no** `pqLock`/`packet_queue.cc` changes) | fresh build | `x86-threads3-roi-classic` | ~37.3s | hung |
| 3 | commit `3a030687a6` (**mislabeled** — see §3a: this commit actually *includes* S-009's lock commit `94a0365951`) | fresh build | `x86-threads3-roi-classic` | ~40.3s | hung |
| 4 | `main` (same as #1) | existing build | `x86-threads-balanced3-roi-classic` (S-013's different checkpoint) | ~36.8s | hung |

Every run: single thread, `wchan=futex_wait_queue`, `stats.txt` 0 bytes,
log tail ending at the identical `interrupts.cc:530` hack line (modulo
one fewer repeated `fwait unimplemented` warning for the different
checkpoint in #4, reflecting a different guest instruction stream up to
that point).

**What this rules out**:
- **TSan-specific** (the original `S-015 §7.4/§12.2` hypothesis) —
  falsified; #1-#4 have zero TSan instrumentation.
- **Wrapper-script launch structure** — falsified; all four used flat
  `taskset`/`exec` launches, not the nested `nohup`/function wrapper.
- **`MAX_TICKS`-bounded vs. unbounded** — not the distinguishing factor;
  `S-015 §7.4/§12.2`'s TSan hangs were on `MAX_TICKS=2e9`-bounded
  commands and still hung; #1-#4 here are unbounded and hang with the
  same signature.
- **Any of S-009 through S-015's cross-domain locking work**
  (`layerLock`/`pioLock`/`cacheLock`/`pqLock`) — **NOT actually ruled
  out; §2's original claim was wrong, see §3a.**
- **Checkpoint content** — falsified by reproduction #4, a structurally
  different guest program state from a different benchmark variant
  (S-013's "balanced" workload).

## 3a. Correction (2026-07-17, later same investigation): reproduction #3
was mislabeled and does not rule out S-009-S-015's locks

Reproduction #3's commit, `3a030687a6`, was recorded above as "pre-S-009
— no cross-domain locks this fork ever added exist yet." That is
**wrong**. `git merge-base --is-ancestor 94a0365951 3a030687a6` returns
true: `3a030687a6` is a *descendant* of `94a0365951` ("mem,dev:
cross-domain locks for BaseXBar and PioDevice", the commit that adds
`layerLock`/`pioLock`), and also of `3f493c8838` (the `AddrRangeMap`
cross-domain lock). The mislabeling traces to `3a030687a6`'s own commit
message — "docs: add pre-S-008 session handoff note" — which describes
the *content* of a note about a pre-S-008 state, not the commit's own
position in the repo's history; that was misread as "this commit is
chronologically pre-S-009."

`git log --oneline 796f662040..3a030687a6` (7 commits) confirms
`3a030687a6` contains, in order: S-009's design docs (`31dcc31dc6`,
`7f02ff5db4`), the `layerLock`/`pioLock` commit itself (`94a0365951`),
the S-009 rollout docs (`f3d8ae5c7c`), the `AddrRangeMap` lock
(`3f493c8838`), and the S-010 docs (`6dc82874f7`) — i.e. reproduction #3
ran with `layerLock`, `pioLock`, and the `AddrRangeMap` cross-domain
guard all present. It does **not** contain `pqLock` (S-015,
`packet_queue.cc`) or the PIT/RTC/IDE hand-wiring
(`4df7e6bfe3`/S-009 §25), both of which land later in the log — so
reproduction #3 does still rule out *those* two, just not `layerLock`/
`pioLock`/the `AddrRangeMap` lock as originally written.

The true last lock-free commit is `796f662040` (or `31dcc31dc6`,
docs-only, one commit later) — before `94a0365951`. Re-testing at that
commit is needed to actually check this rules-out claim; see §7.

## 3b. Re-test at the true pre-S-009 commit: the hang does NOT reproduce
(2026-07-17, same investigation, in progress)

Built `796f662040` fresh in
`/workspace/gem5-wt/bisect-796f662-true-pre-s009-serial-hang`
(mirrored to `/workspace/shm/gem5/bisect-796f662-true-pre-s009-serial-hang/build/`,
built with `taskset -c 0-53,56-91` per the isolcpus reservation). Ran the
same unbounded serial command as every other reproduction
(`CHECKPOINT_DIR=/workspace/gem5-ckpt/x86-threads3-roi-classic
SIM_QUANTUM_TICKS=6660 taskset -c 54 ./build/X86_MESI_Three_Level/gem5.opt
-d /tmp/s016-true-pre-s009-serial docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`,
no `PARALLEL_EVENTQ`, no `MAX_TICKS`).

**It did not freeze at `interrupts.cc:530`.** The log printed the same
`hack: Assuming logical destinations are 1 << id.` line (at `utime≈6296`
ticks, ~63 CPU-s — itself later than the ~37-40s freeze point of every
prior reproduction) and then kept going: `src/dev/x86/pc.cc:117: warn:
Don't know what interrupt to clear for console.` and further `fwait
unimplemented` warnings printed afterward, none of which any prior
(hung) reproduction ever reached. Process state stayed `RN` and `utime`
climbed continuously past 52,923 ticks (~529 CPU-s) with the process
still actively running, `wchan=0` (not blocked) — a live, working
process, not the `futex_wait_queue`/frozen-`utime` signature every other
reproduction showed.

**This overturns §2's headline conclusion.** The hang is NOT independent
of S-009-S-015's cross-domain locking work — reproduction #3, the one
piece of evidence for that claim, was testing the wrong commit (§3a).
With `layerLock`, `pioLock`, and the `AddrRangeMap` cross-domain lock all
genuinely absent, the freeze this document is about does not occur (at
least not at the same point — see §3c for whether it's gone for good or
just delayed). The leading hypothesis is now that one of those three
locks (most likely `pioLock`, given the freeze site's proximity to
`PioDevice`/local-APIC PIO delivery — see §3 on `PioPort<Interrupts>`
being deliberately *un*locked while other devices in the same interrupt-
delivery chain, e.g. the I/O APIC, likely are `PioDevice`s that do take
`pioLock`) is the actual reentrant-lock site, not a pre-existing/stock
gem5 bug as §5 hypothesized.

## 3c. Why removing S-009's locks would produce a **self**-deadlock in
serial mode: the reentrancy candidate

`pioLock` is `UncontendedMutex`, explicitly non-reentrant
(`io_device.hh`/`addr_range_map.hh` both flag this). Serial mode is
single-threaded, so the only way to hit its `futex_wait_queue` slow path
at all is the *same* thread calling `lock()` while it already holds the
lock — i.e. some call chain re-enters `PioPort<PioDevice>::recvAtomic`
(or the timing-mode equivalent) on a device it, or a peer sharing the
same lock instance, already has locked.

The interrupt-delivery chain from `interrupts.cc:530` fits this shape:
a guest write to the local APIC's ICR (a PIO write, arriving through
`PioPort<Interrupts>` — deliberately unlocked per the `io_device.hh`
comment in §3, since the local APIC is domain-private) triggers
`X86ISA::Interrupts::writeReg()`, which for a broadcast IPI loops over
every target and calls `intRequestPort.sendMessage()`
(`interrupts.cc:582-585`) — a *different* delivery path (`IntRequestPort`/
`IntResponsePort`, not `PioPort`) that does not touch `pioLock` at all
per the code read in §3. That path was already traced and cleared.

**Where these ports actually connect, for this checkpoint's config**
(`MESIThreeLevelCacheHierarchy.incorporate_cache()`,
`src/python/gem5/components/cachehierarchies/ruby/mesi_three_level_cache_hierarchy.py:143-147`):
`interrupts[0].pio` and `int_responder` connect to
`l1_cache.sequencer.in_ports`, and `int_requestor` connects to
`l1_cache.sequencer.interrupt_out_port` — i.e. **both the local APIC's
PIO register access and the IPI messages it sends route through each
core's Ruby `RubySequencer`/network, not through `PioPort<PioDevice>`,
`BaseXBar`, or `layerLock`/`pioLock` at all.** Those two locks only guard
the *classic* `iobus` (`IOXBar`), which this same config also has
(`l1_cache.sequencer.connectIOPorts(board.get_io_bus())`,
same file) for the south-bridge devices (PIT/RTC/IDE/console) — a
structurally separate path from local-APIC IPI delivery. So if
`layerLock`/`pioLock` turn out to be the culprit (§3d), the mechanism is
most likely **not** the broadcast IPI itself reentering a device through
these locks, but ordinary south-bridge PIO traffic (PIT/RTC/IDE/console,
all busy during boot) hitting a reentrant `layerLock`/`pioLock` acquisition
at roughly the same point in boot that the one-time `interrupts.cc:530`
`hack_once` print happens to fire — i.e. the two may be coincidentally
concurrent in boot sequence, not causally connected. Not yet checked:
whether any south-bridge `PioDevice` synchronously re-enters its own
`pioLock`-guarded `read()`/`write()`, or `IOXBar` synchronously re-enters
its own `layerLock`-guarded entry point, from within a callback that runs
while the outer lock is still held. This is the next thing to check
(§7), informed by whichever of `94a0365951`/`3f493c8838` §3d's bisection
implicates.

## 3d. Splitting `layerLock`/`pioLock` (`94a0365951`) from the
`AddrRangeMap` lock (`3f493c8838`) — bisection in progress

Building commit `94a0365951` itself (has `layerLock`+`pioLock`, does
**not** yet have the `AddrRangeMap` cross-domain lock, which lands one
commit later at `3f493c8838`) in
`/workspace/gem5-wt/bisect-94a0365-layerlock-pio-only` to test which of
the two is implicated.

**Result: it hangs.** Same unbounded serial command, same checkpoint,
`taskset -c 54`. This time `wchan` was already `futex_wait_queue` the
instant the `interrupts.cc:530` log line appeared (`utime=4028` ticks,
~40.3 CPU-s — inside the same 37-40s band as every §2 reproduction),
confirmed stalled at 30s and again at the 100s mark with zero `utime`
movement — the identical signature. **`layerLock`/`pioLock`
(`94a0365951`) alone are sufficient to reproduce the hang; the
`AddrRangeMap` lock (`3f493c8838`) is not required.**

**Bisection is now closed**: `796f662040` (no fork locks) → clean, 10
minutes monitored, zero stalls. `94a0365951` (+`layerLock`+`pioLock`) →
hangs, confirmed. The root cause is in the `layerLock`/`pioLock` change
itself (or its interaction with pre-existing code), not the
`AddrRangeMap` lock, not S-011's `Consumer` lock, not `pqLock`, and not a
pre-existing stock-gem5 bug as §5 originally hypothesized.

**Next step, not yet done**: read `94a0365951`'s actual diff
(`layerLock` added to `BaseXBar`/`NoncoherentXBar`'s
`recvTimingReq`/`recvTimingResp`/`recvReqRetry`; `pioLock` added to
`PioDevice` via the `PioPort<PioDevice>::recvAtomic` specialization) for
a concrete reentrancy path — e.g. whether any south-bridge `PioDevice`'s
`read()`/`write()`, or any `NoncoherentXBar` entry point, can
synchronously call back into the *same* locked object (a device
retrying through the xbar while the xbar's own `layerLock` is held, or a
device's DMA/self-scheduled callback re-entering `pioLock` on itself)
during ordinary boot-time PIO traffic — consistent with §3c's revised
view that this is likely coincidental-in-boot-order PIO activity
(PIT/RTC/IDE/console), not the broadcast IPI itself, since IPI delivery
in this config routes through Ruby's `RubySequencer`, not through
`PioPort<PioDevice>`/`BaseXBar` at all.

The freeze timing is consistent across all four (~37-40 CPU-seconds),
and the TSan-build hangs documented in `S-015 §7.4` (~294 CPU-seconds)
scale by almost exactly TSan's typical instrumentation-overhead factor
(~7.9x) — strongly suggesting **one deterministic freeze point reached
after a roughly fixed amount of actual simulated work**, not a
wall-clock race.

## 3. The `sendMessage`/broadcast-IPI trace — cleared of one candidate
mechanism

The last log line before every freeze
(`interrupts.cc:530: hack: Assuming logical destinations are 1 << id.`)
comes from a `hack_once` (fires exactly once ever) inside
`X86ISA::Interrupts::writeReg()`'s handling of
`APIC_INTERRUPT_COMMAND_LOW` for a **logical-destination, no-shorthand
broadcast IPI** — i.e. the first time the simulation ever addresses
multiple APICs by bitmask rather than one target or "all." Right after,
the code loops over every target and calls `intRequestPort.sendMessage()`
once per target (`interrupts.cc:582-585`), which in timing mode calls
`schedTimingReq()` → `PacketQueue::schedSendTiming()`
(`src/dev/x86/intdev.hh:137-150`, `src/mem/qport.hh:150-151`) — stock
gem5 code, unmodified in reproduction #3 (`pqLock` didn't exist there).

**Traced this specific path and found no reentrant-lock bug**: for a
burst of N packets built in the same loop iteration (same `curTick() +
latency`), only the *first* insertion into an empty `transmitList` sets
`schedule_send = true` and reaches `schedSendEvent()`/`EventQueue::
schedule()`. Every subsequent same-tick packet's insertion-point search
finds an existing entry with `it->tick <= when` and returns via the
middle-insertion branch (`transmitList.emplace(++it, when, pkt); return;`)
without ever reaching the `schedule_send = true` line
(`src/mem/packet_queue.cc:196-214`). So an N-target broadcast cannot
re-enter `schedSendEvent`/`EventQueue::schedule` N times from one call
frame.

Followed the chain one level further: the delivered IPI reaches
`X86ISA::Interrupts::requestInterrupt()` (`interrupts.cc:227-274`),
which for `FullSystem` unconditionally calls `tc->getCpuPtr()->
wakeup(0)` (`interrupts.cc:273`) — **the same `recvMessage → wakeup →
activateContext → EventQueue::schedule` call chain
[S-006 §11.2-§11.5](./S-006-fs-mode-migration.md) already found and
fixed a real bug in** (the `simQuantumStart`-anchored
`crossDomainSnap()` grid fix). That fix targeted a *parallel*-mode
assert (`when < curTick()` across domains); S-006 §11.3 states plainly
that serial mode cannot hit that specific assert (one queue, no
cross-domain scheduling) — so it isn't a straightforward recurrence of
that bug, but it's the same code region and not yet individually
audited for a *different*, serial-mode-reachable issue (see §5).

## 4. Historical provenance: has an unbounded serial run of this
checkpoint ever actually finished?

Re-reading S-006/S-007/S-008's own accounts of their serial reference
runs (not this session's own testing — this is what's already written
in the project's history) turned up something more significant than a
single reentrant-lock candidate:

- **S-006 §11.3** (the session that first restored this checkpoint in
  FS mode): "同配置 `PARALLEL_EVENTQ=0` 单 EventQueue 重放不可能触发该
  assert... 本次运行**尚在 ROI 中**...结果 simTicks **待补**" — the
  serial reference run was still in-flight, result marked TBD, at the
  end of that session.
- **S-007 §12.6** (a later session, the spin-barrier milestone): "串行
  参考跑（pid 1540862，`/tmp/fs3-restore-serial`，串行中性、**仍在 ROI
  中**）给出的完整-ROI 对照 simTicks **仍 TBD**" — still TBD, in a
  different session with a different PID, i.e. no one had gone back to
  confirm the S-006 run had finished, and this session's own attempt was
  *also* unconfirmed when the section was written.
- **S-008 §16** (later still): a handoff note
  (`docs/refs/what-to-do-next-floofy-wombat.md`, committed as part of
  `3a030687a6` — the exact commit this session's reproduction #2 used)
  claims the run *did* eventually finish:
  `simTicks=210050075139858`, `hostSeconds=16851.86` (~4.68 hours),
  `simInsts=3278585986`. **S-008's own session could not verify this**:
  the host had rebooted in the interim, `/tmp/fs3-restore-serial` was
  gone, and the only source for these numbers was the prior session's
  prose, not a `stats.txt` this session actually read. S-008 explicitly
  flags this as "记录在案的未核实二手数据" (an on-the-record but
  unverified secondhand claim) and recommends re-running rather than
  trusting it for any quantitative conclusion.
- **Since S-008, every "historical serial reference" used in this
  project** (S-009 §27, S-014 §9, S-015 throughout —
  `finalTick=5306177114066`/`simInsts=1475264`, cited repeatedly as "the"
  serial reference) **has come from a `MAX_TICKS=2e9`-bounded run**
  (confirmed at S-014 §9: "bumped to `MAX_TICKS=2e9`... completed
  cleanly"), i.e. a run that stops at a fixed tick offset from restore,
  not one that reaches the workload's actual end. No session after
  S-008 re-ran a genuinely unbounded serial reference to independently
  confirm the S-008 §16 claim — until this session's four reproductions,
  all of which hung.

**Net effect**: this session's reproductions are fully consistent with
"an unbounded serial run of this checkpoint has never actually been
confirmed to finish, going back to the original FS-mode migration" —
this may not be a regression introduced by any commit at all, just the
first time anyone has directly checked process-level CPU activity
(`utime`, `wchan`) on such a run rather than leaving it as an
unconfirmed background job that eventually got cleaned up or forgotten.
**Not conclusive either way**: it remains possible the S-008 §16 claim
is accurate and something environment-specific changed since (different
host/sandbox instance, reboot-related state, kernel/glibc version) that
now causes a hang where one previously didn't occur. This session has no
way to distinguish those two possibilities from the documentary record
alone — S-008 §16's numbers cannot be independently checked against any
surviving artifact.

## 5. Best-supported hypothesis (superseded on origin, mechanism likely
still applies)

**Superseded by §3b-§3d**: this section originally argued the bug was a
pre-existing, pre-fork `UncontendedMutex` reentrancy, since the earlier
(mislabeled) bisection appeared to show the hang predating this fork's
locks entirely. The corrected bisection (§3b/§3d) shows the opposite:
lock-free `796f662040` does not hang, and `94a0365951`
(`layerLock`+`pioLock`) does. **The bug is in this fork's own S-009
change**, not inherited from stock gem5.

The *mechanism* description below likely still holds — it just applies
to `layerLock`/`pioLock` (this fork's `UncontendedMutex` instances)
rather than a stock one:

A genuine self-deadlock somewhere in an `UncontendedMutex`-guarded
critical section (`src/base/uncontended_mutex.hh`, a stock gem5 class —
carries a 2020 Google copyright header, predates this fork entirely, but
is now also used by this fork's own `layerLock`/`pioLock`). Its slow
path (`cv.wait()` on an internal `std::mutex`/condition variable) is
implemented via a futex under glibc/NPTL, matching the
`futex_wait_queue` wchan exactly. In a genuinely single-threaded process
(serial mode: one `EventQueue`, one host thread), the *only* way to
reach that slow path at all is for the same thread to call `lock()` on
an already-locked instance — the mutex is explicitly documented as
non-reentrant (e.g. `addr_range_map.hh:175`'s comment) — i.e. a
nested/reentrant-lock bug, not a race (no second real thread exists to
race with in serial mode; this is notable since `layerLock`/`pioLock`
were designed and TSan-verified for *cross-thread* races under parallel
mode — S-009 §24 — and apparently never exercised for single-thread
reentrancy, which parallel mode's own barrier structure may happen to
avoid).

`interrupts.cc:530`'s log line is very likely a red herring for the
*site* — §3c now favors ordinary south-bridge PIO traffic
(PIT/RTC/IDE/console, all active during boot) reentering `layerLock` on
`IOXBar` or `pioLock` on some `PioDevice`, coincidentally around the
same point in the boot sequence as the one-time `hack_once` print, since
local-APIC IPI delivery in this checkpoint's config routes through
Ruby's `RubySequencer` and never touches `PioPort<PioDevice>`/`BaseXBar`
at all (§3c). This is **not** confirmed by a backtrace (`gdb`/`strace`
both blocked by this sandbox's `ptrace_scope=1`); it is the
best-supported hypothesis given the evidence (deterministic timing,
`futex_wait_queue` wchan, reproducibility independent of every other
variable tested, and now a positive lock-in via bisection), not a
demonstrated mechanism.

## 6. Root cause found by direct code reading: `recvReqRetry` holds
`layerLock` across a call chain stock gem5 already documents as
re-entrant

Read `94a0365951`'s `NoncoherentXBar`/`BaseXBar::Layer`/`PacketQueue`
code (the exact commit that reproduces, per §3d) end to end for the
concrete mechanism, rather than a live backtrace (still blocked by
`ptrace_scope=1`). Found a complete, self-contained reentrant-lock bug:

1. `NoncoherentXBar::recvReqRetry(mem_side_port_id)`
   (`noncoherent_xbar.cc:241-249` at `94a0365951`) takes
   `std::lock_guard<UncontendedMutex> lock(layerLock)` for its whole
   body, then calls `reqLayers[mem_side_port_id]->recvRetry()`.
2. `BaseXBar::Layer::recvRetry()` (`xbar.cc:307-326`), if the layer is
   `IDLE`, calls `retryWaiting()` — **still inside the `layerLock`
   critical section**, since `releaseLayer()` is the only other caller
   of `retryWaiting()` and at this commit does not yet lock
   independently (that's a separate later change, S-015 — see below).
3. `retryWaiting()` (`xbar.cc:274-303`) calls `sendRetry(retryingPort)`.
   Its own comment says plainly: *"tell the port to retry, which **in
   some cases ends up calling the layer again**"* (`xbar.cc:296-297`).
4. `sendRetry(ResponsePort*)` (`xbar.hh:274-278`) calls
   `retry_port->sendRetryReq()` — a synchronous call to whatever is
   wired to this xbar's cpu-side port.
5. For any peer using the standard `QueuedRequestPort`
   (`qport.hh:121`), `recvReqRetry()` → `reqQueue.retry()` →
   `PacketQueue::retry()` (`packet_queue.cc`, stock/unmodified at this
   commit — no `pqLock` yet) → `sendDeferredPacket()`, whose own
   pre-existing, unmodified comment says: *"take the packet off the
   list before sending it, as sending of the packet in some cases
   causes a new packet to be enqueued (most notably when responding to
   the timing CPU, leading to a new request hitting in the L1 icache,
   leading to a new response)"* — i.e. this calls `sendTiming(pkt)`
   **synchronously**, which for a request headed back to the same xbar
   port lands on `NoncoherentXBar::recvTimingReq()` — trying to
   acquire `layerLock` a second time, on the same thread, inside the
   still-live `lock_guard` from step 1.

`UncontendedMutex` is explicitly documented as non-reentrant
(`addr_range_map.hh:175`, `io_device.hh`). Step 5's second `lock_guard`
construction blocks on the futex forever — the observed
`wchan=futex_wait_queue`, frozen `utime`, deterministic freeze point
(same boot-time PIO retry pattern every run, no dependence on
wall-clock scheduling).

**This requires no cross-domain execution or second thread at all** —
pure same-thread, same-object reentrancy, which is exactly why it
reproduces in single-threaded serial mode and does not require any of
S-009's cross-domain motivation to trigger. It is also, structurally,
**the identical class of bug S-014 (`occupyLayer` grid snap) and S-015
(`pqLock` leaf-lock + tolerate-spurious-retry) already found and fixed
in `packet_queue.cc`** — a lock wrapped around "the full body" of an
entry point that stock gem5's own synchronous port-retry protocol can
recurse back into — just not yet applied to `layerLock`/`xbar.cc`
itself. S-015 §8's "leaf lock" design principle (hold the lock only
around the minimal critical section that touches shared state; release
it before calling into anything that can call back into the same
object) is the established precedent for the fix shape here too:
`recvReqRetry` should not hold `layerLock` across the
`retryWaiting`/`sendRetry`/downstream-`sendTiming` call chain.

Confirms (not merely narrows) §3c's "ordinary PIO device traffic, not
the broadcast IPI" reading: this mechanism has nothing to do with
`interrupts.cc` or local APICs specifically — any `PioDevice`
(PIT/RTC/IDE/console, all active during FS boot) whose upstream port
gets blocked-then-retried on the classic `iobus` (a `NoncoherentXBar`)
hits this. `interrupts.cc:530`'s `hack_once` print is coincidental
boot-sequence timing, not causal.

**Confidence**: high, but not backed by an actual debugger backtrace
(still blocked by this sandbox's `ptrace_scope=1`) — this is a code
-reading proof of a viable, sufficient mechanism, matching every
observed symptom, not a captured stack trace of the live hang. The
next concrete step, if this needs to be nailed down further before
fixing, would be adding a `DPRINTF`/temporary log at
`NoncoherentXBar::recvReqRetry`'s `lock_guard` construction and at
`NoncoherentXBar::recvTimingReq`'s, to confirm the second acquisition
attempt's call stack matches this chain exactly. Not yet done. No fix
has been written or applied.

## 7. Not yet done

- Confirm §6's mechanism with a `DPRINTF`/log-based trace (no debugger
  needed) rather than relying on code reading alone, if a live
  confirmation is wanted before writing the fix.
- Write and verify the fix: apply S-015's leaf-lock pattern to
  `NoncoherentXBar::recvReqRetry`/`recvTimingReq`/`recvTimingResp` in
  `layerLock`'s use (§6) — not yet started, no code changes made.
- Read `BaseSimpleCPU::wakeup`/`TimingSimpleCPU::activateContext`
  (already read this session — no lock or reentrancy found in either;
  `TimingSimpleCPU::activateContext`'s cross-domain `crossDomainSnap()`
  branch is parallel-mode-only and structurally unreachable in serial
  mode) — deprioritized now that §3c/§3d point away from the
  `interrupts.cc:530`/`wakeup`/`activateContext` chain and toward
  ordinary PIO device traffic instead.
- No backtrace of a hung process (blocked by sandbox `ptrace_scope`) —
  would need a sandbox/environment with `CAP_SYS_PTRACE`/relaxed
  `yama.ptrace_scope` to get one, which would likely resolve this
  quickly and directly (would show the exact reentrant call frame).
- No audit of the other `UncontendedMutex` call sites unrelated to this
  fork's own locks (`cache/base.cc`/`ruby/Sequencer.*`/`tlb.*`/
  `mc146818.*`/`intel_8254_timer.*`/`ide_disk.*`) — now lower priority
  since the bisection already pinned the introducing commit to
  `94a0365951` specifically (`BaseXBar`/`PioDevice` only).
- No attempt to re-verify S-008 §16's claim under matching conditions
  (would cost ~5 hours of wall-clock if it doesn't hang, per that
  claim's own numbers) — now largely moot: the bug is confirmed to be a
  fork regression introduced at `94a0365951`, not a question of whether
  stock/pre-fork behavior ever worked.
- Bisection worktrees/builds from this investigation were left in
  place, not cleaned up, in case they're useful for whoever picks this
  up next: `/workspace/gem5-wt/bisect-a3e325a-serial-hang` (superseded,
  see §3a — this commit is post-S-009, its "falsifies S-015" conclusion
  in §13.2 of S-015 is unaffected since S-014/S-015 are unrelated to
  `layerLock`/`pioLock`'s introduction), `/workspace/gem5-wt/bisect-3a03068-pre-s009-serial-hang`
  (mislabeled, superseded by the two below),
  `/workspace/gem5-wt/bisect-796f662-true-pre-s009-serial-hang` (clean,
  confirms the bug starts after this commit), and
  `/workspace/gem5-wt/bisect-94a0365-layerlock-pio-only` (hangs,
  confirms the bug is present by this commit) — mirrored builds under
  `/workspace/shm/gem5/...` for each.
- The original hung process from the *other* session (PID 457814 at the
  time of writing, `/tmp/s014-full/serial`) was left running, untouched,
  per that session's/user's own ownership of it.

---

**Related**: [S-015 §13](./S-015-packetqueue-retry-race-beyond-occupylayer-fix.md)
(how this was discovered, while confirming S-015's TSan hygiene pass —
full diagnostic narrative and the first two bisections), [S-006
§11-§12](./S-006-fs-mode-migration.md) (the APIC cross-domain wakeup
wall this project already fixed once, same call chain), [S-007 §12](./S-007-spin-barrier-and-milestone.md)
and [S-008 §16](./S-008-fs-serial-vs-parallel-current-position.md)
(the serial-reference-TBD history traced in §4 above)
**Return**: [INDEX.md](./INDEX.md)
