# Running EdgeWalker

Use a local package or build the native engine with `cargo build --release --locked` from the repository root. The launcher uses Python 3 and its standard library. The package includes the native engine for the platform in its filename. Run `./edgewalker run --help` for client options and `./edgewalker server --help` for server options.

[Test recipes](#test-recipes) give bounded examples. The [command reference](#command-reference) lists every operator command, native option and live JSON plan field.

## Local measurement

Create a private configuration file on the lab endpoints with the same 12–16 character key. The generator produces 16 random characters. Run both terminals from the same source or extracted package directory. Create a new private directory before generating the key. Supply its path with `--config` or `EDGEWALKER_CONFIG`. The per-user fallback is `$XDG_CONFIG_HOME/edgewalker/config.json` or `~/.config/edgewalker/config.json`. A one-run `--key` overrides the file.

```sh
(umask 077 && mkdir -m 700 lab && ./edgewalker keygen > lab/config.json) &&
  ./edgewalker server --config lab/config.json --listen 127.0.0.1:5207 --capacity 1000000
```

In another terminal using the same configuration, run a 20-second combined-mode measurement. The server sends forward traffic and the client sends return traffic. All rates are application bits per second. Each maximum includes every flow, the UDP canary and any timing replies in that direction.

```sh
./edgewalker run --config lab/config.json --connect 127.0.0.1:5207 \
  --mode combined --forward-start 400000 --forward-max 800000 \
  --return-start 300000 --return-max 600000 --duration 20 --output run-01
./edgewalker report run-01
```

Omit a direction to disable its bulk traffic. Choose `udp`, `tcp` or `combined`. An omitted duration runs until stopped. Ctrl-C or SIGTERM closes the native client and records interruption. A dependent sender monitors the control connection during each window. Automatic recovery reauthenticates, reacquires capacity and restarts at the configured starting rates. Use `--manual-resume` to wait for SIGUSR1 before resuming after a control failure. A stop is terminal. A minimum-policy suspension also accepts SIGUSR1. A fixed run retains its original deadline.

To stop the foreground example server after the clients finish, press Ctrl-C in its terminal. The client retains its output directory for later reporting.

## Capacity and measurement

A single server capacity provisions each directional pool. Use `--tx BPS --rx BPS` to provision them independently. The server reserves both requested maxima under one lock before admitting a bidirectional session. Use `--data-ports FIRST-LAST` to bound data listener ports. The default uses ephemeral ports. Eight concurrent setup/session workers and four workers per source bound load. Failed handshakes rate-limit that source. Control frames are length-bounded and encrypted with a fresh Noise session using the result of a role-bound SPAKE2 exchange.

UDP defaults to 1,000-byte sequenced records (set `--udp-size` to 128–9,000 bytes), one stream per direction. Combined mode allocates one tenth of the bulk budget to UDP. The timing sampler requests a small reply every 32 UDP records by default. Set `--timing-every 0` to disable timing exchanges. A one-direction UDP run reserves a reverse allowance for replies. Periodic measurement windows have a bounded drain interval. The server offsets the return receiver cutoff by the control socket’s smoothed round-trip time, up to two seconds, to allow the window acknowledgement and first return data to traverse the path. The series separates current-offer delivery (`delivered_bps`) from the arrival rate of all data records during the receiver interval (`arrival_bps`), including packets queued under an earlier offer. The raw record also includes bytes received during drain. UDP gaps count once, and a late arrival preserves the original gap. An unresolved trailing range leaves coverage incomplete.

TCP starts with one active connection per direction. The engine prepares up to `--flow-limit` data sockets at admission. The controller activates an additional flow when aggregate goodput stays below the offer and checks the gain. Use `--flows N` for a fixed count, within the configured flow limit. Use `--tcp-send-buffer BYTES` to request a per-socket send buffer during controlled comparisons. The raw record retains each flow's measured bytes, congestion window, retransmission counters and kernel RTT where available. A TCP-only report leaves UDP loss unavailable.

## Controller settings

The default starting offer is the requested directional maximum. Set `--forward-start` or `--return-start` to ramp from a lower rate. For per-direction policies, pass `--plan plan.json`. Each active direction has a JSON object under `forward` or `return`. For example:

```json
{
  "forward": {
    "ceiling_bps": 800000, "start_bps": 300000,
    "target_bps": 600000, "minimum_bps": 100000, "minimum_action": "suspend",
    "ramp_step_bps": 100000, "probe_step_bps": 50000, "probe_ms": 10000,
    "window_ms": 5000, "settle_ms": 0, "min_packets": 3,
    "early_loss_count": 3, "persistent_steps": 2,
    "growth_us": 20000, "observe_ms": 5000, "min_gain_bps": 8000,
    "hold_ms": 8000, "suspend_ms": 15000
  }
}
```

Save the example JSON as `forward-plan.json`. From the client terminal, launch a forward-only test with the plan supplying its ceiling and starting rate:

```sh
./edgewalker run --config lab/config.json --connect 127.0.0.1:5207 \
  --mode udp --plan forward-plan.json --duration 30 --output run-plan
```

The CLI's `--window-ms` sets the common native sampling interval. The per-direction window and packet gates set minimum evidence for a clean decision. Timing-aware control uses successive thirds of receiver transit samples. Absolute one-way delay needs synchronised clocks. Excess delay uses a directional baseline and growth uses within-stream changes. For an open UDP sequence range the controller compares receiver-interval arrival rates across the hold. It can use newly arrived timing samples from the previous window when both windows had the same offer. The series records the timing source and throughput used for that decision. Set `loss_only` to true to use loss-driven UDP decisions. Controller thresholds are provisional.

## Records and verification

Choose a new output directory for every run. `session.json` records settings, software version, status and termination. `windows.jsonl` retains incremental native endpoint receipts. `series-fl.csv` and `series-rl.csv` contain measured and requested rates, decisions, phase, coverage and timing. `events.csv` records state changes, interruption and replacement sessions. UTC and local timestamps accompany window records. A process killed before finalisation can leave a running manifest alongside its completed window records. The report preserves that incomplete status.

Run `make check` for offline and bounded localhost tests. `make package` produces a local archive and checksum under `dist/`.

Set `settle_ms` per direction in the plan to exclude transition measurements from clean-rate qualification. The native sampler rounds settling up to whole samples. A server may explicitly enable a bounded overload interval with `--allow-overload-ms N`. The client separately requests it with `--overload-ms N`. Both use the admitted ceiling.

Use `--interface NAME --telemetry-interval SECONDS` on the client for optional local interface observations. The recorder retains polling cost and available counters in `linkstate.csv`. When the next window resolves outstanding UDP sequences, `udp-corrections.csv` records the revision to the originating window. Reports apply those corrections and identify remaining missing coverage. When fresh sequence evidence confirms new gaps in the immediately preceding window at the same offer, the controller can back off. The series keeps those losses in the originating window and records the triggering count separately. Transport outages and replacements appear in the run timeline. A completed run with gaps carries the status `completed_with_gaps`.

To generate a standalone plot and summary from an existing run, choose a new HTML output path.

```sh
./edgewalker report run-01 --html run-01.html
```

A plan must fit the native packet budget at each admitted ceiling. The launcher reports the maximum permitted window before connecting when the configured sampling or overload interval exceeds it. A deadline with zero receiver measurement windows records a failed run and returns exit 1. A timed client retries an initially unreachable server with bounded backoff. Authentication and configuration errors during initial setup terminate the invocation.

For timing-aware UDP, a low offer can extend the native send interval to collect enough timing samples. Upward probes and a shortfall in receiver throughput can also extend the continuous send interval, up to three base sampling intervals, subject to feedback limits, the native budget and time remaining. This reduces the effect of queue credit accumulated during drains. Reports retain the actual interval. When fewer than six UDP packets fit before the feedback timeout, the controller can combine actual timing samples from consecutive clean windows at the same offer. A missing sequence, a changed offer or a stale report breaks that timing history. Only measured send intervals count toward the observation-time gate. A second unsupported increase returns to the previous clean rate or tests an intermediate offer if receiver throughput supports a smaller gain. The last verified rate stays separate from a rate under test.

## Test recipes

Run each client command from the same directory as `edgewalker`, with the server from [Local measurement](#local-measurement) running in the first terminal. These examples use its 1 Mbps capacity in each direction. Run each example with a new output directory. For a two-host test, put the correct local config on both hosts, bind the server to its data-interface IP and use that IP in `--connect`. The numeric endpoint below stays on loopback.

### Forward UDP and return TCP

The first command starts UDP at 200 kbps and permits it to rise to 800 kbps. Inspect `series-fl.csv` for the client receiver's delivered throughput and the reason for each decision. The second sends TCP from client to server, fixes two active flows and writes the return results to `series-rl.csv`.

```sh
./edgewalker run --config lab/config.json --connect 127.0.0.1:5207 \
  --mode udp --forward-start 200000 --forward-max 800000 \
  --window-ms 1000 --duration 15 --output run-forward-udp
./edgewalker run --config lab/config.json --connect 127.0.0.1:5207 \
  --mode tcp --return-max 800000 --flow-limit 2 --flows 2 \
  --window-ms 1000 --duration 15 --output run-return-tcp
```

For simultaneous traffic, use the combined-mode example in [Local measurement](#local-measurement). Both directional CSVs should contain receiver windows. Check the per-flow TCP records and UDP sequence counts in `windows.jsonl` to distinguish the bulk and timing streams.

### Capacity change and loss recovery on a controlled path

Use two endpoints separated by a controllable test bridge. Keep the client maximum above the highest limiter setting so there is room to probe. Configure the bridge independently on the data path and record the times it acknowledges each change. EdgeWalker's `--forward-max` and the server's `--capacity` set admission and sending limits; **neither configures the bridge**.

Set `TEST_SERVER` to the numeric address and control port announced by the test server, then run:

```sh
./edgewalker run --config lab/config.json --connect "$TEST_SERVER" \
  --mode udp --forward-start 200000 --forward-max 600000 \
  --window-ms 1000 --duration 180 --output run-capacity
./edgewalker report run-capacity --html run-capacity.html
```

- **Capacity test.** Use a 400 kbps limit, decrease it to 320 kbps, then restore 400 kbps during the same run. Allow multiple full receiver windows at each level. Compare actual receiver throughput (`delivered_bps`) with an independent payload-rate reference. Mark the first three consecutive full windows in the chosen tolerance band, examine later probes for oscillation, and check the bridge counters before describing the test as lossless.
- **Loss recovery.** Use a fresh run directory, hold capacity fixed, add e.g. 5% loss on the selected egress, then clear it. Keep the logical client running. Compare sender offer, receiver delivery and confirmed versus pending sequences before, during and after loss. To test expiration and reconnection, keep management reachable, interrupt only the data path, then restore it with enough time left before the original deadline. Compare session numbers, coverage gaps and the first fresh delivered window.
- **Delay only.** In a separate run, hold the rate limit constant, add a known delay, and verify the actual shaper still has that limit after the transition. Compare unloaded and loaded RTT, directional excess delay and receiver throughput. Save the bridge's actual settings alongside the run directory, then restore its baseline.

### Initially offline server and automatic recovery

With the key already created, choose a free loopback port (5208 below). Start this client **before** the server. Leave it running in the first terminal.

```sh
./edgewalker run --config lab/config.json --connect 127.0.0.1:5208 \
  --mode udp --forward-max 400000 --window-ms 1000 \
  --duration 30 --output run-offline
```

Within about five seconds, start the server in a second terminal:

```sh
./edgewalker server --config lab/config.json --listen 127.0.0.1:5208 --capacity 1000000
```

For a candidate with initial-offline recovery, expect the original client to record a gap, authenticate when the server comes online and write fresh receiver windows before its original deadline. Inspect `./edgewalker report run-offline` and `events.csv`. If the server stays offline for the full candidate run, expect a failure with zero receiver windows. The original 0.1.0 preview exited on initial connection refusal; see the version in the run manifest when comparing results. Stop the example server after the client finishes.

### Four-flow forward TCP near 2.5 Gbps

Use a dedicated path that supports the offer. On the server host set `TEST_LISTEN` to its numeric data IP and control port, then launch a separate server with sufficient capacity. On the client set `TEST_SERVER` to the reachable server endpoint. Both commands use the local config for their respective host.

```sh
./edgewalker server --config lab/config.json --listen "$TEST_LISTEN" --capacity 2350000000
```

```sh
./edgewalker run --config lab/config.json --connect "$TEST_SERVER" \
  --mode tcp --forward-max 2350000000 --flow-limit 4 --flows 4 \
  --window-ms 300 --duration 20 --output run-four-flow-tcp
./edgewalker report run-four-flow-tcp --html run-four-flow-tcp.html
```

The **2.35 Gbps offer is shared across all four TCP flows**. The 300 ms sampling window fits the native packet budget. Compare the median delivered rate during active windows with total receiver payload bits divided by the complete elapsed measurement time, including drain and control gaps. For full-run goodput use the run start and finish; for a top-rate stage use its start and finish. State the interval with the reported value. The kernel may retransmit on any flow; inspect the individual raw counters where available.

### Reports and deterministic replay

```sh
./edgewalker report run-forward-udp
./edgewalker report run-return-tcp --html run-return-tcp.html
./edgewalker replay examples/udp-replay.json
./edgewalker replay examples/fixed-limit-plateau.json
```

A report reads an existing run directory and prints JSON, including recorded coverage gaps. A replay reads a synthetic trace and prints one JSON decision per line. You can reproduce either kind of output without sending traffic. The fixed-limit trace models a lossless plateau. The UDP trace includes a sequence gap, a late arrival and loss of feedback.

## Command reference

`./edgewalker --help` lists the five launcher commands. Every launcher command accepts `-h` and `--help`. Place a command's options after its name. Integer rates below are **application bits per second** in decimal units, so 400 kbps is `400000`. Every run needs at least one positive directional maximum.

| Command | Inputs and output |
|---|---|
| `keygen` | Prints a JSON object containing one random 16-character key to stdout. Redirect once to a private file. Its native parser accepts global `--config` and `--key`, but key generation does not read either value. |
| `server` | Starts a foreground native listener and prints its bound control address as JSON. Keep it running for the test. |
| `run` | Authenticates the client, obtains capacity, adjusts the offer from receiver feedback and records results in a new directory. |
| `report PATH [--html FILE]` | `PATH` is the run directory containing `session.json`. Prints reconstructed JSON. `--html` also writes a standalone HTML file at a new path; an existing file causes an error. |
| `replay PATH` | `PATH` is a synthetic replay JSON file containing `session`, `directions` and `events`. Prints JSON lines in decision order. |

### Authentication, listener and server capacity

| Option | Scope and behavior |
|---|---|
| `--config PATH` | On `server` and `run`, selects a JSON file containing a string field named `key`. With no flag, lookup order is `EDGEWALKER_CONFIG`, `$XDG_CONFIG_HOME/edgewalker/config.json` when set, then `~/.config/edgewalker/config.json`. Provision the same lab key at each endpoint. |
| `--key KEY` | On `server` and `run`, overrides config lookup for this invocation. Accepts 12–16 characters. A literal command-line value can appear in process listings or shell history. The examples use a config file. |
| `--listen IP:PORT` | Required for `server`. Numeric socket address on the local host. Use port 0 to select an available port and read the emitted `listening` value. Use brackets around an IPv6 literal, e.g. `[::1]:5207`. |
| `--capacity BPS` | Sets the aggregate capacity of **each** server pool across its concurrent sessions. Supply this or both `--tx` and `--rx`. |
| `--tx BPS`, `--rx BPS` | Set server transmit (forward) and receive (return) pools independently. Both are required together; `--capacity` is mutually exclusive. A reservation that exceeds either available pool is rejected before bulk traffic starts. |
| `--data-ports FIRST-LAST` | Inclusive range 1–65535 for server data listeners, e.g. `5300-5310`. First must be at most last. Omit for OS-allocated ports. The control address comes from `--listen`. |
| `--allow-overload-ms MS` | Largest optional overload duration a client may request at admission. Default 0 disables overload. Both endpoints must opt in; the normal admitted rate ceilings still apply. |

### Client run options

| Option | Scope, defaults and interactions |
|---|---|
| `--connect IP:PORT` | Required numeric server control address, for example `127.0.0.1:5207`. Use the address printed by a server listening on port 0. |
| `--mode MODE` | Required. UDP uses sequenced datagrams; TCP uses one or more connections. Combined assigns approximately 10% of each direction’s bulk rate to the UDP canary and the remainder to TCP. |
| `--output DIR` | Required new run directory. Parent directories may already exist. Existing DIR fails so previous evidence is preserved. |
| `--config PATH`, `--key KEY` | Same credential precedence as the server. |
| `--forward-max BPS`, `--return-max BPS` | Hard ceilings for server→client and client→server respectively. Omission or zero disables bulk in that direction unless a plan enables it. At least one direction must be active. A one-way UDP timing test can still reserve opposite-direction reply capacity. |
| `--forward-start BPS`, `--return-start BPS` | Initial offer for that direction and restart rate after a fresh session or readiness restart. Positive and no greater than the corresponding maximum. Each defaults to that maximum. An explicit start overrides the plan's start. |
| `--duration SECONDS` | Positive wall-clock seconds up to 1 billion, fractional values accepted. Includes authentication, measurement and retries across sessions. Omit to run until an operator stop. |
| `--window-ms MS` | Base native measurement interval, 250–10000 ms, default 5000. Per-direction packet budgets may impose a smaller maximum. Some UDP windows extend to qualify low rates or a new offer, and the final window may shorten to meet the deadline. |
| `--udp-size BYTES` | UDP record size including its measurement header, 128–9000 bytes, default 1000. Choose a size the path MTU supports. The window budget uses the smaller of this value and 1000, including TCP mode. |
| `--timing-every N` | Default 32. Request a UDP timing reply once per N data records. Zero disables these replies. Applies to UDP and combined mode. Timing replies use the directional budget. |
| `--flow-limit N` | Maximum prepared TCP connections for a session and default allowed active count per direction, 1–16, default 4. Per-direction plan settings can override it. The engine provisions the largest active direction's limit. |
| `--flows N` | Fixes the active TCP count per direction, from 1 through the flow limit. When omitted, automatic scaling begins with one flow and tries more when a shortfall persists. A plan can override these flow settings. |
| `--tcp-send-buffer BYTES` | Request a per-socket TCP sender buffer at both endpoints, 8192–4194304 bytes. Omit for the OS default. Effective buffer size depends on the OS. |
| `--overload-ms MS` | Request one bounded ceiling-rate window after clean verification below the ceiling. Default 0 disables it. Nonzero values must be 250–10000 ms, fit the window budget and stay within the server's allowance. The run needs enough time and a qualifying observation to trigger it. |
| `--plan FILE` | Per-direction JSON overrides for the controller. See the fields below. Unknown settings or conflicting directional maxima are errors. |
| `--manual-resume` | After an established connection is lost, or a returning feedback window confirms a suspended direction, wait for `SIGUSR1` to the launcher before restarting. The original deadline remains in force. Default recovery is automatic. A minimum-policy suspension also takes `SIGUSR1`. |
| `--interface NAME` | Enables read-only telemetry for an existing interface on the client. Optional. Linux samples available link speed and drop counters as well as state; macOS collects interface state. This selects telemetry only. Routing follows the control and data socket addresses. |
| `--telemetry-interval SECONDS` | With `--interface`, poll every 0.1–60 s, default 1 s. `linkstate.csv` includes the collection time and available counters. |

The preflight limit in milliseconds is `floor(800000000 × min(udp_size, 1000) / ceiling_bps)`, checked separately for each active direction and for any requested overload. For a 2.35 Gbps ceiling with 1,000-byte records it is 340 ms. The native minimum is 250 ms. At low rates the available interval must also fit a complete UDP record and/or one 1,000-byte TCP record per active flow, after the mode split. If the limits do not overlap, change the offer, active flow count, record size or test interval.

### JSON plan fields

Each top-level key names an active direction (`forward` or `return`). Fields below apply to that direction. The launcher constructs defaults from the command-line base window and directional ceiling/start, then applies the plan. If both sources give a ceiling they must agree. An explicit command-line start wins. Set step sizes explicitly when you set a start only in a plan, since fallback steps are computed before plan overrides.

| Field | Meaning and live default |
|---|---|
| `ceiling_bps`, `start_bps` | Positive integer admission ceiling and starting offer. Supply a ceiling via the plan or matching CLI flag. The default start is the ceiling. |
| `ramp_step_bps`, `probe_step_bps` | Positive offered-rate increments during ramp and upward probing. Defaults are half and one tenth of the initial CLI-derived offer, rounded down, each at least 1 bps. |
| `window_ms`, `min_packets` | Minimum clean observation time and UDP sent-packet count. Positive integers; defaults are the base `--window-ms` and 3 packets. Partial windows can still provide loss evidence. |
| `early_loss_count` | Number of confirmed UDP losses that may trigger early backoff ahead of the clean-window gates. Positive integer, default 3. |
| `settle_ms`, `probe_ms` | Settling time after an offer change, default 0; minimum time before another upward probe becomes eligible, default 10000. Both in ms. Settle can be zero; probe must be positive. |
| `hold_ms`, `suspend_ms` | Deadlines since the expected feedback wait began. Hold marks delivery unknown; suspension requires readiness before traffic restarts. Defaults are the larger of 7000 or base window + 2000, and the larger of 15000 or base window + 5000, respectively. Hold must exceed the base window by more than 500 ms and precede suspend. |
| `growth_us`, `observe_ms` | Sustained delay-growth threshold in microseconds (default 20000) and observation window in milliseconds (default 5000) before assessing delivery gain. Both are positive. |
| `min_gain_bps` | Positive meaningful gain in delivered rate, default 8000 bps. Used in delay assessment and when testing the benefit of another TCP flow. |
| `loss_only` | Boolean, default false. True disables the delay-policy hold and backoff for that direction; the recorder still records available timing evidence. |
| `minimum_bps`, `minimum_action`, `persistent_steps` | Default 0 bps, `"alert"`, and 2 qualifying downward steps. Minimum lies between zero and the start. Persistent loss at or below that threshold records an alert or, with `"suspend"`, pauses the direction. A later manual readiness restart starts at the configured starting offer. |
| `target_bps` | Positive reporting benchmark, default null. Recorded alongside the offer and delivered rate. Sending and admission remain governed by the ceiling. |
| `flow_limit`, `initial_flows`, `fixed_flows` | Per-direction TCP socket limit (default CLI limit), initial count (default 1 or `--flows`), and Boolean fixed-count policy (default true when `--flows` is supplied). Require 1 ≤ initial ≤ limit ≤ 16. |

### Native CLI for harness authors

The normal operator entry point is `./edgewalker run`. The low-level engine lives at `bin/edgewalker-native` in a package or `target/release/edgewalker-native` after a source build. The launcher searches `EDGEWALKER_ENGINE` first, then the packaged engine, source release/debug binaries and finally `edgewalker-native` on `PATH`. Use `-V` or `--version` on the **native** binary to identify it. Root and subcommands support `-h` or `--help`. The native `help [COMMAND]` command prints command-specific help.

Native `server` and `keygen` use the options documented above. Native `client` requires `--connect` and `--mode` and accepts `--config`, `--key`, `--forward-max` and `--return-max` (both default 0; supply at least one), `--flow-limit` (default **1**), `--timing-every` (default **0**), `--udp-size` (default 1000), `--tcp-send-buffer` (OS default) and `--overload-ms` (default 0). These flags have the same units and roles as their launcher counterparts. Native global credentials can appear before or after a subcommand.

After admission the native client emits a JSON readiness record. An external controller writes one JSON window per line with an increasing `id`, `ms`, directional `rates` and `flows`. The native window uses `forward` and `reverse` (return). A JSON string `"ready"` requests confirmation; JSON `null` or EOF ends the session. This interface emits native receipts on stdout; the Python launcher supplies adaptive control, disk recording, report inputs and recovery.

For example, with the local server from the quickstart running, this single 500 ms forward UDP window requests 400 kbps and shuts down cleanly. Run inside an extracted package, or substitute `./target/release/edgewalker-native` in a source checkout.

```sh
printf '%s\n' '{"id":1,"ms":500,"rates":{"forward":400000,"reverse":0},"flows":{"forward":1,"reverse":1}}' null |
  ./bin/edgewalker-native client --config lab/config.json \
    --connect 127.0.0.1:5207 --mode udp --forward-max 400000
```
