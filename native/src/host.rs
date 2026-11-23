//! Optional endpoint process CPU observation using the POSIX process clock.
use serde::{Deserialize, Serialize};
use std::time::Instant;

#[derive(Default, Serialize, Deserialize, Debug)]
pub struct Resources {
    pub process_cpu_us: Option<u64>,
    pub wall_us: u64,
    pub available_parallelism: Option<usize>,
}

fn cpu_us() -> Option<u64> {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: ts is aligned, initialised and exclusively writable for the call.
    // The POSIX clock ID requests only this process' cumulative CPU time.
    let rc = unsafe { libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut ts) };
    if rc != 0 {
        return None;
    }
    Some(
        u64::try_from(ts.tv_sec).ok()?.checked_mul(1_000_000)?
            + u64::try_from(ts.tv_nsec).ok()? / 1000,
    )
}
pub struct Sample(Instant, Option<u64>);
impl Sample {
    pub fn start() -> Self {
        Self(Instant::now(), cpu_us())
    }
    pub fn finish(self) -> Resources {
        Resources {
            process_cpu_us: self.1.zip(cpu_us()).and_then(|(a, b)| b.checked_sub(a)),
            wall_us: self.0.elapsed().as_micros() as u64,
            available_parallelism: std::thread::available_parallelism().ok().map(|x| x.get()),
        }
    }
}
