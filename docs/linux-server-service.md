# Linux compatibility package and server startup

Use the Linux x86-64 glibc 2.17 package on a 64-bit Intel or AMD host with glibc 2.17 or later and the system libgcc runtime. The **native gateway server and key generator need no Python**. The interactive client, calibration and reports require Python 3.11 or later. The client can run on a separate machine.

## Verify and extract

Run these commands on the server. Expect `x86_64` and `glibc 2.17` or newer. From a fresh directory, download the compatibility archive and its checksum. For an offline server, transfer both files and run the verification and extraction commands there.

```sh
uname -m
getconf GNU_LIBC_VERSION
archive=edgewalker-0.2.2-rc.2-linux-x86_64-glibc2.17.tar.gz
release=https://github.com/wattsie/EdgeWalker/releases/download/v0.2.2-rc.2
curl -fLO "$release/$archive" &&
  curl -fLO "$release/$archive.sha256" &&
  sha256sum -c "$archive.sha256" &&
  tar -xzf "$archive" && cd "${archive%.tar.gz}" &&
  ./bin/edgewalker-native --version
```

The embedded engine version is 0.2.2-rc.2. Keep this directory available for the service files and field guide.

## Install the gateway

Run these commands from the extracted directory on a systemd host. They install one native executable and a system service. Use a dedicated unprivileged account.

```sh
getent group edgewalker >/dev/null || sudo groupadd --system edgewalker
id edgewalker >/dev/null 2>&1 || sudo useradd --system --gid edgewalker \
  --no-create-home --home-dir /nonexistent --shell /sbin/nologin edgewalker
sudo install -d -m 0755 /opt/edgewalker-server/bin
sudo install -m 0755 bin/edgewalker-native /opt/edgewalker-server/bin/
sudo install -d -o root -g edgewalker -m 0750 /etc/edgewalker
sudo test -e /etc/edgewalker/server.env || \
  sudo install -m 0644 deploy/systemd/server.env.example /etc/edgewalker/server.env
sudo install -m 0644 deploy/systemd/edgewalker-server.service /etc/systemd/system/
```

Provision the shared key once. If the client already has a configuration file, securely transfer it and install it as `/etc/edgewalker/config.json` with owner `root`, group `edgewalker` and mode `0640`. Otherwise run the following to generate a new one on the gateway. Provision the same file securely on the client.

```sh
sudo sh -ec 'if [ ! -e /etc/edgewalker/config.json ]; then
  umask 077
  tmp=$(mktemp /etc/edgewalker/key.XXXXXX)
  trap '\''rm -f "$tmp"'\'' EXIT
  /opt/edgewalker-server/bin/edgewalker-native keygen > "$tmp"
  chgrp edgewalker "$tmp"
  chmod 0640 "$tmp"
  ln "$tmp" /etc/edgewalker/config.json
fi'
```

Edit the listen address, capacity and data ports before starting. The example binds only to loopback and admits 1 Mbps per direction. Use the server's assigned test-interface IP for remote clients. Allow the selected TCP control port and TCP/UDP data port range from the authorized peer. Stop any foreground EdgeWalker server using those ports.

```sh
sudoedit /etc/edgewalker/server.env
sudo systemctl daemon-reload
sudo systemctl enable edgewalker-server.service
sudo systemctl start edgewalker-server.service
systemctl is-enabled edgewalker-server.service
systemctl is-active edgewalker-server.service
sudo journalctl -u edgewalker-server.service -n 20 --no-pager
```

Expect `enabled`, `active` and a journal entry showing the chosen listening address. Enabling the unit starts it on later boots. The restart policy retries after a process failure. Use the 1 Mbps client smoke test in `docs/validation-tests.md` to verify delivery. On the next planned reboot, check `is-active` and the journal again. To apply changes to the environment or key, restart the service between sessions.

## Four clients on one service

Use the same server address and TCP control port (5207 in the example) for each client. The service passes the inclusive data range from `server.env` to the native server. Four 1 Mbps forward tests require at least 4 Mbps of approved transmit capacity and four free data-port numbers. Adjust the capacity only within the link owner's approved budget. The supplied range 5210–5220 has room for four sessions. After a configuration change, restart the service between tests and check the control listener with `sudo ss -ltnp 'sport = :5207'`.

On filtered paths, permit the authorized clients to reach TCP 5207 and the configured data range using TCP, UDP or both according to the mode. Preserve the data port numbers when forwarding through NAT. See the [operator guide](operator.md#simultaneous-clients-and-data-ports) for join and release behavior.

## Stop or uninstall

```sh
sudo systemctl stop edgewalker-server.service
sudo systemctl disable edgewalker-server.service
sudo rm -f /etc/systemd/system/edgewalker-server.service
sudo systemctl daemon-reload
sudo rm -f /opt/edgewalker-server/bin/edgewalker-native
```

The configuration and key remain in `/etc/edgewalker` for reuse or separate retirement.
