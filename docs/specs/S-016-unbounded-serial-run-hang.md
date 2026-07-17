# S-016: Unbounded serial-mode run hangs — independent of S-009 through
S-015, of TSan, and of checkpoint content; possibly never worked at all

> **Status: OPEN, root cause not confirmed.** Discovered by accident
> (2026-07-17) while checking on another session's live confirmation run
> for S-014/S-015. Four independent reproductions across three code
> commits and two checkpoints, all with an identical signature. The
> `sendMessage`/broadcast-IPI code path was traced and cleared of one
> candidate mechanism. Reading S-006/S-007/S-008's own history turned up
> a bigger, separate finding: **this project has never confirmed that an
> unbounded serial run of this checkpoint+workload actually finishes** —
> so this may not be a regression at all, just the first time anyone
> checked process-level CPU activity on such a run instead of leaving it
> as an unconfirmed background job. Set aside here for later
> prioritization against the rest of the project; not root-caused, not
> fixed.

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
| 3 | commit `3a030687a6` (pre-S-009 — **no** cross-domain locks this fork ever added exist yet) | fresh build | `x86-threads3-roi-classic` | ~40.3s | hung |
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
  (`layerLock`/`pioLock`/`cacheLock`/`pqLock`) — falsified by
  reproduction #3, which predates all of it.
- **Checkpoint content** — falsified by reproduction #4, a structurally
  different guest program state from a different benchmark variant
  (S-013's "balanced" workload).

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

## 5. Best-supported hypothesis (not confirmed)

A genuine self-deadlock somewhere in an `UncontendedMutex`-guarded
critical section (`src/base/uncontended_mutex.hh`, a stock gem5 class —
carries a 2020 Google copyright header, predates this fork entirely).
Its slow path (`cv.wait()` on an internal `std::mutex`/condition
variable) is implemented via a futex under glibc/NPTL, matching the
`futex_wait_queue` wchan exactly. In a genuinely single-threaded process
(serial mode: one `EventQueue`, one host thread), the *only* way to
reach that slow path at all is for the same thread to call `lock()` on
an already-locked instance — the mutex is explicitly documented as
non-reentrant (e.g. `addr_range_map.hh:175`'s comment) — i.e. a
nested/reentrant-lock bug, not a race (no second real thread exists to
race with in serial mode). This is **not** confirmed by a backtrace
(`gdb`/`strace` both blocked by this sandbox's `ptrace_scope=1`); it is
the best-supported hypothesis given the evidence (deterministic timing,
`futex_wait_queue` wchan, reproducibility independent of every other
variable tested), not a demonstrated mechanism.

`interrupts.cc:530`'s broadcast-IPI/wakeup/`activateContext` region
(§3) is a plausible *site* for such a bug given it shares history with
a previously-fixed bug in the same call chain, but this has not been
proven — `EventQueue::service_mutex`/`ScopedMigration`/`ScopedRelease`
were read (§13.4 of S-015, folded in above) without finding an obvious
issue for a single-`EventQueue` run, and the full call graph from
`activateContext` onward (specifically `BaseSimpleCPU::wakeup`/
`TimingSimpleCPU::activateContext` themselves) was not read to a
conclusion.

## 6. Not yet done

- Read `BaseSimpleCPU::wakeup`/`TimingSimpleCPU::activateContext`
  directly for a concrete reentrant-lock mechanism (the last unaudited
  link in the chain from `interrupts.cc:530` to `EventQueue::schedule`).
- No backtrace of a hung process (blocked by sandbox `ptrace_scope`) —
  would need a sandbox/environment with `CAP_SYS_PTRACE`/relaxed
  `yama.ptrace_scope` to get one, which would likely resolve this
  quickly.
- No audit of the other `UncontendedMutex` call sites
  (`noncoherent_xbar.cc`/`xbar.cc`/`cache/base.cc`/`ruby/Sequencer.*`/
  `io_device.*`/`tlb.*`/`mc146818.*`/`intel_8254_timer.*`/`ide_disk.*`/
  `addr_range_map.hh`).
- No bisection into this fork's own earliest work (S-001's
  throttle/deadlock-avoidance design, S-002's per-consumer
  `Consumer::lock()`, S-005's host-thread affinity, S-006's FS-mode
  migration itself) — §2's reproduction #3 only rules out S-009 through
  S-015; it's still possible something in S-001-S-008 is responsible,
  or that it's inherited unchanged from upstream gem5.
- No attempt to re-verify S-008 §16's claim under matching conditions
  (would cost ~5 hours of wall-clock if it doesn't hang, per that
  claim's own numbers).
- Two bisection worktrees/builds from this session were left in place,
  not cleaned up, in case they're useful for whoever picks this up next:
  `/workspace/gem5-wt/bisect-a3e325a-serial-hang` and
  `/workspace/gem5-wt/bisect-3a03068-pre-s009-serial-hang` (mirrored
  builds under `/workspace/shm/gem5/...`).
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
