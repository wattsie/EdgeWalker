"""Add the pinned relocatable CPython runtime to the glibc 2.17 client package."""
import hashlib
from pathlib import Path
import tarfile

FILENAME = "cpython-3.11.18+20261124-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
SHA256 = "d873f435e58d396a18858d4f52a335f1abe8cea385cb9d411ec511f958249d54"


def add_runtime(package, package_root, archive, notices):
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
            raise ValueError("unexpected compatibility Python archive checksum")
    # This build links its builtin modules into the interpreter. Copy only the
    # interpreter and standard-library sources used from its relocatable prefix.
    with tarfile.open(archive) as upstream:
        for item in upstream:
            path = Path(item.name)
            if not item.isfile() or path.parts[:1] != ("python",) or ".." in path.parts:
                continue
            library = path.parts[:3] == ("python", "lib", "python3.11")
            if item.name != "python/bin/python3.11" and not (
                library and "site-packages" not in path.parts
                and "ensurepip" not in path.parts and path.suffix in (".so", ".py", ".txt")
            ):
                continue
            with upstream.extractfile(item) as source:
                item.name = f"{package_root}/runtime/{path.relative_to('python')}"
                item.uid = item.gid = 0
                item.uname = item.gname = ""
                package.addfile(item, source)
    for path in sorted(notices.glob("LICENSE.*.txt")):
        package.add(path, f"{package_root}/third-party/python-runtime/{path.name}", recursive=False)
