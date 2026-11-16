"""S&D #193: captive portal on the setup hotspot."""
import http.client
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import captive  # noqa: E402
from netman import MockNetworkManager  # noqa: E402
from router import Router  # noqa: E402


def test_dns_conf_points_the_probe_names_at_the_hotspot_interface(tmp_path):
    path = captive.write_dns_conf("wlan0", str(tmp_path / "c.conf"))
    text = open(path).read()
    assert "interface-name=captive.apple.com,wlan0/4" in text
    assert "interface-name=connectivitycheck.gstatic.com,wlan0/4" in text
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert len(lines) == len(captive.PROBE_HOSTS)
    assert all(line.startswith("interface-name=") for line in lines)   # nothing else, no address=/#/


def test_dns_conf_off_is_comment_only(tmp_path):
    path = captive.write_dns_conf(None, str(tmp_path / "c.conf"))
    assert all(line.startswith("#") for line in open(path).read().splitlines())


def test_bad_interface_name_is_refused(tmp_path):
    with pytest.raises(ValueError):
        captive.dns_conf_text("wlan0\naddress=/#/1.2.3.4")


def _get(port, path="/hotspot-detect.html", host="captive.apple.com"):
    conn = http.client.HTTPConnection("127.0.0.2", port, timeout=5)
    conn.request("GET", path, headers={"Host": host})
    r = conn.getresponse()
    r.read()
    return r


@pytest.fixture()
def portal(tmp_path):
    c = captive.Captive(8080, http_port=0, dns_path=str(tmp_path / "c.conf"))
    # 127.0.0.2 stands in for the hotspot address (any non-loopback-1 local address the
    # phone reaches); setup_url refuses loopback, so allow it for the test only
    c.setup_url = lambda addr: f"http://{addr}:8080/"
    srv = c.start_listener(host="127.0.0.2")
    if srv is None:
        pytest.skip("cannot bind 127.0.0.2 here")
    yield c, srv.server_address[1]
    c.stop_listener()


def test_listener_redirects_probes_to_the_setup_page_while_captive(portal):
    c, port = portal
    c.prepare("wlan0")
    r = _get(port)
    assert r.status == 302
    assert r.getheader("Location") == "http://127.0.0.2:8080/"
    r = _get(port, "/generate_204", "connectivitycheck.gstatic.com")
    assert r.status == 302          # Android wants 204 for "online"; anything else opens sign-in


def test_listener_answers_404_when_captive_is_off(portal):
    c, port = portal
    c.prepare(None)
    assert _get(port).status == 404


def test_setup_url_never_points_at_loopback():
    c = captive.Captive(8080, http_port=0)
    assert c.setup_url("127.0.0.1") is None
    assert c.setup_url("10.42.0.1") == "http://10.42.0.1:8080/"


class FakeCfg:
    def hotspot_password(self):
        return "Str0ng-pass-193"

    def get(self, key):
        return {"hotspot_ssid": "USB-Router", "hotspot_band": "bg", "listen_port": "8080"}[key]

    def flag(self, key):
        return False

    def provisioned_wifi(self):
        return None


class Spy:
    def __init__(self):
        self.calls = []
        self.active = False

    def prepare(self, iface):
        self.calls.append(iface)
        self.active = iface is not None


def test_router_turns_captive_on_for_the_offline_setup_hotspot():
    spy = Spy()
    r = Router(MockNetworkManager(), FakeCfg(), captive=spy)     # no Wi-Fi, no cable: offline
    r.start(role="setup")
    assert spy.calls == ["wlan0"]
    r.stop(manual=False)
    assert spy.calls == ["wlan0", None]


def test_router_keeps_captive_off_for_a_manual_hotspot_with_an_uplink():
    spy = Spy()
    nm = MockNetworkManager()
    nm.ethernet_plugged = True
    Router(nm, FakeCfg(), captive=spy).start(role="manual")
    assert spy.calls == [None]


def test_router_keeps_captive_off_when_sharing_internet():
    spy = Spy()
    nm = MockNetworkManager()
    nm.ethernet_plugged = True                              # the uplink is the cable
    r = Router(nm, FakeCfg(), captive=spy)
    r.start(role="share")
    assert spy.calls == [None]
    r.stop(manual=False)
    assert spy.calls == [None]
