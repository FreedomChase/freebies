"""C&G #270 (Shaun's v8.8 flash, 20 Nov ~03:00 CET): the screen looped black <-> plain blue on site and
nothing on it could say whether the Pi was rebooting (weak power), the browser was restarting, or the HDMI
was dropping. :8080 now shows time on, the start count (once per kernel boot_id), settings-page and browser
restarts, and the firmware's get_throttled power bits, with an under-voltage boot kept across the reset."""
import importlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "pizero"))

import health  # noqa: E402

UV_NOW_AND_SINCE = 0x50005      # the classic weak-charger reading: under-voltage + throttled, now and since
UV_SINCE_ONLY = 0x50000


def boot(tmp_path, boot_id):
    p = tmp_path / "boot_id"
    p.write_text(boot_id + "\n")
    return str(p)


def stored(tmp_path):
    return json.loads((tmp_path / health.STATE_NAME).read_text())


# -- boot count ----------------------------------------------------------------------------------
def test_first_start_is_boot_one(tmp_path):
    rec = health.record_start(str(tmp_path), boot(tmp_path, "aaa"))
    assert rec["count"] == 1 and rec["app_starts"] == 1 and rec["saved"] is True
    assert stored(tmp_path)["count"] == 1 and "saved" not in stored(tmp_path)


def test_a_new_boot_id_counts_one_more_boot(tmp_path):
    for i, b in enumerate(["aaa", "bbb", "ccc"], 1):
        rec = health.record_start(str(tmp_path), boot(tmp_path, b))
        assert rec["count"] == i and rec["app_starts"] == 1


def test_an_app_restart_in_the_same_boot_is_not_a_boot(tmp_path):
    health.record_start(str(tmp_path), boot(tmp_path, "aaa"))
    rec = health.record_start(str(tmp_path), boot(tmp_path, "aaa"))
    assert rec["count"] == 1 and rec["app_starts"] == 2


def test_a_corrupt_file_starts_the_count_again_and_says_so(tmp_path):
    (tmp_path / health.STATE_NAME).write_text("{not json")
    rec = health.record_start(str(tmp_path), boot(tmp_path, "aaa"))
    assert rec["count"] == 1 and rec["restarted_count"] is True


def test_an_unwritable_state_dir_is_reported_not_raised(tmp_path):
    rec = health.record_start(str(tmp_path / "missing" / "dir"), boot(tmp_path, "aaa"))
    assert rec["count"] == 1 and rec["saved"] is False
    assert "count not saved" in health.report(rec, {"source": None}, 30, 0, now=30)["words"]


def test_under_voltage_boot_survives_the_next_boot(tmp_path):
    rec = health.record_start(str(tmp_path), boot(tmp_path, "aaa"))
    health.note_under_voltage(str(tmp_path), rec)
    rec = health.record_start(str(tmp_path), boot(tmp_path, "bbb"))
    assert rec["count"] == 2 and rec["uv_boot"] == 1
    words = health.report(rec, {"source": "vcgencmd", **health.decode_throttled(0)}, 40, 0, now=40)["words"]
    assert "Power was too low on start 1" in words


# -- power bits ----------------------------------------------------------------------------------
@pytest.mark.parametrize("text,value", [("throttled=0x50005", 0x50005), ("50005", 0x50005), ("0x0", 0),
                                        ("0", 0), ("", None), (None, None), ("throttled=", None), ("zz", None)])
def test_parse_throttled_reads_both_hex_forms(text, value):
    assert health.parse_throttled(text) == value


def test_decode_names_every_documented_bit():
    f = health.decode_throttled(UV_NOW_AND_SINCE)
    assert f["under_voltage_now"] and f["throttled_now"] and f["under_voltage_since_boot"] and f["throttled_since_boot"]
    assert not f["freq_capped_now"] and not f["soft_temp_limit_since_boot"]
    one = {bit: health.decode_throttled(1 << bit) for bit in health.BITS}
    for bit, flags in one.items():
        assert [k for k, v in flags.items() if v] == [health.BITS[bit]]


def hwmon_dir(tmp_path, alarm="1", name="rpi_volt"):
    hw = tmp_path / "hwmon3"
    hw.mkdir()
    (hw / "name").write_text(name + "\n")
    (hw / "in0_lcrit_alarm").write_text(alarm + "\n")
    return str(tmp_path / "hwmon*")


def test_read_power_prefers_vcgencmd(tmp_path):
    p = health.read_power(vcgencmd=lambda: UV_NOW_AND_SINCE, hwmon=hwmon_dir(tmp_path, "0"),
                          kernel_log=lambda: False)
    assert p["source"] == "vcgencmd" and p["raw"] == "0x50005" and p["under_voltage_now"]


def test_without_vcgencmd_the_hwmon_alarm_and_kernel_log_answer(tmp_path):
    p = health.read_power(vcgencmd=lambda: None, hwmon=hwmon_dir(tmp_path, "0"), kernel_log=lambda: True)
    assert p == {"source": "hwmon", "raw": None, "under_voltage_now": False, "under_voltage_since_boot": True}
    r = health.report({"count": 3, "app_starts": 1}, p, 300, 0, now=300)
    assert r["warn"] and "dipped" in r["words"]


def test_hwmon_alarm_now_counts_as_since_boot_even_with_a_clean_log(tmp_path):
    p = health.read_power(vcgencmd=lambda: None, hwmon=hwmon_dir(tmp_path, "1"), kernel_log=lambda: False)
    assert p["under_voltage_now"] and p["under_voltage_since_boot"]


def test_an_unreadable_journal_leaves_since_boot_unknown(tmp_path):
    p = health.read_power(vcgencmd=lambda: None, hwmon=hwmon_dir(tmp_path, "0"), kernel_log=lambda: None)
    assert "under_voltage_since_boot" not in p and p["under_voltage_now"] is False


def test_another_hwmon_device_is_not_read_as_power(tmp_path):
    p = health.read_power(vcgencmd=lambda: None, hwmon=hwmon_dir(tmp_path, "1", name="cpu_thermal"),
                          kernel_log=lambda: True)
    assert p["source"] is None


def test_read_power_on_a_laptop_says_not_available(tmp_path):
    p = health.read_power(vcgencmd=lambda: None, hwmon=str(tmp_path / "none*"), kernel_log=lambda: True)
    assert p["source"] is None
    assert "not available" in health.report({"count": 1, "app_starts": 1}, p, 10, 0, now=10)["words"]


@pytest.mark.parametrize("rc,out,err,want", [(0, "Undervoltage detected!\n", "", True), (1, "", "", False),
                                             (1, "", "Failed to open journal", None), (0, "", "", False)])
def test_kernel_log_reading(monkeypatch, rc, out, err, want):
    res = type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()
    seen = []
    monkeypatch.setattr(health.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or res)
    assert health._kernel_log_under_voltage() is want
    assert seen[0][:3] == ["journalctl", "-k", "-b"] and health.UV_LOG in seen[0]


def test_vcgencmd_output_and_refusal(monkeypatch):
    ok = type("R", (), {"returncode": 0, "stdout": "throttled=0x50005\n"})()
    no = type("R", (), {"returncode": 255, "stdout": "VCHI initialization failed\n"})()
    monkeypatch.setattr(health.subprocess, "run", lambda cmd, **kw: ok)
    assert health._vcgencmd() == 0x50005
    monkeypatch.setattr(health.subprocess, "run", lambda cmd, **kw: no)
    assert health._vcgencmd() is None


def test_install_puts_kiosk_in_video_for_vcgencmd():
    sh = open(os.path.join(ROOT, "pizero", "install-pizero.sh")).read()
    loop = [l for l in sh.splitlines() if l.startswith("for g in") and "systemd-journal" in l]
    assert loop and "video" in loop[0].split(";")[0]


def test_system_uptime_reads_proc(tmp_path):
    f = tmp_path / "uptime"
    f.write_text("4321.77 1000.00\n")
    assert health.system_uptime(str(f)) == 4321
    f.write_text("garbage")
    assert health.system_uptime(str(f)) is None
    assert health.system_uptime(str(tmp_path / "nope")) is None


# -- the words on the page -----------------------------------------------------------------------
def rec(count=14, app_starts=1, uv_boot=None):
    return {"count": count, "app_starts": app_starts, "uv_boot": uv_boot, "saved": True}


def pw(value):
    return {"source": "vcgencmd", "raw": hex(value), **health.decode_throttled(value)}


def test_weak_charger_now_names_the_fix():
    r = health.report(rec(), pw(UV_NOW_AND_SINCE), 45, 0, now=40)
    assert r["warn"] is True and r["boot_count"] == 14
    assert "Power too low right now" in r["words"] and "5 V 2.5 A" in r["words"] and "TV's USB" in r["words"]
    assert r["words"].startswith("On for 45 s · start 14 since flashing")


def test_a_dip_since_this_start_is_still_a_warning():
    r = health.report(rec(), pw(UV_SINCE_ONLY), 600, 0, now=600)
    assert r["warn"] and "dipped" in r["words"] and "10 min" in r["words"]


def test_good_power_and_no_restarts_is_calm():
    r = health.report(rec(count=1), pw(0), 3 * 3600 + 120, 0, now=100)
    assert r["warn"] is False and r["words"] == "On for 3 h 2 min · start 1 since flashing. Power OK."


def test_heat_throttling_without_under_voltage_is_named():
    r = health.report(rec(), pw(0x40000), 100, 0, now=100)
    assert "Power OK. Slowed down for heat." in r["words"]


def test_app_and_browser_restarts_are_counted_and_warn():
    r = health.report(rec(app_starts=3), pw(0), 900, 0, now=900, browser_starts=5)
    assert r["warn"] and "settings page restarted 2x" in r["words"] and "browser restarted 4x" in r["words"]
    assert r["browser_starts"] == 5 and r["app_starts"] == 3


def test_one_browser_start_is_not_a_restart():
    r = health.report(rec(), pw(0), 900, 0, now=900, browser_starts=1)
    assert not r["warn"] and "browser" not in r["words"]


@pytest.mark.parametrize("s,words", [(0, "0 s"), (119, "119 s"), (120, "2 min"), (7199, "119 min"),
                                     (7200, "2 h 0 min"), (172800, "2 days")])
def test_durations_are_plain(s, words):
    assert health.fmt_duration(s) == words


# -- the server --------------------------------------------------------------------------------
@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("USB_ROUTER_MOCK", "1")
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("USB_ROUTER_HOTSPOT_PASSWORD", "testpass123")
    monkeypatch.setenv("USB_ROUTER_ADMIN_PIN", "246810")
    for mod in ("server", "config", "netman", "router", "fleet_client", "captive", "health"):
        sys.modules.pop(mod, None)
    srv = importlib.import_module("server")
    srv.app.config["TESTING"] = True
    monkeypatch.setattr(srv.health, "BOOT_ID", boot(tmp_path, "boot-1"))
    monkeypatch.setattr(srv.health, "read_power", lambda: pw(UV_NOW_AND_SINCE))
    monkeypatch.setattr(srv.health, "system_uptime", lambda: 75)
    return srv


def test_status_carries_health_and_notes_the_under_voltage_boot(server, tmp_path):
    server.health_start()
    with server.app.test_client() as c:
        h = c.get("/api/status").get_json()["health"]
    assert h["boot_count"] == 1 and h["uptime"] == 75 and h["warn"] is True
    assert "Power too low right now" in h["words"] and h["power"]["raw"] == "0x50005"
    assert stored(tmp_path)["uv_boot"] == 1


def test_a_brownout_reboot_shows_on_the_next_start(server, tmp_path, monkeypatch):
    server.health_start()
    server.health_now(max_age=0)
    # the Pi browns out and resets: new boot_id, and the firmware's since-boot bits are clear again
    (tmp_path / "boot_id").write_text("boot-2\n")
    monkeypatch.setattr(server.health, "read_power", lambda: pw(0))
    server.health_start()
    h = server.health_now(max_age=0)
    assert h["boot_count"] == 2 and h["uv_boot"] == 1 and "Power was too low on start 1" in h["words"]


def test_the_report_is_cached_so_polls_do_not_run_vcgencmd_each_time(server, monkeypatch):
    calls = []
    monkeypatch.setattr(server.health, "read_power", lambda: calls.append(1) or pw(0))
    server.health_now(max_age=10)
    server.health_now(max_age=10)
    assert len(calls) == 1


def test_browser_report_carries_the_cog_start_count(server):
    server.health_start()
    with server.app.test_client() as c:
        c.post("/api/browser/report", json={"url": "https://www.vvcapital.se/tv", "starts": 4})
        h = c.get("/api/status").get_json()["health"]
    assert h["browser_starts"] == 4 and "browser restarted 3x" in h["words"]


def test_a_restart_report_does_not_clear_the_content_check(server):
    with server.app.test_client() as c:
        c.post("/api/browser/report", json={"url": "https://www.vvcapital.se/tv", "check": "timed out", "fails": 2})
        c.post("/api/browser/report", json={"url": "https://www.vvcapital.se/tv", "check": "", "restart": True,
                                            "starts": 2})
        s = c.get("/api/status").get_json()
    assert s["content_check"]["failed"] == "timed out" and s["health"]["browser_starts"] == 2


@pytest.mark.parametrize("bad", ["x", -1, 0, 10 ** 7, None])
def test_a_bad_start_count_is_ignored(server, bad):
    with server.app.test_client() as c:
        assert c.post("/api/browser/report", json={"url": "u", "starts": bad}).status_code == 200
    assert server._health["browser_starts"] is None


def test_a_remote_phone_cannot_post_a_start_count(server):
    with server.app.test_client() as c:
        r = c.post("/api/browser/report", json={"url": "u", "starts": 9}, environ_base={"REMOTE_ADDR": "192.168.1.50"})
    assert r.status_code == 403 and server._health["browser_starts"] is None


def test_the_page_has_both_health_lines():
    html = open(os.path.join(ROOT, "app", "templates", "index.html")).read()
    js = open(os.path.join(ROOT, "app", "static", "app.js")).read()
    assert 'id="health-line"' in html and 'id="setup-health"' in html
    assert "renderHealth(s)" in js and "textContent = words" in js   # textContent: never HTML


# -- the kiosk -----------------------------------------------------------------------------------
def test_the_kiosk_counts_cog_starts_and_reports_them_after_a_restart():
    import kiosk as K

    class Cog:
        def __init__(self):
            self.running, self.starts = False, 0

        def start(self, url):
            self.running, self.starts = True, self.starts + 1

        def alive(self):
            return self.running

        def open(self, url):
            return True

        def reload(self):
            return True

        def stop(self):
            self.running = False

    sent = []

    def fetch(url, timeout=3, data=None):
        if url.endswith("/report"):
            sent.append(data)
        return {"ok": True, "online": False}

    cog = Cog()
    k = K.Kiosk(cog, "http://127.0.0.1:8080", fetch=fetch, check=lambda u: True, clock=lambda: 1000.0,
                synced=lambda: True)
    k.start()
    k.show_content("https://www.vvcapital.se/tv", ok=True)
    assert sent[-1]["starts"] == 1 and "restart" not in sent[-1]
    cog.running = False                          # cog died
    assert k.step() == "restarted-browser"
    assert sent[-1] == {"url": "https://www.vvcapital.se/tv", "check": "", "restart": True, "starts": 2}


def test_the_real_browser_counts_its_starts(monkeypatch):
    import kiosk as K
    class Proc:
        def __init__(self, cmd):
            self.done = None

        def poll(self):
            return self.done

        def terminate(self):
            self.done = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.done = -9

    monkeypatch.setattr(K.subprocess, "Popen", Proc)
    b = K.Cog()
    b.start("about:blank")
    b.start("about:blank")
    assert b.starts == 2
