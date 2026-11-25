# Dynamic client validation

The November 26, 2026 lab run used the dynamic client and gateway from `d8e8a32` (version `0.2.1-dynamic.2`). Main was tested separately at `dbb2970`. Both use the same launcher and support scripted runs.

## Regression results

| Build and environment | Verified result |
|---|---|
| Main on Apple silicon | 8 Rust tests, 35 Python unit tests, 5 native integration tests, 10 operator integration tests and 1 calibration integration test passed. |
| Dynamic on Apple silicon | 9 Rust tests, 39 Python unit tests, 5 native integration tests, 14 operator integration tests and 1 calibration integration test passed. Formatting and Clippy passed. |
| Dynamic on ARM64 Linux | 9 Rust tests passed in the build environment. The 39 Python unit and 20 integration tests passed independently on each of four physical probes. Native integration, operator loopback and calibration tests used the packaged native executable. |
| Fresh archives | 86 packaged CLI input checks passed on each of macOS ARM64 and Linux ARM64. Each package also completed its extracted loopback smoke test. |
| Mixed-version TCP on loopback | Main client with dynamic gateway and dynamic client with main gateway each completed with five receiver windows. Live reservation resizing requires a compatible gateway. |

## Paired physical paths

Two pairs ran simultaneously. One bridge applied 35 ms of delay and 2% configured random loss toward the client, with 15 ms of delay toward the gateway. The other bridge remained clean. Both clients used the same 160 kbps starting offer, 240 kbps directional ceiling, 500 ms nominal window and 14-second duration. Each of the nine mode and direction combinations completed on both paths. Every active direction recorded receiver bytes and stayed within its offered ceiling. Across all 346 transport and interactive direction windows the recorded actual sender rate stayed within the requested offer. Both paths used management interfaces separate from the test route.

Rates below are delivered payload in kbps, calculated across the measured window durations. Setup and between-window pauses are excluded. Download is forward. Upload is return.

| Mode and direction | Impaired download | Clean download | Impaired upload | Clean upload |
|---|---:|---:|---:|---:|
| udp-forward | 107.4 | 209.1 | n/a | n/a |
| udp-return | n/a | n/a | 205.5 | 214.9 |
| udp-both | 176.0 | 211.4 | 194.0 | 218.3 |
| tcp-forward | 155.6 | 157.0 | n/a | n/a |
| tcp-return | n/a | n/a | 147.7 | 226.3 |
| tcp-both | 212.1 | 156.3 | 149.6 | 150.2 |
| combined-forward | 99.1 | 213.6 | n/a | n/a |
| combined-return | n/a | n/a | 210.2 | 200.2 |
| combined-both | 207.2 | 214.0 | 208.7 | 215.4 |

The recorder marked 2 completed runs with gaps: udp-both on the impaired path, combined-return on the clean path.

Across 24 directional series, 3 belonged to runs marked with incomplete coverage by the reconstructed report. A setup interruption or unresolved trailing UDP range makes coverage incomplete. The delivered-rate columns use observations and leave missing coverage unknown.

## Menu and gateway behavior

On each path the operator requested 500 kbps against a 400 kbps gateway pool. The gateway rejected the request. It then granted 350 kbps in the same session. The operator raised the local ceiling, selected two TCP flows, requested 250 kbps, reduced the reservation to 180 kbps and changed the remaining time. Both receivers reported positive delivery at the 250 kbps two-flow offer, and both tests finished with one authenticated session.

Local integration tests also exercised a stop during manual recovery and a fresh authenticated replacement session retaining an edited deadline, flow count, starting rate and confirmed reservation.

## Delayed TCP cold start

Each build ran on the delayed pair and the clean reference pair in successive trials, with the impairment already in place when the eight TCP flows connected. Each run requested an aggregate 240 kbps for 24 seconds. This checks setup and delivery at a bounded offer.

| Client and gateway | Measured ping RTT (ms) | Receiver windows | Average window goodput (kbps) |
|---|---:|---:|---:|
| main | 700.69 | 8 | 192.0 |
| dynamic | 2.71 | 24 | 192.5 |
| dynamic | 700.60 | 8 | 192.0 |
| main | 2.76 | 24 | 192.7 |

At this particular low offer, each flow receives a 30 kbps budget. In a one-second window that buys 3.75 records of 1,000 bytes. The engine sent three whole records per flow, giving 192 kbps across eight connections during those windows. The observed difference from 240 kbps is consistent with record granularity. This run does not establish a satellite throughput limit.

## Retained failures and limits

The initial delayed crossover attempts missed the clean-path reference because one probe required its supported privileged ping for RTT measurement. The partial receipts were retained and both paired checks were repeated. An earlier build failed to accept a terminal stop while waiting for manual reconnection. A preliminary hardware run also had two combined-mode setups with no receiver windows. An authenticated UDP setup timeout is now eligible for a bounded retry. The exact cause of the clean-path setup failure was undetermined. The first versioned candidate exited with an error at the end of two impaired combined-mode runs when the remaining time could not accommodate another canary. The corrected client ends at its deadline after valid windows. A permanent regression covers that case.

The physical paths used separate equipment and random loss realization. The short trials validate operation under these conditions. Live commands take effect at control boundaries. Socket count above the prepared limit, protocol, credentials and peer changes require fresh setup. Satellite field performance and sustained high-rate delivery remain separate qualifications.

Both kernel queue trees matched their respective baselines after the tests. The temporary installations were removed, all four probe agents were active, and traffic inventory was idle.

## Test package checksums

- `edgewalker-0.2.1-dynamic.2-linux-aarch64.tar.gz` SHA-256 `4bc540103bcf5b813d0c17e07d12af0c6f63e967267da3a42dc0d200a8ec4940`
- `edgewalker-0.2.1-dynamic.2-darwin-arm64.tar.gz` SHA-256 `9d09c562a1e403a870c5ac677b2472244d2b3c0ed5a1aeb7330265fe6887d697`
