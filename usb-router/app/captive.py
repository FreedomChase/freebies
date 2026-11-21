"""
Captive portal for the setup hotspot (S&D #193).

A phone that joins the screen's own hotspot checks for internet by fetching a
known probe URL (iOS: captive.apple.com/hotspot-detect.html, Android:
connectivitycheck.gstatic.com/generate_204, ...). When the answer is not the
expected one, the phone opens a "sign in to network" page on its own. So:

  DNS   NetworkManager's shared-mode dnsmasq reads every file in
        /etc/NetworkManager/dnsmasq-shared.d when the hotspot comes up. The
        installer links usb-router-captive.conf there to dns_conf_path() below, which
        this module rewrites before each hotspot start: while the screen has no
        internet it answers the probe names with the hotspot's own address
        (interface-name=, so no address is hard-coded); otherwise it is empty.
  HTTP  a small listener on port 80 answers every request with a redirect to
        the setup page on listen_port, but only while the captive mode is on.

Only the probe names are pointed at the screen, never everything: the setup
hotspot has no internet to give, and when it shares one (role "share") the
captive mode is off and phones see the real internet.
"""
import ipaddress
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("usb-router.captive")

DNS_CONF_NAME = "captive-dns.conf"


def dns_conf_path():
    return os.path.join(os.environ.get("USB_ROUTER_STATE_DIR", "/var/lib/usb-router"), DNS_CONF_NAME)

# The names phones and laptops fetch to decide "is there internet here?"
PROBE_HOSTS = (
    # Apple (iOS, iPadOS, macOS)
    "captive.apple.com", "www.appleiphonecell.com", "www.itools.info", "www.ibook.info",
    "www.airport.us", "www.thinkdifferent.us",
    # Android / Chrome OS
    "connectivitycheck.gstatic.com", "connectivitycheck.android.com",
    "clients1.google.com", "clients3.google.com", "www.google.com",
    # Samsung, Huawei, Xiaomi
    "connectivity.samsung.com.cn", "connectivitycheck.platform.hicloud.com",
    "connect.rom.miui.com",
    # Windows, Firefox
    "www.msftconnecttest.com", "www.msftncsi.com", "detectportal.firefox.com",
)

_IFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


def _lease_line(max_clients):
    """C&G #281: NetworkManager's shared dnsmasq gives no more addresses than the client cap."""
    if max_clients is None:
        return []
    n = int(max_clients)
    if not 1 <= n <= 10:
        raise ValueError(f"bad client cap {max_clients!r}")
    return [f"dhcp-lease-max={n}"]


def dns_conf_text(iface, max_clients=None):
    """The dnsmasq lines for captive mode on iface (none when iface is None), plus the lease cap."""
    if not iface:
        return "\n".join(["# captive portal off (S&D #193)"] + _lease_line(max_clients)) + "\n"
    if not _IFACE_RE.match(iface):
        raise ValueError(f"bad interface name {iface!r}")
    lines = ["# written by usb-router (S&D #193): captive portal on " + iface]
    lines += [f"interface-name={h},{iface}/4" for h in PROBE_HOSTS]
    lines += _lease_line(max_clients)
    return "\n".join(lines) + "\n"


def write_dns_conf(iface, path=None, max_clients=None):
    path = path or dns_conf_path()
    text = dns_conf_text(iface, max_clients)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path


class Captive:
    """Holds the on/off state and runs the port-80 listener."""

    def __init__(self, setup_port, http_port=80, dns_path=None):
        self.setup_port = int(setup_port)
        self.http_port = int(http_port)
        self.dns_path = dns_path
        self.iface = None
        self.server = None
        self._lock = threading.Lock()

    @property
    def active(self):
        return self.iface is not None

    def prepare(self, iface, max_clients=None):
        """Call before the hotspot comes up: iface turns captive mode on, None turns it off.
        max_clients (C&G #281) caps NetworkManager's DHCP leases on the hotspot."""
        with self._lock:
            try:
                write_dns_conf(iface, self.dns_path, max_clients)
            except OSError as e:
                # no DNS answers means phones say "no internet" instead of opening the page;
                # the hotspot itself must still start
                log.warning("captive DNS file not written (%s); phones open http://10.42.0.1:%s by hand",
                            e, self.setup_port)
            self.iface = iface
        log.info("captive portal %s", f"on ({iface})" if iface else "off")

    def setup_url(self, local_addr):
        try:
            ip = ipaddress.ip_address(local_addr)
        except ValueError:
            return None
        if ip.is_loopback or ip.is_unspecified:
            return None
        host = f"[{ip}]" if ip.version == 6 else str(ip)
        return f"http://{host}:{self.setup_port}/"

    def start_listener(self, host="0.0.0.0"):
        """Serve port 80 in a thread. Needs CAP_NET_BIND_SERVICE (the systemd unit grants it)."""
        captive = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "usb-router"
            sys_version = ""

            def _answer(self):
                url = captive.setup_url(self.connection.getsockname()[0]) if captive.active else None
                if not url:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                body = (f'<!doctype html><meta charset="utf-8"><title>Screen setup</title>'
                        f'<p><a href="{url}">Open the screen setup</a></p>').encode()
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_POST = do_HEAD = _answer

            def log_message(self, fmt, *args):  # probes arrive every few seconds; keep the journal quiet
                log.debug("captive %s %s", self.address_string(), fmt % args)

        try:
            self.server = ThreadingHTTPServer((host, self.http_port), Handler)
        except OSError as e:
            log.warning("captive listener not started on port %s: %s", self.http_port, e)
            return None
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="captive-http", daemon=True).start()
        log.info("captive listener on %s:%s", host, self.server.server_address[1])
        return self.server

    def stop_listener(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
