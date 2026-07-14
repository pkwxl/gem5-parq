/*
 * Copyright (c) 2020-2021 ARM Limited
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
 * Copyright (c) 2012 Mark D. Hill and David A. Wood
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

#include "mem/ruby/common/Consumer.hh"

namespace gem5
{

namespace ruby
{

Consumer::Consumer(ClockedObject *_em, Event::Priority ev_prio)
    : m_wakeup_event([this]{ processCurrentEvent(); },
                    "Consumer Event", false, ev_prio),
      em(_em), m_ev_prio(ev_prio)
{ }

void
Consumer::scheduleEvent(Cycles timeDelta)
{
    // Own-thread only: clockEdge() computes against the calling thread's
    // TLS curTick and mutates the Clocked cache, so a cross-domain caller
    // would both read a skewed horizon and race em's own thread (design
    // doc section 9.4/9.5). Cross-domain callers must go through
    // scheduleEventAbsolute() with a sender-relative arrival tick. All
    // in-tree callers are objects scheduling themselves from their own
    // wakeup, so this holds today.
    assert(!inParallelMode || curEventQueue() == em->eventQueue());
    Tick when = em->clockEdge(timeDelta);
    m_wakeup_ticks.insert(when);
    commitTick(when);
}

void
Consumer::scheduleEventAbsolute(Tick evt_time)
{
    Tick when = divCeil(evt_time, em->clockPeriod()) * em->clockPeriod();

    // Cross-domain callers (a different EventQueue's thread than the one
    // that services `em`) must not hand this domain an arrival time that
    // its own clock may already have passed by the time it's observed --
    // domains only drift by at most sim_quantum between GlobalSyncEvent
    // barriers (design doc sections 2.5/8.2), so snapping the arrival up
    // to the next quantum boundary guarantees `when` is never behind any
    // domain's current tick, since no domain can be more than one
    // (uncrossed) boundary ahead of another at any instant.
    if (inParallelMode && curEventQueue() != em->eventQueue()) {
        assert(simQuantum > 0);
        when = divCeil(when, simQuantum) * simQuantum;
    }

    m_wakeup_ticks.insert(when);
    commitTick(when);
}

void
Consumer::commitTick(Tick when)
{
    // Always called under lock() (see MessageBuffer::enqueue() and the
    // fire paths below), so all the scheduling state here is race-free.
    // `when` is either the tick the caller just inserted into
    // m_wakeup_ticks (insert paths, possibly cross-domain) or
    // *m_wakeup_ticks.begin() (fire-path re-anchor, always em's own
    // thread).
    //
    // Invariant (design doc section 8.8): the earliest pending wakeup
    // tick always has a fire committed at exactly that tick in
    // m_inflight_ticks; later ticks are re-covered chain-style after
    // each fire, so every tick is consumed exactly once, at its own
    // time.
    //
    // Deliberately no em->clockEdge() reads here: from a cross-domain
    // thread that value is computed against the *caller's* TLS curTick
    // (skewed by up to a quantum) and the call mutates em's Clocked
    // cache -- both bit us at large quanta (design doc section 9.4).
    // The inflight set alone decides: if some fire is already committed
    // at or before `when`, the earliest pending tick is covered (chain
    // re-anchoring handles the rest); otherwise `when` is the new
    // earliest and needs a commitment now.
    if (!m_inflight_ticks.empty() && *m_inflight_ticks.begin() <= when)
        return;

    bool owning = !inParallelMode || curEventQueue() == em->eventQueue();

    if (m_wakeup_scheduled && owning && !m_wakeup_async_pending) {
        // The only case where reschedule() is legal: we are em's owning
        // thread AND the in-flight schedule was made locally, so the
        // event is in em's main queue (an async-pending event may still
        // be in the async queue, where reschedule()'s remove() would
        // panic "event not found").
        em->reschedule(m_wakeup_event, when, true);
        m_inflight_ticks.erase(m_wakeup_when);
        m_inflight_ticks.insert(when);
        m_wakeup_when = when;
    } else if (!m_wakeup_scheduled) {
        // schedule() is legal from any thread -- a cross-domain caller
        // is routed through asyncInsert() and merged at the next quantum
        // boundary, which the arrival-tick quantum snap guarantees is
        // still ahead of `when`.
        em->schedule(m_wakeup_event, when);
        m_wakeup_scheduled = true;
        m_wakeup_when = when;
        m_wakeup_async_pending = !owning;
        m_inflight_ticks.insert(when);
    } else {
        // The main event is in flight but untouchable (we are a foreign
        // thread, or its schedule is async-pending): commit the earlier
        // fire with a one-shot self-deleting kick instead. Unreachable
        // in serial mode, where `owning` is always true and
        // m_wakeup_async_pending always false.
        auto *kick = new EventFunctionWrapper(
            [this]{ processKick(); }, "Consumer kick",
            true /* AutoDelete */, m_ev_prio);
        em->schedule(kick, when);
        m_inflight_ticks.insert(when);
    }
}

void
Consumer::consumeCurrentTick()
{
    // Caller (a fire path below) holds lock() and has already removed
    // this fire's commitment from m_inflight_ticks. Fire paths always
    // run on em's own thread, so the clockEdge() reads here are sound.
    auto curr = m_wakeup_ticks.begin();
    assert(em->clockEdge() == *curr);
    m_wakeup_ticks.erase(curr);
    wakeup();
    // Re-anchor: commit the next pending tick if it isn't covered yet.
    // Every remaining tick is in the future (each tick is consumed at
    // exactly its own time), and inflight commitments only ever target
    // pending ticks, so begin() is the right anchor.
    if (!m_wakeup_ticks.empty()) {
        assert(m_inflight_ticks.empty() ||
               *m_inflight_ticks.begin() >= *m_wakeup_ticks.begin());
        commitTick(*m_wakeup_ticks.begin());
    }
}

void
Consumer::processCurrentEvent()
{
    // m_wakeup_ticks is also touched by scheduleEventAbsolute() from
    // cross-domain threads (under this same lock, see MessageBuffer::
    // enqueue()); reading/erasing it here without the lock is a data race
    // -- a foreign thread's concurrent insert can corrupt the underlying
    // std::set out from under this read (observed as a segfault inside
    // std::set's rbtree code, not just the assert in consumeCurrentTick()
    // firing). Cover the whole method, not just wakeup(), so the
    // erased/processed tick and the next-wakeup scheduling both see a
    // consistent set.
    lock();
    // This in-flight dispatch has now actually fired -- clear before
    // touching m_wakeup_ticks so a concurrent commitTick() (which
    // can only run once it acquires this same lock) never observes a
    // stale "still in flight" state (design doc section 8.7; this
    // replaces relying on m_wakeup_event.scheduled(), which
    // EventQueue::serviceOne() clears before this method -- and thus
    // lock() -- is even reached).
    m_wakeup_scheduled = false;
    m_wakeup_async_pending = false;
    [[maybe_unused]] auto erased = m_inflight_ticks.erase(em->clockEdge());
    assert(erased == 1);
    consumeCurrentTick();
    unlock();
}

void
Consumer::processKick()
{
    lock();
    [[maybe_unused]] auto erased = m_inflight_ticks.erase(em->clockEdge());
    assert(erased == 1);
    consumeCurrentTick();
    unlock();
}

void
Consumer::lock()
{
    std::thread::id self = std::this_thread::get_id();
    if (m_wakeup_mutex_owner == self) {
        ++m_wakeup_mutex_depth;
        return;
    }
    m_wakeup_mutex.lock();
    m_wakeup_mutex_owner = self;
    m_wakeup_mutex_depth = 1;
}

void
Consumer::unlock()
{
    assert(m_wakeup_mutex_owner == std::this_thread::get_id());
    if (--m_wakeup_mutex_depth == 0) {
        m_wakeup_mutex_owner = std::thread::id();
        m_wakeup_mutex.unlock();
    }
}

} // namespace ruby
} // namespace gem5
