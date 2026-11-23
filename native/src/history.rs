//! Reconcile the previous UDP window as its delayed tail or the next sequence arrives.
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

#[derive(Default, Debug, Clone, Serialize, Deserialize)]
pub struct Correction {
    pub window: u32,
    pub sent: u64,
    pub lost: u64,
    pub pending: u64,
    pub reordered: u64,
    pub duplicates: u64,
}
pub struct Prior {
    pub window: u32,
    pub base: u64,
    pub high: u64,
    pub lost: u64,
    pub reordered: u64,
    pub duplicates: u64,
    pub seen: HashSet<u64>,
}
impl Prior {
    pub fn receive(
        &mut self,
        window: u32,
        seq: u64,
        next_window: u32,
        next_base: u64,
    ) -> Result<()> {
        let sent = next_base
            .checked_sub(self.base)
            .ok_or_else(|| anyhow::anyhow!("invalid UDP boundary"))?;
        ensure!(
            self.high <= sent,
            "sender boundary contradicts received sequences"
        );
        if window == next_window && seq > next_base {
            self.lost += sent - self.high;
            self.high = sent;
        } else if window == self.window && seq > self.base && seq <= next_base {
            let offset = seq - self.base;
            if !self.seen.insert(offset) {
                self.duplicates += 1;
            } else if offset <= self.high {
                self.reordered += 1;
            } else {
                self.lost += offset - self.high - 1;
                self.high = offset;
            }
        }
        Ok(())
    }
    pub fn close(&self, next_base: u64) -> Result<Correction> {
        let sent = next_base
            .checked_sub(self.base)
            .ok_or_else(|| anyhow::anyhow!("invalid UDP boundary"))?;
        ensure!(self.high <= sent, "invalid sender final sequence");
        Ok(Correction {
            window: self.window,
            sent,
            lost: self.lost,
            pending: sent - self.high,
            reordered: self.reordered,
            duplicates: self.duplicates,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn late_tail_and_reorder_stay_in_original_window() {
        let mut prior = Prior {
            window: 1,
            base: 0,
            high: 1,
            lost: 0,
            reordered: 0,
            duplicates: 0,
            seen: [1].into(),
        };
        prior.receive(1, 2, 2, 4).unwrap();
        assert_eq!(prior.close(4).unwrap().pending, 2);
        prior.receive(2, 5, 2, 4).unwrap();
        prior.receive(1, 4, 2, 4).unwrap();
        prior.receive(1, 4, 2, 4).unwrap();
        let result = prior.close(4).unwrap();
        assert_eq!(
            (
                result.lost,
                result.pending,
                result.reordered,
                result.duplicates
            ),
            (2, 0, 1, 1)
        );
    }
}
