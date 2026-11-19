"""C&G #254: the real NetworkManager backend updates an existing profile instead of skipping it."""
import importlib, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


def test_existing_profile_gets_modify_with_the_new_psk(monkeypatch):
    netman = importlib.import_module("netman")
    calls = []
    monkeypatch.setattr(netman, "nmcli", lambda *a, **k: calls.append(a) or "")
    nm = netman.NetworkManager()
    monkeypatch.setattr(nm, "saved_connections", lambda: [{"name": "Office", "type": "802-11-wireless"}])
    assert nm.ensure_profile("Office", "newpass22", priority=10) is False
    assert calls == [("connection", "modify", "Office", "connection.autoconnect", "yes",
                      "connection.autoconnect-priority", "10",
                      "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "newpass22")]
    calls.clear()
    monkeypatch.setattr(nm, "saved_connections", lambda: [])
    assert nm.ensure_profile("Office", "newpass22") is True
    assert calls[0][:2] == ("connection", "add")
