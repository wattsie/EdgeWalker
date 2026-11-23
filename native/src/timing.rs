//! Four-timestamp UDP timing samples. Replies use a separate packet format.
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    net::UdpSocket,
    time::{Duration, Instant},
};
pub const BYTES: usize = 40;

#[derive(Default, Serialize, Deserialize, Debug)]
pub struct Timing {
    pub requested: u64,
    pub valid: u64,
    pub replies_sent: u64,
    pub mean_raw_rtt_us: Option<u64>,
    pub mean_adjusted_rtt_us: Option<u64>,
    pub mean_abs_rtt_variation_us: Option<u64>,
}
pub struct Sampler {
    every: u64,
    origin: Instant,
    id: u32,
    credit_start: Instant,
    next: Instant,
    reply_bps: u64,
    seen: HashSet<u64>,
    raw: u128,
    adjusted: u128,
    variation: u128,
    previous: Option<u64>,
    result: Timing,
}
impl Sampler {
    pub fn new(every: u64, origin: Instant, id: u32, bps: u64) -> Self {
        let now = Instant::now();
        Self {
            every,
            origin,
            id,
            credit_start: now,
            next: now,
            reply_bps: bps,
            seen: HashSet::new(),
            raw: 0,
            adjusted: 0,
            variation: 0,
            previous: None,
            result: Timing::default(),
        }
    }
    pub fn respond(&mut self, sock: &UdpSocket, pkt: &[u8], cutoff: Instant) -> Result<()> {
        if pkt.get(20..24) != Some(b"EWD1")
            || self.every == 0
            || self.reply_bps == 0
            || pkt[..4] != self.id.to_be_bytes()
        {
            return Ok(());
        }
        let seq = u64::from_be_bytes(pkt[4..12].try_into()?);
        let now = Instant::now();
        if seq % self.every != 0 || now < self.next || now > cutoff {
            return Ok(());
        }
        let credit = (now - self.credit_start).as_nanos() * self.reply_bps as u128;
        if credit < (self.result.replies_sent + 1) as u128 * (BYTES * 8) as u128 * 1_000_000_000 {
            return Ok(());
        }
        let mut reply = [0u8; BYTES];
        reply[..20].copy_from_slice(&pkt[..20]);
        reply[20..28].copy_from_slice(&((now - self.origin).as_micros() as u64).to_be_bytes());
        reply[28..36].copy_from_slice(&(self.origin.elapsed().as_micros() as u64).to_be_bytes());
        reply[36..].copy_from_slice(b"EWT1");
        sock.send(&reply)?;
        self.result.replies_sent += 1;
        self.next =
            now + Duration::from_nanos((BYTES as u64 * 8 * 1_000_000_000).div_ceil(self.reply_bps));
        Ok(())
    }
    pub fn observe(
        &mut self,
        pkt: &[u8],
        requests: &std::collections::HashMap<u64, u64>,
    ) -> Result<()> {
        if pkt.len() != BYTES
            || self.every == 0
            || pkt[36..] != *b"EWT1"
            || pkt[..4] != self.id.to_be_bytes()
        {
            return Ok(());
        }
        let seq = u64::from_be_bytes(pkt[4..12].try_into()?);
        if requests.get(&seq) != Some(&u64::from_be_bytes(pkt[12..20].try_into()?))
            || self.seen.contains(&seq)
        {
            return Ok(());
        }
        let get = |i| u64::from_be_bytes(pkt[i..i + 8].try_into().unwrap());
        let Some(raw) = (self.origin.elapsed().as_micros() as u64).checked_sub(get(12)) else {
            return Ok(());
        };
        let Some(adjusted) = get(28)
            .checked_sub(get(20))
            .and_then(|turn| raw.checked_sub(turn))
        else {
            return Ok(());
        };
        self.seen.insert(seq);
        if let Some(last) = self.previous {
            self.variation += raw.abs_diff(last) as u128
        }
        self.previous = Some(raw);
        self.raw += raw as u128;
        self.adjusted += adjusted as u128;
        self.result.valid += 1;
        Ok(())
    }
    pub fn finish(mut self) -> Timing {
        let n = self.result.valid as u128;
        self.result.mean_raw_rtt_us = self.raw.checked_div(n).map(|x| x as u64);
        self.result.mean_adjusted_rtt_us = self.adjusted.checked_div(n).map(|x| x as u64);
        if n > 1 {
            self.result.mean_abs_rtt_variation_us = Some((self.variation / (n - 1)) as u64);
        }
        self.result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_matched_complete_replies_contribute() {
        let origin = Instant::now();
        let mut sampler = Sampler::new(1, origin, 9, 0);
        let requests = [(2u64, 0u64)].into();
        let mut reply = [0u8; BYTES];
        reply[..4].copy_from_slice(&9u32.to_be_bytes());
        reply[4..12].copy_from_slice(&2u64.to_be_bytes());
        reply[36..].copy_from_slice(b"EWT1");
        sampler.observe(&reply[..20], &requests).unwrap();
        reply[36..].copy_from_slice(b"NOTE");
        sampler.observe(&reply, &requests).unwrap();
        assert_eq!(sampler.result.valid, 0);
        reply[36..].copy_from_slice(b"EWT1");
        sampler.observe(&reply, &requests).unwrap();
        sampler.observe(&reply, &requests).unwrap();
        assert_eq!(sampler.finish().valid, 1);
    }
}
