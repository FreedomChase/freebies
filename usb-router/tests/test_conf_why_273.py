"""C&G #273 (Shaun at the TV, 20 Nov): the setup screen said "usb-router.conf has no Wi-Fi name (wifi_ssid is
empty, or the file was not read)" and nothing more. It now names the cause: not found (and the near-miss names on
the drive), read from usb-router.conf.txt, could not be read, no wifi_ssid line, wifi_ssid empty on line N, under
another heading, or beaten by a saved setting / the environment. Never what is typed in the file."""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


def _cfg(tmp_path, monkeypatch, files=None, saved=None, env=None):
    """A boot drive folder holding `files` ({name: bytes}); CONF_PATHS points at its usb-router.conf."""
    boot = tmp_path / "boot"
    boot.mkdir(exist_ok=True)
    for name, data in (files or {}).items():
        if data is None:
            (boot / name).mkdir()
        else:
            (boot / name).write_bytes(data)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if saved is not None:
        import json
        (state / "settings.json").write_text(json.dumps(saved))
    monkeypatch.delenv("USB_ROUTER_CONF", raising=False)
    for k in list(os.environ):
        if k.startswith("USB_ROUTER_WIFI_"):
            monkeypatch.delenv(k)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(state))
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "CONF_PATHS", [str(boot / "usb-router.conf")])
    return config.Config(), str(boot)


def test_not_found_names_the_folder(tmp_path, monkeypatch):
    c, boot = _cfg(tmp_path, monkeypatch)
    assert c.provisioned_wifi() == []
    assert c.wifi_why() == f"usb-router.conf was not found (looked in {boot})"


def test_not_found_lists_the_near_miss_names(tmp_path, monkeypatch):
    c, _ = _cfg(tmp_path, monkeypatch, {"USB-Router (1).conf": b"wifi_ssid = X\n", "notes.conf.txt": b"",
                                        "usb-router.conf.example": b"", "cmdline.txt": b""})
    why = c.wifi_why()
    assert "was not found" in why
    assert "USB-Router (1).conf" in why and "notes.conf.txt" in why
    assert "example" not in why and "cmdline" not in why
    assert "Rename it to usb-router.conf" in why


def test_usb_router_conf_txt_is_read_and_the_screen_says_rename(tmp_path, monkeypatch):
    c, _ = _cfg(tmp_path, monkeypatch, {"usb-router.conf.txt": b"[usb-router]\r\nwifi_ssid = OWNIT\r\nwifi_password = pw12345678\r\n"})
    assert c.provisioned_wifi() == [("OWNIT", "pw12345678")]
    assert any("usb-router.conf.txt was read as usb-router.conf" in p for p in c.conf_problems())


def test_the_real_name_wins_over_a_txt_copy(tmp_path, monkeypatch):
    c, _ = _cfg(tmp_path, monkeypatch, {"usb-router.conf": b"wifi_ssid = Real\n", "usb-router.conf.txt": b"wifi_ssid = Txt\n"})
    assert c.provisioned_wifi() == [("Real", None)]
    assert not any(".txt was read" in p for p in c.conf_problems())


def test_bom_and_no_header_just_work(tmp_path, monkeypatch):
    c, _ = _cfg(tmp_path, monkeypatch, {"usb-router.conf": "﻿wifi_ssid = OWNIT\r\nwifi_password = pw12345678\r\n".encode()})
    assert c.provisioned_wifi() == [("OWNIT", "pw12345678")]


def test_empty_wifi_ssid_names_its_line(tmp_path, monkeypatch):
    data = b"[usb-router]\r\nhomepage = https://example.org\r\nwifi_ssid =\r\nwifi_password = Secret-pw-99\r\n"
    c, _ = _cfg(tmp_path, monkeypatch, {"usb-router.conf": data})
    why = c.wifi_why()
    assert why.startswith("usb-router.conf: wifi_ssid on line 3 is empty")
    assert "Secret-pw-99" not in why


def test_empty_wifi_ssid_line_number_in_a_headerless_bom_file(tmp_path, monkeypatch):
    c, _ = _cfg(tmp_path, monkeypatch, {"usb-router.conf": "﻿# my screen\nwifi_ssid = \n".encode()})
    assert "wifi_ssid on line 2 is empty" in c.wifi_why()


def test_no_wifi_ssid_line(tmp_path, monkeypatch):
    c, _ = _cfg(tmp_path, monkeypatch, {"usb-router.conf": b"[usb-router]\nhomepage = https://example.org\n"})
    assert c.wifi_why() == "usb-router.conf was read but has no wifi_ssid line. Add one: wifi_ssid = your Wi-Fi name"


def test_wifi_ssid_under_another_heading(tmp_path, monkeypatch):
    data = b"[usb-router]\nhomepage = https://example.org\n[MyHomeWifi-Secret]\nwifi_ssid = OWNIT\n"
    c, _ = _cfg(tmp_path, monkeypatch, {"usb-router.conf": data})
    assert c.provisioned_wifi() == []
    why = c.wifi_why()
    assert "wifi_ssid on line 4 is under another [ ] heading, not [usb-router]" in why
    assert "MyHomeWifi" not in why and "OWNIT" not in why          # nothing typed in the file is repeated


def test_unreadable_file(tmp_path, monkeypatch):
    c, _ = _cfg(tmp_path, monkeypatch, {"usb-router.conf": None})   # a folder by that name: cannot be opened
    assert "could not be opened" in c.wifi_why()


def test_a_saved_blank_setting_beats_the_file(tmp_path, monkeypatch):
    c, _ = _cfg(tmp_path, monkeypatch, {"usb-router.conf": b"wifi_ssid = OWNIT\n"}, saved={"wifi_ssid": ""})
    assert c.provisioned_wifi() == []
    assert c.wifi_why().startswith("a blank Wi-Fi name was saved on this screen")


def test_the_environment_beats_the_file(tmp_path, monkeypatch):
    c, _ = _cfg(tmp_path, monkeypatch, {"usb-router.conf": b"wifi_ssid = OWNIT\n"}, env={"USB_ROUTER_WIFI_SSID": ""})
    assert c.provisioned_wifi() == []
    assert "USB_ROUTER_WIFI_SSID" in c.wifi_why()


def test_image_defaults_in_etc_are_not_the_users_file(tmp_path, monkeypatch):
    """/etc/usb-router/usb-router.conf (the image's own) is never named as the file someone edited."""
    etc = tmp_path / "etc.conf"
    etc.write_bytes(b"[usb-router]\nwifi_ssid =\n")
    c, boot = _cfg(tmp_path, monkeypatch)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "CONF_PATHS", ["/etc/usb-router/does-not-exist.conf", os.path.join(boot, "usb-router.conf")])
    assert config.Config().wifi_why() == f"usb-router.conf was not found (looked in {boot})"


def test_the_report_carries_the_cause():
    import netman
    [r] = netman.conf_wifi_report([], None, set(), None, why="usb-router.conf was not found (looked in /boot/firmware)")
    assert r["state"] == "none" and r["words"] == "usb-router.conf was not found (looked in /boot/firmware)"
    [r] = netman.conf_wifi_report([], None, set(), None)
    assert "was not read" in r["words"]                                # old words kept when no cause is given


def test_status_shows_the_cause_on_the_setup_screen(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "usb-router.conf.TXT.bak").write_bytes(b"")
    monkeypatch.setenv("USB_ROUTER_MOCK", "1")
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("USB_ROUTER_HOTSPOT_PASSWORD", "testpass123")
    monkeypatch.setenv("USB_ROUTER_ADMIN_PIN", "246810")
    monkeypatch.delenv("USB_ROUTER_CONF", raising=False)
    for k in list(os.environ):
        if k.startswith("USB_ROUTER_WIFI_"):
            monkeypatch.delenv(k)
    for mod in ("server", "config", "netman", "router", "fleet_client", "captive"):
        sys.modules.pop(mod, None)
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "CONF_PATHS", [str(boot / "usb-router.conf")])
    srv = importlib.import_module("server")
    srv.app.config["TESTING"] = True
    srv._conf_wifi_cache.update(at=0.0, value=None)
    with srv.app.test_client() as c:
        body = c.get("/api/status").get_json()
    [w] = body["conf_wifi"]
    assert w["state"] == "none"
    assert w["words"].startswith(f"usb-router.conf was not found (looked in {boot}); on the drive instead: usb-router.conf.TXT.bak")
