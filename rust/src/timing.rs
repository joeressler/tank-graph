//! Monotonic clocks and whole-microsecond rounding for CLI phase reports.

use std::cell::Cell;
use std::time::Instant;

/// Nanosecond timeline used to bound load, build, and query phases.
pub trait Clock {
    fn now_ns(&self) -> u128;
}

/// Wall-clock origin for production measurements.
pub struct InstantClock {
    origin: Instant,
}

impl InstantClock {
    pub fn new() -> Self {
        Self {
            origin: Instant::now(),
        }
    }
}

impl Default for InstantClock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock for InstantClock {
    fn now_ns(&self) -> u128 {
        self.origin.elapsed().as_nanos()
    }
}

/// Test clock with an explicit nanosecond counter.
pub struct FakeClock {
    now: Cell<u128>,
    auto_step: Cell<u128>,
}

impl FakeClock {
    pub fn new() -> Self {
        Self {
            now: Cell::new(0),
            auto_step: Cell::new(0),
        }
    }

    pub fn with_auto_step(step: u128) -> Self {
        Self {
            now: Cell::new(0),
            auto_step: Cell::new(step),
        }
    }

    pub fn advance_ns(&self, delta: u128) {
        self.now.set(self.now.get().saturating_add(delta));
    }
}

impl Default for FakeClock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock for FakeClock {
    fn now_ns(&self) -> u128 {
        let value = self.now.get();
        let step = self.auto_step.get();
        if step > 0 {
            self.now.set(value.saturating_add(step));
        }
        value
    }
}

/// Round a completed duration up to the next whole microsecond.
pub fn ceil_micros(ns: u128) -> u64 {
    if ns == 0 {
        0
    } else {
        u64::try_from(ns.div_ceil(1000)).unwrap_or(u64::MAX)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sub_microsecond_completed_phase_rounds_up_to_one() {
        assert_eq!(ceil_micros(1), 1);
        assert_eq!(ceil_micros(999), 1);
        assert_eq!(ceil_micros(1000), 1);
        assert_eq!(ceil_micros(1001), 2);
        assert_eq!(ceil_micros(0), 0);
    }

    #[test]
    fn fake_clock_advances() {
        let clock = FakeClock::new();
        assert_eq!(clock.now_ns(), 0);
        clock.advance_ns(50);
        assert_eq!(clock.now_ns(), 50);
    }
}
