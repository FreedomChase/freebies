#!/usr/bin/env python3
"""Render the Plymouth theme's pictures from the setup page's own stylesheet (C&G #209 v8.2),
so the Plymouth splash and the page's .boot-splash are the same picture.

  python3 render.py http://127.0.0.1:8080 OUT_DIR     (a running connection manager, mock is fine)

bg.png   1920x1080, the .boot-splash ground without the logo (Plymouth scales it to the screen)
logo.png the logo in its light-to-pink gradient, transparent around it, 560 px wide
Needs playwright (chromium). Not used on the device.
"""
import sys
from playwright.sync_api import sync_playwright

base, out = sys.argv[1].rstrip("/"), sys.argv[2].rstrip("/")
css = f'<link rel="stylesheet" href="{base}/static/style.css">'
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1920, "height": 1080})
    pg.goto(base + "/healthz")   # same origin, so the stylesheet's relative brand/ URLs resolve
    pg.set_content(f'<html><head>{css}</head><body><div class="boot-splash"></div></body></html>')
    pg.wait_for_timeout(500)
    pg.screenshot(path=f"{out}/bg.png")
    pg.set_content(f'<html><head>{css}<style>html,body{{background:none!important}}'
                   f'.op-lockup{{width:560px}}</style></head><body><div class="op-lockup" id="l"></div></body></html>')
    pg.wait_for_timeout(500)
    pg.locator("#l").screenshot(path=f"{out}/logo.png", omit_background=True)
    b.close()
print("rendered", out + "/bg.png", out + "/logo.png")
