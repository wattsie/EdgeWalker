//! Read-only TCP metrics. Keep the platform call and ABI decoding in this module.
use serde::{Deserialize, Serialize};
use std::{mem::size_of, net::TcpStream, os::fd::AsRawFd};

#[derive(Clone, Copy, Default, Serialize, Deserialize, Debug)]
pub struct TcpStats {
    pub cwnd_bytes: u64,
    pub retransmitted_packets: u64,
    pub srtt_us: u64,
}

pub fn sample(stream: &TcpStream) -> Option<TcpStats> {
    #[cfg(target_os = "macos")]
    let option = libc::TCP_CONNECTION_INFO;
    #[cfg(target_os = "linux")]
    let option = libc::TCP_INFO;
    let mut buffer = [0u8; 512];
    let mut len = buffer.len() as libc::socklen_t;
    // SAFETY: the borrowed socket outlives the call. Both output pointers are
    // aligned for their types, exclusively writable and valid for the stated
    // lengths. Only the returned prefix is decoded, using checked byte slices.
    let ok = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::IPPROTO_TCP,
            option,
            buffer.as_mut_ptr().cast(),
            &mut len,
        )
    };
    if ok != 0 {
        return None;
    }
    let bytes = buffer.get(..len as usize)?;
    let u32_at =
        |i| Some(u32::from_ne_bytes(bytes.get(i..i + size_of::<u32>())?.try_into().ok()?) as u64);
    #[cfg(target_os = "macos")]
    {
        // Darwin netinet/tcp.h: bitfields occupy one u32 before the aligned
        // u64 counters. libc's Darwin bitfield layout varies from the C ABI.
        Some(TcpStats {
            cwnd_bytes: u32_at(24)?,
            srtt_us: u32_at(44)? * 1000,
            retransmitted_packets: u64::from_ne_bytes(bytes.get(104..112)?.try_into().ok()?),
        })
    }
    #[cfg(target_os = "linux")]
    {
        use std::mem::offset_of;
        Some(TcpStats {
            cwnd_bytes: u32_at(offset_of!(libc::tcp_info, tcpi_snd_cwnd))?
                * u32_at(offset_of!(libc::tcp_info, tcpi_snd_mss))?,
            srtt_us: u32_at(offset_of!(libc::tcp_info, tcpi_rtt))?,
            retransmitted_packets: u32_at(offset_of!(libc::tcp_info, tcpi_total_retrans))?,
        })
    }
}
