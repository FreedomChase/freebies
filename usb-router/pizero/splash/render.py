#!/usr/bin/env python3
"""Render the Plymouth theme's pictures from the setup page's own stylesheet (C&G #209 v8.4),
so the Plymouth splash and the page's .boot-splash are the same picture.

  python3 render.py http://127.0.0.1:8080 OUT_DIR     (a running connection manager, mock is fine)

bg.png     1920x1080, the .boot-splash ground with nothing on it (Plymouth scales it to the screen)
icon.png   the Display Player icon as the page draws it (11vh tall at 1080), transparent around it
text.png   "Setting up, please wait..." as the page draws it at 1920 wide, transparent around it
track.png  the empty progress bar, fill.png the full one (Plymouth scales it to the progress)
Needs playwright (chromium). Not used on the device.
"""
import sys
from playwright.sync_api import sync_playwright

base, out = sys.argv[1].rstrip("/"), sys.argv[2].rstrip("/")
css = f'<link rel="stylesheet" href="{base}/static/style.css">'
clear = "<style>html,body{background:none!important}.boot-splash{background:none!important}</style>"
box = ('<div class="boot-splash"><div class="boot-box"><span class="dp-mark boot-mark" id="m"></span><p class="boot-text" id="t">Setting up, please wait…</p>'
       '<div class="boot-bar" id="b" style="--boot-p:{p}"><i id="f"></i></div></div></div>')
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1920, "height": 1080})
    pg.goto(base + "/healthz")   # same origin, so the stylesheet's relative brand/ URLs resolve
    pg.set_content(f'<html><head>{css}</head><body><div class="boot-splash"></div></body></html>')
    pg.wait_for_timeout(500)
    pg.screenshot(path=f"{out}/bg.png")
    pg.set_content(f'<html><head>{css}{clear}</head><body>{box.replace("{p}", "0%")}</body></html>')
    pg.evaluate("document.fonts.ready")
    pg.wait_for_timeout(900)
    pg.locator("#t").screenshot(path=f"{out}/text.png", omit_background=True)
    pg.locator("#m").screenshot(path=f"{out}/icon.png", omit_background=True)
    # the column as the page lays it out, for the Plymouth script's gaps (checked by t/ against the script)
    print("layout", pg.evaluate("""() => ['m','t','b'].map(i => { const r = document.getElementById(i)
      .getBoundingClientRect(); return i + ' ' + Math.round(r.top) + ' ' + Math.round(r.height); }).join(' | ')"""))
    pg.locator("#b").screenshot(path=f"{out}/track.png", omit_background=True)
    pg.set_content(f'<html><head>{css}{clear}<style>.boot-bar{{background:none!important}}</style></head>'
                   f'<body>{box.replace("{p}", "100%")}</body></html>')
    pg.wait_for_timeout(900)
    pg.locator("#b").screenshot(path=f"{out}/fill.png", omit_background=True)
    b.close()
print("rendered", *(f"{out}/{n}.png" for n in ("bg", "icon", "text", "track", "fill")))
