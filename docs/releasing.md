# Package every release

For every release and prerelease, build four archives from one source revision. Each archive needs its own SHA-256 file. Keep the compatibility build alongside the standard Linux build.

| Host or builder | Target suffix | Packaging command after a locked release build |
|---|---|---|
| Apple silicon macOS | darwin-arm64 | `python3 scripts/package.py --output dist` |
| ARM64 Linux | linux-aarch64 | `python3 scripts/package.py --output dist` |
| Standard x86-64 Linux | linux-x86_64 | `python3 scripts/package.py --output dist` |
| x86-64 Linux with glibc 2.17 | linux-x86_64-glibc2.17 | `python3 scripts/package.py --glibc-2-17 --python-runtime "$runtime" --output dist` |

Use Python 3.11 or later. Run `cargo build --release --locked` in each target environment. For the compatibility target, compile and test in the glibc 2.17 environment. The packager verifies the extracted launcher and authenticated loopback delivery. Run `python3 tests/package_inputs.py ARCHIVE NEW_RESULTS_DIRECTORY` for each archive in its target environment.

Collect the four archives and four matching checksums in a fresh directory. Run `make check-release RELEASE_DIR=/path/to/assets` against the release checkout before upload. It checks all four targets, the checksum and archive root for each, the executable, current guides, and Linux systemd files. Repeat this check on fresh downloads after publication. Use local builds to avoid hosted Actions minutes.

For the compatibility client, download the pinned install-only stripped CPython 3.11.18 x86-64 GNU/Linux build once. Set `runtime` to that archive's local path in the builder. The packager verifies its SHA-256 before copying the interpreter and standard library into the compatibility package. The runtime notices in `third-party/python-runtime` come from the matching full build. Refresh the version, digest, notices and clean-host smoke test together when updating this runtime. [CPython build assets](https://github.com/astral-sh/python-build-standalone/releases/tag/20261124)

```sh
runtime=cpython-3.11.18+20261124-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
curl -fLO "https://github.com/astral-sh/python-build-standalone/releases/download/20261124/$runtime"
```
