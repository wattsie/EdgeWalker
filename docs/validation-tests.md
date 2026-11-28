# Validation Tests

EdgeWalker satellite field guide for the interactive prerelease `0.2.2-rc.7`.

Start with a 30-second forward TCP test to verify the route, delivery and shutdown. Then compare one TCP connection with two under the same total offer. UDP calibration is a separate, optional test. All commands run locally from the extracted package directory. Each client run needs a new output directory.

## Before leaving for the site

Take the appropriate release archive and a way to provision the shared key securely. The x86-64 archive contains this guide. Keep its matching checksum if you plan to verify it again onsite. The glibc 2.17 x86-64 archive includes its own Python. Other hosts using the launcher need Python 3.11 or later. The available binaries support Linux x86-64, Linux ARM64 and Apple silicon. Agree on directional caps, duration and a quiet test window with the link owner.

### Intel and AMD Linux field installation

**Older server or automatic startup:** For a server with glibc 2.17 through 2.33, use the [compatibility prerelease](https://github.com/wattsie/EdgeWalker/releases/tag/v0.2.2-rc.7). The archive name ends in `linux-x86_64-glibc2.17.tar.gz`. Follow the bundled [Linux server setup](linux-server-service.md) for verification, key provisioning, and systemd startup. The native server runs without Python. The compatibility archive includes CPython 3.11 for client commands.

Use the same Linux x86-64 package on AMD Ryzen and Intel Core or Xeon machines. Run these checks **on each host**, including the gateway:

```sh
uname -m
getconf GNU_LIBC_VERSION
python3 --version
```

For the standard package and Python launcher below, proceed if the results are `x86_64`, glibc **2.34 or later**, and Python **3.11 or later**. The native engine also uses the system `libgcc_s.so.1`. Kernel version and CPU governor alone cannot confirm these requirements.

For the interactive field test, download the archive and matching checksum into a new directory on an internet-connected machine. Verify the archive on the connected machine before transferring it. For verification on the offline host as well, keep the checksum alongside it. The archive contains the field guide.

```sh
release=https://github.com/wattsie/EdgeWalker/releases/download/v0.2.2-rc.7
archive=edgewalker-0.2.2-rc.7-linux-x86_64.tar.gz
curl -fLO "$release/$archive" &&
  curl -fLO "$release/$archive.sha256" &&
  sha256sum -c "$archive.sha256"
```

**Single-file handoff.** If you already verified the checksum on the connected machine and transferred only the archive using a trusted copy method, run this on the field host:

```sh
archive=edgewalker-0.2.2-rc.7-linux-x86_64.tar.gz
tar -xzf "$archive" && cd "${archive%.tar.gz}" &&
  ./bin/edgewalker-native --version && ./edgewalker --menu --help
```

**With the checksum onsite**, use this alternative from the download directory:

```sh
archive=edgewalker-0.2.2-rc.7-linux-x86_64.tar.gz
sha256sum -c "$archive.sha256" &&
  tar -xzf "$archive" &&
  cd "${archive%.tar.gz}" &&
  ./bin/edgewalker-native --version &&
  ./edgewalker --menu --help
```

Expect the archive checksum to report `OK`, native version `0.2.2-rc.7`, and the menu help text. If any check fails, stop before starting traffic. From the extracted directory, open `docs/validation-tests.md` for the field procedure.

### ARM installations

For ARM hosts, use the matching archive and checksum from the [ARM field-guide prerelease](https://github.com/wattsie/EdgeWalker/releases/tag/v0.2.2-rc.7) and the appropriate block below.

Linux ARM64:

```sh
archive=edgewalker-0.2.2-rc.7-linux-aarch64.tar.gz
sha256sum -c "$archive.sha256" && tar -xzf "$archive" && cd "${archive%.tar.gz}"
```

Apple silicon:

```sh
archive=edgewalker-0.2.2-rc.7-darwin-arm64.tar.gz
shasum -a 256 -c "$archive.sha256" && tar -xzf "$archive" && cd "${archive%.tar.gz}"
```

On ARM hosts, check the installation:

```sh
python3 --version
./bin/edgewalker-native --version
./edgewalker --menu --help
```

Expect native version `0.2.2-rc.7`.

### Shared key on all platforms

For a new installation, generate one key on the gateway. This command refuses to overwrite an existing file.

```sh
(umask 077 && mkdir -p lab && set -C && ./edgewalker keygen --output lab/config.json)
```

Securely provision that same file at `lab/config.json` inside the satellite client's extracted directory. If credentials are already provisioned, use them. On each host run `chmod 600 lab/config.json`. Keep the key file separate from test results.

## Test 1  Verify the satellite path at low load

The terrestrial gateway runs the server. The satellite site runs the client and initiates the connection. **Forward** is download to the satellite site. **Return** is upload to the gateway.

If the gateway service is already active with the specified address, ports and capacity, proceed to the client command.

**Gateway terminal.** Replace the example IP with an address assigned to the gateway's test interface. Permit the satellite site's traffic to TCP control port 62000 and the selected TCP/UDP data ports 62001 through 62008. If the gateway is behind NAT, arrange forwarding for those ports and give the client the reachable address. Keep this terminal open.

```sh
GATEWAY_BIND=192.0.2.10
./edgewalker --server --config lab/config.json \
  --listen "$GATEWAY_BIND:62000" --data-ports 62001-62008 --capacity 1M
```

**Satellite client terminal.** Replace the example with the reachable gateway IP. On Linux run `ip route get "$GATEWAY"`; on macOS run `route -n get "$GATEWAY"`. Check the interface and next hop against the site's routing plan. If a tunnel or alternate WAN is present, confirm traffic on the satellite WAN counters during the test.

```sh
GATEWAY=192.0.2.10
./edgewalker run --config lab/config.json --connect "$GATEWAY:62000" \
  --mode tcp --forward-start 1M --forward-max 1M \
  --flows 1 --flow-limit 1 --window-ms 1000 \
  --duration 30 --output sat-smoke-01
./edgewalker report sat-smoke-01
```

**Proceed when** the client exits successfully, the report shows forward receiver windows and positive received bytes, and `sat-smoke-01/session.json` records deadline termination with `stop_acknowledged` true. Check the report's coverage flag and gaps. A run with no receiver windows has no measured throughput. If setup fails, check the addresses, matching keys, ports, and gateway capacity before increasing the load.

### Optional four-client check

On an approved path, run the first command from four clients at the same time against one gateway control address. Give each process its own output directory. To keep total test load within 1 Mbps, set each client's forward start and maximum to `250k` and keep the server capacity at `1000000`. Allow four free numbers in the server data range. Verify overlapping receiver windows and positive delivery in all four reports. Stop one client and start a replacement while the others continue. If an intervening firewall filters the data range, consult [simultaneous clients and data ports](operator.md#simultaneous-clients-and-data-ports).

## Test 2  Compare one TCP flow with two

The suspected single-flow plateau is around **38 Mbps**. The example requests **60 Mbps total** to allow a measurable gain above 38 Mbps. Use 60 Mbps only if approved, or replace all three occurrences of 60000000 below with the agreed rate in bits per second. A ceiling below the suspected plateau cannot test that hypothesis. A 60 Mbps ceiling also limits the gain visible from two flows.

**Gateway.** Stop the smoke-test server with Ctrl-C, then restart in the same terminal:

```sh
./edgewalker --server --config lab/config.json \
  --listen "$GATEWAY_BIND:62000" --data-ports 62001-62008 --capacity 60000000
```

**Satellite client.** Start one fixed flow with a second socket prepared:

```sh
./edgewalker --menu --config lab/config.json --connect "$GATEWAY:62000" \
  --mode tcp --forward-start 60000000 --forward-max 60000000 \
  --flows 1 --flow-limit 2 --window-ms 1000 \
  --duration 180 --output sat-tcp-01
```

Enter `y` at the Start prompt. Watch one-flow delivery until it settles across several fresh windows. Then enter this line in the running menu and press Enter:

```text
flows forward 2
```

Use `status` to inspect the current settings. Confirm that a subsequent measured row shows two flows. Compare settled rows with the same actual offered load and record how long each phase took to stabilize. Retain startup and transition rows separately. Let the deadline end the run, or enter `stop` to finish early. Ctrl-C interrupts the client during setup or recovery.

```sh
./edgewalker report sat-tcp-01
```

In `series-fl.csv`, compare `offer_bps`, `actual_offer_bps`, `delivered_bps`, `tcp_flows` and retransmissions. The edit time is in `events.csv`. Per-flow counters and available kernel TCP statistics are in `windows.jsonl`. Active-window goodput excludes setup and pauses. To assess whole-run throughput, divide total receiver payload bits by elapsed wall time.

If total delivery rises beyond the single-flow plateau with the same offer, parallel flows helped under those conditions. If both flows share roughly the original total, investigate a shared limit. Record RTT, sender congestion window, advertised receive window if available, retransmissions, and any satellite acceleration or proxy. At 700 ms RTT, 38 Mbps corresponds to about 3.3 million bytes in flight. Throughput alone cannot identify which limit caused the plateau.

## Test 3  Calibrate UDP if needed

Run calibration when you want directional UDP controller settings. The TCP comparison above can run independently. This example allows 400 kbps forward and 200 kbps return for a total wall-clock budget of ten minutes. Either gateway capacity used above can admit it when capacity is available. Confirm the UDP data-port range is reachable.

**Satellite client:**

```sh
./edgewalker calibrate --config lab/config.json --connect "$GATEWAY:62000" \
  --forward-max 400000 --return-max 200000 \
  --duration 600 --output sat-calibration-01
```

Retain `sat-calibration-01/plan.json` and `evidence.json` together. Inspect the setting evidence for each direction. Exit status 2 can indicate insufficient evidence. Unsupported adjustments remain at their provisional values. If desired, exercise the resulting plan in a separate two-minute UDP run:

```sh
./edgewalker run --config lab/config.json --connect "$GATEWAY:62000" \
  --mode udp --plan sat-calibration-01/plan.json --timing-every 0 \
  --duration 120 --output sat-udp-verification-01
./edgewalker report sat-udp-verification-01
```

## Finish and retain evidence

After the last test, stop the foreground gateway server with Ctrl-C. Check that the trial listener has closed (Linux `ss -ltn`, macOS `lsof -nP -iTCP:62000 -sTCP:LISTEN`) and that satellite WAN traffic returns to its prior level. Remove any temporary forwarding or firewall access arranged for the trial.

Keep each complete results directory, the package versions, approved caps, the route used, and the approximate UTC time of any visible outage or flow-count edit. Missing feedback is a coverage gap. Retain failed runs alongside their repeats. This guide and the result files can be read offline.

## High-delay UDP smoke test

At roughly 640 ms RTT, allow 60 seconds for a first bidirectional check. Authentication, admission and readiness count toward the deadline. A shorter run may collect too few settled windows to verify a clean rate. From the client, set GATEWAY to the test-interface address and provision the matching key first.

```sh
./edgewalker run --connect "${GATEWAY}:62000" --config lab/config.json \
  --mode udp --forward-start 1M --forward-max 1M --return-start 1M --return-max 1M \
  --window-ms 5000 --duration 60 --output udp-check
./edgewalker report udp-check
```

Use RC7 at both ends. When a receiver snapshot has an open UDP tail, the peers exchange an authenticated sender count and collect outstanding datagrams for up to one additional second at each receiver. The client allows up to three seconds after the run deadline to collect the outstanding receipt. This exchange sends no further test payload. A missing packet followed by a higher sequence counts as loss even if it arrives later. Silence at the final sequence leaves that suffix pending. An interrupted control channel can also leave coverage incomplete.

In each direction compare active-window delivered rate (`active_delivered_bps`) with `udp_cohort_bps`, the unique UDP bytes observed for those sender windows divided by their combined duration. The series preserves the original cutoff measurement. Check `coverage_complete`, pending ranges, and the last controller reason before treating a zero loss count as a clean result. Neither rate includes setup or inter-window pauses.

The launcher can be symlinked into PATH. The glibc 2.17 x86-64 archive automatically selects its bundled interpreter. To select a separately installed Python 3.11+ interpreter, set `EDGEWALKER_PYTHON=/opt/python311/bin/python3` to its actual path. The native server and key generator run directly without Python. When extracting shared program files under /opt as root, use a public, traversable program directory and a subshell with `umask 022`. Keep keys in a separate private directory. Check journalctl -u edgewalker-server for peer, assigned data port, admission and connection-end events.

For a 1 Mbps bidirectional UDP run with five-second windows, field checks on a roughly 640 ms path took about 3.2 seconds to reach readiness. Two 20-second tests verified both directions. A 60-second test produced eight verified windows. Use the longer run when comparing rates. Compare `udp_cohort_bps` with the UDP offer and retain the cutoff figure for arrival timing. In combined mode, one tenth of the offer goes to the UDP canary. At a 1 Mbps aggregate offer and 1,000 bytes per datagram that is about 12.5 UDP packets per active sending second.

When TCP setup ends before the first window, collect the gateway journal for the matching peer and time. Record the negotiated port, transport, prepared flow count and end reason. All prepared TCP flows in one session use the same destination port. Combined mode also binds UDP at that number. Successful UDP delivery alone does not verify TCP data-port reachability.

## RC7 satellite retest for TCP and combined mode

Use the same RC7 package version at both sites. On a systemd gateway, after replacing the service executable and restarting the service, verify the running process. Check that the configured capacity admits 1 Mbps in each direction and that its control and negotiated data ports are reachable.

```sh
systemctl is-active edgewalker-server
pid=$(systemctl show -p MainPID --value edgewalker-server 2>/dev/null) || pid=$(systemctl show -p MainPID edgewalker-server | cut -d= -f2)
sudo "/proc/$pid/exe" --version
```

On the satellite client, set GATEWAY to the gateway test address. These use your configured key, a 30-second deadline per run, 1 Mbps per direction, and the default automatic flow policy. Run the two tests sequentially to compare them on the same path. Each invocation creates a fresh results directory.

```sh
GATEWAY='replace-with-gateway-address'
edgewalker --version
trial=$(date -u +%Y%m%dT%H%M%SZ)
for mode in tcp combined; do
  out="rc7-${mode}-${trial}"
  edgewalker run --connect "${GATEWAY}:62000" --mode "$mode" \
    --forward-max 1M --return-max 1M --duration 30 --output "$out"
  rc=$?
  printf '%s exit=%s output=%s\n' "$mode" "$rc" "$out"
  if test -f "$out/session.json"; then edgewalker report "$out"; fi
done
```

### One-command field comparison

The archive includes the [field validation scripts](../validation/README.md). With RC7 installed at both ends, verify the gateway can admit 1 Mbps per direction and confirm fresh terminal monitoring. Run the baseline once from a writable client directory. Set the absolute launcher path when the package directory is read-only. Use your existing protected config or set `EDGEWALKER_CONFIG` to its path.

```sh
GATEWAY='your-gateway:62000'
PACKAGE='/absolute/path/to/extracted-package'
export EDGEWALKER_CLI="$PACKAGE/edgewalker"
sh "$PACKAGE/validation/field.sh" baseline "$GATEWAY"
```

Check that the ZIP summary shows one completed session, an acknowledged stop, final TCP coverage complete and zero outstanding records in every flow. Compare the cutoff, through-drain and final confirmed byte counts in the CSV. An incomplete receipt can still confirm a prefix of each TCP stream. Keep the ZIP from an incomplete or interrupted run as evidence.

For an approved higher-rate comparison, run each command separately with a healthy terminal. Each lasts 60 seconds and requests one flow at 1.5 Mbps. Check terminal events and traffic health before starting the other direction.

```sh
sh "$PACKAGE/validation/field.sh" higher-forward "$GATEWAY"
```

```sh
sh "$PACKAGE/validation/field.sh" higher-return "$GATEWAY"
```

For each mode, check for one session, positive TCP goodput in both series files, acknowledged stop, complete coverage and no replacement. In combined mode also check UDP sent, lost and pending counts separately. A clean first attempt matters for this regression, even if a replacement later succeeds. If setup fails, keep the entire run directory and correlate its UTC timestamps with the gateway journal.

```sh
sudo journalctl -u edgewalker-server --since '10 minutes ago' --utc --no-pager
```

### Interpreting a high-RTT TCP run

At roughly 640 ms RTT, use 30 seconds for a startup smoke test. For a rate comparison, start with 60 seconds at the same approved ceiling and one fixed flow (`--flows 1 --flow-limit 1`). Compare successive full five-second windows after startup. Sixty seconds is a proposed observation period for TCP. The field trial verified that duration for UDP only. Keep terminal monitoring active when assessing service impact.

For TCP, RC7 uses a configurable end-to-end closing allowance. Check the chosen budget and actual wait in the final receipt. A deadline gap means that receipt was unavailable. The run's elapsed time alone does not establish when the final payload arrived. Correlate the client events and the gateway journal by time. A gateway EOF establishes that its control peer closed the connection; the client records its own reason.

For archived RC6 runs, the current-window cutoff and per-window drain counters remain available. RC7 additionally provides a session-final receipt. To distinguish a low sender rate from delayed delivery, run the following on an existing client's results directory. It uses recorded TCP application records of 1,000 bytes each. The three rates use the same sender-window duration. The last includes bytes received during the bounded drain, so it is a lower bound if the receipt still lacks records. A missing window has no measurement.

```sh
python3 - "$RUN_DIR/windows.jsonl" <<'PY'
import json, sys
print("session window direction flows offer_kbps written_kbps cutoff_kbps through_drain_kbps")
with open(sys.argv[1]) as records:
    for line in records:
        r = json.loads(line)
        w = r["window"]
        for direction, sender, receiver in (("forward","server","client"), ("reverse","client","server")):
            if not w["rates"][direction] or not r[sender]["tcp"]:
                continue
            tx = sum(f["sent"] for f in r[sender]["tcp"]) * 1000
            rx = [f["received"] for f in r[receiver]["tcp"]]
            cutoff = sum(f["window_bytes"] for f in rx)
            drained = sum(f["bytes"] for f in rx)
            print(r["session"], w["id"], direction, w["flows"][direction],
                  w["rates"][direction]/1000,
                  *(round(b*8/w["ms"], 1) for b in (tx, cutoff, drained)))
PY
```

The offer column includes the canary in combined mode. Use TCP-only recordings for a direct comparison against a 1 Mbps TCP offer.
