"""Exercise the installed calibration command on localhost through calibration and its ceiling diagnostic."""

import json
import os
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CalibrationLoopback(unittest.TestCase):
    def test_held_out_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.json"
            config.write_text(json.dumps({"key": secrets.token_hex(8)}))
            config.chmod(0o600)
            launcher = os.environ.get("EDGEWALKER_LAUNCHER", str(ROOT / "edgewalker"))
            server = subprocess.Popen(
                [
                    launcher,
                    "server",
                    "--listen",
                    "127.0.0.1:0",
                    "--config",
                    str(config),
                    "--capacity",
                    "400000",
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                address = json.loads(server.stdout.readline())["listening"]
                result = subprocess.run(
                    [
                        launcher,
                        "calibrate",
                        "--check-ceiling",
                        "--connect",
                        address,
                        "--config",
                        str(config),
                        "--forward-max",
                        "400000",
                        "--duration",
                        "45",
                        "--window-ms",
                        "500",
                        "--output",
                        str(root / "result"),
                    ],
                    check=False,
                    timeout=50,
                )
                self.assertIn(result.returncode, (0, 2))
                evidence = json.loads((root / "result/measurements.json").read_text())[
                    "forward"
                ]
                self.assertEqual(len(evidence), 6)
                self.assertEqual(
                    json.loads(
                        (root / "result/ceiling_check-forward.json").read_text()
                    )["forward"]["start_bps"],
                    400000,
                )
                self.assertTrue(
                    all(
                        d["cap_ok"] and d["windows"] >= 3 and d["sent"]
                        for d in evidence.values()
                    )
                )
                plan = json.loads((root / "result/plan.json").read_text())["forward"]
                self.assertEqual(plan["ceiling_bps"], 400000)
            finally:
                server.terminate()
                server.wait(timeout=4)
                server.stdout.close()


if __name__ == "__main__":
    unittest.main()
