use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    io::{ErrorKind, Read, Write},
    net::{TcpStream, UdpSocket},
    sync::atomic::{AtomicBool, Ordering},
    thread,
    time::{Duration, Instant},
};

pub const SIZE: usize = 1000;
const BITS: u64 = SIZE as u64 * 8;
#[derive(Clone, Copy, Serialize, Deserialize, PartialEq, Debug, clap::ValueEnum)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Udp,
    Tcp,
    Combined,
}
#[derive(Clone, Copy, Default, Serialize, Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct Rates {
    pub forward: u64,
    pub reverse: u64,
}
#[derive(Clone, Copy, Serialize, Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct Window {
    pub id: u32,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub overload: bool,
    pub ms: u64,
    pub rates: Rates,
    #[serde(default = "one_flow")]
    pub flows: Rates,
}
pub fn one_flow() -> Rates {
    Rates {
        forward: 1,
        reverse: 1,
    }
}
#[derive(Default, Serialize, Deserialize, Debug)]
pub struct Received {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub sparse_transit_us: Vec<i64>,
    pub packets: u64,
    pub bytes: u64,
    pub window_bytes: u64,
    pub arrival_window_bytes: u64,
    pub skipped: u64,
    pub reordered: u64,
    pub duplicates: u64,
    pub highest: u64,
    pub elapsed_us: u64,
    pub median_transit_us: Option<i64>,
    pub delay_growth_us: Option<i64>,
    pub mean_abs_variation_us: Option<u64>,
}
#[derive(Default, Serialize, Deserialize, Debug)]
pub struct Lane {
    pub tcp_stats: Option<crate::tcp_stats::TcpStats>,
    pub retransmissions: Option<u64>,
    pub sent: u64,
    pub received: Received,
}
#[derive(Default, Serialize, Deserialize, Debug)]
pub struct Endpoint {
    pub resources: crate::host::Resources,
    pub timing: crate::timing::Timing,
    pub udp_sequence_base: u64,
    pub prior_udp: Option<crate::history::Correction>,
    pub late_udp_observations: Received,
    pub udp: Lane,
    pub tcp: Vec<Lane>,
}

impl Received {
    fn validate(&self, size: usize, sent: Option<u64>) -> Result<()> {
        ensure!(
            self.sparse_transit_us.len() <= 5
                && self.sparse_transit_us.len() as u64 <= self.packets
                && self.packets <= 1_000_000
                && self.bytes == self.packets * size as u64
                && self.window_bytes <= self.bytes
                && self.window_bytes <= self.arrival_window_bytes
                && self.skipped <= self.highest
                && self.duplicates <= self.packets
                && self.reordered <= self.packets - self.duplicates
                && sent.is_none_or(|count| self.highest <= count),
            "inconsistent receiver counts"
        );
        Ok(())
    }
}

impl Endpoint {
    pub fn validate_feedback(
        &self,
        local: &Endpoint,
        w: Window,
        mode: Mode,
        udp_size: usize,
        base: u64,
    ) -> Result<()> {
        let (udp_offer, tcp_offer) = split(w.rates.forward, mode);
        let within = |count: u128, size: usize, offer: u64| {
            count * size as u128 * 8000 <= offer as u128 * w.ms as u128
        };
        ensure!(
            self.udp_sequence_base == base
                && self.tcp.len() == local.tcp.len()
                && within(self.udp.sent as u128, udp_size, udp_offer)
                && within(
                    self.tcp.iter().map(|l| l.sent as u128).sum(),
                    SIZE,
                    tcp_offer
                )
                && self.timing.valid <= self.timing.requested,
            "feedback outside negotiated window"
        );
        self.udp.received.validate(udp_size, Some(local.udp.sent))?;
        local.udp.received.validate(udp_size, Some(self.udp.sent))?;
        self.late_udp_observations.validate(udp_size, None)?;
        for (remote_lane, local_lane) in self.tcp.iter().zip(&local.tcp) {
            remote_lane.received.validate(SIZE, Some(local_lane.sent))?;
            local_lane.received.validate(SIZE, Some(remote_lane.sent))?;
        }
        if let Some(prior) = &self.prior_udp {
            ensure!(
                prior.window < w.id
                    && prior.sent <= 1_000_000
                    && prior.pending <= prior.sent
                    && prior.lost <= prior.sent - prior.pending,
                "inconsistent previous UDP window"
            );
        }
        Ok(())
    }
}

fn split(rate: u64, mode: Mode) -> (u64, u64) {
    match mode {
        Mode::Udp => (rate, 0),
        Mode::Tcp => (0, rate),
        Mode::Combined => {
            let canary = rate / 10;
            (canary, rate - canary)
        }
    }
}
pub fn supported(rate: u64, mode: Mode, ms: u64, udp_size: usize) -> bool {
    if rate == 0 {
        return true;
    }
    let (udp, tcp) = split(rate, mode);
    (mode == Mode::Tcp || udp as u128 * ms as u128 >= udp_size as u128 * 8000)
        && (mode == Mode::Udp || tcp as u128 * ms as u128 >= BITS as u128 * 1000)
}
fn send(
    rate: u64,
    window: Window,
    origin: Instant,
    cancel: &AtomicBool,
    base: u64,
    size: usize,
    mut write: impl FnMut(&mut [u8], bool) -> Result<bool>,
) -> Result<u64> {
    if rate == 0 {
        return Ok(0);
    }
    let start = Instant::now();
    let end = start + Duration::from_millis(window.ms);
    let bits = size as u64 * 8;
    let interval = Duration::from_nanos((bits * 1_000_000_000).div_ceil(rate));
    // Accumulate at most one millisecond of credit (or one record at low rates).
    // The first record and every batch are paid for before transmission.
    let batch = (rate / (bits * 1000)).clamp(1, 64);
    let total = rate as u128 * window.ms as u128 / (1000 * bits as u128);
    let mut due = start;
    let mut seq = 0u64;
    let mut buf = vec![0; batch as usize * size];
    while seq < total as u64 {
        let count = batch.min(total as u64 - seq);
        due += interval * count as u32;
        while Instant::now() < due && !cancel.load(Ordering::Relaxed) {
            thread::sleep(
                due.saturating_duration_since(Instant::now())
                    .min(Duration::from_millis(25)),
            );
        }
        ensure!(!cancel.load(Ordering::Relaxed), "control closed");
        if Instant::now() > end + Duration::from_millis(2) {
            break;
        }
        let data = &mut buf[..count as usize * size];
        for packet in data.chunks_exact_mut(size) {
            seq += 1;
            packet[20..24].copy_from_slice(b"EWD1");
            packet[..4].copy_from_slice(&window.id.to_be_bytes());
            packet[4..12].copy_from_slice(&(base + seq).to_be_bytes());
            packet[12..20].copy_from_slice(&(origin.elapsed().as_micros() as u64).to_be_bytes());
        }
        let final_batch = seq == total as u64 || Instant::now() + Duration::from_millis(1) >= end;
        if write(data, final_batch)? {
            break;
        }
        if due + interval * (batch as u32) < Instant::now() {
            due = Instant::now();
        }
    }
    Ok(seq)
}

#[derive(Default)]
struct Counter {
    rx: Received,
    base: u64,
    seen: HashSet<u64>,
    transit: Vec<i64>,
    variation: u64,
    pairs: u64,
    prev: Option<(u64, i64)>,
    first: Option<Instant>,
    last: Option<Instant>,
}
impl Counter {
    fn add(&mut self, buf: &[u8], id: u32, origin: Instant, cutoff: Instant) -> Result<()> {
        ensure!(buf.get(20..24) == Some(b"EWD1"), "invalid record version");
        let now = Instant::now();
        if now <= cutoff {
            self.rx.arrival_window_bytes += buf.len() as u64;
        }
        if u32::from_be_bytes(buf[..4].try_into()?) != id {
            return Ok(());
        }
        let seq = u64::from_be_bytes(buf[4..12].try_into()?)
            .checked_sub(self.base)
            .context("sequence before window")?;
        ensure!(seq > 0 && seq <= 1_000_000, "invalid sequence");
        ensure!(self.rx.packets < 1_000_000, "window receive limit exceeded");
        let tx = u64::from_be_bytes(buf[12..20].try_into()?);
        ensure!(tx < 1u64 << 50, "invalid timestamp");
        self.first.get_or_insert(now);
        self.last = Some(now);
        self.rx.bytes += buf.len() as u64;
        if now <= cutoff {
            self.rx.window_bytes += buf.len() as u64;
        }
        self.rx.packets += 1;
        if !self.seen.insert(seq) {
            self.rx.duplicates += 1;
            return Ok(());
        }
        if seq <= self.rx.highest {
            self.rx.reordered += 1;
            return Ok(());
        }
        self.rx.skipped += seq - self.rx.highest - 1;
        self.rx.highest = seq;
        let transit = (now - origin).as_micros() as i64 - i64::try_from(tx)?;
        if let Some((prev, old)) = self.prev {
            if prev + 1 == seq {
                self.variation += old.abs_diff(transit);
                self.pairs += 1;
            }
        }
        self.prev = Some((seq, transit));
        self.transit.push(transit);
        Ok(())
    }
    fn finish(mut self) -> Received {
        if let (Some(first), Some(last)) = (self.first, self.last) {
            self.rx.elapsed_us = (last - first).as_micros() as u64;
        }
        self.rx.mean_abs_variation_us = self.variation.checked_div(self.pairs);
        let n = self.transit.len() / 3;
        if n >= 2 {
            fn median(slice: &mut [i64]) -> i64 {
                let n = slice.len() / 2;
                *slice.select_nth_unstable(n).1
            }
            let a = median(&mut self.transit[..n]);
            let b = median(&mut self.transit[n..2 * n]);
            let c = median(&mut self.transit[2 * n..]);
            self.rx.delay_growth_us = Some((b - a).min(c - b));
            self.rx.median_transit_us = Some(median(&mut self.transit));
        } else {
            self.rx.sparse_transit_us = self.transit;
        }
        self.rx
    }
}

pub struct Flow {
    socket: TcpStream,
    pending: Vec<u8>,
}
impl Flow {
    pub fn new(socket: TcpStream) -> Self {
        Self {
            socket,
            pending: Vec::with_capacity(65536 + SIZE),
        }
    }
}
fn read_tcp(
    tcp: &TcpStream,
    pending: &mut Vec<u8>,
    w: Window,
    origin: Instant,
    cutoff: Instant,
    end: Instant,
    cancel: &AtomicBool,
) -> Result<Received> {
    let mut c = Counter::default();
    let mut sock = tcp;
    let mut buf = [0; 65536];
    while Instant::now() < end && !cancel.load(Ordering::Relaxed) {
        match sock.read(&mut buf) {
            Ok(0) => bail!("data connection closed"),
            Ok(n) => {
                pending.extend_from_slice(&buf[..n]);
                let (records, tail) = pending.as_chunks::<SIZE>();
                let used = pending.len() - tail.len();
                let mut finished = false;
                for record in records {
                    c.add(record, w.id, origin, cutoff)?;
                    finished |= record[..4] == w.id.to_be_bytes() && &record[24..28] == b"EWTE";
                }
                pending.drain(..used);
                if finished {
                    break;
                }
            }
            Err(e)
                if matches!(
                    e.kind(),
                    ErrorKind::TimedOut | ErrorKind::WouldBlock | ErrorKind::Interrupted
                ) =>
            {
                continue
            }
            Err(e) => return Err(e.into()),
        }
    }
    Ok(c.finish())
}

pub struct Run {
    pub mode: Mode,
    pub w: Window,
    pub client: bool,
    pub origin: Instant,
    pub base: u64,
    pub peer_base: u64,
    pub every: u64,
    pub udp_size: usize,
    pub outgoing_limit: u64,
}
pub fn reply_rate(bulk: u64, mode: Mode, every: u64, udp_size: usize) -> u64 {
    if every == 0 {
        return 0;
    }
    let udp = split(bulk, mode).0;
    ((udp as u128 * crate::timing::BYTES as u128).div_ceil(udp_size as u128 * every as u128)) as u64
}
pub fn measure(
    control: &TcpStream,
    udp: Option<&UdpSocket>,
    tcp: &mut [Flow],
    previous_udp: &mut Option<crate::history::Prior>,
    run: Run,
) -> Result<Endpoint> {
    let resource_sample = crate::host::Sample::start();
    let Run {
        mode,
        w,
        client,
        origin,
        base,
        peer_base,
        every,
        udp_size,
        outgoing_limit,
    } = run;
    ensure!(
        base <= u64::MAX - 1_000_001 && peer_base <= u64::MAX - 1_000_001,
        "sequence exhausted"
    );
    let (out, input, send_flows, recv_flows) = if client {
        (
            w.rates.reverse,
            w.rates.forward,
            w.flows.reverse,
            w.flows.forward,
        )
    } else {
        (
            w.rates.forward,
            w.rates.reverse,
            w.flows.forward,
            w.flows.reverse,
        )
    };
    let replies = reply_rate(input, mode, every, udp_size);
    let replies = if out == 0 {
        replies.min(outgoing_limit)
    } else {
        replies.min(out)
    };
    let (us, ts) = split(out.saturating_sub(replies), mode);
    let (ur, tr) = split(input, mode);
    let nominal_end = Instant::now() + Duration::from_millis(w.ms);
    // The server starts its clock when it acknowledges the window. Return data
    // first traverses the acknowledgement path, then the data path. Allow one
    // measured control RTT for that offset, bounded independently of the offer.
    let offset = if !client && input > 0 {
        crate::tcp_stats::sample(control)
            .map(|stats| Duration::from_micros(stats.srtt_us.min(2_000_000)))
            .unwrap_or_default()
    } else {
        Duration::ZERO
    };
    let cutoff = nominal_end + offset;
    let end = cutoff + Duration::from_millis(400);
    let pending_timing = std::sync::Mutex::new(std::collections::HashMap::<u64, u64>::new());
    let requests = &pending_timing;
    control.set_nonblocking(true)?;
    let finished = AtomicBool::new(false);
    let cancelled = AtomicBool::new(false);
    let cancel = &cancelled;
    let result = thread::scope(|watch| {
        watch.spawn(|| {
            while !finished.load(Ordering::Relaxed) {
                match control.peek(&mut [0u8; 1]) {
                    Ok(0) => break,
                    Err(e)
                        if !matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::Interrupted) =>
                    {
                        break
                    }
                    _ => thread::sleep(Duration::from_millis(20)),
                }
            }
            cancelled.store(true, Ordering::Relaxed);
        });
        let result = thread::scope(|scope| -> Result<Endpoint> {
            let u = scope.spawn(move || {
                send(us, w, origin, cancel, base, udp_size, |buf, _| {
                    for packet in buf.chunks_exact(udp_size) {
                        let seq = u64::from_be_bytes(packet[4..12].try_into()?);
                        if every != 0 && seq % every == 0 {
                            requests
                                .lock()
                                .unwrap()
                                .insert(seq, u64::from_be_bytes(packet[12..20].try_into()?));
                        }
                        udp.context("UDP unavailable")?.send(packet)?;
                    }
                    Ok(false)
                })
            });
            let ud = scope.spawn(move || -> Result<(Received, Received, crate::timing::Timing, Option<crate::history::Correction>)> {
                let mut c = Counter {
                    base: peer_base,
                    ..Default::default()
                };
                let mut late = Counter {
                    base: previous_udp.as_ref().map_or(0, |p| p.base),
                    ..Default::default()
                };
                let mut sampler = crate::timing::Sampler::new(every, origin, w.id, replies);
                let mut buf = vec![0;udp_size+1];
                while (ur > 0 || (us > 0 && every > 0))
                    && Instant::now() < end
                    && !cancel.load(Ordering::Relaxed)
                {
                    match udp.context("UDP unavailable")?.recv(&mut buf) {
                        Ok(n) if n==udp_size => {
                            let window_id = u32::from_be_bytes(buf[..4].try_into()?);
                            if window_id != w.id && !previous_udp.as_ref().is_some_and(|p| p.window == window_id) {
                                continue;
                            }
                            c.add(&buf[..udp_size], w.id, origin, cutoff)?;
                            if let Some(prev)=previous_udp.as_mut(){
                                if u32::from_be_bytes(buf[..4].try_into()?) == prev.window {
                                    late.add(&buf[..udp_size], prev.window, origin, cutoff)?;
                                }
                                prev.receive(u32::from_be_bytes(buf[..4].try_into()?),u64::from_be_bytes(buf[4..12].try_into()?),w.id,peer_base)?;
                            }
                            sampler.respond(
                                udp.unwrap(),
                                &buf[..udp_size],
                                cutoff,
                            )?;
                        }
                        Ok(crate::timing::BYTES) => sampler.observe(&buf[..crate::timing::BYTES],&requests.lock().unwrap())?,
                        Ok(_) => continue,
                        Err(e)
                            if matches!(e.kind(), ErrorKind::TimedOut | ErrorKind::WouldBlock | ErrorKind::Interrupted) =>
                        {
                            continue
                        }
                        Err(e) => return Err(e.into()),
                    }
                }
                let prior=previous_udp.as_ref().map(|p|p.close(peer_base)).transpose()?;
                *previous_udp=Some(crate::history::Prior{window:w.id,base:peer_base,high:c.rx.highest,
                    lost:c.rx.skipped,reordered:c.rx.reordered,duplicates:c.rx.duplicates,
                    seen:std::mem::take(&mut c.seen)});
                Ok((c.finish(), late.finish(), sampler.finish(),prior))
            });
            let mut flows = Vec::new();
            for (i, flow) in tcp.iter_mut().enumerate() {
                let sock = &flow.socket;
                let pending = &mut flow.pending;
                let i = i as u64;
                let rate = if i < send_flows {
                    ts / send_flows + u64::from(i < ts % send_flows)
                } else {
                    0
                };
                let before = crate::tcp_stats::sample(sock);
                let writer = scope.spawn(move || {
                    let mut s = sock;
                    send(rate, w, origin, cancel, 0, SIZE, |buf, final_batch| {
                        if final_batch {
                            let last = buf.len() - SIZE;
                            buf[last + 24..last + 28].copy_from_slice(b"EWTE");
                        }
                        s.write_all(buf)?;
                        Ok(final_batch)
                    })
                });
                let reader = scope.spawn(move || {
                    if i < recv_flows && tr > 0 {
                        read_tcp(sock, pending, w, origin, cutoff, end, cancel)
                    } else {
                        Ok(Received::default())
                    }
                });
                flows.push((writer, reader, before, sock));
            }
            let mut tcp = Vec::new();
            for (writer, reader, before, sock) in flows {
                let sent = writer.join().unwrap()?;
                let received = reader.join().unwrap()?;
                let after = crate::tcp_stats::sample(sock);
                tcp.push(Lane {
                    sent,
                    received,
                    tcp_stats: after,
                    retransmissions: before.zip(after).and_then(|(a, b)| {
                        b.retransmitted_packets.checked_sub(a.retransmitted_packets)
                    }),
                });
            }
            let sent = u.join().unwrap()?;
            let (received, late_udp_observations, mut timing, prior_udp) = ud.join().unwrap()?;
            if let Some(last) = (base + sent).checked_div(every) {
                timing.requested = last - base / every;
            }
            // A reliable ordered end marker removes the TCP drain, while the
            // measurement interval still spans its full nominal duration.
            while Instant::now() < nominal_end && !cancel.load(Ordering::Relaxed) {
                thread::sleep(
                    nominal_end
                        .saturating_duration_since(Instant::now())
                        .min(Duration::from_millis(20)),
                );
            }
            Ok(Endpoint {
                resources: resource_sample.finish(),
                timing,
                udp_sequence_base: base,
                prior_udp,
                late_udp_observations,
                udp: Lane {
                    tcp_stats: None,
                    retransmissions: None,
                    sent,
                    received,
                },
                tcp,
            })
        });
        finished.store(true, Ordering::Relaxed);
        result
    });
    control.set_nonblocking(false)?;
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn window_feedback_must_match_admitted_offer_and_sender_range() {
        let w = Window {
            id: 1,
            ms: 1000,
            overload: false,
            rates: Rates {
                forward: 8000,
                reverse: 0,
            },
            flows: one_flow(),
        };
        let mut peer = Endpoint::default();
        let local = Endpoint::default();
        peer.udp.sent = 1;
        assert!(peer
            .validate_feedback(&local, w, Mode::Udp, 1000, 0)
            .is_ok());
        peer.udp.sent = 2;
        assert!(peer
            .validate_feedback(&local, w, Mode::Udp, 1000, 0)
            .is_err());
        peer.udp.sent = 1;
        peer.udp.received.highest = 1;
        assert!(peer
            .validate_feedback(&local, w, Mode::Udp, 1000, 0)
            .is_err());
        peer.udp.received.highest = 0;
        assert!(peer
            .validate_feedback(&local, w, Mode::Udp, 1000, 1)
            .is_err());
    }

    #[test]
    fn rejected_record_header_leaves_no_receiver_observation() {
        let mut c = Counter::default();
        let now = Instant::now();
        assert!(c.add(b"ordinary note", 1, now, now).is_err());
        assert_eq!(c.rx.arrival_window_bytes, 0);
        assert_eq!(c.rx.packets, 0);
    }
}
