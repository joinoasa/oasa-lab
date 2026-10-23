"""Device inventory backends, and Cloudflare Access JWT checking."""

import asyncio
import http.server
import json
import socket
import threading
import time

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from oasa_lab.service.config import Device
from oasa_lab.service.inventory import SimulatedInventory, UsbSysfsInventory

from .conftest import start, stop


def test_simulated_inventory_follows_the_config_and_overrides():
    devs = [Device("pluto", "pluto"), Device("hub", "hub", simulated_present=False)]
    inv = SimulatedInventory(devs)
    got = {d.id: d.present for d in asyncio.run(inv.scan())}
    assert got == {"pluto": True, "hub": False}
    inv.overrides["hub"] = True
    assert {d.id: d.present for d in asyncio.run(inv.scan())}["hub"] is True


def fake_sysfs(root, devices):
    for i, (vp, serial) in enumerate(devices):
        d = root / f"1-{i}"
        d.mkdir(parents=True)
        (d / "idVendor").write_text(vp.split(":")[0] + "\n")
        (d / "idProduct").write_text(vp.split(":")[1] + "\n")
        if serial:
            (d / "serial").write_text(serial + "\n")


def test_usb_inventory_reads_sysfs_and_gives_two_identical_boards_one_match_each(tmp_path):
    fake_sysfs(tmp_path, [("0bda:2838", "00000001"), ("10c4:ea60", "0001"), ("10c4:ea60", "0001")])
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    devs = [Device("rtlsdr", "rtlsdr", usb="0bda:2838"), Device("heltec-a", "serial-radio", usb="10c4:ea60"),
            Device("heltec-b", "serial-radio", usb="10c4:ea60"), Device("heltec-c", "lora", usb="10c4:ea60"),
            Device("hub", "hub", usb="2109:2817"), Device("pluto", "pluto", tcp=f"127.0.0.1:{port}"),
            Device("pluto2", "pluto", tcp="127.0.0.1:9"), Device("nothing", "x")]
    got = {d.id: d.present for d in asyncio.run(UsbSysfsInventory(devs, tmp_path).scan())}
    srv.close()
    assert got == {"rtlsdr": True, "heltec-a": True, "heltec-b": True, "heltec-c": False, "hub": False,
                   "pluto": True, "pluto2": False, "nothing": False}


def test_usb_inventory_matches_a_serial_number(tmp_path):
    fake_sysfs(tmp_path, [("10c4:ea60", "AAA"), ("10c4:ea60", "BBB")])
    devs = [Device("a", "serial-radio", usb="10c4:ea60", serial="BBB"), Device("b", "lora", usb="10c4:ea60", serial="ZZZ")]
    got = {d.id: d.present for d in asyncio.run(UsbSysfsInventory(devs, tmp_path).scan())}
    assert got == {"a": True, "b": False}


def test_usb_inventory_with_no_sysfs_reports_everything_missing(tmp_path):
    got = asyncio.run(UsbSysfsInventory([Device("a", "x", usb="1111:2222")], tmp_path / "nope").scan())
    assert [d.present for d in got] == [False]


# --- Cloudflare Access JWT -------------------------------------------------------------------------
TEAM = "lab.cloudflareaccess.com"
AUD = "aud-tag-123"


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="k1", alg="RS256", use="sig")
    body = json.dumps({"keys": [jwk]}).encode()

    class H(http.server.BaseHTTPRequestHandler):  # Cloudflare's /cdn-cgi/access/certs, stood in for
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield key, f"http://127.0.0.1:{srv.server_address[1]}/certs"
    srv.shutdown()


@pytest.fixture
def access_svc(make_service, keys):
    _, jwks = keys
    return make_service(extra={"cloudflare_access": {"team_domain": TEAM, "audience": AUD, "jwks_url": jwks}})


def make_jwt(key, **over):
    now = int(time.time())
    claims = {"aud": [AUD], "iss": f"https://{TEAM}", "email": "owner@example.invalid", "iat": now, "exp": now + 300,
              "sub": "x", **over}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def test_a_valid_access_jwt_for_a_known_email_gets_in(access_svc, keys):
    t = make_jwt(keys[0])
    r = httpx.get(f"{access_svc.url}/api/lab", headers={"Cf-Access-Jwt-Assertion": t})
    assert r.status_code == 200
    row = [x for x in access_svc.audit() if x.get("path") == "/api/lab" and x.get("status") == 200][-1]
    assert row["actor"] == "owner" and row["via"] == "cf-access"
    # the role comes from the config: this email belongs to the admin
    assert httpx.post(f"{access_svc.url}/api/pause", headers={"Cf-Access-Jwt-Assertion": t}).status_code == 200


@pytest.mark.parametrize("what", ["unknown_email", "wrong_audience", "wrong_issuer", "expired", "wrong_key", "no_email", "alg_none", "garbage"])
def test_bad_access_jwts_are_rejected(access_svc, keys, what):
    key = keys[0]
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    t = {
        "unknown_email": lambda: make_jwt(key, email="stranger@example.invalid"),
        "wrong_audience": lambda: make_jwt(key, aud=["other"]),
        "wrong_issuer": lambda: make_jwt(key, iss="https://evil.cloudflareaccess.com"),
        "expired": lambda: make_jwt(key, exp=int(time.time()) - 10, iat=int(time.time()) - 100),
        "wrong_key": lambda: make_jwt(other),
        "no_email": lambda: make_jwt(key, email=""),
        "alg_none": lambda: jwt.encode({"aud": [AUD], "iss": f"https://{TEAM}", "email": "owner@example.invalid"}, None, algorithm="none"),
        "garbage": lambda: "not.a.jwt",
    }[what]()
    assert httpx.get(f"{access_svc.url}/api/lab", headers={"Cf-Access-Jwt-Assertion": t}).status_code == 401


def test_an_identity_header_alone_is_not_identity(access_svc):
    h = {"Cf-Access-Authenticated-User-Email": "owner@example.invalid", "X-Forwarded-User": "owner"}
    assert httpx.get(f"{access_svc.url}/api/lab", headers=h).status_code == 401


def test_access_jwt_does_not_unlock_the_connector(access_svc, keys):
    """The connector (MCP) takes OAuth or API tokens only; a browser-style Access JWT is not enough."""
    r = httpx.post(f"{access_svc.url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Cf-Access-Jwt-Assertion": make_jwt(keys[0]), "Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
