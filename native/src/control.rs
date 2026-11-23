use anyhow::{ensure, Context, Result};
use serde::{de::DeserializeOwned, Serialize};
use snow::{Builder, TransportState};
use spake2::{Ed25519Group, Identity, Password, Spake2};
use std::{
    io::{Read, Write},
    net::TcpStream,
    time::{Duration, Instant},
};

fn read_by(s: &mut TcpStream, mut dest: &mut [u8], deadline: Instant) -> Result<()> {
    while !dest.is_empty() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        ensure!(!remaining.is_zero(), "control read deadline");
        s.set_read_timeout(Some(remaining))?;
        match s.read(dest) {
            Ok(0) => anyhow::bail!("peer closed control"),
            Ok(n) => dest = &mut dest[n..],
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}
fn read_frame(s: &mut TcpStream) -> Result<Vec<u8>> {
    let timeout = s.read_timeout()?.context("missing control timeout")?;
    let deadline = Instant::now() + timeout;
    let mut len = [0; 2];
    read_by(s, &mut len, deadline)?;
    let mut bytes = vec![0; u16::from_be_bytes(len) as usize];
    read_by(s, &mut bytes, deadline)?;
    s.set_read_timeout(Some(timeout))?;
    Ok(bytes)
}
fn write_frame(s: &mut TcpStream, data: &[u8]) -> Result<()> {
    s.write_all(&u16::try_from(data.len())?.to_be_bytes())?;
    s.write_all(data)?;
    Ok(())
}

pub struct Control(pub TcpStream, TransportState);
impl Control {
    pub fn authenticate(mut s: TcpStream, key: &str, client: bool) -> Result<Self> {
        s.set_nodelay(true)?;
        s.set_read_timeout(Some(Duration::from_secs(5)))?;
        s.set_write_timeout(Some(Duration::from_secs(5)))?;
        let password = Password::new(key.as_bytes());
        let a = Identity::new(b"EdgeWalker/v1/client");
        let b = Identity::new(b"EdgeWalker/v1/server");
        let (pake, msg) = if client {
            Spake2::<Ed25519Group>::start_a(&password, &a, &b)
        } else {
            Spake2::<Ed25519Group>::start_b(&password, &a, &b)
        };
        write_frame(&mut s, &msg)?;
        let secret = pake
            .finish(&read_frame(&mut s)?)
            .map_err(|_| anyhow::anyhow!("authentication failed"))?;
        let builder = Builder::new("Noise_NNpsk0_25519_ChaChaPoly_SHA256".parse()?)
            .psk(0, secret.as_slice().try_into()?)?;
        let mut hs = if client {
            builder.build_initiator()?
        } else {
            builder.build_responder()?
        };
        let mut buf = [0; 256];
        for writing in [client, !client] {
            if writing {
                let n = hs.write_message(&[], &mut buf)?;
                write_frame(&mut s, &buf[..n])?;
            } else {
                hs.read_message(&read_frame(&mut s)?, &mut buf)
                    .context("authentication failed")?;
            }
        }
        Ok(Self(s, hs.into_transport_mode()?))
    }
    pub fn send<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<()> {
        let plain = serde_json::to_vec(value)?;
        ensure!(plain.len() < 65519, "control frame too large");
        let mut buf = vec![0; plain.len() + 16];
        let n = self.1.write_message(&plain, &mut buf)?;
        write_frame(&mut self.0, &buf[..n])
    }
    pub fn recv<T: DeserializeOwned>(&mut self) -> Result<T> {
        let cipher = read_frame(&mut self.0)?;
        let mut buf = vec![0; cipher.len()];
        let n = self.1.read_message(&cipher, &mut buf)?;
        Ok(serde_json::from_slice(&buf[..n])?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{Shutdown, TcpListener};

    #[test]
    fn incomplete_frame_returns_no_message() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let mut writer = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let (mut reader, _) = listener.accept().unwrap();
        reader
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        writer.write_all(&[0, 3, b'A']).unwrap();
        writer.shutdown(Shutdown::Write).unwrap();
        assert!(read_frame(&mut reader).is_err());
    }
}
