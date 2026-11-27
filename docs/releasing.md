# Package every release

For every release and prerelease, build four archives from one source revision. Each archive needs its own SHA-256 file. Keep the compatibility build alongside the standard Linux build.

| Host or builder | Target suffix | Packaging command after a locked release build |
|---|---|---|
| Apple silicon macOS | darwin-arm64 | `python3 scripts/package.py --output dist` |
| ARM64 Linux | linux-aarch64 | `python3 scripts/package.py --output dist` |
| Standard x86-64 Linux | linux-x86_64 | `python3 scripts/package.py --output dist` |
| x86-64 Linux with glibc 2.17 | linux-x86_64-glibc2.17 | `python3 scripts/package.py --glibc-2-17 --output dist` |

Use Python 3.11 or later. Run `cargo build --release --locked` in each target environment. For the compatibility target, compile and test in the glibc 2.17 environment. The packager verifies the extracted launcher and authenticated loopback delivery. Run `python3 tests/package_inputs.py ARCHIVE NEW_RESULTS_DIRECTORY` for each archive in its target environment.

Collect the four archives and four matching checksums in a fresh directory. Run `make check-release RELEASE_DIR=/path/to/assets` against the release checkout before upload. It checks all four targets, the checksum and archive root for each, the executable, current guides, and Linux systemd files. Repeat this check on fresh downloads after publication. Use local builds to avoid hosted Actions minutes.
