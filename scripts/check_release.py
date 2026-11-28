"""Verify all supported archives and their checksums before publication."""

import argparse
import hashlib
from pathlib import Path
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("darwin-arm64", "linux-aarch64", "linux-x86_64", "linux-x86_64-glibc2.17")
SERVICE_FILES = (
    "deploy/systemd/edgewalker-server.service",
    "deploy/systemd/server.env.example",
)


def verify(directory):
    version = tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"]
    for target in TARGETS:
        stem = f"edgewalker-{version}-{target}"
        archive = directory / f"{stem}.tar.gz"
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        checksum = directory / f"{archive.name}.sha256"
        if checksum.read_text().split() != [digest, archive.name]:
            raise ValueError(f"incorrect checksum or filename in {checksum.name}")
        files = ["README.md", "docs/validation-tests.md", "docs/linux-server-service.md"]
        if target.startswith("linux-"):
            files.extend(SERVICE_FILES)
        with tarfile.open(archive) as package:
            directories = {m.name for m in package if m.isdir() and m.mode == 0o755}
            required = {str(parent) for m in package if m.isfile() for parent in Path(m.name).parents if parent != Path(".")}
            if not required <= directories:
                raise ValueError(f"{archive.name} lacks explicit traversable directories")
            for path in files:
                member = package.getmember(f"{stem}/{path}")
                if not member.isfile() or package.extractfile(member).read() != (ROOT / path).read_bytes():
                    raise ValueError(f"{archive.name} has incorrect {path}")
            if target.endswith("glibc2.17"):
                runtime = package.getmember(f"{stem}/runtime/bin/python3.11")
                if not runtime.isfile() or not runtime.mode & 0o111:
                    raise ValueError("missing compatibility Python interpreter")
                package.getmember(f"{stem}/third-party/python-runtime/LICENSE.cpython.txt")
            engine = package.getmember(f"{stem}/bin/edgewalker-native")
            if not engine.isfile() or not engine.size or not engine.mode & 0o111:
                raise ValueError(f"{archive.name} has no executable engine")
        print(f"OK {archive.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        verify(args.directory)
    except (OSError, KeyError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"Release check failed: {error}\n")
