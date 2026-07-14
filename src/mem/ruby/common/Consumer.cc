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
      em(_em)
{ }

void
Consumer::scheduleEvent(Cycles timeDelta)
{
    m_wakeup_ticks.insert(em->clockEdge(timeDelta));
    scheduleNextWakeup();
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
    scheduleNextWakeup();
}

void
Consumer::scheduleNextWakeup()
{
    // Always called under lock() (see MessageBuffer::enqueue() and
    // processCurrentEvent() below), so m_wakeup_scheduled/
    // m_wakeup_scheduled_when are race-free here.

    // look for the next tick in the future to schedule
    auto it = m_wakeup_ticks.lower_bound(em->clockEdge());
    if (it == m_wakeup_ticks.end())
        return;

    Tick when = *it;
    assert(when >= em->clockEdge());

    if (m_wakeup_scheduled) {
        if (when < m_wakeup_scheduled_when) {
            // Only em's own owning thread may call reschedule()
            // (eventq.hh's assert enforces this); see design doc section
            // 8.7 for the known-unresolved case of two cross-domain
            // threads racing to move this earlier.
            em->reschedule(m_wakeup_event, when, true);
            m_wakeup_scheduled_when = when;
        }
        return;
    }

    em->schedule(m_wakeup_event, when);
    m_wakeup_scheduled = true;
    m_wakeup_scheduled_when = when;
}

void
Consumer::processCurrentEvent()
{
    // m_wakeup_ticks is also touched by scheduleEventAbsolute() from
    // cross-domain threads (under this same lock, see MessageBuffer::
    // enqueue()); reading/erasing it here without the lock is a data race
    // -- a foreign thread's concurrent insert can corrupt the underlying
    // std::set out from under this read (observed as a segfault inside
    // std::set's rbtree code, not just the assert below firing). Cover
    // the whole method, not just wakeup(), so the erased/processed tick
    // and the next-wakeup scheduling both see a consistent set.
    lock();
    // This in-flight dispatch has now actually fired -- clear before
    // touching m_wakeup_ticks so a concurrent scheduleNextWakeup() (which
    // can only run once it acquires this same lock) never observes a
    // stale "still in flight" state (design doc section 8.7; this
    // replaces relying on m_wakeup_event.scheduled(), which
    // EventQueue::serviceOne() clears before this method -- and thus
    // lock() -- is even reached).
    m_wakeup_scheduled = false;
    auto curr = m_wakeup_ticks.begin();
    assert(em->clockEdge() == *curr);
    m_wakeup_ticks.erase(curr);
    wakeup();
    scheduleNextWakeup();
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
