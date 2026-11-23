# Edgewalker application design brief

Edgewalker measures how much traffic a link can sustain as conditions change. It raises the sending rate when the receiver confirms clean delivery, backs off during impairment and tests for recovery throughout the session.

# Agreed design

## Client and server

The client starts a session with a remote server and authenticates before test traffic begins. Both launchers read the lab key from a local configuration file. An optional `--key` overrides the file’s key for that run. The client and server each need the same key to connect. A missing key stops startup with a clear error. Accept operator-supplied codes of 12 to 16 characters based on length alone. The optional generator defaults to 16 random characters. Use a password-authenticated key exchange and rate-limit failed handshakes. Both endpoints use the code to authenticate and establish a fresh session key automatically. Session setup uses an encrypted control channel and requires proof of possession of the shared key at both ends. The key stays local and operators need no certificates. This authenticates lab membership; any holder can act as either endpoint. A session can request forward, return or bidirectional traffic. Each active direction has its own sending rate, receiver feedback and limits.

## Configuration location

Both launchers support `--config PATH` for a single run. Otherwise they use the default file path configured in the launch script. The proposed per-user fallback is `$XDG_CONFIG_HOME/edgewalker/config.json` when that variable is set, or `~/.config/edgewalker/config.json` otherwise. An installation under `/usr/local` or `/opt` can point its launcher at a deployment-specific file. Startup needs only read access to that file and does not write to `/etc`. Restrict read access to the account that runs the process when the file contains the lab key.

## Rates set at launch

Operators and scripts configure each direction through the client command line. The maximum is a hard ceiling for all traffic, including probes. The optional target is a reporting benchmark. The minimum is the rate at which persistent loss triggers the selected action: alert and continue below the threshold, or suspend test traffic.

The controller uses receiver feedback to determine the fallback rate. It can settle below the reporting target and continue probing for recovery.

## Backoff and recovery

Ramp up quickly from the starting rate while fresh receiver measurements show clean delivery. Record packet loss, latency and jitter as the rate changes. Count a skipped UDP sequence immediately as loss for control and reporting. A later arrival cannot clear that loss. Apply the normal backoff assessment to an isolated gap. Fresh receiver evidence of sustained loss can trigger prompt backoff before the clean-rate window ends. By default, sustained rising delay after a rate increase pauses further increases for a configurable observation window. Hold the offered rate and compare fresh delay samples with receiver throughput. Resume probing when delay settles and clean-rate criteria are met. If delay continues rising while delivered throughput stops improving, step back to the last verified clean rate and revalidate it. In loss-only mode, record delay without using it to hold or reduce the rate. Keep the offered rate, delivered rate and most recent verified clean rate distinct. On a lossless plateau, if repeated increases fail to improve receiver throughput and delay stays flat, hold the lowest verified sending rate that sustained the best measured throughput. Continue periodic upward probes.

Hold the clean rate and probe upward periodically. After a failed probe, return to the most recent clean rate and verify it again. After a successful probe, retain the gain. Continue probing so a link that recovers after a minute or two can return to its ceiling automatically.
Flag persistent loss when fresh receiver measurements show that loss has failed to improve across a configurable number of consecutive downward rate steps in that direction. Record the rates and loss measurements and leave the cause open. Continue stepping down until delivery is clean or the configured minimum is reached. If loss persists at the minimum, apply the action selected at launch.

## Evidence for a clean rate

For sequenced UDP measurements, begin the clean-rate window at the first sequence assigned to measurement after settling. The sender communicates this boundary so a missing first packet still counts. Declare a rate clean only when all expected packets are received in order within each stream and both a configurable observation time and a minimum packet count have been reached. Missing or stale feedback leaves the current rate unverified. Five seconds is the initial minimum observation time to evaluate in replay and emulation. If that time elapses before the minimum packet count is reached, continue observing at the current rate and defer upward probes. Sustained loss may still trigger early backoff, and feedback timeouts still apply.

## Packet loss by phase

Operators and scripts can configure the starting rate, ramp and probe step sizes, probe interval, settling period, measurement window and minimum packet count per direction. Record the effective settings with each session.

The sender marks phase boundaries by packet sequence. Attribute late and reordered packets to their original sending phase. For UDP, including the UDP probe in combined mode, report steady-rate packet loss as the headline percentage: sum the distinct sequence numbers lost under the in-order delivery rule in all steady measurement windows and divide by the packets sent in those windows. Include failed windows. Report ramp and probe loss separately, including their settling periods, and report whole-session loss across all packets. Loss during a transition still informs backoff. In TCP-only runs report goodput and retransmissions; a UDP packet-loss percentage is unavailable. If missing receiver records prevent a complete total, label the result incomplete and report the coverage gap.

## Session duration and automatic recovery

The client accepts a fixed duration or an open-ended run. A timed run ends at its deadline, including time spent suspended. An open-ended run continues until stopped. On an operator interrupt or other catchable termination signal, stop test traffic, notify the peer, close the session’s data and control connections, finalise available results and exit the client. If the client is killed before it can notify the peer, the remote sender independently suspends test traffic at its feedback timeout and closes abandoned session connections when the session expires.

Each direction has two configurable feedback timeouts. At the first, hold the sending rate, pause upward probes and mark delivery unknown. At the second, suspend test traffic and retain a low-rate control heartbeat. After feedback-timeout suspension, resume automatically at the configured starting rate once the authenticated peer confirms the same session is active and the receiver is ready. A fresh readiness exchange must work while test traffic is suspended. Confirm delivery from new receiver measurements after restart. Scripts may require manual resumption. A stopped or expired session cannot resume. Once the server detects a closed control connection or declares the session expired, stop its data flows and release its capacity. In automatic mode a still-running client keeps retrying with bounded backoff until stopped or its original deadline passes. Each replacement session requires fresh authentication, capacity admission and receiver readiness before restarting at the configured starting rate. Correlate replacement sessions with the same run and report the interruption as a coverage gap.

# Architecture and measurement

Edgewalker runs as a standalone client and server on operator-controlled hosts. Forward means server to client and return means client to server. A client-side controller for each direction shares the session clock and event log. The orchestrator enforces the plan, a native engine at each endpoint sends and measures traffic, optional telemetry plugins contribute observations, and the recorder writes results. Expose command-line and CSV or JSON interfaces for integration.

## Traffic and feedback

UDP uses paced streams, sequence numbers and receiver timestamps. Count delivered bytes per measurement interval, track late, reordered and duplicate packets, and record socket overruns, sender errors, CPU saturation and negotiated interface speed separately. Benchmark packet size and pacing accuracy across the supported rates. TCP measures receiver goodput per connection and in aggregate. For each active TCP direction, default to automatic connection scaling, starting with one connection. Operators can override the initial count, set a flow limit or request a fixed count. Use aggregate receiver goodput to judge progress toward the session’s target and to track the edge within the admitted ceiling. Record retransmissions and congestion-window measurements where available. Combined mode adds a low-rate UDP probe alongside bulk TCP so delay and loss can be correlated with goodput. Keep TCP retransmissions and UDP loss as distinct metrics.

Use a versioned control connection per session for capabilities, negotiated parameters and ports, sequence-range feedback, reason-coded rate changes, events, heartbeats and stop. Keep the feedback interval configurable. Multiple feedback reports can contribute to a single clean-rate observation window. Severe loss can trigger backoff before that window closes. Delay trends can use changes in one-way delay; absolute one-way delay needs verified clock synchronisation. Coordinate or alternate probes when both directions are loaded so each direction’s effect on the other can be examined.

## Link events and telemetry

Compare delivered rate, loss and delay with optional telemetry such as interface state, host counters and reachability. Record the time, duration, confidence and supporting signals for each event. An unexplained event stays unclassified. Freeze the edge estimate during confirmed outages and revalidate after recovery. Track freshness so an earlier clean rate cannot silently become the current estimate.

Telemetry adapters are optional and read-only. Make their polling interval configurable and measure collection overhead under load. The controller must operate with receiver feedback alone.

## Operations and records

Run the server unprivileged with bounded memory, disk, sessions, port ranges and per-source load. The server owner configures two aggregate test capacities: server transmit for forward traffic and server receive for return traffic. Each pool covers every active test in that direction. A single supplied capacity sets both directional limits to that value. Two explicitly labelled values set transmit and receive independently. Neither client nor server assumes a fixed bandwidth default. At session admission, reserve the requested maximum in each requested direction atomically across concurrent requests. A bidirectional test starts only if both reservations fit. On rejection, neither pool is charged. If the request exceeds the provisioned capacity or the remaining unreserved capacity, reject it before starting traffic and report the requested, provisioned and currently available rates. Release reservations when sessions terminate or expire. Rotate logs, bound the retry frequency and prevent duplicate runs from competing for ports. Overload testing may exceed the estimated clean rate only within the effective hard ceiling for that direction. It requires explicit server and operator opt-in and a time limit. Package for Linux and macOS first, with Windows later. Agree on host ownership, patching and shared-link duty cycle before field trials.

Write a session manifest, one per-window series per direction, controller and link events, and original telemetry samples. Include versions, effective limits, sequence coverage, offered and delivered rates, loss, delay, jitter, controller state, reason and current edge estimate. Preserve interrupted runs for reconstruction. Show UTC with local time alongside. Reports should overlay the edge with link state, distinguish outages and missing coverage, and show both raw and outage-excluded figures alongside the agreed phase accounting.

# Delivery and acceptance

The first release supports UDP, TCP and the combined TCP-plus-UDP mode. Each mode must operate forward only, return only and simultaneously in both directions. Develop a deterministic, replayable controller. Evaluate OB-UDPST and iperf3 as independent comparison tools. Use emulation to test controller decisions against known changes in capacity. Use Rust for the network-facing server and native client and server packet engines. Prefer safe Rust for parsing, buffer management and concurrency. Isolate and review any necessary unsafe platform bindings. Use Python for orchestration, deterministic replay and reporting. Validate the selected engine’s pacing and accounting on supported platforms. The brief defines the release requirements. During implementation, use the validation scenarios below to select initial configurable defaults and record the tested values and results before release.

Test loopback pacing across the supported rate range. Emulate varying latency, deep queues, random and rate-dependent loss, outages and capacity changes. Test each direction independently and then concurrently. Exercise control loss, server exit, link outage and disk exhaustion.

Proposed acceptance targets are to estimate a known sustainable rate within 5% in under ten minutes, track a capacity change within 10% in under two minutes, and limit oscillation to one probe step after convergence. In labelled impairment scenarios, aim to classify at least 90% of link events correctly. Verify cap and stop enforcement, control-loss recovery, bounded resource use and reproducible session reports.

## Calibration across GEO LEO and terrestrial paths

Calibrate against measured path behaviour in each direction. Use GEO, LEO and terrestrial conditions as validation profiles. Operators can override the resulting defaults. The metrics below are candidates for calibration; production thresholds require replay and emulation.

For rate changes, compare offered rate, receiver-delivered rate, per-direction delay variation and sequence errors over the same qualified interval. Establish a low-load RTT baseline and report excess loaded delay in milliseconds and as a percentage of that baseline. Pair sustained delay growth with receiver throughput flattening before delay-triggered backoff. RTT includes the reverse path; use directional timing samples when attributing congestion. [Broadband Forum TR-471](https://www.broadband-forum.org/pdfs/tr-471-4-0-0.pdf) · [RFC 9097 capacity measurement](https://datatracker.ietf.org/doc/html/rfc9097) · [RFC 6349 TCP throughput metrics](https://datatracker.ietf.org/doc/html/rfc6349)

For feedback timers, measure report cadence, smoothed round-trip time and its variation. Evaluate hold and suspend timers that allow for report generation, path delay and variability. Compare against fixed timers using false suspensions and time to detect a genuine outage. Calibrate silent-peer expiry separately from transient feedback suspension. Release the reservation on confirmed closure or expiry. QUIC’s probe timer is a precedent for adaptive timing. Edgewalker feedback is periodic rather than a QUIC acknowledgement. [RFC 9002 RTT estimation and probe timeout](https://www.rfc-editor.org/rfc/rfc9002.html#section-6.2.1)

For UDP, retain zero observed in-order losses as the clean criterion. Report sample size, duration, loss fraction and longest consecutive loss burst. As an initial packet-count benchmark, about 3,000 independent packets with zero observed losses yield a one-sided 95% upper loss bound near 0.1%. Bursty losses violate independence. Calibrate severe loss and persistent-loss decisions against burst traces and multiple fresh windows. For TCP, compare aggregate goodput with the requested rate and check bandwidth-delay product and available flow windows before interpreting a plateau as the path limit.

Exercise long stable RTT with deep queues for GEO, changing base RTT and transient disruptions for LEO, and short RTT with both shallow and congested queues for terrestrial paths. Vary delay and capacity independently. Include a reverse-path-only disturbance and a delay step with unchanged capacity. Score convergence and recovery time, error against known capacity, false backoffs and false suspensions by profile. A LEO measurement study observed periodic variation on one network; its period is not a universal timer. [LEO end-to-end latency measurements](https://www.nature.com/articles/s44459-026-00044-z)

# Calibration work for implementation

1. UDP loss calibration. Strict in-order delivery, minimum observation time, a packet-count gate and early backoff on sustained loss are agreed. Tune the minimum sample count and sustained-loss trigger with controlled loss and reordering.

2. Rate and delay calibration. Rapid ramp, smaller probes, delay-triggered holds and throughput-aware backoff are agreed. Tune step sizes, timing and plateau thresholds. Use the operator’s lossless rate limiter as a known-capacity scenario: vary the cap and record offered rate, delivered rate, excess delay, convergence time and recovery. Calibrate the minimum meaningful throughput gain and verify the lossless plateau hold-and-probe rule.

3. Feedback calibration. Tune hold, suspend and dead-peer detection timers against delay and report variability. Verify fresh setup after an outage of around two minutes, including a case where another test has claimed the released capacity.

4. Persistent loss calibration. Flag loss that fails to improve over a configurable number of measured downward steps, then apply the minimum-rate policy. Tune the default number of steps and the minimum improvement distinguishable from measurement noise. Use controlled loss for this test.

# Technical appendix

## Components and responsibilities

The CLI reads the session plan and configuration, negotiates limits and controls the session lifecycle. A controller on the client manages each direction. For forward traffic the client commands the remote sender. For return traffic it adjusts the local sender. Both endpoints run the data engine and record local resource limits. The recorder correlates both directions on one session timeline. Optional telemetry adapters can be disconnected without disabling the core test.

## Control protocol

Use a versioned, framed control connection. The proposed message sequence is `hello` for version and capabilities, `session` for directions and requested limits, then `ready` for accepted parameters and ports. Authenticate before accepting a test. Reject an unsupported or over-capacity request before starting either sender. Use an existing, maintained implementation of the agreed password-authenticated key exchange with the shared lab key. Library selection, failed-handshake rate limits and the key rotation procedure belong to implementation and deployment planning.

During a run, `feedback` identifies a direction, observation interval, packet sequence range, receiver timestamp, packet and byte counts, missing packets, reorder, duplicates, jitter and delay trend. A `rate` command carries a direction, target and reason. The sender reports each stream’s effective sequence boundaries for the transition and subsequent measurement phase, plus its sent count and final sequence at window close. This lets the receiver account for missing leading or trailing packets. `event`, `keepalive` and `stop` communicate state changes, liveness and orderly termination. A configurable port range supports one UDP port per stream. A heartbeat or reconnect must never resurrect a stopped or expired session.

## Data engine

The proposed UDP header carries a protocol marker and version, stream ID, direction, 64-bit sequence number, send timestamp and payload length. Configure the datagram size to suit the path MTU. Allocate the admitted directional rate across all active streams, including any canary. Pace each UDP stream within its allocation, using a token bucket or equivalent and native batched sends where supported. Benchmark timer precision, CPU use and pacing error on each platform.

Track sequence order independently per stream and direction across measurement windows. When a higher sequence arrives, mark any skipped sequences lost and advance past the higher sequence. If a skipped packet arrives later, record it as reordered while retaining its original loss exactly once. Duplicates do not add loss. Attribute each loss to the sender’s phase for that sequence. Track raw received bytes separately from in-order accepted bytes. Compute delivered rate from receiver bytes over the measurement interval. Monitor socket overruns, send errors, host CPU and interface limits at both ends. Preserve local-drop evidence separately so that endpoint saturation cannot silently become a reported path limit.

In TCP and combined modes, apply flow settings independently per direction. In automatic mode, start with the configured initial flow count. When goodput remains below the rate being tested and existing flows have settled, add one connection, allow it to settle, and measure the change in aggregate receiver goodput over a common time window. Stop adding at the flow limit, when the rate is reached, when extra flows yield no meaningful gain, or when congestion or local resource limits call for backoff. Enforce the aggregate sending ceiling throughout. Report the achieved rate, active flow count and limiting condition. An observed single-flow plateau alone cannot distinguish a per-flow limit from a shared bottleneck. Record delivered goodput, retransmissions and congestion window where the operating system exposes them. Combined mode pairs bulk TCP with a low-rate, constant UDP stream to observe delay, loss and reordering under load. Include both components in the requested directional capacity.

## In-stream timing and jitter

Each UDP data datagram carries its stream and direction, sequence number and sender monotonic timestamp. The receiver timestamps arrival locally. Negotiate timing capabilities, clock units and resolution during setup. A configurable subset of ordinary data packets requests a timing reply. The reply identifies the originating stream, direction and sequence, and carries the responder’s receive and reply-send timestamps. The initiator records reply arrival locally. Pace the small replies and account for their reverse-direction bandwidth at admission, including during a one-direction bulk test. Keep timing replies separate from bulk data sequence and loss counters.

Compute raw round-trip time from the initiator’s send and reply-arrival times. Subtract the responder’s measured turnaround for a processing-adjusted round trip when both intervals are valid. For consecutive in-order data packets in one stream, compute signed one-way delay variation as receiver arrival spacing minus sender transmit spacing. A constant clock offset cancels; clock drift can still bias the result. For round-trip variation, compare successive valid timing exchanges. Report absolute one-way delay only with synchronised wall-clock timestamps and a documented clock-quality bound.

Per window and direction, record valid sample counts, missing timing replies, round-trip summaries and mean absolute delay variation in time units. Keep signed variation available for delay trends. Skip variation pairs across missing or reordered sequences and clock discontinuities. Mark unavailable timing samples explicitly. A missing timing reply does not prove forward data loss. The existing strict in-order rule still governs each UDP data stream.

For TCP, an optional length-delimited application record can carry a record ID and send timestamp; timestamp delivery when the complete record is read. This measures application delivery timing, including buffering, retransmission and head-of-line delay. TCP segmentation is controlled by the transport. Retain the UDP canary for packet-level timing in combined mode. Record timing format and sampling frequency in the manifest.

Validation uses known send and arrival spacing, a fixed cross-host clock offset, deliberate responder delay, dropped replies and reordered data. Verify that constant delay produces near-zero variation, added turnaround affects raw RTT but cancels from adjusted RTT, and a missing reply leaves the independently observed data result intact. [IRTT packet format](https://github.com/heistp/irtt/blob/master/packet.go) · [IRTT timing calculations](https://github.com/heistp/irtt/blob/master/recorder.go)

## Controller states

| State | Sender action and evidence |
|---|---|
| Start and ramp | Begin at the configured starting rate. Raise the offered rate while fresh receiver evidence supports the increase. |
| Settle | Mark the sequence boundary for the rate change. Allow in-flight traffic and delayed feedback to clear before assessing the new rate. Severe loss can interrupt settling. |
| Backoff | Reduce the rate and test whether delivery improves. Record the trigger and the result for each measured step. |
| Cruise and probe | Hold a verified clean rate. Probe above it within the ceiling. Accept a clean gain or return to the previous clean rate and verify it again. |
| Freeze and recover | Stop updating the capacity estimate during a confirmed outage. Apply the agreed feedback timeouts. After a feedback timeout, apply the configured automatic or manual recovery policy. For automatic recovery, confirm the active session and receiver readiness before restarting at the configured starting rate. An operator stop or expired session is terminal. |
| Overload | With explicit opt-in at both ends, hold above the estimated edge for a bounded interval within the admitted capacity. |

Maintain offered rate, receiver-delivered rate, last verified clean rate and estimated capacity independently. Attach an observation time and confidence to the estimate. Use monotonic intervals for control. Associate feedback with the rate that generated its traffic. Record absolute one-way delay only when endpoint clock synchronisation supports it. During bidirectional tests, alternating probes can help reveal interaction between directions.

## Session files and reporting

| Proposed file | Contents |
|---|---|
| `session.json` | Format and software versions, effective settings, capabilities, admitted limits, enabled plugins, start and end, termination reason and completion status. Exclude authentication secrets. |
| `series-fl.csv` and `series-rl.csv` | One row per measurement window for each active direction, including time, phase, sequence coverage, offered and delivered rates, loss, jitter, delay gradient, state, estimated edge and reason. |
| `events.csv` | Rate changes, state transitions, impairments, recovery and gaps, with timestamps and durations where known. |
| `linkstate.csv` | Timestamped samples from enabled telemetry adapters. |

Keep enough incremental data to reconstruct interrupted runs. Plot estimated capacity and delivered rate over time with state transitions, outages and coverage gaps marked. Include comparable views by phase and direction. Record UTC and display local time alongside it. Report missing coverage explicitly, including intervals in which the receiver saw no packets.

## Operations and portability

Package a native engine for each supported platform and a small controller and reporting layer. Run the server as an unprivileged service with a configuration file, bounded logs, memory and disk use. Enforce session, port and idle limits. Track owned processes and sockets so termination reliably releases resources on Linux and macOS. Apply admission reservations atomically and release them on session termination or expiry.

## Implementation and validation sequence

1. Compare existing adaptive UDP tooling and fixed-rate reference tools using controlled tests. Verify current tool capabilities before selecting a dependency.

2. Build a deterministic controller that can replay a sequence of receiver reports and reproduce the same decisions. Use an emulator with known capacity to test how the link responds to changed actions.

3. Benchmark the native sender and receiver on loopback. Check pacing, delivered-byte accounting, packet sequencing and local-drop detection before testing a network path. For TCP, emulate a 30 Mbps per-connection limit with sufficient aggregate capacity and a 300 Mbps target. Verify that parallel connections can approach the target and that per-flow goodput sums to the aggregate over the same interval. Repeat with a 30 Mbps shared bottleneck and verify that automatic mode stops adding ineffective connections.

4. Emulate asymmetric rates, feedback delay, deep queues, reordering, random and rate-dependent loss, abrupt changes in capacity and outages. Verify that receiving sequences 1, 3, 2 records one loss and one reordered arrival for sequence 2, with no duplicate loss. Measure reaction time, settling, oscillation and recovery against the acceptance criteria.

5. Exercise authentication failure, concurrent admission, control loss, reconnect, process termination, resource exhaustion and stop during recovery. Verify the server holds reservations for a recoverable, active session, releases them on termination or expiry, and both endpoints close the session connections on shutdown.

6. Validate all nine protocol-mode and direction combinations before the first release. Exercise forward and return independently before simultaneous bidirectional tests. Verify receiver measurements, per-direction capacity enforcement and reporting for each combination, including TCP flow scaling and the UDP canary where applicable. Compare with an independent reference under comparable conditions.

## Terminology

**Forward** means server to client. **Return** means client to server. **Offered rate** is the sender’s traffic rate. **Delivered rate** is the receiver’s measured throughput. The **edge** is the estimated highest sustainable clean rate under current conditions. A **feedback window** summarises receiver observations. A **clean-rate observation window** begins after settling and requires fresh receiver evidence, the configured elapsed time and packet count, and zero UDP losses under the per-stream in-order delivery rule. A **canary** is the small UDP measurement stream used alongside bulk TCP.
