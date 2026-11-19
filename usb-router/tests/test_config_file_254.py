"""C&G #254 v8.7 (dev4 raises): usb-router.conf saved by Notepad (BOM), '%' in a password,
no section header, ANSI save: the Wi-Fi in the file is still read, Config() never crashes."""
import importlib, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


def _wifi(tmp_path, monkeypatch, data: bytes):
    conf = tmp_path / "usb-router.conf"
    conf.write_bytes(data)
    monkeypatch.setenv("USB_ROUTER_CONF", str(conf))
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path / "state"))
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "CONF_PATHS", [])
    return config.Config().provisioned_wifi()


def test_bom_file_is_read(tmp_path, monkeypatch):
    data = "﻿[usb-router]\r\nwifi_ssid = VVAdmin\r\nwifi_password = pass123\r\n".encode("utf-8")
    assert _wifi(tmp_path, monkeypatch, data) == [("VVAdmin", "pass123")]


def test_percent_in_password_does_not_crash(tmp_path, monkeypatch):
    data = b"[usb-router]\nwifi_ssid = Shop\nwifi_password = 50%off\n"
    assert _wifi(tmp_path, monkeypatch, data) == [("Shop", "50%off")]


def test_file_without_header_is_read(tmp_path, monkeypatch):
    data = b"wifi_ssid = VVAdmin\nwifi_password = pass123\n"
    assert _wifi(tmp_path, monkeypatch, data) == [("VVAdmin", "pass123")]


def test_ansi_file_with_accent_is_read(tmp_path, monkeypatch):
    data = "[usb-router]\nwifi_ssid = Oficina\nwifi_password = contraseña1\n".encode("cp1252")
    assert _wifi(tmp_path, monkeypatch, data) == [("Oficina", "contraseña1")]


def test_bom_plus_percent_plus_no_header(tmp_path, monkeypatch):
    data = "﻿wifi_ssid = VV Admin\r\nwifi_password = a%b%%c\r\n".encode("utf-8")
    assert _wifi(tmp_path, monkeypatch, data) == [("VV Admin", "a%b%%c")]
