"""Line-oriented controls for a local terminal or SSH session."""
import getpass
import json
from pathlib import Path
import select
import sys
import time

HELP = ("Commands (finish with Enter): rate D BPS | limit D BPS | flows D N/auto | "
        "reserve D BPS | set D SETTING VALUE | remaining SECONDS/open | resume | status | save NEW_PATH | stop | help. D is forward (download) or return (upload).")


class OperatorStop(Exception):
    """A stop requested at a command boundary."""


def setup(args, parser):
    if not sys.stdin.isatty():
        raise ValueError("menu requires a terminal")
    print("EdgeWalker client setup (Ctrl-C cancels)", file=sys.stderr)
    if not args.connect:
        args.connect = input("Gateway address and port: ").strip()
    if not args.mode:
        args.mode = input("Mode [tcp], udp or combined: ").strip() or "tcp"
    if args.mode not in ("tcp", "udp", "combined") or not args.connect:
        raise ValueError("a gateway address and valid mode are required")
    if not args.plan and args.forward_max is None and args.return_max is None:
        args.forward_max = int(input("Download reservation (bps, 0 disables): "))
        args.return_max = int(input("Upload reservation (bps, 0 disables): "))
    if not args.output:
        name = input("New results directory: ").strip()
        if not name:
            raise ValueError("a new output directory is required")
        args.output = Path(name)
    if args.duration is None:
        value = input("Duration in seconds (Enter for open-ended): ").strip()
        args.duration = float(value) if value else None
    print(f"{args.mode.upper()} to {args.connect}, results in {args.output}", file=sys.stderr)
    while True:
        for direction in ("forward", "return"):
            limit = getattr(args, direction + "_max")
            if limit is not None:
                print(f"{direction}: requested reservation {limit} bps", file=sys.stderr)
        answer = input("Start test? [y/N], edit or help: ").strip().lower()
        if answer in ("y", "yes"):
            break
        if answer == "help":
            parser.print_help()
            continue
        if answer != "edit":
            return False
        options = {flag: action for action in parser._actions for flag in action.option_strings
                   if flag.startswith("--") and flag != "--help"}
        print(" ".join(options), file=sys.stderr)
        flag = input("Option to edit: ").strip()
        action = options.get(flag)
        if action is None:
            print("Unknown option", file=sys.stderr)
            continue
        raw = getpass.getpass("Lab key: ") if flag == "--key" else input("Value: ").strip()
        try:
            if action.nargs == 0:
                if raw.lower() not in ("yes", "no"):
                    raise ValueError("enter yes or no")
                value = raw.lower() == "yes"
            else:
                value = (action.type or str)(raw)
            if action.choices and value not in action.choices:
                raise ValueError("choose one of " + ", ".join(action.choices))
            setattr(args, action.dest, value)
        except ValueError as error:
            print(f"Edit rejected: {error}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return True


class LiveMenu:
    def __init__(self, settings, mode):
        self.reservations = {d: cfg["ceiling_bps"] for d, cfg in settings.items()}
        self.mode = mode

    def poll(self, active, settings, end, record, resize, resume, validate):
        if not select.select([sys.stdin], [], [], 0)[0]:
            return end
        line = sys.stdin.readline()
        if not line:
            raise OperatorStop
        parts = line.split()
        if parts == ["stop"]:
            raise OperatorStop
        if parts == ["resume"]:
            resume.set()
            return end
        if parts == ["status"]:
            if not active:
                print("Waiting for a session. No fresh receiver measurement.", file=sys.stderr)
            for d, live in active.items():
                print(f"{d}: reserved {self.reservations[d]}, ceiling {settings[d]['ceiling_bps']}, "
                      f"next offer {live.c.rate} bps, flows {live.flows}, state {live.c.state}", file=sys.stderr)
            return end
        if parts == ["help"]:
            print(HELP, file=sys.stderr)
            return end
        try:
            if parts[:1] == ["save"]:
                path = line.strip().partition(" ")[2].strip()
                if not path:
                    raise ValueError("choose a new plan file")
                output = {d: {k: v for k, v in cfg.items() if k not in ("udp_size", "delay_policy")}
                          for d, cfg in settings.items()}
                try:
                    with Path(path).open("x") as handle:
                        json.dump(output, handle, indent=2)
                        handle.write("\n")
                except OSError as error:
                    raise ValueError("could not create new plan file") from error
                print("Current directional plan saved.", file=sys.stderr)
                return end
            if len(parts) == 2 and parts[0] == "remaining":
                if parts[1] == "open":
                    record("operator_remaining_seconds", "", "open")
                    return float("inf")
                seconds = int(parts[1])
                if not 1 <= seconds <= 1_000_000_000:
                    raise ValueError("remaining time must be 1..1000000000 seconds")
                record("operator_remaining_seconds", "", seconds)
                return time.monotonic() + seconds
            if len(parts) == 4 and parts[0] == "set" and parts[1] in active:
                _, direction, field, raw = parts
                if field not in ("start_bps", "ramp_step_bps", "probe_step_bps", "probe_ms",
                                 "feedback_ms", "hold_ms", "suspend_ms", "settle_ms", "window_ms",
                                 "min_packets", "early_loss_count", "minimum_bps", "minimum_action",
                                 "persistent_steps", "growth_us", "observe_ms", "min_gain_bps",
                                 "target_bps", "loss_only"):
                    raise ValueError("setting is fixed at setup or has a dedicated command")
                if field == "loss_only":
                    if raw not in ("true", "false"):
                        raise ValueError("use true or false")
                    value = raw == "true"
                else:
                    value = raw if field == "minimum_action" else int(raw)
                if field == "target_bps" and value < 1:
                    raise ValueError("target must be positive")
                validated = validate(direction, {field: value})
                live = active[direction]
                settings[direction].update(validated)
                if "delay_policy" not in validated:
                    settings[direction].pop("delay_policy", None)
                live.c.delay_policy = validated.get("delay_policy")
                record("operator_set_" + field, direction, value)
                print("Setting accepted.", file=sys.stderr)
                return end
            if len(parts) != 3 or parts[1] not in active:
                raise ValueError("choose an active direction; type help for commands")
            command, direction, raw = parts
            live = active[direction]
            if command == "flows" and raw == "auto":
                if self.mode == "udp":
                    raise ValueError("TCP flows require TCP or combined mode")
                settings[direction].update(fixed_flows=False, initial_flows=1)
                live.flow_blocked_rate = live.scale_reference = None
                record("operator_flows", direction, "auto")
                print("Automatic flow scaling enabled.", file=sys.stderr)
                return end
            value = int(raw)
            cfg = settings[direction]
            if live.suspended:
                raise ValueError("direction is suspended")
            if command == "reserve":
                validate(direction, {"ceiling_bps": value, "start_bps": min(cfg["start_bps"], value)})
                if value < max(1, cfg["minimum_bps"]):
                    raise ValueError("reservation below minimum")
                requested = dict(self.reservations)
                requested[direction] = value
                # Check record size before releasing any capacity.
                tcp = value - value // 10 if self.mode == "combined" else value
                udp = value // 10 if self.mode == "combined" else value
                if ((self.mode != "udp" and tcp // live.flows * cfg["feedback_ms"] < 8_000_000)
                        or (self.mode != "tcp" and udp * cfg["feedback_ms"] < cfg["udp_size"] * 8000)):
                    raise ValueError("reservation too low for streams")
                record("operator_reservation_requested", direction, value)
                print("Reservation requested; waiting for the gateway.", file=sys.stderr, flush=True)
                resize(requested)
                self.reservations = requested
                record("operator_reservation_confirmed", direction, value)
                print(f"Gateway confirmed {value} bps.", file=sys.stderr, flush=True)
                if value >= cfg["ceiling_bps"]:
                    return end
                command = "limit"
            if command == "flows":
                if self.mode == "udp" or not 1 <= value <= cfg["flow_limit"]:
                    raise ValueError("choose a prepared TCP flow count")
                rate = min(live.c.rate, cfg["start_bps"])
                tcp = rate - rate // 10 if self.mode == "combined" else rate
                if tcp // value * cfg["feedback_ms"] < 8_000_000:
                    raise ValueError("offer too low for this many TCP flows")
                cfg.update(fixed_flows=True, initial_flows=value)
                live.flows = value
            elif command in ("limit", "rate"):
                if command == "limit":
                    validate(direction, {"ceiling_bps": value, "start_bps": min(value, cfg["start_bps"])})
                maximum = self.reservations[direction] if command == "limit" else cfg["ceiling_bps"]
                if not max(1, cfg["minimum_bps"]) <= value <= maximum:
                    raise ValueError(f"value outside allowed range up to {maximum} bps")
                udp = value // 10 if self.mode == "combined" else value
                tcp = value - value // 10 if self.mode == "combined" else value
                if ((self.mode != "udp" and tcp // live.flows * cfg["feedback_ms"] < 8_000_000)
                        or (self.mode != "tcp" and udp * cfg["feedback_ms"] < cfg["udp_size"] * 8000)):
                    raise ValueError("rate too low for the active streams and feedback interval")
                if command == "limit":
                    cfg["ceiling_bps"] = value
                    cfg["start_bps"] = min(cfg["start_bps"], value)
                    live.c.rate = min(live.c.rate, value)
                else:
                    live.c.rate = value
            else:
                raise ValueError("unknown command; type help")
            live.scale_reference = None
            live.flow_blocked_rate = None
            live.c.clean_history.clear()
            live.c.recovering_delay = False
            live.c.successful_probes = 0
            live.c.reset_window()
            live.c.reset_plateau()
            live.c.edge = live.c.last_clean = live.c.checking_offer = None
            live.c.state = "ramp"
            live.settle_left = cfg.get("settle_ms", 0)
            record("operator_" + command, direction, value)
            print("Accepted for the next window.", file=sys.stderr)
        except ValueError as error:
            print(f"Edit rejected: {error}", file=sys.stderr)
        return end
