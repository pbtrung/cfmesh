# cfmesh

Generate Cloudflare Zero Trust WireGuard configs without `warp-cli` or Docker.

`cfmesh.py` enrolls a device into your Zero Trust organization using an Access
service token and writes a plain WireGuard `.conf` you can use with `wg-quick`
or any WireGuard client. It can also rotate the key of an already-enrolled
device while keeping the same device and IP.

The generated config is IPv4-only, mesh-only (`AllowedIPs = 100.96.0.0/12` by
default) and does not override DNS.

> **Note:** this uses Cloudflare's undocumented client API, which may change
> between client releases. Override the base URL with `CF_CLIENT_API` if needed.

## Install

Requires Python 3.8+ and the `cryptography` package. Bringing the tunnel up
with `wg-quick` also needs `wireguard-tools`; on Arch Linux:

```bash
sudo pacman -S wireguard-tools
```

```bash
git clone <repo-url> cfmesh
cd cfmesh
python3 -m venv .venv
source .venv/bin/activate        # fish: source .venv/bin/activate.fish
pip install cryptography
```

### Arch Linux package

`arch/PKGBUILD` builds `cfmesh-git` from the latest commit on GitHub. It
installs `cfmesh` to `/usr/bin`, the systemd units, and pulls in
`python-cryptography` and `wireguard-tools`.

```bash
cd arch
makepkg -si
```

With the package, run `cfmesh` instead of `python3 cfmesh.py`.

## Setup

1. In the Zero Trust dashboard, create an **Access service token**.
2. Add a device enrollment rule with the **Service Auth** action that allows
   that token.
3. Export the token credentials:

```bash
export CF_CLIENT_ID=<client-id>.access
export CF_CLIENT_SECRET=<client-secret>
```

## Usage

### Enroll a new device

```bash
python3 cfmesh.py enroll <team> --name my-node --out ./configs
```

- `<team>` — your team name (`<team>.cloudflareaccess.com`)
- `--name` — device name shown in the dashboard (default `wg-node`)
- `--out` — output directory (default `.`)
- `--allowed-ips` — comma-separated AllowedIPs (default `100.96.0.0/12`)

This writes two files:

- `cfmesh-<id>.conf` — the WireGuard config
- `cfmesh-<id>.reg.json` — registration state (ID, token, private key), needed
  for `rotate`

Both contain secrets and are created with `0600` permissions. Keep them out of
version control.

Bring the tunnel up:

```bash
sudo cp configs/cfmesh-<id>.conf /etc/wireguard/cfmesh.conf
sudo wg-quick up cfmesh
```

### Rotate a device key

```bash
python3 cfmesh.py rotate configs/cfmesh-<id>.reg.json
```

- `--out` — output directory (default: next to the reg file)
- `--allowed-ips` — override AllowedIPs (default: keep previous)

Rotation does not need `CF_CLIENT_ID`/`CF_CLIENT_SECRET`. The old key stops
working immediately, so deploy the new `.conf` and restart the tunnel.

### Daily rotation with systemd

`systemd/` contains a service and timer that rotate the key every day at
00:00:00 UTC, install the new config as `/etc/wireguard/<iface>.conf` and
restart `wg-quick@<iface>`. The service waits for the network to be online and
retries up to 3 times, 5 minutes apart. A run missed while the machine was off
starts at the next boot.

The service runs `/usr/bin/cfmesh`, so install the [Arch Linux
package](#arch-linux-package) first, or install the script there yourself
(this needs `cryptography` available to the system `python3`, e.g.
`sudo pacman -S python-cryptography`):

```bash
sudo install -m 755 cfmesh.py /usr/bin/cfmesh
sudo cp systemd/cfmesh.service systemd/cfmesh.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Then enroll, configure and enable the timer:

```bash
# Enroll into /etc/cfmesh and set the registration ID
sudo mkdir -p /etc/cfmesh
sudo -E cfmesh enroll <team> --out /etc/cfmesh
printf 'CFMESH_REG_ID=<id>\nCFMESH_IFACE=cfmesh\n' |
  sudo install -m 600 /dev/stdin /etc/cfmesh/cfmesh.env

# Bring the tunnel up once, then enable the timer
sudo install -m 600 /etc/cfmesh/cfmesh-<id>.conf /etc/wireguard/cfmesh.conf
sudo systemctl enable --now wg-quick@cfmesh
sudo systemctl enable --now cfmesh.timer
```

Check it with `systemctl list-timers cfmesh.timer` and
`journalctl -u cfmesh.service`.

## Environment variables

| Variable           | Used by | Description                                   |
| ------------------ | ------- | --------------------------------------------- |
| `CF_CLIENT_ID`     | enroll  | Access service token client ID                |
| `CF_CLIENT_SECRET` | enroll  | Access service token client secret            |
| `CF_CLIENT_API`    | both    | Optional override of the client API base URL  |

## License

See [LICENSE](LICENSE).
