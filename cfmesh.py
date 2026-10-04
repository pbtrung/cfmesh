#!/usr/bin/env python3
"""
cfmesh.py - Zero Trust WireGuard configs without warp-cli or Docker.

Commands
  enroll <team>       Register a new device with a fresh key and write a .conf
  rotate <reg.json>   Replace the WireGuard key of an existing device (same device, same IP)
  status <reg.json>   Show what Cloudflare reports for an existing device

Requires: pip install cryptography
Env:      CF_CLIENT_ID, CF_CLIENT_SECRET  (Access service token; enroll only)
          CF_CLIENT_API (optional override of the client API base URL / version)
Output:   IPv4-only, mesh-only config with no DNS override.
"""

import argparse, base64, datetime, json, os, re, sys, uuid
import urllib.request, urllib.error
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

# Undocumented client API. The version segment changes between client releases.
CLIENT_API = os.environ.get(
    "CF_CLIENT_API", "https://zero-trust-client.cloudflareclient.com/v0a2158"
)
CLIENT_HEADERS = {
    "User-Agent": "okhttp/3.12.1",
    "CF-Client-Version": "a-6.30-3596",
    "Content-Type": "application/json; charset=UTF-8",
}


def die(msg):
    sys.exit(f"Error: {msg}")


def http(method, url, headers=None, body=None, opener=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        r = (opener or urllib.request.build_opener()).open(req, timeout=30)
        return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def access_jwt(team):
    cid = os.environ.get("CF_CLIENT_ID") or die("CF_CLIENT_ID not set")
    sec = os.environ.get("CF_CLIENT_SECRET") or die("CF_CLIENT_SECRET not set")
    st, hdrs, raw = http(
        "GET",
        f"https://{team}.cloudflareaccess.com/warp",
        {
            "CF-Access-Client-Id": cid,
            "CF-Access-Client-Secret": sec,
            "User-Agent": CLIENT_HEADERS["User-Agent"],
        },
        opener=urllib.request.build_opener(_NoRedirect),
    )
    blob = (hdrs.get("Location") or "") + raw.decode(errors="ignore")
    m = re.search(r"token=([A-Za-z0-9._-]+)", blob)
    if not m:
        for c in hdrs.get_all("Set-Cookie") or []:
            m = re.match(r"CF_Authorization=([^;]+)", c)
            if m:
                break
    if not m:
        die(
            f"no Access JWT in response (HTTP {st}); check the Service Auth enrollment rule"
        )
    return m.group(1)


def b64(b):
    return base64.b64encode(b).decode()


def new_keypair():
    priv = X25519PrivateKey.generate()
    return (
        b64(
            priv.private_bytes(
                ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()
            )
        ),
        b64(priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)),
    )


def with_port(h):
    if h.startswith("["):
        h = h[1 : h.index("]")]
    elif h.count(":") == 1:
        h = h.split(":")[0]
    return f"[{h}]:2408" if ":" in h else f"{h}:2408"


def parse_result(raw):
    res = json.loads(raw)
    return res.get("result", res)


def write_files(out, reg_id, token, priv_b64, res, allowed_ips):
    cfg = res["config"]
    peer, addr = cfg["peers"][0], cfg["interface"]["addresses"]
    ep = peer["endpoint"]
    v4_ep = with_port(ep["v4"]) if ep.get("v4") else with_port(ep["host"])

    os.umask(0o077)
    base = os.path.join(out, f"cfmesh-{reg_id}")
    tmp = base + ".reg.json.tmp"
    with open(tmp, "w") as f:
        json.dump(
            {
                "id": reg_id,
                "token": token,
                "private_key": priv_b64,
                "client_api": CLIENT_API,
                "allowed_ips": allowed_ips,
                "raw": res,
            },
            f,
            indent=2,
        )
    os.replace(tmp, base + ".reg.json")
    with open(base + ".conf", "w") as f:
        f.write(f"""# Registration ID: {reg_id}
[Interface]
PrivateKey = {priv_b64}
Address = {addr['v4']}/32
MTU = 1420

[Peer]
PublicKey = {peer['public_key']}
AllowedIPs = {allowed_ips}
Endpoint = {v4_ep}
PersistentKeepalive = 25
""")
    print(f"Saved {base}.conf  (device IP {addr['v4']})")
    return addr["v4"]


def cmd_enroll(a):
    jwt = access_jwt(a.team)
    priv_b64, pub_b64 = new_keypair()
    body = {
        "key": pub_b64,
        "key_type": "curve25519",
        "tunnel_type": "wireguard",
        "install_id": "",
        "fcm_token": "",
        "model": "Linux",
        "type": "Linux",
        "name": a.name,
        "serial_number": str(uuid.uuid4()),
        "locale": "en_US",
        "tos": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    st, _, raw = http(
        "POST",
        f"{CLIENT_API}/reg",
        {**CLIENT_HEADERS, "CF-Access-Jwt-Assertion": jwt},
        body,
    )
    if st >= 300:
        die(f"/reg -> HTTP {st}: {raw[:400]!r}")
    res = parse_result(raw)
    write_files(
        a.out, res.get("id", "unknown"), res.get("token"), priv_b64, res, a.allowed_ips
    )


def load_state(reg_file):
    try:
        state = json.load(open(reg_file))
    except (OSError, ValueError) as e:
        die(f"cannot read {reg_file}: {e}")
    if not state.get("id") or not state.get("token"):
        die("reg file has no id/token; re-enroll this device instead")
    api = os.environ.get("CF_CLIENT_API") or state.get("client_api") or CLIENT_API
    return state, api


def device_ip(res):
    return res.get("config", {}).get("interface", {}).get("addresses", {}).get("v4")


def public_key(priv_b64):
    priv = X25519PrivateKey.from_private_bytes(base64.b64decode(priv_b64))
    return b64(priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw))


def redact(obj):
    if isinstance(obj, dict):
        return {
            k: (
                "<redacted>"
                if k in ("token", "private_key") or "secret" in k
                else redact(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def cmd_rotate(a):
    state, api = load_state(a.reg_file)
    reg_id, token = state["id"], state["token"]
    old_ip = device_ip(state.get("raw", {}))

    priv_b64, pub_b64 = new_keypair()
    st, _, raw = http(
        "PATCH",
        f"{api}/reg/{reg_id}",
        {**CLIENT_HEADERS, "Authorization": f"Bearer {token}"},
        {"key": pub_b64, "key_type": "curve25519", "tunnel_type": "wireguard"},
    )
    if st >= 300:
        die(
            f"PATCH /reg/{reg_id} -> HTTP {st}: {raw[:400]!r}  (old key still valid, nothing changed)"
        )
    res = parse_result(raw)
    if "config" not in res:  # some versions return only a status; fetch the config
        st, _, raw = http(
            "GET",
            f"{api}/reg/{reg_id}",
            {**CLIENT_HEADERS, "Authorization": f"Bearer {token}"},
        )
        if st >= 300:
            die(f"key changed but GET /reg -> HTTP {st}; new private key: {priv_b64}")
        res = parse_result(raw)
    if res.get("key") and res["key"] != pub_b64:
        die(f"server did not accept the new key (still {res['key']}); nothing written")

    out = a.out or os.path.dirname(os.path.abspath(a.reg_file))
    allowed = a.allowed_ips or state.get("allowed_ips") or "100.96.0.0/12"
    new_ip = write_files(out, reg_id, token, priv_b64, res, allowed)
    if old_ip and new_ip != old_ip:
        print(f"Warning: device IP changed {old_ip} -> {new_ip}", file=sys.stderr)
    print("Old key is now invalid: deploy the new .conf and restart the tunnel.")


def cmd_status(a):
    state, api = load_state(a.reg_file)
    reg_id = state["id"]
    st, _, raw = http(
        "GET",
        f"{api}/reg/{reg_id}",
        {**CLIENT_HEADERS, "Authorization": f"Bearer {state['token']}"},
    )
    if st >= 300:
        die(f"GET /reg/{reg_id} -> HTTP {st}: {raw[:400]!r}")
    res = parse_result(raw)
    if a.json:
        print(json.dumps(redact(res), indent=2))
        return

    cfg = res.get("config", {})
    peer = (cfg.get("peers") or [{}])[0]
    local_ip = device_ip(state.get("raw", {}))
    local_key = public_key(state["private_key"]) if state.get("private_key") else None
    rows = [
        ("Device ID", reg_id),
        ("Device IP", device_ip(res)),
        ("Device IPv6", cfg.get("interface", {}).get("addresses", {}).get("v6")),
        ("Server key", res.get("key")),
        ("Local key", local_key),
        ("Peer key", peer.get("public_key")),
        ("Endpoint", peer.get("endpoint", {}).get("v4")),
    ]
    # Surface any other scalar fields (status flags, timestamps) as-is
    shown = {"id", "key", "token", "config"}
    for scope, obj in (("", res), ("account.", res.get("account") or {})):
        for k, v in obj.items():
            if k not in shown and not isinstance(v, (dict, list)):
                rows.append((scope + k, v))
    w = max(len(k) for k, _ in rows)
    for k, v in rows:
        if v is not None and v != "":
            print(f"{k:<{w}}  {v}")

    ok = True
    if local_key and res.get("key") and local_key != res["key"]:
        print(
            "Warning: local key does not match the server key; rotate or re-enroll",
            file=sys.stderr,
        )
        ok = False
    if local_ip and device_ip(res) and local_ip != device_ip(res):
        print(
            f"Warning: device IP changed {local_ip} -> {device_ip(res)}; run rotate to refresh the .conf",
            file=sys.stderr,
        )
        ok = False
    if not ok:
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    s = p.add_subparsers(dest="cmd", required=True)
    e = s.add_parser("enroll", help="register a new device")
    e.add_argument("team", help="Zero Trust team name (<team>.cloudflareaccess.com)")
    e.add_argument(
        "--name", default="wg-node", help="device name shown in the dashboard"
    )
    e.add_argument("--out", default=".", help="output directory")
    e.add_argument(
        "--allowed-ips", default="100.96.0.0/12", help="comma-separated AllowedIPs"
    )
    e.set_defaults(fn=cmd_enroll)
    r = s.add_parser("rotate", help="replace the key of an existing device")
    r.add_argument("reg_file", help="cfmesh-<id>.reg.json from enroll")
    r.add_argument("--out", help="output directory (default: next to reg file)")
    r.add_argument("--allowed-ips", help="override AllowedIPs (default: keep previous)")
    r.set_defaults(fn=cmd_rotate)
    t = s.add_parser("status", help="show what Cloudflare reports for a device")
    t.add_argument("reg_file", help="cfmesh-<id>.reg.json from enroll")
    t.add_argument(
        "--json", action="store_true", help="print the full response (secrets redacted)"
    )
    t.set_defaults(fn=cmd_status)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
