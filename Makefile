.PHONY: build check package
build:
	cargo build --release --locked

check:
	cargo fmt --check
	cargo clippy --locked --all-targets -- -D warnings
	cargo test --locked
	cargo build --locked
	python3 -B -m unittest discover -s tests -v
	python3 -B tests/integration/native_loopback.py -v
	python3 -B tests/integration/operator_loopback.py -v
	python3 -B tests/integration/calibration_loopback.py -v

package: build
	python3 -B scripts/package.py --output dist

.PHONY: check-release
RELEASE_DIR ?= dist
check-release:
	python3 -B scripts/check_release.py "$(RELEASE_DIR)"
