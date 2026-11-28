"""Exercise the extracted release in an isolated Linux network namespace.

Run inside a disposable --network none container with NET_ADMIN, Python 3.11+,
iproute2 and ping. Arguments are an extracted launcher and a new output directory.
TC and PING may select wrapper executables when the tools are provided separately.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launcher", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    cli = args.launcher.resolve(strict=True)
    output = args.output.resolve()
    tc = os.environ.get("TC", "tc")
    ping = os.environ.get("PING", "ping")
    active = {p.name for p in Path("/sys/class/net").iterdir()
              if p.is_dir() and int((p / "flags").read_text(), 16) & 1}
    if active != {"lo"} or len(Path("/proc/net/route").read_text().splitlines()) != 1:
        parser.error("use a disposable network namespace with only loopback up and no IPv4 routes")
    original = subprocess.check_output([tc, "qdisc", "show", "dev", "lo"], text=True)
    if not original.startswith("qdisc noqueue") or len(original.splitlines()) != 1:
        parser.error("loopback already has a configured queue")
    output.mkdir(parents=True, exist_ok=False)
    def change(*parts):
        subprocess.run([tc, *parts], check=True)
    change("qdisc", "add", "dev", "lo", "root", "handle", "1:", "prio", "bands", "3",
           "priomap", *(["0"] * 16))
    try:
        change("qdisc", "add", "dev", "lo", "parent", "1:1", "handle", "10:", "netem", "delay", "320ms")
        change("qdisc", "add", "dev", "lo", "parent", "1:2", "handle", "20:", "netem", "loss", "100%")
        change("qdisc", "add", "dev", "lo", "parent", "1:3", "handle", "30:", "netem", "delay", "1600ms")
        result = subprocess.check_output([ping, "-c", "3", "127.0.0.1"], text=True)
        (output / "rtt.txt").write_text(result)
        samples = re.findall(r"time=([\d.]+) ms", result)
        assert len(samples) == 3 and all(600 <= float(n) <= 800 for n in samples)
        with tempfile.TemporaryDirectory() as secrets:
            cfg = Path(secrets) / "config.json"
            subprocess.run([cli, "keygen", "--output", cfg], check=True)
            with (output / "gateway.log").open("w") as log:
                server = subprocess.Popen([cli, "server", "--listen", "127.0.0.1:0", "--config", cfg,
                    "--data-ports", "62001-62001", "--capacity", "2M"], stdout=subprocess.PIPE, stderr=log, text=True)
                try:
                    addr = json.loads(server.stdout.readline())["listening"]
                    for profile, mode in (("clean", "tcp"), ("combined", "combined"), ("late", "tcp"), ("missing", "tcp")):
                        out = output / profile
                        with (output / (profile + ".log")).open("w") as log:
                            client = subprocess.Popen([cli, "run", "--config", cfg, "--connect", addr,
                                "--mode", mode, "--forward-max", "1M", "--return-max", "1M",
                                "--flows", "1", "--flow-limit", "1", "--duration", "22",
                                "--window-ms", "2000", "--output", out], stdout=log, stderr=log)
                            try:
                                if profile in ("late", "missing"):
                                    deadline = time.monotonic() + 16
                                    while not (out / "windows.jsonl").exists():
                                        if time.monotonic() > deadline or client.poll() is not None:
                                            raise RuntimeError("missing initial healthy window")
                                        time.sleep(.02)
                                    change("filter", "add", "dev", "lo", "parent", "1:", "protocol", "ip",
                                           "prio", "1", "u32", "match", "ip", "protocol", "6", "0xff",
                                           "match", "ip", "dport", "62001", "0xffff", "flowid", "1:3" if profile == "late" else "1:2")
                                rc = client.wait(timeout=32)
                            finally:
                                if client.poll() is None:
                                    client.kill()
                                client.wait()
                        manifest = json.loads((out / "session.json").read_text())
                        receipts = [json.loads(l) for l in (out / "tcp-final.jsonl").read_text().splitlines()]
                        assert len(receipts) == 1 and manifest["sessions"] == 1
                        receipt = receipts[0]
                        assert receipt["wait_ms"] <= receipt["effective_max_ms"] + 100
                        if profile != "missing":
                            assert rc == 0 and receipt["status"] == "complete" and manifest["stop_acknowledged"]
                            for lane in ("forward", "return"):
                                assert all(f["sent_records"] == f["received_records"] > 0 for f in receipt["directions"][lane])
                            if profile == "late":
                                windows = [json.loads(l) for l in (out / "windows.jsonl").read_text().splitlines()]
                                final_window = windows[-1]
                                sent = final_window["client"]["tcp"][0]["sent"]
                                captured = final_window["server"]["tcp"][0]["received"]["bytes"]
                                assert sent * 1000 > captured, "fixture did not delay the last TCP records"
                                change("filter", "del", "dev", "lo", "parent", "1:", "prio", "1")
                        else:
                            assert receipt["status"] == "incomplete" and receipt["stop_acknowledged"]
                            ret, = receipt["directions"]["return"]
                            assert 0 < ret["received_records"] < ret["sent_records"]
                            assert manifest["status"] != "completed"
                        print(profile, rc, receipt["status"], receipt["wait_ms"], flush=True)
                finally:
                    server.terminate()
                    server.wait(timeout=5)
    finally:
        (output / "queue-stats.txt").write_text(subprocess.check_output([tc, "-s", "qdisc", "show", "dev", "lo"], text=True))
        change("qdisc", "del", "dev", "lo", "root")
        assert subprocess.check_output([tc, "qdisc", "show", "dev", "lo"], text=True) == original


if __name__ == "__main__":
    main()
