"""Deterministic replay of a, single-stream UDP controller per direction."""

from rates import normalize_rates
import argparse
import json
from pathlib import Path


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


class Direction:
    def __init__(self, settings):
        for key in ("start_bps", "ceiling_bps", "ramp_step_bps", "probe_step_bps",
                    "window_ms", "min_packets", "early_loss_count", "probe_ms", "hold_ms", "suspend_ms"):
            integer(settings[key], key, 1)
        if settings["start_bps"] > settings["ceiling_bps"]:
            raise ValueError("starting rate exceeds ceiling")
        if settings["hold_ms"] >= settings["suspend_ms"]:
            raise ValueError("hold must precede suspension")
        self.delay_policy = settings.get("delay_policy")
        if self.delay_policy is not None:
            if not isinstance(self.delay_policy, dict):
                raise ValueError("delay_policy must be an object")
            for key in ("growth_us", "observe_ms", "min_gain_bps"):
                integer(self.delay_policy[key], key, 1)
        self.clean_delay = None
        self.clean_history = {}
        self.successful_probes = 0
        self.checking_offer = None
        self.revalidation_delay=None
        self.recovering_delay = False
        self.settings = settings
        self.rate = settings["start_bps"]
        self.state = "ramp"
        self.last_feedback = self.epoch = 0
        self.last_clean = self.edge = None
        self.probe_due = 0
        self.last_seq = None
        self.seen = set()
        self.reset_plateau()
        self.reset_window()

    def reset_plateau(self):
        self.best_offer = self.best_delivery = None
        self.previous_offer = self.previous_delay = None
        self.flat_steps = 0
        self.plateau_found = False

    def reset_window(self):
        self.duration = self.sent = self.loss = 0
        self.window_start = self.window_end = None
        self.window_bytes = 0
        self.window_delay = None
        self.flat_delay = True
        self.min_growth = None
        self.complete_timing = True

    def deadline(self):
        if self.state == "suspended":
            return None
        timer = "suspend_ms" if self.state == "hold" else "hold_ms"
        return self.last_feedback + self.settings[timer]

    def timeout(self, now):
        self.checking_offer = None
        self.successful_probes = 0
        self.recovering_delay = False
        self.clean_delay = None
        self.reset_plateau()
        self.reset_window()
        self.edge = None
        self.epoch = now
        if self.state == "hold":
            self.state, self.rate = "suspended", 0
            return "feedback_suspend"
        self.state = "hold"
        return "feedback_hold"

    def record_sequences(self, event):
        first = integer(event["first"], "first", 1)
        last = integer(event["last"], "last", first)
        if self.last_seq is not None and first <= self.last_seq:
            raise ValueError("sender ranges must advance")
        if "sequence_counts" in event:
            counts = event["sequence_counts"]
            if not isinstance(counts, dict):
                raise ValueError("sequence_counts must be an object")
            sent = last-first+1
            values = {key: integer(counts[key], key) for key in ("lost", "reordered", "duplicates")}
            if values["lost"] > sent:
                raise ValueError("loss exceeds sender range")
            self.last_seq = last
            return dict(sent=sent, **values)
        arrivals = event["arrivals"]
        if not isinstance(arrivals, list):
            raise ValueError("arrivals must be a list")
        expected = first
        lost = reordered = duplicates = 0
        for seq in arrivals:
            integer(seq, "sequence", 1)
            if seq > last:
                raise ValueError("arrival exceeds sender boundary")
            if seq in self.seen:
                duplicates += 1
            elif seq < expected:
                reordered += 1
            else:
                lost += seq - expected
                expected = seq + 1
            self.seen.add(seq)
        lost += last + 1 - expected
        self.last_seq = last
        return dict(sent=last-first+1, lost=lost, reordered=reordered, duplicates=duplicates)

    def plateau_decision(self, now, delivery, delay, evidence):
        # Only compare contiguous clean windows with the same measured delay.
        if delay is None or delay != self.previous_delay:
            self.reset_plateau()
        if delay is None:
            return None
        self.previous_delay = delay
        if self.best_offer is None or delivery > self.best_delivery:
            self.best_offer, self.best_delivery = self.rate, delivery
            self.flat_steps = 0
        elif self.rate == self.best_offer and delivery < self.best_delivery:
            # The old operating point no longer sustains its measured throughput.
            self.reset_plateau()
            self.best_offer, self.best_delivery = self.rate, delivery
            self.previous_delay = delay
        elif delivery == self.best_delivery and self.rate < self.best_offer:
            self.best_offer = self.rate
        elif self.rate > self.previous_offer and self.rate > self.best_offer:
            self.flat_steps += 1

        self.previous_offer = self.rate
        evidence.update(best_offer_bps=self.best_offer, best_delivered_bps=self.best_delivery)
        if self.rate > self.best_offer and (self.plateau_found or self.flat_steps >= 2):
            reason = "plateau_probe_return" if self.plateau_found else "plateau_hold"
            self.plateau_found = True
            self.rate = self.edge = self.best_offer
            self.state = "cruise"
            self.probe_due = now + self.settings["probe_ms"]
            return reason
        return None

    def delay_decision(self, duration, delivery, delay, growth, evidence):
        if self.delay_policy is None:
            return None
        if growth is None or delay is None:
            return "timing_unverified"
        threshold = self.delay_policy["growth_us"]
        rising = growth >= threshold
        if self.recovering_delay:
            reference = self.clean_history.get(self.rate, (None, self.rate))[1]
            steady = (abs(growth)<threshold and self.revalidation_delay is not None
                      and abs(delay-self.revalidation_delay)<threshold
                      and delivery>=reference-max(self.delay_policy["min_gain_bps"], reference//20))
            self.revalidation_delay = delay
            if (rising and delay >= self.clean_delay + threshold
                    and delivery + self.delay_policy["min_gain_bps"] < self.rate):
                self.recovering_delay = False
                self.before_delay_hold = "revalidate"
                self.state = "delay_hold"
                self.hold_delivery = delivery
                self.hold_duration = 0
                return "rising_delay_hold"
            if rising or (delay>self.clean_delay+threshold and not steady):
                return "delay_revalidating"
            self.recovering_delay = False
        if self.state == "delay_hold":
            self.hold_duration += duration
            evidence.update(hold_observed_ms=self.hold_duration,
                            hold_reference_bps=self.hold_delivery)
            if self.hold_duration < self.delay_policy["observe_ms"]:
                return "delay_observing"
            if rising:
                if delivery - self.hold_delivery < self.delay_policy["min_gain_bps"]:
                    if self.last_clean < self.rate:
                        self.rate=self.last_clean
                    else:
                        lower=[r for r in self.clean_history if r<self.rate and r<=delivery+self.delay_policy["min_gain_bps"]]
                        self.rate=max(lower,default=max(1,self.rate//2))
                        self.clean_delay=self.clean_history.get(self.rate,(self.clean_delay,None))[0]
                    self.edge = None
                    self.state = "revalidate"
                    self.recovering_delay = True
                    self.revalidation_delay=None
                    self.reset_plateau()
                    return "rising_delay_flat_throughput"
                self.hold_delivery = delivery
                self.hold_duration = 0
                return "delay_rising_with_gain"
            self.state = self.before_delay_hold
        elif rising and self.last_clean is not None and self.clean_delay is not None and (self.rate > self.last_clean or delivery+self.delay_policy["min_gain_bps"]<self.rate):
            self.before_delay_hold = self.state
            self.state = "delay_hold"
            self.hold_delivery = delivery
            self.hold_duration = 0
            return "rising_delay_hold"
        self.clean_delay = delay
        return None

    def loss_backoff(self):
        self.checking_offer = None
        self.successful_probes = 0
        self.edge = None
        self.recovering_delay = False
        self.reset_window()
        self.reset_plateau()
        if self.last_clean is not None and self.last_clean < self.rate:
            self.rate = self.last_clean
        else:
            self.rate = max(1, self.rate // 2)
        self.state = "revalidate"

    def feedback(self, event, now, evidence):
        start = integer(event["start_ms"], "start_ms")
        end = integer(event["end_ms"], "end_ms", start+1)
        if end > now:
            raise ValueError("measurement ends after arrival")
        offered = integer(event["offered_bps"], "offered_bps", 1)
        evidence.update(interval_ms=[start, end], measured_offer_bps=offered)
        if (self.state == "suspended" or start < max(self.epoch, self.last_feedback)
                or now-end >= self.settings["hold_ms"] or offered != self.rate):
            return "ignored_feedback"

        first = event["first"]
        if start != self.last_feedback or (self.last_seq is not None and first != self.last_seq + 1):
            self.reset_plateau()
        adjacent_reports = (event.get("same_stream_windows") is True and self.window_end is not None
                            and 0 <= start - self.window_end < self.settings["hold_ms"])
        if ((start != self.window_end and not adjacent_reports) or
                (self.last_seq is not None and first != self.last_seq+1)):
            self.reset_window()
        counts = self.record_sequences(event)
        elapsed = end - start
        received = integer(event["received_bytes"], "received_bytes")
        evidence.update(**counts, delivered_bps=received * 8000 // elapsed)
        delay = event.get("excess_delay_us")
        if delay is not None:
            integer(delay, "excess_delay_us")
            evidence["excess_delay_us"] = delay
        if self.duration and delay != self.window_delay:
            self.flat_delay = False
        self.window_delay = delay
        growth = event.get("delay_growth_us")
        if growth is None or delay is None:
            self.complete_timing = False
        else:
            if type(growth) is not int:
                raise ValueError("delay_growth_us must be a signed integer")
            evidence["delay_growth_us"] = growth
            self.min_growth = growth if self.min_growth is None else min(self.min_growth, growth)
        if self.window_start is None:
            self.window_start = start
        self.window_bytes += received
        self.duration += elapsed
        self.sent += counts["sent"]
        self.loss += counts["lost"]
        self.window_end = self.last_feedback = end
        if self.state == "hold":
            self.state = "revalidate"
        if self.loss:
            self.edge = None
        qualified = self.duration >= self.settings["window_ms"] and self.sent >= self.settings["min_packets"]
        if not qualified and self.loss < self.settings["early_loss_count"]:
            return "observing"

        evidence.update(window_loss=self.loss, window_sent=self.sent, window_interval_ms=[self.window_start, end])
        delivery = self.window_bytes * 8000 // self.duration
        growth = self.min_growth if self.complete_timing else None
        duration = self.duration
        flat_delay = self.window_delay if self.flat_delay else None
        if delay is not None:
            evidence["window_delivered_bps"] = delivery
        self.reset_window()
        self.epoch = now
        if evidence["window_loss"]:
            self.loss_backoff()
            return "loss_backoff"

        reason = self.delay_decision(duration, delivery, delay, growth, evidence)
        if reason:
            return reason
        # A complete cohort may have drained after the measurement cutoff.
        # Recheck an unsupported increase over a longer native sampling interval
        # before recording a new operating point. One packet of quantisation
        # alone is insufficient evidence of a delivery plateau.
        quantum = event.get("udp_quantum_bps")
        if (quantum is not None and self.delay_policy and self.last_clean is not None
                and self.rate > self.last_clean):
            tolerance = max(integer(quantum, "udp_quantum_bps", 1),
                            self.delay_policy["min_gain_bps"])
            if self.rate - delivery > tolerance:
                self.edge = None
                if self.checking_offer != self.rate:
                    self.checking_offer = self.rate
                    return "verify_delivered_offer"
                step = self.settings["probe_step_bps"]
                gain = max(0, delivery - self.last_clean - tolerance)
                intermediate = self.last_clean + (gain // step) * step
                self.rate = min(self.rate - 1, intermediate)
                self.state = "probe" if self.rate > self.last_clean else "revalidate"
                self.successful_probes = 0
                self.checking_offer = None
                return "plateau_revalidate"
        self.checking_offer = None
        if self.delay_policy and self.state == "probe" and self.last_clean is not None:
            previous = self.clean_history.get(self.last_clean)
            gain = self.delay_policy["min_gain_bps"]
            if previous and delivery >= previous[1] + gain and delivery + gain >= self.rate:
                self.successful_probes += 1
            else:
                self.successful_probes = 0
            if self.successful_probes >= 2:
                self.state = "ramp"
                self.successful_probes = 0
        self.last_clean = self.edge = self.rate
        if self.delay_policy and delay is not None:
            self.clean_history.pop(self.rate,None)
            self.clean_history[self.rate]=(delay,delivery)
            if len(self.clean_history)>64:
                self.clean_history.pop(next(iter(self.clean_history)))
        reason = self.plateau_decision(now, delivery, flat_delay, evidence)
        if reason:
            return reason
        if self.rate == self.settings["ceiling_bps"]:
            self.state = "cruise"
            return "clean_at_ceiling"
        if self.state == "ramp":
            step = self.settings["ramp_step_bps"]
            if self.delay_policy and self.clean_history and delivery + self.delay_policy["min_gain_bps"] < self.rate:
                step = min(step, self.settings["probe_step_bps"])
            self.rate = min(self.settings["ceiling_bps"], self.rate+step)
            return "clean_ramp"
        if self.state == "cruise" and now >= self.probe_due:
            self.rate = min(self.settings["ceiling_bps"], self.rate+self.settings["probe_step_bps"])
            self.state = "probe"
            return "upward_probe"
        if self.state != "cruise":
            self.probe_due = now + self.settings["probe_ms"]
        self.state = "cruise"
        return "clean_hold"


def replay(trace):
    if not isinstance(trace, dict) or not isinstance(trace.get("events"), list):
        raise ValueError("replay requires an object with an events array")
    session = trace["session"]
    configs = trace["directions"]
    if not isinstance(configs, dict) or not configs or set(configs) - {"forward", "return"}:
        raise ValueError("expected forward and/or return configuration")
    if any(not isinstance(cfg, dict) for cfg in configs.values()):
        raise ValueError("direction settings must be objects")
    controllers = {}
    for name in sorted(configs):
        cfg = normalize_rates(configs[name])
        if isinstance(cfg.get("delay_policy"), dict):
            cfg["delay_policy"] = normalize_rates(cfg["delay_policy"])
        controllers[name] = Direction(cfg)
    rows = []

    def emit(name, now, reason, evidence=None):
        c = controllers[name]
        rows.append(dict(at_ms=now, direction=name, state=c.state, reason=reason,
                         offered_bps=c.rate, last_clean_bps=c.last_clean, edge_bps=c.edge,
                         **(evidence or {})))

    for name in controllers:
        emit(name, 0, "start")
    previous = 0
    for event in trace["events"]:
        if not isinstance(event, dict):
            raise ValueError("replay events must be objects")
        now = integer(event["at_ms"], "at_ms", previous)
        previous = now
        while True:
            deadlines = [(c.deadline(), name) for name, c in controllers.items()
                         if c.deadline() is not None]
            if not deadlines or min(deadlines)[0] > now:
                break
            when, name = min(deadlines)
            emit(name, when, controllers[name].timeout(when))
        kind = event["type"]
        if kind == "tick":
            continue
        name = event["direction"]
        if not isinstance(name, str) or name not in controllers:
            raise ValueError("event direction is not active")
        c = controllers[name]
        evidence = {}
        if kind == "feedback":
            reason = c.feedback(normalize_rates(event), now, evidence)
        elif kind == "ready":
            reason = "ignored_ready"
            if (c.state == "suspended" and event["session"] == session
                    and c.epoch <= integer(event["confirmed_ms"], "confirmed_ms") <= now
                    and now-event["confirmed_ms"] < c.settings["hold_ms"]):
                c.state, c.rate = "ramp", c.settings["start_bps"]
                c.epoch = c.last_feedback = now
                reason = "receiver_ready_restart"
        else:
            raise ValueError(f"unknown event type {kind!r}")
        emit(name, now, reason, evidence)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    try:
        rows = replay(json.loads(args.trace.read_text(encoding="utf-8")))
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.exit(2, f"invalid replay: {exc}\n")
    for row in rows:
        print(json.dumps(row, sort_keys=True))


if __name__ == "__main__":
    main()
