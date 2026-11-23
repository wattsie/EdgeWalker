"""Standalone HTML visualization of a saved session. No network dependencies."""

import csv
from html import escape
import json
from pathlib import Path

from records import report


def rows(path):
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def chart(data, events):
    if not data:
        return "<p>No completed windows.</p>"
    width, height = 800, 240
    left, right, top, bottom = 60, 780, 15, 206
    end = max(
        int(data[-1]["decision_ms"]),
        max((int(e["at_ms"]) for e in events), default=0),
        1,
    )
    peak = (
        max(
            max(int(r[k] or 0) for r in data)
            for k in ("edge_bps", "delivered_bps", "offer_bps")
        )
        or 1
    )
    x = lambda t: round(left + (right - left) * int(t) / end, 1)
    y = lambda b: round(bottom - (bottom - top) * int(b) / peak, 1)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Offered, delivered and estimated rates in kbps over elapsed seconds">'
    ]
    for e in events:
        if e["event"] == "coverage_gap":
            xx = x(e["at_ms"])
            parts.append(
                f'<path d="M{xx},{top}V{bottom}" stroke="#b45309" stroke-dasharray="3 3"><title>Coverage gap detected</title></path>'
            )
    for row in data:
        if row.get("link_state") == "down":
            xx = x(row["start_ms"])
            ww = x(row["end_ms"]) - xx
            parts.append(
                f'<rect x="{xx}" y="{top}" width="{ww}" height="{bottom - top}" fill="#fbd6d8"/>'
            )
    for frac in (0, 0.5, 1):
        yy = y(peak * frac)
        parts.append(
            f'<path d="M{left},{yy}H{right}" stroke="#ddd"/><text x="{left - 5}" y="{yy + 4}" text-anchor="end">{peak * frac / 1000:g}</text>'
        )
    for k, color in (
        ("offer_bps", "#627380"),
        ("delivered_bps", "#1268a1"),
        ("edge_bps", "#16804a"),
    ):
        for r in data:
            if not r[k]:
                continue
            xx = x(r["start_ms"])
            yy = y(r[k])
            parts.append(
                f'<path d="M{xx},{yy}H{x(r["end_ms"])}" stroke="{color}" stroke-width="2.5"><title>{escape(k)} {int(r[k])} bps · {escape(r["reason"])}</title></path>'
            )
    parts.append(
        f'<text x="{left}" y="230">0 s</text><text x="{right}" y="230" text-anchor="end">{end / 1000:g} s</text></svg>'
    )
    return "".join(parts)


def write_html(directory, destination):
    directory = Path(directory)
    summary = report(directory)
    manifest = json.loads((directory / "session.json").read_text())
    events = rows(directory / "events.csv")
    parts = [
        """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>EdgeWalker session report</title><style>
body{font:16px/1.5 system-ui,sans-serif;max-width:900px;margin:3rem auto;padding:0 1rem;color:#17232d}
h1,h2{line-height:1.2}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:.35rem .7rem;text-align:left;border-bottom:1px solid #d8dfe3}svg{width:100%;font:12px system-ui}
small{color:#49555f}.offer{color:#627380}.delivery{color:#1268a1}.edge{color:#16804a}
</style><main><h1>EdgeWalker session report</h1>"""
    ]
    parts.append(
        f"<p>{escape(manifest['mode'].upper())} · {escape(summary['status'])} · {escape(manifest['started_utc'])}<br><small>Run {escape(manifest['run_id'])}</small></p>"
    )
    for name, d in summary["directions"].items():
        data = rows(
            directory / ("series-fl.csv" if name == "forward" else "series-rl.csv")
        )
        parts.append(
            f'<h2>{name.capitalize()}</h2><p><b class="offer">Offered</b> · <b class="delivery">Delivered</b> · <b class="edge">Estimated edge</b> (kbps)</p>'
        )
        parts.append(chart(data, events))
        parts.append(
            "<table><thead><tr><th>Windows</th><th>Received bytes</th><th>UDP lost / sent</th><th>UDP loss</th></tr></thead><tbody>"
        )
        for key, stats in d["phases"].items():
            pct = stats["udp_loss_pct"]
            count = (
                f"{stats['udp_lost']:,} / {stats['udp_sent']:,}"
                if stats["udp_sent"]
                else "N/A"
            )
            display = "Unavailable" if pct is None else f"{pct:.3f}%"
            parts.append(
                f"<tr><td>{escape(key.replace('_', ' '))}</td><td>{stats['received_bytes']:,}</td><td>{count}</td><td>{display}</td></tr>"
            )
        parts.append("</tbody></table>")
    parts.append("<h2>Coverage and events</h2>")
    parts.append(
        "<p>Recorded windows have complete coverage.</p>"
        if summary["coverage_complete"]
        else "<p>Coverage is incomplete. See session state, pending packets and recorded gaps.</p>"
    )
    parts.append(
        "<table><tr><th>Elapsed (s)</th><th>Direction</th><th>Event</th><th>Detail</th></tr>"
    )
    for e in events:
        parts.append(
            f"<tr><td>{int(e['at_ms']) / 1000:.3f}</td><td>{escape(e['direction'])}</td><td>{escape(e['event'])}</td><td>{escape(e['detail'])}</td></tr>"
        )
    parts.append("</table></main></html>")
    with Path(destination).open("x", encoding="utf-8") as f:
        f.write("".join(parts))
