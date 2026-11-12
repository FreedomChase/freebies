#!/usr/bin/env python3
"""
USB Router browser — a small custom web browser built on Qt WebEngine (Chromium).

Two modes:
  connect mode   shows the local connection-manager page with no toolbar and
                 polls it until the computer is online (or the user clicks
                 "Start browsing" / "Open the sign-in page").
  browser mode   normal tabs + address bar. F11 toggles fullscreen; HTML5
                 fullscreen requests (videos) are honoured. With kiosk=true
                 the browser goes fullscreen with no toolbar automatically.

Shortcuts: F11 fullscreen · Esc leave fullscreen · Ctrl+L address bar · Ctrl+T new tab
           Ctrl+W close tab · Ctrl+N network screen · Ctrl+R reload · Alt+Left/Right history
           Ctrl+Q quit (if allowed)

Usage: browser.py [--connect-url http://127.0.0.1:8080] [--home URL] [--kiosk] [--no-connect]
"""

import argparse
import json
import os
import sys
import urllib.request

from PyQt6.QtCore import QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QKeySequence
from PyQt6.QtWebEngineCore import QWebEngineDownloadRequest, QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (QApplication, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
                             QStyle, QTabWidget, QToolBar, QWidget, QSizePolicy)

APP_NAME = "USB Router Browser"
DEFAULT_CONNECT_URL = os.environ.get("USB_ROUTER_CONNECT_URL", "http://127.0.0.1:8080")


def fetch_json(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


class Page(QWebEnginePage):
    """Opens target=_blank links in a new tab instead of silently dropping them."""
    new_tab_requested = pyqtSignal(QUrl)

    def createWindow(self, _type):
        page = Page(self.profile(), self)
        page.urlChanged.connect(lambda url, p=page: (self.new_tab_requested.emit(url), p.deleteLater()))
        return page

    def certificateError(self, error):  # noqa: N802
        return False  # never bypass TLS errors silently


class Tab(QWebEngineView):
    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.setPage(Page(profile, self))
        s = self.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)


class Browser(QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 800)
        self.kiosk = args.kiosk
        self.allow_exit = True
        self.homepage = args.home or "https://duckduckgo.com"
        self.connect_mode = not args.no_connect
        self.html_fullscreen = False

        self.profile = QWebEngineProfile("usb-router", self)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
        self.profile.downloadRequested.connect(self.on_download)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self.on_tab_changed)
        self.setCentralWidget(self.tabs)

        self._build_toolbar()
        self._build_shortcuts()

        self.progress = QProgressBar()
        self.progress.setMaximumHeight(3)
        self.progress.setTextVisible(False)
        self.statusBar().addPermanentWidget(self.progress, 1)
        self.statusBar().setSizeGripEnabled(False)

        first = self.new_tab(args.connect_url if self.connect_mode else self.homepage)
        if self.connect_mode:
            self.set_connect_mode(True)
            self.poll = QTimer(self)
            self.poll.timeout.connect(self.poll_manager)
            self.poll.start(2000)
        elif self.kiosk:
            self.enter_kiosk()

    # -- chrome ------------------------------------------------------------
    def _build_toolbar(self):
        tb = QToolBar("Navigation")
        tb.setMovable(False)
        tb.setIconSize(tb.iconSize() * 1.1)
        self.addToolBar(tb)
        self.toolbar = tb
        std = self.style().standardIcon

        def act(icon, text, slot, shortcut=None):
            a = QAction(std(icon), text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            tb.addAction(a)
            return a

        act(QStyle.StandardPixmap.SP_ArrowBack, "Back", lambda: self.current().back(), "Alt+Left")
        act(QStyle.StandardPixmap.SP_ArrowForward, "Forward", lambda: self.current().forward(), "Alt+Right")
        act(QStyle.StandardPixmap.SP_BrowserReload, "Reload", lambda: self.current().reload(), "Ctrl+R")
        act(QStyle.StandardPixmap.SP_DirHomeIcon, "Home", lambda: self.current().setUrl(QUrl(self.homepage)), "Alt+Home")

        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("Search or type a web address")
        self.url_bar.setClearButtonEnabled(True)
        self.url_bar.returnPressed.connect(self.navigate_to_input)
        tb.addWidget(self.url_bar)

        act(QStyle.StandardPixmap.SP_FileDialogNewFolder, "New tab", lambda: self.new_tab(self.homepage), "Ctrl+T")
        act(QStyle.StandardPixmap.SP_ComputerIcon, "Network", self.show_network, "Ctrl+N")
        self.fs_action = act(QStyle.StandardPixmap.SP_TitleBarMaxButton, "Fullscreen", self.toggle_fullscreen, "F11")
        self.quit_action = act(QStyle.StandardPixmap.SP_DialogCloseButton, "Quit", self.quit, "Ctrl+Q")

    def _build_shortcuts(self):
        def shortcut(keys, slot):
            a = QAction(self)
            a.setShortcut(QKeySequence(keys))
            a.triggered.connect(slot)
            self.addAction(a)
        shortcut("Ctrl+L", lambda: (self.url_bar.setFocus(), self.url_bar.selectAll()))
        shortcut("Ctrl+W", lambda: self.close_tab(self.tabs.currentIndex()))
        shortcut("Esc", self.on_escape)
        shortcut("Ctrl+Tab", lambda: self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) % max(1, self.tabs.count())))
        shortcut("F5", lambda: self.current().reload())
        shortcut("Ctrl+=", lambda: self.current().setZoomFactor(self.current().zoomFactor() + 0.1))
        shortcut("Ctrl+-", lambda: self.current().setZoomFactor(self.current().zoomFactor() - 0.1))
        shortcut("Ctrl+0", lambda: self.current().setZoomFactor(1.0))

    # -- tabs --------------------------------------------------------------
    def current(self) -> Tab:
        return self.tabs.currentWidget()

    def new_tab(self, url):
        tab = Tab(self.profile)
        tab.page().new_tab_requested.connect(self.new_tab)
        tab.urlChanged.connect(lambda u, t=tab: self.on_url_changed(t, u))
        tab.titleChanged.connect(lambda title, t=tab: self.tabs.setTabText(self.tabs.indexOf(t), (title or "New tab")[:32]))
        tab.iconChanged.connect(lambda icon, t=tab: self.tabs.setTabIcon(self.tabs.indexOf(t), icon))
        tab.loadProgress.connect(lambda p, t=tab: self.progress.setValue(p) if t is self.current() else None)
        tab.loadFinished.connect(lambda ok, t=tab: self.progress.setValue(0) if t is self.current() else None)
        tab.page().fullScreenRequested.connect(self.on_html_fullscreen)
        idx = self.tabs.addTab(tab, "New tab")
        self.tabs.setCurrentIndex(idx)
        tab.setUrl(QUrl(url) if isinstance(url, str) else url)
        return tab

    def close_tab(self, index):
        if self.tabs.count() <= 1:
            self.current().setUrl(QUrl(self.homepage))
            return
        w = self.tabs.widget(index)
        self.tabs.removeTab(index)
        w.deleteLater()

    def on_tab_changed(self, _index):
        if self.current():
            self.url_bar.setText(self.current().url().toString())
            self.setWindowTitle(f"{self.current().title() or APP_NAME} - {APP_NAME}")

    def on_url_changed(self, tab, url):
        if tab is self.current():
            self.url_bar.setText(url.toString())
            self.url_bar.setCursorPosition(0)

    # -- navigation --------------------------------------------------------
    def navigate_to_input(self):
        text = self.url_bar.text().strip()
        if not text:
            return
        if " " in text or ("." not in text and not text.startswith("localhost")):
            url = QUrl("https://duckduckgo.com/?q=" + QUrl.toPercentEncoding(text).data().decode())
        else:
            url = QUrl.fromUserInput(text)
        self.current().setUrl(url)
        self.current().setFocus()

    def show_network(self):
        """Ctrl+N: bring up the connection manager in a tab (always allowed, even in kiosk)."""
        for i in range(self.tabs.count()):
            if self.tabs.widget(i).url().toString().startswith(self.args.connect_url):
                self.tabs.setCurrentIndex(i)
                return
        self.new_tab(self.args.connect_url)

    # -- connect mode / polling -------------------------------------------
    def set_connect_mode(self, on):
        self.connect_mode = on
        self.toolbar.setVisible(not on and not self.kiosk)
        self.tabs.tabBar().setVisible(not on and not self.kiosk)
        self.statusBar().setVisible(not on)
        if on:
            self.showMaximized()

    def poll_manager(self):
        data = fetch_json(self.args.connect_url.rstrip("/") + "/api/browser/poll")
        if not data:
            return
        self.homepage = self.args.home or data.get("homepage") or self.homepage
        self.allow_exit = bool(data.get("allow_exit", True))
        self.quit_action.setVisible(self.allow_exit)
        kiosk = self.args.kiosk or bool(data.get("kiosk"))

        target = data.get("navigate")
        if target:
            # explicit request from the page: "Start browsing" or "Open the sign-in page"
            self.leave_connect_mode(target, kiosk if data.get("online") else False)
        elif self.connect_mode and data.get("online"):
            self.leave_connect_mode(self.homepage, kiosk)

    def leave_connect_mode(self, url, kiosk):
        self.kiosk = kiosk
        if self.connect_mode:
            self.set_connect_mode(False)
            self.current().setUrl(QUrl(url))
        else:
            self.new_tab(url)
        if kiosk:
            self.enter_kiosk()
        # keep polling so Ctrl+N / "Start browsing" from the network tab still works
        self.poll.setInterval(4000)

    def enter_kiosk(self):
        self.kiosk = True
        self.toolbar.hide()
        self.tabs.tabBar().hide()
        self.statusBar().hide()
        self.showFullScreen()

    # -- fullscreen --------------------------------------------------------
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            if self.kiosk:
                # leaving fullscreen in kiosk mode reveals the toolbar so the user can get around
                self.kiosk = False
                self.toolbar.show()
                self.tabs.tabBar().show()
                self.statusBar().show()
        else:
            self.showFullScreen()

    def on_html_fullscreen(self, request):
        request.accept()
        if request.toggleOn():
            self.html_fullscreen = True
            self._chrome_before_fs = (self.toolbar.isVisible(), self.tabs.tabBar().isVisible(), self.statusBar().isVisible(), self.isFullScreen())
            self.toolbar.hide(); self.tabs.tabBar().hide(); self.statusBar().hide()
            self.showFullScreen()
        else:
            self.html_fullscreen = False
            tb, tabs, sb, was_fs = getattr(self, "_chrome_before_fs", (True, True, True, False))
            self.toolbar.setVisible(tb); self.tabs.tabBar().setVisible(tabs); self.statusBar().setVisible(sb)
            if not was_fs:
                self.showNormal()

    def on_escape(self):
        if self.html_fullscreen:
            self.current().page().triggerAction(QWebEnginePage.WebAction.ExitFullScreen)
        elif self.isFullScreen() and not self.kiosk:
            self.showNormal()
        else:
            self.current().stop()

    # -- downloads / quit --------------------------------------------------
    def on_download(self, item: QWebEngineDownloadRequest):
        downloads = os.path.expanduser("~/Downloads")
        os.makedirs(downloads, exist_ok=True)
        item.setDownloadDirectory(downloads)
        item.accept()
        self.statusBar().showMessage(f"Downloading {item.downloadFileName()} to {downloads}", 6000)

    def quit(self):
        if not self.allow_exit:
            return
        if QMessageBox.question(self, APP_NAME, "Quit the browser?") == QMessageBox.StandardButton.Yes:
            QApplication.quit()

    def closeEvent(self, event):  # noqa: N802
        if self.allow_exit:
            event.accept()
        else:
            event.ignore()


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--connect-url", default=DEFAULT_CONNECT_URL, help="connection manager URL")
    parser.add_argument("--home", default=None, help="homepage (overrides the connection manager setting)")
    parser.add_argument("--kiosk", action="store_true", help="fullscreen, no toolbar once online")
    parser.add_argument("--no-connect", action="store_true", help="skip the connect screen, open the homepage")
    parser.add_argument("url", nargs="?", help="open this URL instead of the connect screen")
    args = parser.parse_args()
    if args.url:
        args.no_connect = True
        args.home = args.url

    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--enable-gpu-rasterization --ignore-gpu-blocklist")
    QApplication.setApplicationName(APP_NAME)
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon.fromTheme("web-browser"))
    win = Browser(args)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
