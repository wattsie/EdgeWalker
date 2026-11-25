#![deny(unsafe_code)]
mod control;
mod engine;
mod history;
#[allow(unsafe_code)]
mod host;
#[allow(unsafe_code)]
mod tcp_stats;
mod timing;
use anyhow::{bail, ensure, Context, Result};
use clap::{Parser, Subcommand};
use control::Control;
use engine::{Endpoint, Mode, Rates, Window};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    collections::HashMap,
    env, fs,
    io::{self, BufRead, Read, Write},
    net::{IpAddr, SocketAddr, TcpListener, TcpStream, UdpSocket},
    path::PathBuf,
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

#[derive(Parser)]
#[command(version, about = "EdgeWalker authenticated measurement engine")]
struct Args {
    #[arg(long, global = true)]
    config: Option<PathBuf>,
    #[arg(long, global = true)]
    key: Option<String>,
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    Keygen,
    Server {
        #[arg(long)]
        listen: SocketAddr,
        #[arg(long,value_parser=parse_ports)]
        data_ports: Option<(u16, u16)>,
        #[arg(long, default_value_t = 0)]
        allow_overload_ms: u64,
        #[arg(long, conflicts_with_all=["tx","rx"])]
        capacity: Option<u64>,
        #[arg(long, requires = "rx")]
        tx: Option<u64>,
        #[arg(long, requires = "tx")]
        rx: Option<u64>,
    },
    Client {
        #[arg(long)]
        connect: SocketAddr,
        #[arg(long, value_enum)]
        mode: Mode,
        #[arg(long, default_value_t = 0)]
        forward_max: u64,
        #[arg(long, default_value_t = 1)]
        flow_limit: u64,
        #[arg(long, default_value_t = 0)]
        timing_every: u64,
        #[arg(long)]
        tcp_send_buffer: Option<usize>,
        #[arg(long, default_value_t = 1000)]
        udp_size: usize,
        #[arg(long, default_value_t = 0)]
        overload_ms: u64,
        #[arg(long, default_value_t = 0)]
        return_max: u64,
    },
}
#[derive(Deserialize)]
struct Config {
    key: String,
}
fn key(args: &Args) -> Result<String> {
    let value = if let Some(value) = &args.key {
        value.clone()
    } else {
        let path = args
            .config
            .clone()
            .or_else(|| env::var_os("EDGEWALKER_CONFIG").map(PathBuf::from))
            .or_else(|| {
                env::var_os("XDG_CONFIG_HOME")
                    .map(PathBuf::from)
                    .filter(|p| !p.as_os_str().is_empty())
                    .map(|p| p.join("edgewalker/config.json"))
            })
            .or_else(|| {
                env::var_os("HOME").map(|p| PathBuf::from(p).join(".config/edgewalker/config.json"))
            })
            .context("supply --config or --key")?;
        let bytes = fs::read(path).context("unable to read lab key configuration")?;
        serde_json::from_slice::<Config>(&bytes)
            .map_err(|_| anyhow::anyhow!("invalid lab key configuration"))?
            .key
    };
    ensure!(
        (12..=16).contains(&value.chars().count()),
        "lab key must have 12 to 16 characters"
    );
    Ok(value)
}
fn random() -> Result<[u8; 32]> {
    let mut bytes = [0; 32];
    getrandom::fill(&mut bytes).map_err(|_| anyhow::anyhow!("random source unavailable"))?;
    Ok(bytes)
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Session {
    version: u8,
    mode: Mode,
    max: Rates,
    flow_limit: u64,
    timing_every: u64,
    tcp_send_buffer: Option<usize>,
    udp_size: usize,
    overload_ms: u64,
}
#[derive(Serialize, Deserialize)]
enum Admission {
    OverloadDenied {
        allowed_ms: u64,
    },
    Ready {
        udp: u16,
        tcp: u16,
        token: [u8; 32],
    },
    Rejected {
        requested: Rates,
        provisioned: Rates,
        available: Rates,
    },
}
#[derive(Default)]
struct Ledger {
    used: Rates,
    workers: HashMap<IpAddr, usize>,
    failed: HashMap<IpAddr, Instant>,
}
type Shared = Arc<Mutex<Ledger>>;
struct Slot(Shared, IpAddr, Rates);
impl Drop for Slot {
    fn drop(&mut self) {
        let mut l = self.0.lock().unwrap();
        l.used.forward -= self.2.forward;
        l.used.reverse -= self.2.reverse;
        let n = l.workers.get_mut(&self.1).unwrap();
        *n -= 1;
        if *n == 0 {
            l.workers.remove(&self.1);
        }
    }
}
impl Slot {
    fn reserve(&mut self, cap: Rates, r: Rates) -> std::result::Result<(), Admission> {
        let mut l = self.0.lock().unwrap();
        let available = Rates {
            forward: cap.forward - l.used.forward,
            reverse: cap.reverse - l.used.reverse,
        };
        if r.forward > available.forward || r.reverse > available.reverse {
            return Err(Admission::Rejected {
                requested: r,
                provisioned: cap,
                available,
            });
        }
        l.used.forward += r.forward;
        l.used.reverse += r.reverse;
        self.2 = r;
        Ok(())
    }
}

fn validate_session(plan: &Session) -> Result<()> {
    ensure!(
        (128..=9000).contains(&plan.udp_size)
            && plan.version == 1
            && (1..=16).contains(&plan.flow_limit)
            && (plan.max.forward > 0 || plan.max.reverse > 0),
        "unsupported session"
    );
    ensure!(
        plan.tcp_send_buffer
            .is_none_or(|n| (8192..=4 * 1024 * 1024).contains(&n)),
        "TCP send buffer must be 8192..4194304 bytes"
    );
    Ok(())
}

const DATA_CONNECT_TIMEOUT: Duration = Duration::from_secs(3);

fn join_timeout(mode: Mode, flows: u64) -> Duration {
    let connections = if mode == Mode::Udp { 0 } else { flows };
    Duration::from_secs(5) + DATA_CONNECT_TIMEOUT * connections as u32
}

fn tcp_write_timeout(srtt_us: u64) -> Duration {
    Duration::from_micros(srtt_us.saturating_mul(3))
        .clamp(Duration::from_millis(500), Duration::from_secs(3))
}

fn io_settings(tcp: &TcpStream, buffer: Option<usize>) -> Result<()> {
    tcp.set_read_timeout(Some(Duration::from_millis(30)))?;
    let srtt_us = tcp_stats::sample(tcp).map_or(0, |s| s.srtt_us);
    tcp.set_write_timeout(Some(tcp_write_timeout(srtt_us)))?;
    tcp.set_nodelay(true)?;
    if let Some(bytes) = buffer {
        socket2::SockRef::from(tcp).set_send_buffer_size(bytes)?;
    }
    Ok(())
}
fn validate(w: Window, plan: &Session, prev: u32) -> Result<()> {
    ensure!(
        w.id > prev && (250..=10000).contains(&w.ms),
        "invalid window id or duration"
    );
    ensure!(
        w.rates.forward <= plan.max.forward && w.rates.reverse <= plan.max.reverse,
        "rate exceeds reservation"
    );
    for (rate, flows) in [
        (w.rates.forward, w.flows.forward),
        (w.rates.reverse, w.flows.reverse),
    ] {
        ensure!(
            (1..=plan.flow_limit).contains(&flows),
            "invalid active flow count"
        );
        if plan.mode != Mode::Udp && rate > 0 {
            let tcp_rate = if plan.mode == Mode::Combined {
                rate - rate / 10
            } else {
                rate
            };
            ensure!(
                tcp_rate as u128 * w.ms as u128 >= flows as u128 * 8_000_000,
                "too few TCP bytes per flow"
            );
        }
        ensure!(
            engine::supported(rate, plan.mode, w.ms, plan.udp_size),
            "too few bytes for requested window"
        );
        ensure!(
            rate as u128 * w.ms as u128 <= 800_000_000 * plan.udp_size.min(engine::SIZE) as u128,
            "window packet limit exceeded"
        );
    }
    Ok(())
}
fn parse_ports(text: &str) -> Result<(u16, u16)> {
    let (low, high) = text.split_once('-').context("expected FIRST-LAST")?;
    let (low, high) = (low.parse::<u16>()?, high.parse::<u16>()?);
    ensure!(low > 0 && low <= high, "invalid data port range");
    Ok((low, high))
}
fn bind_data<T>(
    ip: IpAddr,
    range: Option<(u16, u16)>,
    bind: impl Fn(SocketAddr) -> io::Result<T>,
) -> Result<T> {
    let (low, high) = range.unwrap_or((0, 0));
    for port in low..=high {
        match bind(SocketAddr::new(ip, port)) {
            Ok(socket) => return Ok(socket),
            Err(e) if e.kind() == io::ErrorKind::AddrInUse => continue,
            Err(e) => return Err(e.into()),
        }
    }
    bail!("data ports exhausted")
}
#[derive(Serialize, Deserialize)]
enum Request {
    Window(Window, u64),
    Ready(u32),
    Stop,
}
fn serve_session(
    s: TcpStream,
    key: &str,
    cap: Rates,
    ports: Option<(u16, u16)>,
    allow_overload_ms: u64,
    mut slot: Slot,
) -> Result<()> {
    let local = s.local_addr()?.ip();
    let peer = s.peer_addr()?.ip();
    let mut c = match Control::authenticate(s, key, false) {
        Ok(c) => c,
        Err(e) => {
            let mut l = slot.0.lock().unwrap();
            if l.failed.len() < 1024 {
                l.failed.insert(peer, Instant::now());
            }
            return Err(e);
        }
    };
    let plan: Session = c.recv()?;
    validate_session(&plan)?;
    if plan.overload_ms > allow_overload_ms {
        c.send(&Admission::OverloadDenied {
            allowed_ms: allow_overload_ms,
        })?;
        return Ok(());
    }
    let mut remaining_overload = plan.overload_ms;
    let mut reservation = plan.max;
    if reservation.forward == 0 {
        reservation.forward = engine::reply_rate(
            plan.max.reverse,
            plan.mode,
            plan.timing_every,
            plan.udp_size,
        )
    }
    if reservation.reverse == 0 {
        reservation.reverse = engine::reply_rate(
            plan.max.forward,
            plan.mode,
            plan.timing_every,
            plan.udp_size,
        )
    }
    if let Err(rejection) = slot.reserve(cap, reservation) {
        c.send(&rejection)?;
        return Ok(());
    }
    let udp = bind_data(local, ports, UdpSocket::bind)?;
    let listener = bind_data(local, ports, TcpListener::bind)?;
    let token = random()?;
    c.send(&Admission::Ready {
        udp: udp.local_addr()?.port(),
        tcp: listener.local_addr()?.port(),
        token,
    })?;
    // The client opens its admitted sockets in order before acknowledging the join.
    c.0.set_read_timeout(Some(join_timeout(plan.mode, plan.flow_limit)))?;
    ensure!(c.recv::<String>()? == "joined", "missing data setup");
    c.0.set_read_timeout(Some(Duration::from_secs(5)))?;
    listener.set_nonblocking(true)?;
    let mut tcp = Vec::new();
    if plan.mode != Mode::Udp {
        for _ in 0..plan.flow_limit {
            let (mut flow, addr) = listener.accept()?;
            ensure!(addr.ip() == peer, "unexpected data peer");
            flow.set_read_timeout(Some(Duration::from_secs(3)))?;
            let mut proof = [0; 32];
            flow.read_exact(&mut proof)?;
            ensure!(proof == token, "invalid data token");
            io_settings(&flow, plan.tcp_send_buffer)?;
            tcp.push(engine::Flow::new(flow));
        }
    }
    drop(listener);
    if plan.mode != Mode::Tcp {
        udp.set_read_timeout(Some(Duration::from_secs(3)))?;
        let mut buf = [0; 33];
        let (n, addr) = udp.recv_from(&mut buf)?;
        ensure!(
            n == 32 && buf[..32] == token && addr.ip() == peer,
            "invalid UDP join"
        );
        udp.connect(addr)?;
        let challenge = random()?;
        udp.send(&challenge)?;
        ensure!(
            c.recv::<[u8; 32]>()? == challenge,
            "UDP reachability failed"
        );
        udp.set_read_timeout(Some(Duration::from_millis(30)))?;
    }
    c.send(&"ready")?;
    c.0.set_read_timeout(Some(Duration::from_secs(20)))?;
    let origin = Instant::now();
    let mut prev = 0;
    let mut base = 0;
    let mut previous_udp = None;
    loop {
        let (w, peer_base) = match c.recv::<Request>()? {
            Request::Window(w, base) => (w, base),
            Request::Ready(id) => {
                ensure!(id == prev, "wrong readiness generation");
                c.send(&id)?;
                continue;
            }
            Request::Stop => break,
        };
        validate(w, &plan, prev)?;
        if w.overload {
            remaining_overload = remaining_overload
                .checked_sub(w.ms)
                .context("overload allowance exhausted")?;
        }
        prev = w.id;
        c.send(&base)?;
        let result = engine::measure(
            &c.0,
            Some(&udp),
            &mut tcp,
            &mut previous_udp,
            engine::Run {
                mode: plan.mode,
                w,
                client: false,
                outgoing_limit: reservation.forward,
                origin,
                base,
                peer_base,
                every: plan.timing_every,
                udp_size: plan.udp_size,
            },
        )?;
        base += result.udp.sent;
        c.send(&result)?;
    }
    drop(slot);
    c.send(&"stopped")?;
    Ok(())
}
fn server(
    listen: SocketAddr,
    cap: Rates,
    ports: Option<(u16, u16)>,
    allow_overload_ms: u64,
    key: String,
) -> Result<()> {
    let listener = TcpListener::bind(listen)?;
    println!(
        "{}",
        json!({"listening":listener.local_addr()?.to_string()})
    );
    let ledger = Shared::default();
    let key = Arc::new(key);
    for sock in listener.incoming() {
        let s = sock?;
        let peer = s.peer_addr()?.ip();
        let mut l = ledger.lock().unwrap();
        l.failed.retain(|_, t| t.elapsed() < Duration::from_secs(2));
        if l.failed.contains_key(&peer)
            || l.failed.len() >= 1024
            || l.workers.values().sum::<usize>() >= 8
            || l.workers.get(&peer).copied().unwrap_or(0) >= 4
        {
            continue;
        }
        *l.workers.entry(peer).or_default() += 1;
        drop(l);
        let slot = Slot(ledger.clone(), peer, Rates::default());
        let key = key.clone();
        thread::spawn(move || {
            let _ = serve_session(s, &key, cap, ports, allow_overload_ms, slot);
        });
    }
    Ok(())
}
fn client(addr: SocketAddr, plan: Session, key: &str) -> Result<()> {
    validate_session(&plan)?;
    let sock = TcpStream::connect_timeout(&addr, Duration::from_secs(5)).inspect_err(|e| {
        if matches!(
            e.kind(),
            io::ErrorKind::ConnectionRefused
                | io::ErrorKind::TimedOut
                | io::ErrorKind::NetworkUnreachable
                | io::ErrorKind::HostUnreachable
        ) {
            println!("{}", json!({"status":"peer_offline"}));
        }
    })?;
    let local = sock.local_addr()?.ip();
    let mut c = Control::authenticate(sock, key, true)?;
    c.send(&plan)?;
    let (up, tp, token) = match c.recv::<Admission>()? {
        Admission::Ready { udp, tcp, token } => (udp, tcp, token),
        r => bail!("admission rejected: {}", serde_json::to_string(&r)?),
    };
    let mut tcp = Vec::new();
    if plan.mode != Mode::Udp {
        for _ in 0..plan.flow_limit {
            let mut flow =
                TcpStream::connect_timeout(&SocketAddr::new(addr.ip(), tp), DATA_CONNECT_TIMEOUT)?;
            flow.write_all(&token)?;
            io_settings(&flow, plan.tcp_send_buffer)?;
            tcp.push(engine::Flow::new(flow));
        }
    }
    let udp = UdpSocket::bind((local, 0))?;
    if plan.mode != Mode::Tcp {
        udp.connect((addr.ip(), up))?;
        udp.set_read_timeout(Some(Duration::from_secs(4)))?;
        udp.send(&token)?;
    }
    c.send(&"joined")?;
    if plan.mode != Mode::Tcp {
        let mut challenge = [0; 33];
        ensure!(udp.recv(&mut challenge)? == 32, "invalid challenge");
        c.send(&challenge[..32])?;
        udp.set_read_timeout(Some(Duration::from_millis(30)))?;
    }
    ensure!(c.recv::<String>()? == "ready", "receiver unavailable");
    c.0.set_read_timeout(Some(Duration::from_secs(15)))?;
    println!("{}", json!({"status":"ready","version":1}));
    let mut expected_peer_base = 0;
    let mut prev = 0;
    let origin = Instant::now();
    let mut base = 0;
    let mut previous_udp = None;
    for line in io::stdin().lock().lines() {
        let line = line?;
        if line.trim() == "\"ready\"" {
            c.send(&Request::Ready(prev))?;
            ensure!(c.recv::<u32>()? == prev, "readiness generation mismatch");
            println!("{}", json!({"status":"ready","after":prev}));
            continue;
        }
        let request: Option<Window> = serde_json::from_str(&line)?;
        let Some(w) = request else { break };
        validate(w, &plan, prev)?;
        prev = w.id;
        c.send(&Request::Window(w, base))?;
        let peer_base = c.recv::<u64>()?;
        ensure!(
            peer_base == expected_peer_base,
            "unexpected sender sequence boundary"
        );
        let local = engine::measure(
            &c.0,
            Some(&udp),
            &mut tcp,
            &mut previous_udp,
            engine::Run {
                mode: plan.mode,
                w,
                client: true,
                outgoing_limit: if plan.max.reverse == 0 {
                    engine::reply_rate(
                        plan.max.forward,
                        plan.mode,
                        plan.timing_every,
                        plan.udp_size,
                    )
                } else {
                    plan.max.reverse
                },
                origin,
                base,
                peer_base,
                every: plan.timing_every,
                udp_size: plan.udp_size,
            },
        )?;
        base += local.udp.sent;
        let remote: Endpoint = c.recv()?;
        remote.validate_feedback(&local, w, plan.mode, plan.udp_size, peer_base)?;
        expected_peer_base = peer_base
            .checked_add(remote.udp.sent)
            .context("UDP sequence exhausted")?;
        println!("{}", json!({"window":w,"client":local,"server":remote}));
    }
    c.send(&Request::Stop)?;
    ensure!(c.recv::<String>()? == "stopped", "stop unconfirmed");
    Ok(())
}
fn main() -> Result<()> {
    let args = Args::parse();
    match args.command {
        Command::Keygen => {
            let alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
            let code: String = random()?[..16]
                .iter()
                .map(|b| alphabet[(b & 63) as usize] as char)
                .collect();
            println!("{}", json!({"key":code}));
            Ok(())
        }
        Command::Server {
            listen,
            data_ports,
            allow_overload_ms,
            capacity,
            tx,
            rx,
        } => {
            let cap = Rates {
                forward: capacity.or(tx).context("set explicit capacity")?,
                reverse: capacity.or(rx).context("set explicit capacity")?,
            };
            server(listen, cap, data_ports, allow_overload_ms, key(&args)?)
        }
        Command::Client {
            connect,
            mode,
            forward_max,
            flow_limit,
            timing_every,
            tcp_send_buffer,
            udp_size,
            overload_ms,
            return_max,
        } => client(
            connect,
            Session {
                version: 1,
                flow_limit,
                timing_every,
                tcp_send_buffer,
                udp_size,
                overload_ms,
                mode,
                max: Rates {
                    forward: forward_max,
                    reverse: return_max,
                },
            },
            &key(&args)?,
        ),
    }
}

#[cfg(test)]
mod high_latency_tests {
    use super::*;

    #[test]
    fn setup_covers_serial_connections_without_extending_udp_setup() {
        assert_eq!(join_timeout(Mode::Udp, 16), Duration::from_secs(5));
        assert_eq!(join_timeout(Mode::Tcp, 8), Duration::from_secs(29));
        assert_eq!(join_timeout(Mode::Combined, 16), Duration::from_secs(53));
    }

    #[test]
    fn tcp_stall_budget_allows_ack_progress_and_remains_bounded() {
        assert_eq!(tcp_write_timeout(0), Duration::from_millis(500));
        assert_eq!(tcp_write_timeout(700_000), Duration::from_millis(2100));
        assert_eq!(tcp_write_timeout(u64::MAX), Duration::from_secs(3));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn typed_v1_commands_and_directional_reservations() {
        let mut input = json!({"id":1,"ms":1000,"rates":{"forward":8000,"reverse":0}});
        let w: Window = serde_json::from_value(input.clone()).unwrap();
        let session = Session {
            version: 1,
            mode: Mode::Udp,
            max: w.rates,
            flow_limit: 1,
            timing_every: 0,
            udp_size: 1000,
            tcp_send_buffer: None,
            overload_ms: 0,
        };
        assert!(validate(w, &session, 0).is_ok());
        assert!(validate(w, &session, 1).is_err());
        let mut invalid = w;
        invalid.rates.reverse = 8000;
        assert!(validate(invalid, &session, 0).is_err());
        input["note"] = json!("Café & June");
        assert!(serde_json::from_value::<Window>(input).is_err());
        assert!(serde_json::from_value::<Request>(json!("ordinary note")).is_err());
    }
}
