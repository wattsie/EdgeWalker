"""Exercise four clients on one running server. Requires 1.6 Mbps each way."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
from pathlib import Path
import select
import subprocess


def exercise(engine, config, address):
    def receive(proc):
        if not select.select([proc.stdout], [], [], 15)[0]:
            raise RuntimeError("client response timed out")
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("client disconnected before responding")
        return json.loads(line)

    def send(proc, message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def close(proc):
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)

    def launch(mode, flows):
        proc = subprocess.Popen(
            [engine, "--config", config, "client", "--connect", address, "--mode", mode,
             "--forward-max", "400000", "--return-max", "400000", "--flow-limit", str(flows)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        cleanup.callback(close, proc)
        return proc, mode, flows

    def check_ready(client):
        proc, mode, _ = client
        reply = receive(proc)
        assert reply["status"] == "ready", reply
        tcp, udp = reply["tcp_port"], reply["udp_port"]
        assert bool(tcp) == (mode != "udp") and bool(udp) == (mode != "tcp")
        assert mode != "combined" or tcp == udp
        return tcp or udp

    def measure(client, sequence):
        proc, mode, flows = client
        send(proc, dict(id=sequence, ms=1000, rates=dict(forward=400000, reverse=400000),
                        flows=dict(forward=flows, reverse=flows)))
        result = receive(proc)
        received = []
        for side, peer in [("client", "server"), ("server", "client")]:
            lanes = []
            if mode != "tcp":
                lanes.append((result[side]["udp"], result[peer]["udp"]))
            if mode != "udp":
                assert len(result[side]["tcp"]) == flows
                lanes.extend(zip(result[side]["tcp"], result[peer]["tcp"]))
            total = 0
            for local, remote in lanes:
                count = local["received"]["bytes"]
                assert count == 1000 * remote["sent"] and count > 0, (mode, side, count)
                total += count
            received.append(total)
        return received

    with ExitStack() as cleanup, ThreadPoolExecutor(max_workers=4) as pool:
        modes = [("udp", 1), ("tcp", 4), ("combined", 8), ("tcp", 16)]
        clients = [launch(*settings) for settings in modes]
        ports = [check_ready(c) for c in clients]
        assert len(set(ports)) == 4
        records = []
        def window(active, sequence):
            totals = list(pool.map(measure, active, sequence))
            records.append(dict(clients=len(active), received_bytes=totals))

        window(clients, [1]*4)
        send(clients[0][0], None)
        assert clients[0][0].wait(timeout=5) == 0
        window(clients[1:], [2]*3)
        close(clients[1][0])
        window(clients[2:], [3]*2)
        replacements = [launch(*settings) for settings in modes[:2]]
        new_ports = [check_ready(c) for c in replacements]
        assert len(set(new_ports + ports[2:])) == 4
        remaining = replacements + clients[2:]
        window(remaining, [1, 1, 4, 4])
        for proc, _, _ in remaining:
            send(proc, None)
            assert proc.wait(timeout=5) == 0
        return dict(ports=ports, replacement_ports=new_ports, windows=records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("address")
    args = parser.parse_args()
    print(json.dumps(exercise(args.engine.resolve(), args.config, args.address)))
