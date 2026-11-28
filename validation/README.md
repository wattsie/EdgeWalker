# Field TCP accounting check

Use this kit on the client with EdgeWalker 0.2.2 RC6 at both sites. A configured gateway can stay running. Verify its running version and available data ports in the offline validation guide. Confirm the client has the matching key in its normal config file and the test route crosses the intended interface. Run from a writable directory with enough space for the local recordings. The scripts use the local package, the installed edgewalker on PATH, or the explicit path in EDGEWALKER_CLI. The legacy Linux package provides its interpreter in runtime/bin/python3.11.

## First run

Extract the field kit in the working directory. Set GATEWAY to the gateway's reachable address, including its control port. If needed, set EDGEWALKER_CONFIG to your existing protected config and EDGEWALKER_CLI to the full path of the extracted edgewalker launcher.

```sh
GATEWAY='your-gateway:62000'
sh validation/field.sh baseline "$GATEWAY"
```

This runs TCP with one fixed flow per direction, a 1 Mbps starting rate and ceiling in each direction, five-second windows and a 60-second duration. The controller can back off. Both directions run concurrently. Stop early with Ctrl-C if other services degrade, the terminal drops registration or control connectivity deteriorates. Keep the local run directory even if the command fails.

## Optional directional comparison

Run these individually after verifying fresh terminal monitoring, a clean baseline and approval for 1.5 Mbps in the direction under test. Check terminal events, coverage gaps and service health after each command before proceeding. The gateway needs at least 1.5 Mbps of available capacity in that direction. Each command starts a separate 60-second run with one TCP flow. The opposite direction still carries acknowledgements and control messages.

```sh
sh validation/field.sh higher-forward "$GATEWAY"
```

```sh
sh validation/field.sh higher-return "$GATEWAY"
```

## Return the evidence

Each command prints a new review ZIP path. Transfer that ZIP back for review. Keep the original recordings private for follow-up. To build a ZIP from an existing run entirely offline, including an interrupted run:

```sh
sh validation/field.sh review /path/to/run
```

The summary includes the recorded build, UTC times, sessions, completion status and stop acknowledgement when available. A fresh test includes the client exit code. The event timeline includes selected session and gap events. The CSV records TCP sender and receiver counters per flow, window and direction. Blank retransmission and socket statistics mean unavailable. The bundle omits the config, addresses and free-text logs. If a recording is missing or partially unreadable, the reader retains available rows, writes a diagnostic in the ZIP and exits nonzero.

Compare written bytes to current-window bytes at cutoff and through drain. The all-cohorts cutoff column also includes older records arriving during the measurement interval. Adding that entire column to through-drain bytes would double count. For aggregate comparisons, sum bytes over matching sender windows. Missing receipts or an unmatched final count leave the tail unknown. A stop acknowledgement confirms control shutdown. RC6 has no final per-flow byte receipt or configurable end-to-end drain timer. The kit captures the measurements RC6 exposes.
