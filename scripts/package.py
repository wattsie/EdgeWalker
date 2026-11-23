"""Package the current host build without publishing or installing anything."""

import argparse
import hashlib
import json
import os
import select
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def dependency_notices(tmp, metadata_path):
    if metadata_path:
        metadata = json.loads(metadata_path.read_text())
    else:
        metadata = json.loads(
            subprocess.check_output(
                ["cargo", "metadata", "--format-version", "1", "--locked", "--offline"],
                cwd=ROOT,
            )
        )
    index = []
    files = []
    for package in sorted(
        metadata["packages"], key=lambda p: (p["name"], p["version"])
    ):
        if package["source"] is None:
            continue
        directory = Path(package["manifest_path"]).parent
        if not directory.exists():
            # Metadata can originate in the container that compiled a vendored build.
            directory = ROOT / "vendor" / directory.name
        label = f"{package['name']}-{package['version']}"
        notices = sorted(
            p
            for p in directory.iterdir()
            if p.is_file()
            and p.name.lower().startswith(
                ("license", "licence", "copying", "notice", "copyright", "unlicense")
            )
        )
        files.extend((p, f"third-party/{label}/{p.name}") for p in notices)
        index.append(
            dict(
                name=package["name"],
                version=package["version"],
                license=package["license"],
                repository=package["repository"],
                files=[f"{label}/{p.name}" for p in notices],
            )
        )
    inventory = tmp / "dependencies.json"
    inventory.write_text(json.dumps(index, indent=2) + "\n")
    return [(inventory, "third-party/dependencies.json"), *files]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--engine", type=Path, default=ROOT / "target/release/edgewalker-native"
    )
    parser.add_argument(
        "--cargo-metadata",
        type=Path,
        help="locked build metadata from the build container",
    )
    args = parser.parse_args()
    binary = args.engine.resolve()
    version = subprocess.check_output([binary, "--version"], text=True).split()[-1]
    name = (
        f"edgewalker-{version}-{platform.system().lower()}-{platform.machine().lower()}"
    )
    args.output.mkdir(parents=True, exist_ok=True)
    dest = args.output / (name + ".tar.gz")
    with tempfile.TemporaryDirectory(dir=args.output) as tmp:
        archive = Path(tmp) / (name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as t:
            for src, target in [
                (binary, "bin/edgewalker-native"),
                *dependency_notices(Path(tmp), args.cargo_metadata),
                (ROOT / "edgewalker", "edgewalker"),
                (ROOT / "README.md", "README.md"),
                (ROOT / "docs/brief.md", "docs/brief.md"),
                (ROOT / "docs/operator.md", "docs/operator.md"),
                *[
                    (ROOT / "src" / f, "src/" + f)
                    for f in [
                        "operator_cli.py",
                        "replay.py",
                        "records.py",
                        "telemetry.py",
                        "html_report.py",
                    ]
                ],
                *[
                    (p, "examples/" + p.name)
                    for p in sorted((ROOT / "examples").glob("*.json"))
                ],
            ]:
                t.add(src, name + "/" + target, recursive=False)
        with tarfile.open(archive) as t:
            # Smoke-test only the regular files just placed under this package
            # root. Bookworm build images may predate tarfile extraction filters.
            for member in t:
                path = Path(member.name)
                if not member.isfile() or path.parts[:1] != (name,) or ".." in path.parts:
                    raise ValueError("unexpected package member")
                destination = Path(tmp) / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                with t.extractfile(member) as source:
                    destination.write_bytes(source.read())
                destination.chmod(member.mode & 0o755)
        cli = Path(tmp) / name / "edgewalker"
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"EDGEWALKER_ENGINE", "EDGEWALKER_CONFIG"}
        }
        subprocess.run([cli, "--help"], check=True, env=env, stdout=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory() as smoke:
            config = Path(smoke) / "key.json"
            with config.open("x") as f:
                config.chmod(0o600)
                subprocess.run([cli, "keygen"], stdout=f, check=True, env=env)
            assert len(json.loads(config.read_text())["key"]) == 16
            env["EDGEWALKER_CONFIG"] = str(config)
            server = subprocess.Popen(
                [cli, "server", "--listen", "127.0.0.1:0", "--capacity", "400000"],
                stdout=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                if not select.select([server.stdout], [], [], 5)[0]:
                    raise RuntimeError("packaged server startup timed out")
                address = json.loads(server.stdout.readline())["listening"]
                run = Path(smoke) / "run"
                subprocess.run(
                    [
                        cli,
                        "run",
                        "--connect",
                        address,
                        "--mode",
                        "udp",
                        "--forward-max",
                        "320000",
                        "--window-ms",
                        "500",
                        "--duration",
                        "2",
                        "--output",
                        run,
                    ],
                    env=env,
                    check=True,
                    timeout=10,
                )
                result = json.loads(
                    subprocess.check_output([cli, "report", run], env=env)
                )
                assert (
                    result["directions"]["forward"]["phases"]["whole"]["received_bytes"]
                    > 0
                )
            finally:
                server.terminate()
                server.wait(timeout=5)
                server.stdout.close()
        archive.replace(dest)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    dest.with_suffix(dest.suffix + ".sha256").write_text(f"{digest}  {dest.name}\n")
    print(dest)


if __name__ == "__main__":
    main()
