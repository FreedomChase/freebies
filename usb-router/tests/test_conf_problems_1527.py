"""P&T #1527 (found on site, C&G #254): Shaun added a second homepage line to usb-router.conf
instead of changing the shipped one. configparser raised DuplicateOptionError, the player
dropped the WHOLE file and showed DuckDuckGo. Now a repeated setting = the last one wins, a bad
line is skipped instead of being fatal, and every problem is said on the screen."""
import importlib, os, sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
EXAMPLE = os.path.join(HERE, "..", "config", "usb-router.conf.example")
VVC_TV = "https://www.vvcapital.se/tv"


def _config(tmp_path, monkeypatch, text, saved=None):
    conf = tmp_path / "usb-router.conf"
    conf.write_bytes(text.encode("utf-8"))
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if saved is not None:
        import json
        (state / "settings.json").write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setenv("USB_ROUTER_CONF", str(conf))
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(state))
    sys.modules.pop("config", None)
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "CONF_PATHS", [])
    return config.Config()


def _example():
    return open(EXAMPLE, encoding="utf-8").read()


def _line_of(text, prefix):
    return next(n for n, l in enumerate(text.splitlines(), 1) if l.startswith(prefix))


# -- the on-site case ------------------------------------------------------------
def test_shauns_file_second_homepage_line_wins(tmp_path, monkeypatch):
    text = _example().rstrip("\n") + f"\nhomepage = {VVC_TV}\n"
    cfg = _config(tmp_path, monkeypatch, text)
    assert cfg.get("homepage") == VVC_TV                       # was https://duckduckgo.com
    first, last = _line_of(text, "homepage ="), len(text.splitlines())
    problems = cfg.conf_problems()
    assert problems == [f"usb-router.conf: homepage is set 2 times (lines {first}, {last}); "
                        f"the last one, line {last}, is used."]


def test_the_rest_of_the_file_still_counts_when_a_key_repeats(tmp_path, monkeypatch):
    text = ("[usb-router]\nhomepage = https://a.example\nwifi_ssid = VVAdmin\nwifi_password = pass123\n"
            "homepage = https://b.example\nhomepage = https://c.example\nkiosk = false\n")
    cfg = _config(tmp_path, monkeypatch, text)
    assert cfg.get("homepage") == "https://c.example"
    assert cfg.provisioned_wifi() == [("VVAdmin", "pass123")]
    assert cfg.flag("kiosk") is False
    assert "homepage is set 3 times (lines 2, 5, 6); the last one, line 6, is used." in cfg.conf_problems()[0]


def test_repeated_key_in_a_file_without_header_and_any_case(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, "Homepage = https://a.example\r\nhomepage: https://b.example\r\n")
    assert cfg.get("homepage") == "https://b.example"
    assert "homepage is set 2 times (lines 1, 2)" in cfg.conf_problems()[0]


def test_shipped_example_has_no_problems(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, _example())
    assert cfg.conf_problems() == []
    assert cfg.public()["conf_problems"] == []


# -- other ways a laptop edit goes wrong ----------------------------------------------
def test_line_without_equals_is_skipped_and_named_by_number_only(tmp_path, monkeypatch):
    # a password typed on its own line: the line is skipped, its text is never repeated back
    text = "[usb-router]\nwifi_ssid = Office\nSecretPass99\nhomepage = https://a.example\n"
    cfg = _config(tmp_path, monkeypatch, text)
    assert cfg.get("homepage") == "https://a.example" and cfg.get("wifi_ssid") == "Office"
    problems = cfg.conf_problems()
    assert problems == ["usb-router.conf, line 3: not a setting (no = sign), so it was skipped."]
    assert "SecretPass99" not in " ".join(problems)


def test_bad_line_number_is_right_without_a_header(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, "wifi_ssid = Office\njunk\n")
    assert cfg.get("wifi_ssid") == "Office"
    assert cfg.conf_problems() == ["usb-router.conf, line 2: not a setting (no = sign), so it was skipped."]


def test_misspelt_setting_is_named(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, "[usb-router]\nhome_page = https://a.example\nwifi_pasword = x\n")
    assert cfg.get("homepage") == "https://duckduckgo.com"
    assert cfg.conf_problems() == ["usb-router.conf, lines 2, 3: not a setting the player knows, so it is "
                                   "ignored. Check the spelling of the name before the = sign."]


def test_problems_never_carry_a_value(tmp_path, monkeypatch):
    text = ("[usb-router]\nwifi_password = Hunter2Secret\nwifi_password = Hunter3Secret\n"
            "wifi_pass = Hunter4Secret\nHunter5Secret\n")
    joined = " ".join(_config(tmp_path, monkeypatch, text).conf_problems())
    assert "Hunter" not in joined and "wifi_password is set 2 times" in joined


# -- the second on-site cause: a Save on the screen beats the file ---------------------
def test_saved_homepage_that_beats_the_file_is_said(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, f"[usb-router]\nhomepage = {VVC_TV}\n",
                  saved={"homepage": "https://duckduckgo.com"})
    assert cfg.get("homepage") == "https://duckduckgo.com"
    assert cfg.conf_problems() == ["Content URL was saved on this screen, so homepage in usb-router.conf "
                                   "is not used. Change it here: editing the file will not change it."]


def test_saved_value_equal_to_the_file_is_not_a_problem(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, f"[usb-router]\nhomepage = {VVC_TV}\n", saved={"homepage": VVC_TV})
    assert cfg.conf_problems() == []


def test_saved_setting_not_in_the_file_is_not_a_problem(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, "[usb-router]\nkiosk = true\n", saved={"homepage": VVC_TV})
    assert cfg.conf_problems() == []


# -- on the screen and the phone ------------------------------------------------------
@pytest.fixture()
def srv(tmp_path, monkeypatch):
    conf = tmp_path / "usb-router.conf"
    conf.write_text("[usb-router]\nhomepage = https://a.example\nhomepage = https://b.example\n", encoding="utf-8")
    monkeypatch.setenv("USB_ROUTER_CONF", str(conf))
    monkeypatch.setenv("USB_ROUTER_MOCK", "1")
    monkeypatch.setenv("USB_ROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("USB_ROUTER_HOTSPOT_PASSWORD", "testpass123")
    monkeypatch.setenv("USB_ROUTER_ADMIN_PIN", "246810")
    for mod in ("server", "config", "netman", "router", "fleet_client", "captive"):
        sys.modules.pop(mod, None)
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "CONF_PATHS", [])
    server = importlib.import_module("server")
    server.app.config["TESTING"] = True
    return server


def test_status_carries_the_problems_to_screen_and_phone(srv, caplog):
    c = srv.app.test_client()
    assert srv.cfg.get("homepage") == "https://b.example"
    for env in ({}, {"REMOTE_ADDR": "10.42.0.7"}):
        problems = c.get("/api/status", environ_base=env).get_json()["settings"]["conf_problems"]
        assert len(problems) == 1 and "homepage is set 2 times" in problems[0]


def test_page_has_the_warning_places(srv):
    html = srv.app.test_client().get("/").get_data(as_text=True)
    for needle in ('id="setup-conf"', 'id="conf-banner"', 'id="conf-list"'):
        assert needle in html, needle
    js = open(os.path.join(HERE, "..", "app", "static", "app.js"), encoding="utf-8").read()
    assert "renderConf(s, show)" in js and "esc(p)" in js and "esc(list[0])" in js


# -- P&T #1528 (dev4 raises on #1527) ----------------------------------------------------
SECRETS = ("S3cretKey", "Hunter2pw", "P4ssLine", "Indent9pw", "Twice7pw", "Url5pw")


def test_nothing_typed_in_the_file_is_ever_shown(tmp_path, monkeypatch):
    # every wrong place a password can be pasted: as a key, a repeated key, a bare line,
    # an indented line under a setting, a key with a colon, a web address with it in
    text = ("[usb-router]\nS3cretKey = x\nTwice7pw = a\nTwice7pw = b\nP4ssLine\n"
            "wifi_password = Hunter2pw\n    Indent9pw\nhttps://Url5pw.example/tv\nHunter2pw: y\n")
    cfg = _config(tmp_path, monkeypatch, text)
    shown = " ".join(cfg.conf_problems() + cfg.public()["conf_problems"])
    for secret in SECRETS:
        assert secret.lower() not in shown.lower(), secret
    for where in ("lines 2, 3, 4, 9:", "line 5:", "line 7:", "line 8:"):   # every bad line still named
        assert where in shown, where


def test_the_on_site_repeat_comes_first_with_stray_lines_around_it(tmp_path, monkeypatch):
    text = ("stray words\n" + _example().rstrip("\n") + f"\nhome_page = x\nhttps://a.example\n"
            f"homepage = {VVC_TV}\njunk again\n")
    cfg = _config(tmp_path, monkeypatch, text)
    problems = cfg.conf_problems()
    assert cfg.get("homepage") == VVC_TV
    assert problems[0].startswith("usb-router.conf: homepage is set 2 times"), problems
    assert len(problems) == 4      # repeat, web address, no = sign, unknown


def test_a_save_on_the_screen_ranks_above_a_typo(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, f"[usb-router]\nhome_page = x\nhomepage = {VVC_TV}\n",
                  saved={"homepage": "https://duckduckgo.com"})
    assert cfg.conf_problems()[0].startswith("Content URL was saved on this screen")


def test_bare_web_address_is_named_as_one(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, f"[usb-router]\nkiosk = true\n{VVC_TV}\n")
    assert cfg.conf_problems() == ["usb-router.conf, line 3: a web address on its own, not a setting, so it "
                                   "is ignored. To show it, write homepage = in front of it."]
    assert cfg.get("homepage") == "https://duckduckgo.com"


def test_indented_line_that_joins_the_homepage_is_named(tmp_path, monkeypatch):
    text = "[usb-router]\nhomepage = https://a.example\n\n# a note\n  https://b.example\nkiosk = true\n"
    cfg = _config(tmp_path, monkeypatch, text)
    assert "\n" in cfg.get("homepage")            # what configparser does with it
    assert cfg.conf_problems() == ["usb-router.conf, line 5: starts with a space, so it is added to homepage "
                                   "above it and homepage is wrong. Remove the space at the start."]


def test_indented_comment_and_indented_first_line_are_not_joins(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, "[usb-router]\nhomepage = https://a.example\n   # note\nkiosk = true\n")
    assert cfg.conf_problems() == [] and cfg.get("homepage") == "https://a.example"


def test_scan_matches_what_configparser_joins(tmp_path, monkeypatch):
    # two indented lines under wifi_ssid, one under an unknown key (that one is not named by key)
    text = "[usb-router]\nwifi_ssid = Office\n x\n\ty\nfoo = 1\n z\n"
    cfg = _config(tmp_path, monkeypatch, text)
    assert cfg.get("wifi_ssid") == "Office\nx\ny"
    problems = cfg.conf_problems()
    assert problems[0] == ("usb-router.conf, lines 3, 4: start with a space, so they are added to wifi_ssid "
                           "above it and wifi_ssid is wrong. Remove the space at the start.")
    assert problems[1].startswith("usb-router.conf, line 5: not a setting the player knows")
    assert "foo" not in " ".join(problems)
