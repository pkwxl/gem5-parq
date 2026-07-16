/*
 * Copyright (c) 2013 ARM Limited
 * All rights reserved
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

#ifndef __BASE_BARRIER_HH__
#define __BASE_BARRIER_HH__

#include <atomic>
#include <condition_variable>
#include <mutex>

#if defined(__x86_64__) || defined(__i386__)
#include <immintrin.h>
#endif

#include "base/critpath_trace.hh"

namespace gem5
{

/**
 * Selects how Barrier::wait() blocks until all threads arrive.
 *   Cv     - std::condition_variable (futex sleep/wake). The historical
 *            default; leaves the original behaviour byte-for-byte.
 *   Spin   - sense-reversing busy-wait on an atomic generation counter with
 *            a cpuRelax() hint. A released waiter sees the flip in ~ns with no
 *            syscall, but a waiting thread burns a whole core, so this only
 *            pays off when every thread owns a physical core (host-thread
 *            pinning, no oversubscription).
 *   Hybrid - spin up to spinIters iterations, then fall back to the Cv path.
 *            Bounds the wasted spinning of imbalanced/long quanta while still
 *            avoiding the futex for the common short wait.
 */
enum class BarrierMode
{
    Cv,
    Spin,
    Hybrid
};

/**
 * Architecture-specific "spin-loop hint": relieves pressure on the pipeline
 * (and a hyperthread sibling) during a busy-wait. Falls back to a compiler
 * barrier where no hint instruction is available.
 */
inline void
cpuRelax()
{
#if defined(__x86_64__) || defined(__i386__)
    _mm_pause();
#elif defined(__aarch64__) || defined(__arm__)
    __asm__ __volatile__("yield" ::: "memory");
#else
    std::atomic_signal_fence(std::memory_order_seq_cst);
#endif
}

class Barrier
{
  private:
    /// Mutex to protect access to numLeft and generation
    std::mutex bMutex;
    /// Condition variable for waiting on barrier
    std::condition_variable bCond;
    /// Number of threads we should be waiting for before completing the barrier
    unsigned numWaiting;
    /// Generation of this barrier
    unsigned generation;
    /// Number of threads remaining for the current generation
    unsigned numLeft;

    /// How wait() blocks: condition variable, pure spin, or spin-then-cv.
    const BarrierMode mode;
    /// For Hybrid: spin iterations before falling back to the condition var.
    const unsigned spinIters;
    /// Sense-reversing spin state (Spin/Hybrid only). Kept separate from the
    /// cv fields so the Cv path is left completely untouched.
    std::atomic<unsigned> spinLeft;
    std::atomic<unsigned> spinGen;

  public:
    Barrier(unsigned _numWaiting, BarrierMode _mode = BarrierMode::Cv,
            unsigned _spinIters = 0)
        : numWaiting(_numWaiting), generation(0), numLeft(_numWaiting),
          mode(_mode), spinIters(_spinIters),
          spinLeft(_numWaiting), spinGen(0)
    {}

    /**
     * Wait for all numWaiting threads to arrive. Exactly one caller -- the
     * last to arrive -- returns true; every other caller returns false. This
     * "one true return" contract is relied upon by callers that must run a
     * single action once per barrier (e.g. GlobalEvent::process()).
     *
     * Note: in serial mode (numWaiting == 1) the first and only arriver
     * completes the barrier immediately without ever waiting, so the choice
     * of mode has no observable effect on a single-threaded run.
     *
     * `ctx`, if non-null, identifies this call for S-012 critical-path
     * instrumentation (design doc §4.2): the caller (globalBarrier(),
     * src/sim/global_event.hh) supplies the quantum-boundary tick and
     * which of the two per-quantum barriers this is. Barrier has no
     * notion of "domain" or "quantum" on its own -- ctx is exactly the
     * (and only the) information the caller already has that Barrier
     * doesn't. A null ctx (the default; every non-instrumented caller,
     * e.g. SimulatorThreads::barrier, design §6) or tracing being off
     * takes the original, unmodified path.
     */
    bool
    wait(const CritPathBarrierCtx *ctx = nullptr)
    {
        if (!ctx || !critPathTracing())
            return (mode == BarrierMode::Cv) ? waitCv() : waitSpin();

        const auto t0 = critPathNow();
        const bool isLast =
            (mode == BarrierMode::Cv) ? waitCv() : waitSpin();
        const auto dur =
            isLast ? CritPathClock::duration{} : (critPathNow() - t0);
        critPathRecordBarrierPass(*ctx, isLast, dur);
        return isLast;
    }

  private:
    bool
    waitCv()
    {
        std::unique_lock<std::mutex> lock(bMutex);
        unsigned int gen = generation;

        if (--numLeft == 0) {
            generation++;
            numLeft = numWaiting;
            bCond.notify_all();
            return true;
        }
        while (gen == generation)
            bCond.wait(lock);
        return false;
    }

    bool
    waitSpin()
    {
        const unsigned gen = spinGen.load(std::memory_order_acquire);

        // Arrive. The thread that drives the count to zero is the single
        // "last arriver": it resets the count for the next generation and
        // releases everyone else by advancing the generation. The acq_rel
        // fetch_sub + release store of spinGen + acquire loads below give
        // the same full-barrier happens-before as the mutex in waitCv().
        if (spinLeft.fetch_sub(1, std::memory_order_acq_rel) == 1) {
            spinLeft.store(numWaiting, std::memory_order_relaxed);
            spinGen.store(gen + 1, std::memory_order_release);
            // Pure Spin never parks, so it needs no notify (and no syscall);
            // Hybrid may have parked waiters on the cv, so wake them.
            if (mode == BarrierMode::Hybrid) {
                std::lock_guard<std::mutex> lock(bMutex);
                bCond.notify_all();
            }
            return true;
        }

        // Not the last arriver: spin until the generation advances. In
        // Hybrid mode, once the spin budget is exhausted, park on the cv
        // (the last arriver's notify_all above releases us).
        unsigned spins = 0;
        while (spinGen.load(std::memory_order_acquire) == gen) {
            if (mode == BarrierMode::Hybrid && ++spins >= spinIters) {
                std::unique_lock<std::mutex> lock(bMutex);
                while (spinGen.load(std::memory_order_acquire) == gen)
                    bCond.wait(lock);
                break;
            }
            cpuRelax();
        }
        return false;
    }
};

} // namespace gem5

#endif // __BASE_BARRIER_HH__
