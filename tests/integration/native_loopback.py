"""Run explicitly after cargo build --locked. Loopback only."""

import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "target/debug/edgewalker-native"


class NativeLoopback(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cfg = Path(directory.name) / "config.json"
        self.cfg.touch(mode=0o600)
        self.cfg.write_text(json.dumps({"key": secrets.token_hex(8)}))
        self.server = self.start(
            "server", "--listen", "127.0.0.1:0", "--capacity", "800000"
        )
        self.addr = json.loads(self.server.stdout.readline())["listening"]

    def start(self, *args):
        p = subprocess.Popen(
            [BIN, "--config", self.cfg, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def finish():
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=3)

        self.addCleanup(finish)
        return p

    def client(self, f, r, mode="udp", flows=1):
        return self.start(
            "client",
            "--connect",
            self.addr,
            "--mode",
            mode,
            "--forward-max",
            str(f),
            "--return-max",
            str(r),
            "--flow-limit",
            str(flows),
        )

    def test_nine_combinations(self):
        for mode in ("udp", "tcp", "combined"):
            for f, r in ((800000, 0), (0, 800000), (800000, 800000)):
                with self.subTest(mode=mode, forward=f, reverse=r):
                    p = self.client(f, r, mode, 2)
                    command = dict(
                        id=1,
                        ms=500,
                        rates=dict(forward=f, reverse=r),
                        flows=dict(forward=2, reverse=2),
                    )
                    out, err = p.communicate(
                        json.dumps(command) + "\nnull\n", timeout=8
                    )
                    self.assertEqual(p.returncode, 0, err)
                    ready, result = map(json.loads, out.splitlines())
                    self.assertEqual(ready["status"], "ready")
                    for rate, tx, rx in [
                        (f, result["server"], result["client"]),
                        (r, result["client"], result["server"]),
                    ]:
                        lanes = [(tx["udp"], rx["udp"])] if mode != "tcp" else []
                        if mode != "udp":
                            lanes.extend(zip(tx["tcp"], rx["tcp"]))
                        for sent, received in lanes:
                            count = sent["sent"]
                            self.assertEqual(count > 0, rate > 0)
                            self.assertEqual(
                                received["received"]["bytes"], 1000 * count
                            )
                            self.assertEqual(received["received"]["skipped"], 0)
                        self.assertLessEqual(
                            sum(s["sent"] for s, _ in lanes) * 8000, rate // 2
                        )

    def test_atomic_rejection_and_stop_release(self):
        held = self.client(600000, 0)
        self.assertEqual(json.loads(held.stdout.readline())["status"], "ready")
        blocked = self.client(400000, 600000)
        _, err = blocked.communicate(timeout=5)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('"available":{"forward":200000,"reverse":800000}', err)
        spare = self.client(0, 800000)
        self.assertEqual(json.loads(spare.stdout.readline())["status"], "ready")
        spare.communicate("null\n", timeout=3)
        held.communicate("null\n", timeout=3)
        p = self.client(800000, 800000)
        out, err = p.communicate("null\n", timeout=3)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("ready", out)

    def test_timing_reservation_and_paced_replies(self):
        p = self.start(
            "client",
            "--connect",
            self.addr,
            "--mode",
            "udp",
            "--forward-max",
            "800000",
            "--timing-every",
            "4",
            "--udp-size",
            "500",
        )
        self.assertEqual(json.loads(p.stdout.readline())["status"], "ready")
        competitor = self.client(0, 800000)
        _, err = competitor.communicate(timeout=3)
        self.assertIn('"reverse":784000', err)
        w = dict(id=1, ms=1000, rates=dict(forward=800000, reverse=0))
        out, err = p.communicate(json.dumps(w) + "\nnull\n", timeout=4)
        self.assertEqual(p.returncode, 0, err)
        r = json.loads(out)
        sender, receiver = r["server"], r["client"]
        self.assertEqual(
            receiver["udp"]["received"]["bytes"], 500 * sender["udp"]["sent"]
        )
        t = sender["timing"]
        self.assertGreater(t["valid"], 0)
        self.assertLessEqual(t["valid"], t["requested"])
        self.assertLessEqual(t["mean_adjusted_rtt_us"], t["mean_raw_rtt_us"])
        self.assertLessEqual(receiver["timing"]["replies_sent"] * 320, 16000)

    def test_server_overload_opt_in(self):
        p = self.start(
            "client",
            "--connect",
            self.addr,
            "--mode",
            "udp",
            "--forward-max",
            "800000",
            "--overload-ms",
            "500",
        )
        out, err = p.communicate(timeout=3)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("OverloadDenied", err)
        self.assertFalse(out)

    def test_wrong_key(self):
        # The server retains its original key in memory.
        self.cfg.write_text(json.dumps({"key": secrets.token_hex(8)}))
        p = self.client(800000, 0)
        out, _ = p.communicate(timeout=6)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
