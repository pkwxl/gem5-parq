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
 * Copyright (c) 1999-2008 Mark D. Hill and David A. Wood
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

/*
 * This is the virtual base class of all classes that can be the
 * targets of wakeup events.  There is only two methods, wakeup() and
 * print() and no data members.
 */

#ifndef __MEM_RUBY_COMMON_CONSUMER_HH__
#define __MEM_RUBY_COMMON_CONSUMER_HH__

#include <iostream>
#include <set>
#include <thread>

#include "base/uncontended_mutex.hh"
#include "sim/clocked_object.hh"

namespace gem5
{

namespace ruby
{

class Consumer
{
  public:
    Consumer(ClockedObject *em,
             Event::Priority ev_prio = Event::Default_Pri);

    virtual
    ~Consumer()
    { }

    virtual void wakeup() = 0;
    virtual void print(std::ostream& out) const = 0;
    virtual void storeEventInfo(int info) {}

    bool
    alreadyScheduled(Tick time)
    {
        return m_wakeup_ticks.find(time) != m_wakeup_ticks.end();
    }

    ClockedObject *
    getObject()
    {
        return em;
    }

    void scheduleEventAbsolute(Tick timeAbs);
    void scheduleEvent(Cycles timeDelta);

    /*
     * Per-consumer wakeup mutex (design doc:
     * docs/specs/S-001-design-background-and-proposal.md §2.3/§6.2).
     * Any thread enqueueing into one of this Consumer's inbound
     * MessageBuffers, and this Consumer's own wakeup() dispatch, must hold
     * this lock for the duration -- it is what makes "wakeup() scans all
     * inbound buffers" atomic w.r.t. a concurrent cross-thread enqueue.
     *
     * Same-thread re-entrant, cross-thread exclusive: SLICC-generated
     * wakeup() code can enqueue into the consumer's own buffers (recycle,
     * reanalyzeMessages, enqueueDeferredMessages) while already holding this
     * lock on the same thread; a *different* thread trying to acquire it
     * always blocks normally. Recursion never changes how many *distinct*
     * locks a thread holds, so it doesn't weaken the section 6.2 invariant
     * (a thread holds at most one such lock at a time).
     */
    void lock();
    void unlock();

  private:
    std::set<Tick> m_wakeup_ticks;
    EventFunctionWrapper m_wakeup_event;
    ClockedObject *em;

    /*
     * Consumer-owned scheduling state (design doc sections 8.7/8.8),
     * only ever touched while holding m_wakeup_mutex. Deliberately not
     * derived from m_wakeup_event.scheduled(): that core Event flag is
     * cleared by EventQueue::serviceOne() before the callback (and thus
     * before lock()) runs, so a cross-domain thread holding this lock can
     * observe an honest-but-stale "not scheduled" reading in the window
     * between serviceOne()'s dequeue and processCurrentEvent() actually
     * acquiring the lock, and double-schedule the same Event object.
     *
     * m_wakeup_async_pending records that m_wakeup_event's current
     * schedule came from a cross-domain thread (via asyncInsert()), in
     * which case the event may still be sitting in em's async queue --
     * reschedule()'s internal remove() only searches the main queue and
     * would panic -- so it must not be rescheduled by anyone until it
     * fires. m_inflight_ticks holds every tick at which some event (the
     * main wakeup event or a one-shot kick) is committed to fire; the
     * invariant is that the earliest pending wakeup tick always has a
     * fire committed at exactly that tick, later ticks being re-covered
     * chain-style after each fire.
     */
    bool m_wakeup_scheduled = false;
    Tick m_wakeup_when = 0;
    bool m_wakeup_async_pending = false;
    std::set<Tick> m_inflight_ticks;
    Event::Priority m_ev_prio;

    UncontendedMutex m_wakeup_mutex;
    std::thread::id m_wakeup_mutex_owner;
    unsigned m_wakeup_mutex_depth = 0;

    void commitTick(Tick when);
    void consumeCurrentTick();
    void processCurrentEvent();
    void processKick();
};


inline std::ostream&
operator<<(std::ostream& out, const Consumer& obj)
{
    obj.print(out);
    out << std::flush;
    return out;
}

} // namespace ruby
} // namespace gem5

#endif // __MEM_RUBY_COMMON_CONSUMER_HH__
