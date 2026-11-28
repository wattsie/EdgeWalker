"""Operator rate notation. Internal and recorded rates use integer bits per second."""
import argparse
import re

MAX_RATE = 2**64 - 1
RATE = re.compile(r"([0-9]+)(?:\.([0-9]+))?([kKmMgG]?)")
HINT = "use whole bits/s or decimal k/M/G, e.g. 1k, 1.5M or 1G"


def rate_bps(value):
    if type(value) is int and 0 <= value <= MAX_RATE:
        return value
    match = RATE.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match:
        raise ValueError(HINT)
    whole, fraction, suffix = match.groups()
    places = {"": 0, "k": 3, "m": 6, "g": 9}[suffix.lower()]
    fraction = (fraction or "").rstrip("0")
    if len(fraction) > places:
        raise ValueError("rate must resolve to a whole bit per second")
    digits = (whole + fraction.ljust(places, "0")).lstrip("0") or "0"
    if len(digits) > 20 or int(digits) > MAX_RATE:
        raise ValueError("rate exceeds the unsigned 64-bit limit")
    return int(digits)


def rate_argument(text):
    try:
        return rate_bps(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def normalize_rates(fields):
    return {k: rate_bps(v) if k.endswith("_bps") and v is not None else v
            for k, v in fields.items()}


def display_rate(value):
    if value is None:
        return "unknown"
    for places, unit in ((9, "Gbps"), (6, "Mbps"), (3, "kbps")):
        if value >= 10**places:
            whole, remainder = divmod(value, 10**places)
            fraction = str(remainder).zfill(places).rstrip("0")
            return f"{whole}{'.' + fraction if fraction else ''} {unit}"
    return f"{value} bps"
