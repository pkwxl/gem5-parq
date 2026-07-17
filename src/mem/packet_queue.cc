/*
 * Copyright (c) 2012,2015,2018-2020 ARM Limited
 * All rights reserved.
 *
 * The license below extends only to copyright in the software and shall
 * not be construed as granting a license to any other intellectual
 * property including but not limited to intellectual property relating
 * to a hardware implementation of the functionality of the software
 * licensed hereunder.  You may use the software subject to the license
 * terms below provided that you ensure that this notice is replicated
 * unmodified and in its entirety in all distributions of the software,
 * modified or unmodified, in source code or in binary form.
 *
 * Copyright (c) 2006 The Regents of The University of Michigan
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are
 * met: redistributions of source code must retain the above copyright
 * notice, this list of conditions and the following disclaimer;
 * redistributions in binary form must reproduce the above copyright
 * notice, this list of conditions and the following disclaimer in the
 * documentation and/or other materials provided with the distribution;
 * neither the name of the copyright holders nor the names of its
 * contributors may be used to endorse or promote products derived from
 * this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 * A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 * OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#include "mem/packet_queue.hh"

#include <atomic>
#include <cstdint>

#include "base/intmath.hh"
#include "base/logging.hh"
#include "base/trace.hh"
#include "debug/Drain.hh"
#include "debug/PacketQueue.hh"

namespace gem5
{

// S-015 §10.6: counters for the "tolerate spurious retry/send" relaxation.
// Under the parallel-EventQueue relaxed-timing model a retry or a fired
// sendEvent can reach a PacketQueue that is not (or no longer) in the state
// the upstream single-thread invariants assume -- another domain's thread
// already sent/consumed the head, or the head's ready tick is still in this
// domain's future. Rather than assert (the S-015 crash: assert(
// deferredPacketReady()) at the historical tick 5305999323366), we tolerate it
// -- recovering by rescheduling for the ready time and dropping nothing -- and
// count + log each occurrence so the frequency of the deliberate relaxation is
// quantified. Global (across all queues); the log message names the queue.
static std::atomic<uint64_t> numSpuriousRetries{0};
static std::atomic<uint64_t> numSpuriousDeferredSends{0};

// Cap the per-event warn() volume so a pathological workload cannot flood the
// log; the atomic counters keep counting past the cap and every event is still
// DPRINTF'd under the PacketQueue debug flag.
static constexpr uint64_t maxRelaxationWarnings = 100;

PacketQueue::PacketQueue(EventManager& _em, const std::string& _label,
                         const std::string& _sendEventName,
                         bool force_order,
                         bool disable_sanity_check)
    : em(_em), sendEvent([this]{ processSendEvent(); }, _sendEventName),
      crossWakeEvent([this]{ processCrossWake(); }, _sendEventName + ".cross"),
      wakePending(false),
      _disableSanityCheck(disable_sanity_check),
      forceOrder(force_order),
      label(_label), waitingOnRetry(false), sending(false)
{
}

PacketQueue::~PacketQueue()
{
}

void
PacketQueue::retry()
{
    DPRINTF(PacketQueue, "Queue %s received retry\n", name());
    // Thin locked wrapper over an unlocked core: clear the retry flag under
    // pqLock, then send with the lock released (leaf rule, S-015 section 8.3).
    // This entry point is reachable cross-domain (qport.hh recvRespRetry/
    // recvReqRetry called directly by a peer domain's thread).
    bool spurious;
    {
        std::lock_guard<UncontendedMutex> lock(pqLock);
        // Tolerate a spurious retry (S-015 section 10.6): under relaxed
        // cross-domain timing a retry can arrive when we were not actually
        // awaiting one (the queue was already serviced by another path).
        // Upstream asserts waitingOnRetry here; we recover instead.
        spurious = !waitingOnRetry;
        waitingOnRetry = false;
    }
    if (spurious) {
        uint64_t n = ++numSpuriousRetries;
        DPRINTF(PacketQueue, "Queue %s: spurious retry tolerated (#%llu)\n",
                name(), n);
        if (n <= maxRelaxationWarnings) {
            warn("PacketQueue relaxation: tolerated spurious retry on %s "
                 "(#%llu) -- not awaiting a retry\n", name(), n);
        }
        // Nothing to resend on this path: any pending packet already has a
        // sendEvent scheduled (a ready packet with waitingOnRetry==false
        // always does). Do not call sendDeferredPacket.
        return;
    }
    sendDeferredPacket();
}

bool
PacketQueue::checkConflict(const PacketPtr pkt, const int blk_size) const
{
    // caller is responsible for ensuring that all packets have the
    // same alignment
    for (const auto& p : transmitList) {
        if (p.pkt->matchBlockAddr(pkt, blk_size))
            return true;
    }
    return false;
}

bool
PacketQueue::trySatisfyFunctional(PacketPtr pkt)
{
    pkt->pushLabel(label);

    auto i = transmitList.begin();
    bool found = false;

    while (!found && i != transmitList.end()) {
        // If the buffered packet contains data, and it overlaps the
        // current packet, then update data
        found = pkt->trySatisfyFunctional(i->pkt);
        ++i;
    }

    pkt->popLabel();

    return found;
}

void
PacketQueue::schedSendTiming(PacketPtr pkt, Tick when)
{
    DPRINTF(PacketQueue, "%s for %s address %x size %d when %lu ord: %i\n",
            __func__, pkt->cmdString(), pkt->getAddr(), pkt->getSize(), when,
            forceOrder);

    // we can still send a packet before the end of this tick
    assert(when >= curTick());

    // express snoops should never be queued
    assert(!pkt->isExpressSnoop());

    // Insert into transmitList under pqLock (this entry point is reachable
    // cross-domain, e.g. qport.hh schedTimingResp on a peer domain's thread --
    // the S-015 section 7.2 TSan-confirmed race). schedSendEvent() is called
    // with the lock released (leaf rule, section 8.3).
    bool schedule_send = false;
    {
        std::lock_guard<UncontendedMutex> lock(pqLock);

        // add a very basic sanity check on the port to ensure the
        // invisible buffer is not growing beyond reasonable limits
        if (!_disableSanityCheck && transmitList.size() > 1024) {
            panic("Packet queue %s has grown beyond 1024 packets\n",
                  name());
        }

        // we should either have an outstanding retry, or a send event
        // scheduled, but there is an unfortunate corner case where the
        // x86 page-table walker and timing CPU send out a new request as
        // part of the receiving of a response (called by
        // PacketQueue::sendDeferredPacket), in which we end up calling
        // ourselves again before we had a chance to update waitingOnRetry.
        // With the S-015 restructure this reentrant call is safe: the send
        // in sendDeferredPacket runs with pqLock released, so this lock_guard
        // does not self-deadlock, and the reentrantly-added packet is picked
        // up by sendDeferredPacket's own tail schedSendEvent once sending
        // clears.
        // assert(waitingOnRetry || sendEvent.scheduled());

        // this belongs in the middle somewhere, so search from the end to
        // order by tick; however, if forceOrder is set, also make sure
        // not to re-order in front of some existing packet with the same
        // address
        auto it = transmitList.end();
        while (it != transmitList.begin()) {
            --it;
            if ((forceOrder && it->pkt->matchAddr(pkt)) || it->tick <= when) {
                // emplace inserts the element before the position pointed to
                // by the iterator, so advance it one step
                transmitList.emplace(++it, when, pkt);
                return;
            }
        }
        // either the packet list is empty or this has to be inserted
        // before every other packet
        transmitList.emplace_front(when, pkt);
        schedule_send = true;
    }

    if (schedule_send)
        schedSendEvent(when);
}

void
PacketQueue::schedSendEvent(Tick when)
{
    // Snapshot the flow-control state under pqLock, then release it before
    // touching sendEvent or the EventQueue (leaf rule, S-015 section 8.5).
    // If we are waiting on a retry, or a send is in flight, just hold off --
    // the pending retry / in-flight send will schedule the next send once it
    // settles.
    {
        std::lock_guard<UncontendedMutex> lock(pqLock);
        if (waitingOnRetry || sending) {
            DPRINTF(PacketQueue, "Not scheduling send (waitingOnRetry=%d "
                    "sending=%d)\n", waitingOnRetry, sending);
            return;
        }
    }

    if (when == MaxTick) {
        // we get a MaxTick when there is no more to send, so if we're
        // draining, we may be done at this point
        bool idle;
        {
            std::lock_guard<UncontendedMutex> lock(pqLock);
            idle = transmitList.empty() && !sending;
        }
        if (drainState() == DrainState::Draining && idle &&
            !sendEvent.scheduled()) {

            DPRINTF(Drain, "PacketQueue done draining,"
                    "processing drain event\n");
            signalDrainDone();
        }
        return;
    }

    // we cannot go back in time, and to be consistent we stick to
    // one tick in the future
    when = std::max(when, curTick() + 1);
    // @todo Revisit the +1

    // Cross-domain send (e.g. RubyPort PIO forwarding a response from a
    // device domain's thread into this port's own domain, or any other
    // classic PioDevice reached via a QueuedRequestPort/QueuedResponsePort):
    // em.eventQueue() is bound at construction to this port's own domain and
    // never changes, so comparing it against curEventQueue() (the calling
    // thread's TLS) reliably detects a cross-domain call here (design doc
    // S-009 sections 17-19).
    if (!inParallelMode || curEventQueue() == em.eventQueue()) {
        // B1 same-domain: the calling thread owns this queue's EventQueue, so
        // only the owner ever reaches this branch -- touching sendEvent
        // directly (as the pre-S-015 code always did) is legal and race free.
        if (!sendEvent.scheduled()) {
            em.schedule(&sendEvent, when);
        } else if (when < sendEvent.when()) {
            // if the new time is earlier than when the event
            // currently is scheduled, move it forward
            em.reschedule(&sendEvent, when);
        }
    } else {
        // B2 cross-domain: MUST NOT touch sendEvent.scheduled()/reschedule()
        // -- both are owner-thread-only by assertion (eventq.hh); a foreign
        // reschedule() does remove()/insert() on a queue it does not own,
        // racing the owner's serviceOne() (the S-015 section 1b "event not
        // found!" / "event scheduled in the past" crash variants, which no
        // PacketQueue-level lock can serialize). Instead snap to the next
        // quantum boundary (same reason as the pre-S-015 snap: `when` was
        // computed from the calling thread's curTick(), up to one quantum
        // ahead of the target domain's clock, so it can land in the target's
        // past; grid anchored at simQuantumStart to survive an FS restore)
        // and hand the real scheduling to the owner thread via crossWakeEvent,
        // posted through the one cross-domain-safe primitive (asyncInsert,
        // reached because crossWakeEvent belongs to em's own queue).
        // wakePending keeps at most one outstanding wake.
        assert(simQuantum > 0);
        when = std::max(when,
            simQuantumStart +
                divCeil(when - simQuantumStart, simQuantum) *
                    simQuantum);
        if (!wakePending.exchange(true)) {
            em.schedule(&crossWakeEvent, when);
        }
    }
}

void
PacketQueue::processCrossWake()
{
    // Runs on the owner thread only (crossWakeEvent fires from em's own
    // queue), so schedSendEvent() below now takes the B1 same-domain path and
    // does the real (re)schedule of sendEvent where it is legal.
    wakePending.store(false);
    Tick when;
    {
        std::lock_guard<UncontendedMutex> lock(pqLock);
        when = deferredPacketReadyTime();
    }
    schedSendEvent(when);
}

void
PacketQueue::sendDeferredPacket()
{
    // Pop the head under pqLock and mark sending, then release the lock for
    // the actual send (leaf rule, S-015 section 8.4). Taking the packet off
    // the list before sending matters because sending it can, in some cases,
    // cause a new packet to be enqueued (most notably when responding to the
    // timing CPU, leading to a new request hitting in the L1 icache, leading
    // to a new response -- the x86-PTW reentrant path). Dropping pqLock across
    // the send is what lets that reentrant schedSendTiming on this same queue
    // take pqLock without self-deadlocking; the `sending` flag closes the
    // window where the head is popped but waitingOnRetry is not yet updated.
    DeferredPacket dp(0, nullptr);
    bool spurious = false;
    Tick ready_at = MaxTick;
    {
        std::lock_guard<UncontendedMutex> lock(pqLock);
        assert(!waitingOnRetry);
        // Tolerate a spurious deferred send (S-015 section 10.6): under
        // relaxed cross-domain timing this can run with no packet ready --
        // another domain's thread already sent/consumed the head, or the
        // head's ready tick is still in this domain's future. Upstream
        // asserts deferredPacketReady() here (the S-015 crash); we recover by
        // rescheduling for the ready time instead, dropping nothing.
        if (!deferredPacketReady()) {
            spurious = true;
            ready_at = deferredPacketReadyTime();   // MaxTick if empty
        } else {
            dp = transmitList.front();
            transmitList.pop_front();
            sending = true;
        }
    }

    if (spurious) {
        uint64_t n = ++numSpuriousDeferredSends;
        DPRINTF(PacketQueue, "Queue %s: spurious deferred send tolerated "
                "(#%llu, readyAt=%llu, now=%llu)\n",
                name(), n, ready_at, curTick());
        if (n <= maxRelaxationWarnings) {
            warn("PacketQueue relaxation: tolerated spurious deferred send on "
                 "%s (#%llu) -- %s at tick %llu\n", name(), n,
                 ready_at == MaxTick ? "queue empty" : "head not yet ready",
                 curTick());
        }
        // Re-derive the correct schedule from current state: if a (future)
        // packet remains, reschedule its send; if empty, this routes to the
        // drain check. pqLock is released here (leaf rule).
        schedSendEvent(ready_at);
        return;
    }

    // use the appropriate implementation of sendTiming based on the
    // type of queue -- cross-domain outbound, pqLock NOT held
    bool ok = sendTiming(dp.pkt);

    Tick next;
    {
        std::lock_guard<UncontendedMutex> lock(pqLock);
        sending = false;
        waitingOnRetry = !ok;
        if (!ok) {
            // put the packet back at the front of the list
            transmitList.emplace_front(dp);
            next = MaxTick;
        } else {
            next = deferredPacketReadyTime();
        }
    }

    // if we succeeded and are not waiting for a retry, schedule the
    // next send (with pqLock released)
    if (ok)
        schedSendEvent(next);
}

void
PacketQueue::processSendEvent()
{
    // sendDeferredPacket asserts !waitingOnRetry under pqLock; no separate
    // unlocked read here (would be a TSan-flagged data race against retry()).
    sendDeferredPacket();
}

DrainState
PacketQueue::drain()
{
    // A drain must not complete mid-send: the head is popped (transmitList may
    // read empty) while sending is true (S-015 section 8.8 item c).
    bool drained;
    {
        std::lock_guard<UncontendedMutex> lock(pqLock);
        drained = transmitList.empty() && !sending;
    }
    if (drained) {
        return DrainState::Drained;
    } else {
        DPRINTF(Drain, "PacketQueue not drained\n");
        return DrainState::Draining;
    }
}

ReqPacketQueue::ReqPacketQueue(EventManager& _em, RequestPort& _mem_side_port,
                               const std::string _label)
    : PacketQueue(_em, _label, name(_mem_side_port, _label)),
      memSidePort(_mem_side_port)
{
}

bool
ReqPacketQueue::sendTiming(PacketPtr pkt)
{
    return memSidePort.sendTimingReq(pkt);
}

SnoopRespPacketQueue::SnoopRespPacketQueue(EventManager& _em,
                                           RequestPort& _mem_side_port,
                                           bool force_order,
                                           const std::string _label)
    : PacketQueue(_em, _label, name(_mem_side_port, _label), force_order),
      memSidePort(_mem_side_port)
{
}

bool
SnoopRespPacketQueue::sendTiming(PacketPtr pkt)
{
    return memSidePort.sendTimingSnoopResp(pkt);
}

RespPacketQueue::RespPacketQueue(EventManager& _em,
                                 ResponsePort& _cpu_side_port,
                                 bool force_order,
                                 const std::string _label)
    : PacketQueue(_em, _label, name(_cpu_side_port, _label), force_order),
      cpuSidePort(_cpu_side_port)
{
}

bool
RespPacketQueue::sendTiming(PacketPtr pkt)
{
    return cpuSidePort.sendTimingResp(pkt);
}

} // namespace gem5
