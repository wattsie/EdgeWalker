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
    collections::{HashMap, HashSet},
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
    Keygen {
        /// Create a new private configuration file (default: standard output).
        #[arg(long)]
        output: Option<PathBuf>,
    },
    Server {
        #[arg(long, default_value = "127.0.0.1:62000")]
        listen: SocketAddr,
        /// Inclusive data port range, or auto to let the OS select ports.
        #[arg(long, default_value = "62001-62008", value_parser=parse_ports)]
        data_ports: PortSelection,
        #[arg(long, default_value_t = 0)]
        allow_overload_ms: u64,
        #[arg(value_parser = parse_rate, long, conflicts_with_all=["tx","rx"])]
        capacity: Option<u64>,
        #[arg(value_parser = parse_rate, long, requires = "rx")]
        tx: Option<u64>,
        #[arg(value_parser = parse_rate, long, requires = "tx")]
        rx: Option<u64>,
    },
    Client {
        #[arg(long)]
        connect: SocketAddr,
        #[arg(long, value_enum)]
        mode: Mode,
        #[arg(value_parser = parse_rate, long, default_value_t = 0)]
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
        #[arg(value_parser = parse_rate, long, default_value_t = 0)]
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
    DataPortsUnavailable {
        first: u16,
        last: u16,
    },
    OverloadDenied {
        allowed_ms: u64,
    },
    Ready {
        udp: u16,
        tcp: u16,
        token: [u8; 32],
        #[serde(default)]
        resize: bool,
        #[serde(default)]
        reconcile: bool,
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
    data_ports: HashSet<u16>,
    workers: HashMap<IpAddr, usize>,
    failed: HashMap<IpAddr, Instant>,
}
type Shared = Arc<Mutex<Ledger>>;
struct Slot(Shared, IpAddr, Rates, Option<u16>);
impl Drop for Slot {
    fn drop(&mut self) {
        let mut l = self.0.lock().unwrap();
        if let Some(port) = self.3 {
            l.data_ports.remove(&port);
        }
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
            forward: cap.forward - (l.used.forward - self.2.forward),
            reverse: cap.reverse - (l.used.reverse - self.2.reverse),
        };
        if r.forward > available.forward || r.reverse > available.reverse {
            return Err(Admission::Rejected {
                requested: r,
                provisioned: cap,
                available,
            });
        }
        l.used.forward = l.used.forward - self.2.forward + r.forward;
        l.used.reverse = l.used.reverse - self.2.reverse + r.reverse;
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

fn accept_by(listener: &TcpListener, deadline: Instant) -> io::Result<(TcpStream, SocketAddr)> {
    loop {
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "TCP data accept deadline",
            ));
        }
        match listener.accept() {
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(10))
            }
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            result => return result,
        }
    }
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
/// Parse decimal SI prefixes exactly, including values beyond f64 precision.
fn parse_rate(text: &str) -> Result<u64> {
    let text = text.trim();
    let places = match text.as_bytes().last().map(u8::to_ascii_lowercase) {
        Some(b'k') => 3,
        Some(b'm') => 6,
        Some(b'g') => 9,
        _ => 0,
    };
    let number = if places == 0 {
        text
    } else {
        &text[..text.len() - 1]
    };
    let (whole, fraction) = match number.split_once('.') {
        Some((_, "")) => bail!("missing fractional digits"),
        Some(parts) => parts,
        None => (number, ""),
    };
    ensure!(
        !whole.is_empty()
            && whole
                .bytes()
                .chain(fraction.bytes())
                .all(|b| b.is_ascii_digit()),
        "use whole bits/s or decimal k/M/G, e.g. 1k, 1.5M or 1G"
    );
    let fraction = fraction.trim_end_matches('0');
    ensure!(
        fraction.len() <= places,
        "rate must resolve to a whole bit per second"
    );
    let digits = whole
        .bytes()
        .chain(fraction.bytes())
        .chain(std::iter::repeat_n(b'0', places - fraction.len()));
    digits
        .into_iter()
        .try_fold(0u64, |n, b| {
            n.checked_mul(10)?.checked_add(u64::from(b - b'0'))
        })
        .context("rate exceeds the unsigned 64-bit limit")
}

#[derive(Clone, Copy)]
struct PortSelection(Option<(u16, u16)>);

fn parse_ports(text: &str) -> Result<PortSelection> {
    if text == "auto" {
        return Ok(PortSelection(None));
    }
    let (low, high) = text.split_once('-').context("expected FIRST-LAST")?;
    let (low, high) = (low.parse::<u16>()?, high.parse::<u16>()?);
    ensure!(low > 0 && low <= high, "invalid data port range");
    Ok(PortSelection(Some((low, high))))
}
struct DataPorts {
    port: u16,
    udp: Option<UdpSocket>,
    tcp: Option<TcpListener>,
}

impl DataPorts {
    fn bind(ip: IpAddr, port: u16, mode: Mode) -> io::Result<Self> {
        let tcp = if mode != Mode::Udp {
            Some(TcpListener::bind((ip, port))?)
        } else {
            None
        };
        let port = tcp
            .as_ref()
            .map(TcpListener::local_addr)
            .transpose()?
            .map_or(port, |a| a.port());
        let udp = if mode != Mode::Tcp {
            Some(UdpSocket::bind((ip, port))?)
        } else {
            None
        };
        let port = udp
            .as_ref()
            .map(UdpSocket::local_addr)
            .transpose()?
            .map_or(port, |a| a.port());
        Ok(Self { port, udp, tcp })
    }
}

impl Slot {
    fn allocate_ports(
        &mut self,
        control: SocketAddr,
        range: Option<(u16, u16)>,
        mode: Mode,
    ) -> io::Result<Option<DataPorts>> {
        // For an unrestricted range, let the kernel choose a port. Retry if
        // another session owns that number on the other transport.
        let (first, last) = range.unwrap_or((0, 63));
        for candidate in first..=last {
            let port = if range.is_some() { candidate } else { 0 };
            if port == control.port() {
                continue;
            }
            let sockets = match DataPorts::bind(control.ip(), port, mode) {
                Ok(sockets) => sockets,
                Err(e) if e.kind() == io::ErrorKind::AddrInUse => continue,
                Err(e) => return Err(e),
            };
            if sockets.port != control.port()
                && self.0.lock().unwrap().data_ports.insert(sockets.port)
            {
                self.3 = Some(sockets.port);
                return Ok(Some(sockets));
            }
        }
        Ok(None)
    }
}
#[derive(Serialize, Deserialize)]
enum Request {
    Window(Window, u64),
    Reconcile { window: u32, sent: u64 },
    Resize(Rates),
    Ready(u32),
    Stop,
}
fn reserved_rates(plan: &Session, rates: Rates) -> Rates {
    let mut reservation = rates;
    if reservation.forward == 0 {
        reservation.forward =
            engine::reply_rate(rates.reverse, plan.mode, plan.timing_every, plan.udp_size)
    }
    if reservation.reverse == 0 {
        reservation.reverse =
            engine::reply_rate(rates.forward, plan.mode, plan.timing_every, plan.udp_size)
    }
    reservation
}
#[derive(Serialize, Deserialize)]
enum ResizeResult {
    Accepted,
    Rejected(Admission),
}

fn serve_session(
    s: TcpStream,
    key: &str,
    cap: Rates,
    ports: Option<(u16, u16)>,
    allow_overload_ms: u64,
    mut slot: Slot,
) -> Result<&'static str> {
    let local = s.local_addr()?;
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
    let mut plan: Session = c.recv()?;
    validate_session(&plan)?;
    if plan.overload_ms > allow_overload_ms {
        eprintln!("session rejected peer={peer} reason=overload");
        c.send(&Admission::OverloadDenied {
            allowed_ms: allow_overload_ms,
        })?;
        return Ok("overload_rejected");
    }
    let mut remaining_overload = plan.overload_ms;
    let mut reservation = reserved_rates(&plan, plan.max);
    if let Err(rejection) = slot.reserve(cap, reservation) {
        eprintln!("session rejected peer={peer} reason=capacity");
        c.send(&rejection)?;
        return Ok("capacity_rejected");
    }
    let Some(DataPorts {
        port,
        udp,
        tcp: listener,
    }) = slot.allocate_ports(local, ports, plan.mode)?
    else {
        let (first, last) = ports.unwrap_or((0, 0));
        eprintln!("session rejected peer={peer} reason=data_ports");
        c.send(&Admission::DataPortsUnavailable { first, last })?;
        return Ok("ports_exhausted");
    };
    eprintln!(
        "session admitted peer={peer} mode={:?} tcp_port={:?} udp_port={:?} forward_max_bps={} return_max_bps={} prepared_flows={}",
        plan.mode,listener.as_ref().map(|_|port),udp.as_ref().map(|_|port),plan.max.forward,plan.max.reverse,plan.flow_limit
    );
    let token = random()?;
    c.send(&Admission::Ready {
        udp: if udp.is_some() { port } else { 0 },
        tcp: if listener.is_some() { port } else { 0 },
        token,
        resize: true,
        reconcile: true,
    })?;
    // The client opens its admitted sockets in order before acknowledging the join.
    c.0.set_read_timeout(Some(join_timeout(plan.mode, plan.flow_limit)))?;
    ensure!(c.recv::<String>()? == "joined", "missing data setup");
    c.0.set_read_timeout(Some(Duration::from_secs(5)))?;
    let mut tcp = Vec::new();
    if let Some(listener) = &listener {
        listener.set_nonblocking(true)?;
        let deadline = Instant::now() + Duration::from_secs(5);
        for _ in 0..plan.flow_limit {
            let (mut flow, addr) = accept_by(listener, deadline).context("tcp_accept")?;
            ensure!(addr.ip() == peer, "unexpected data peer");
            flow.set_read_timeout(Some(
                deadline
                    .checked_duration_since(Instant::now())
                    .context("tcp_join_timeout")?,
            ))?;
            let mut proof = [0; 32];
            flow.read_exact(&mut proof).context("tcp_token_read")?;
            ensure!(proof == token, "invalid data token");
            io_settings(&flow, plan.tcp_send_buffer)?;
            tcp.push(engine::Flow::new(flow));
        }
    }
    if let Some(udp) = &udp {
        udp.set_read_timeout(Some(Duration::from_secs(3)))?;
        let mut buf = [0; 33];
        let (n, addr) = udp.recv_from(&mut buf).context("udp_join_receive")?;
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
    let mut reconcile_limit = None;
    loop {
        let (w, peer_base) = match c.recv::<Request>()? {
            Request::Window(w, base) => (w, base),
            Request::Reconcile { window, sent } => {
                let limit = reconcile_limit.take().context("no open UDP window")?;
                ensure!(
                    window == prev && sent <= limit,
                    "invalid UDP close boundary"
                );
                let correction = engine::drain_tail(
                    udp.as_ref().context("UDP unavailable")?,
                    previous_udp.as_mut().context("no UDP receiver")?,
                    sent,
                    plan.udp_size,
                )?;
                c.send(&correction)?;
                continue;
            }
            Request::Ready(id) => {
                ensure!(id == prev, "wrong readiness generation");
                c.send(&id)?;
                continue;
            }
            Request::Resize(rates) => {
                ensure!(
                    (rates.forward == 0) == (plan.max.forward == 0)
                        && (rates.reverse == 0) == (plan.max.reverse == 0),
                    "resize must preserve directions"
                );
                let wanted = reserved_rates(&plan, rates);
                let result = match slot.reserve(cap, wanted) {
                    Ok(()) => {
                        plan.max = rates;
                        reservation = wanted;
                        ResizeResult::Accepted
                    }
                    Err(reason) => ResizeResult::Rejected(reason),
                };
                c.send(&result)?;
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
            udp.as_ref(),
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
        reconcile_limit = (plan.mode != Mode::Tcp)
            .then(|| engine::udp_count_limit(w.rates.reverse, plan.mode, w.ms, plan.udp_size));
        base += result.udp.sent;
        c.send(&result)?;
    }
    drop(tcp);
    drop(udp);
    drop(listener);
    drop(slot);
    c.send(&"stopped")?;
    Ok("stopped")
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
        let slot = Slot(ledger.clone(), peer, Rates::default(), None);
        let key = key.clone();
        thread::spawn(move || {
            let started = Instant::now();
            let outcome = serve_session(s, &key, cap, ports, allow_overload_ms, slot);
            let reason = match &outcome {
                Ok(reason) => *reason,
                Err(e) => match e.to_string().as_str() {
                    "tcp_accept" => "tcp_accept",
                    "tcp_token_read" => "tcp_token_read",
                    "unexpected data peer" => "tcp_peer_mismatch",
                    "invalid data token" => "tcp_token_mismatch",
                    "udp_join_receive" => "udp_join_receive",
                    "invalid UDP join" => "udp_peer_or_token_mismatch",
                    "UDP reachability failed" => "udp_challenge_mismatch",
                    "peer closed control" => "control_closed",
                    "authentication failed" => "authentication_failed",
                    _ => "connection_error",
                },
            };
            eprintln!(
                "connection ended peer={peer} orderly={} duration_ms={} reason={reason}",
                outcome.is_ok(),
                started.elapsed().as_millis()
            );
        });
    }
    Ok(())
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResizeInput {
    resize: Rates,
}
fn client(addr: SocketAddr, mut plan: Session, key: &str) -> Result<()> {
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
    let (up, tp, token, resize, reconcile) = match c.recv::<Admission>()? {
        Admission::Ready {
            udp,
            tcp,
            token,
            resize,
            reconcile,
        } => (udp, tcp, token, resize, reconcile),
        r => bail!("admission rejected: {}", serde_json::to_string(&r)?),
    };
    let (mut tcp, udp) = (|| -> Result<_> {
        let mut tcp = Vec::new();
        if plan.mode != Mode::Udp {
            for _ in 0..plan.flow_limit {
                let mut flow = TcpStream::connect_timeout(
                    &SocketAddr::new(addr.ip(), tp),
                    DATA_CONNECT_TIMEOUT,
                )?;
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
            let length = udp.recv(&mut challenge)?;
            ensure!(length == 32, "invalid challenge");
            c.send(&challenge[..32])?;
            udp.set_read_timeout(Some(Duration::from_millis(30)))?;
        }
        ensure!(c.recv::<String>()? == "ready", "receiver unavailable");
        Ok((tcp, udp))
    })()
    .inspect_err(|error| {
        use io::ErrorKind::*;
        if error.downcast_ref::<io::Error>().is_some_and(|e| {
            matches!(
                e.kind(),
                WouldBlock
                    | TimedOut
                    | UnexpectedEof
                    | ConnectionReset
                    | BrokenPipe
                    | ConnectionAborted
                    | ConnectionRefused
            )
        }) {
            println!("{}", json!({"status":"data_setup_timeout"}));
        }
    })?;
    c.0.set_read_timeout(Some(Duration::from_secs(15)))?;
    println!(
        "{}",
        json!({"status":"ready","version":1,"resize":resize,"tcp_port":tp,"udp_port":up})
    );
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
        if let Ok(edit) = serde_json::from_str::<ResizeInput>(&line) {
            ensure!(resize, "gateway does not support resize");
            c.send(&Request::Resize(edit.resize))?;
            let result: ResizeResult = c.recv()?;
            if matches!(result, ResizeResult::Accepted) {
                plan.max = edit.resize;
            }
            println!("{}", json!({"resize":result}));
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
        let mut local = engine::measure(
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
        let mut remote: Endpoint = c.recv()?;
        remote.validate_feedback(&local, w, plan.mode, plan.udp_size, peer_base)?;
        if reconcile
            && plan.mode != Mode::Tcp
            && (remote.udp.received.highest < local.udp.sent
                || local.udp.received.highest < remote.udp.sent)
        {
            c.send(&Request::Reconcile {
                window: w.id,
                sent: local.udp.sent,
            })?;
            local.closed_udp = Some(engine::drain_tail(
                &udp,
                previous_udp.as_mut().context("no UDP receiver")?,
                remote.udp.sent,
                plan.udp_size,
            )?);
            let correction: history::Correction = c.recv()?;
            ensure!(
                correction.window == w.id
                    && correction.sent == local.udp.sent
                    && correction.pending <= correction.sent - remote.udp.received.highest
                    && correction.lost >= remote.udp.received.skipped
                    && correction.lost <= correction.sent - correction.pending
                    && correction.reordered <= correction.lost,
                "invalid final UDP coverage"
            );
            remote.closed_udp = Some(correction);
        }
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
        Command::Keygen { output } => {
            ensure!(
                args.key.is_none() && args.config.is_none(),
                "keygen takes --output for a new key file"
            );
            let alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
            let code: String = random()?[..16]
                .iter()
                .map(|b| alphabet[(b & 63) as usize] as char)
                .collect();
            let config = json!({"key":code});
            if let Some(path) = output {
                use std::os::unix::fs::OpenOptionsExt;
                let mut file = fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(path)?;
                writeln!(file, "{config}")?;
            } else {
                println!("{config}");
            }
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
            server(listen, cap, data_ports.0, allow_overload_ms, key(&args)?)
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
    fn data_handshake_can_finish_after_control_join() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let addr = listener.local_addr().unwrap();
        let worker = thread::spawn(move || {
            thread::sleep(Duration::from_millis(320));
            TcpStream::connect(addr).unwrap()
        });
        let (_, peer) = accept_by(&listener, Instant::now() + Duration::from_secs(3)).unwrap();
        assert_eq!(peer, worker.join().unwrap().local_addr().unwrap());
        assert_eq!(
            accept_by(&listener, Instant::now()).unwrap_err().kind(),
            io::ErrorKind::TimedOut
        );
    }

    #[test]
    fn readiness_waits_for_last_authenticated_flow_in_tcp_and_combined() -> Result<()> {
        for mode in [Mode::Tcp, Mode::Combined] {
            let control = TcpListener::bind("127.0.0.1:0")?;
            let addr = control.local_addr()?;
            let key = "test-only-fixture";
            let rates = Rates {
                forward: 1000000,
                reverse: 1000000,
            };
            let server = thread::spawn(move || -> Result<_> {
                let (socket, peer) = control.accept()?;
                let shared = Shared::default();
                shared.lock().unwrap().workers.insert(peer.ip(), 1);
                serve_session(
                    socket,
                    key,
                    rates,
                    None,
                    0,
                    Slot(shared, peer.ip(), Rates::default(), None),
                )
            });
            let mut channel = Control::authenticate(TcpStream::connect(addr)?, key, true)?;
            channel.send(&Session {
                version: 1,
                mode,
                max: rates,
                flow_limit: 2,
                timing_every: 0,
                tcp_send_buffer: None,
                udp_size: 1000,
                overload_ms: 0,
            })?;
            let Admission::Ready {
                tcp, udp, token, ..
            } = channel.recv()?
            else {
                bail!("setup rejected")
            };
            let mut first = TcpStream::connect((addr.ip(), tcp))?;
            first.write_all(&token)?;
            let datagrams = UdpSocket::bind("127.0.0.1:0")?;
            if mode == Mode::Combined {
                datagrams.connect((addr.ip(), udp))?;
                datagrams.set_read_timeout(Some(Duration::from_secs(3)))?;
                datagrams.send(&token)?;
            }
            channel.send(&"joined")?;
            thread::sleep(Duration::from_millis(320));
            let mut last = TcpStream::connect((addr.ip(), tcp))?;
            last.write_all(&token)?;
            if mode == Mode::Combined {
                let mut challenge = [0; 32];
                ensure!(datagrams.recv(&mut challenge)? == 32);
                channel.send(&challenge)?;
            }
            ensure!(channel.recv::<String>()? == "ready");
            channel.send(&Request::Stop)?;
            ensure!(channel.recv::<String>()? == "stopped");
            ensure!(server.join().unwrap()? == "stopped");
        }
        Ok(())
    }

    #[test]
    fn rate_units_are_exact_and_bounded() {
        for (text, expected) in [
            ("0", 0),
            ("1000000", 1000000),
            ("1k", 1000),
            ("1.5m", 1500000),
            ("1G", 1000000000),
            ("0.000000001g", 1),
            ("18446744073.709551615G", u64::MAX),
        ] {
            assert_eq!(parse_rate(text).unwrap(), expected);
        }
        for text in [
            "1MB",
            "0.0001k",
            "1e6",
            "NaN",
            "-1",
            "1.2.3M",
            "18446744073.709551616G",
        ] {
            assert!(parse_rate(text).is_err());
        }
    }

    #[test]
    fn resize_preserves_other_sessions_and_rolls_back_both_pools_on_rejection() {
        let ip = IpAddr::from([127, 0, 0, 1]);
        let shared = Shared::default();
        shared.lock().unwrap().workers.insert(ip, 2);
        let mut first = Slot(shared.clone(), ip, Rates::default(), None);
        let mut second = Slot(shared.clone(), ip, Rates::default(), None);
        let rates = |forward, reverse| Rates { forward, reverse };
        let cap = rates(100, 90);
        assert!(first.reserve(cap, rates(60, 30)).is_ok());
        assert!(second.reserve(cap, rates(20, 45)).is_ok());
        assert!(first.reserve(cap, rates(70, 50)).is_err());
        assert_eq!(shared.lock().unwrap().used.forward, 80);
        assert_eq!(shared.lock().unwrap().used.reverse, 75);
        assert!(first.reserve(cap, rates(40, 15)).is_ok());
        assert!(second.reserve(cap, rates(60, 75)).is_ok());
        drop(first);
        drop(second);
        let ledger = shared.lock().unwrap();
        assert_eq!((ledger.used.forward, ledger.used.reverse), (0, 0));
    }

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
