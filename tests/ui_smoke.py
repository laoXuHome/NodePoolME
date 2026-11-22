import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:18000"
SHOTS = "/tmp/shots"
os.makedirs(SHOTS, exist_ok=True)

errors = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 950})
    page.on("console", lambda m: errors.append(f"console:{m.type}:{m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))

    page.goto(f"{BASE}/#/login", wait_until="networkidle")
    page.screenshot(path=f"{SHOTS}/01-login.png")
    page.fill("input[type=password]", "test123")
    page.click("button:has-text('进入控制台')")
    page.wait_for_url("**/#/dashboard", timeout=10000)
    page.wait_for_timeout(1500)
    assert "v1.0.0" not in page.inner_text("body"), "sidebar version still shows the login-route fallback"
    page.screenshot(path=f"{SHOTS}/02-dashboard.png", full_page=True)

    routes = ["airports", "nodes", "groups", "subscription", "settings"]
    for name in routes:
        page.goto(f"{BASE}/#/{name}", wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)
        text = page.inner_text("body")
        print(f"{name}: chars={len(text)} rows={page.locator('.el-table__row').count()}")

    # interaction checks
    page.goto(f"{BASE}/#/airports", wait_until="networkidle")
    page.click("button:has-text('添加机场')")
    page.wait_for_timeout(600)
    page.fill("input[placeholder='例如：机场A']", "验证机场")
    page.fill("textarea", "http://127.0.0.1:18001/sub.txt")
    page.click("button:has-text('保存')")
    page.wait_for_timeout(1200)
    print("airport rows:", page.locator(".el-table__row").count())
    page.screenshot(path=f"{SHOTS}/airports-add.png", full_page=True)

    page.goto(f"{BASE}/#/nodes", wait_until="networkidle")
    page.wait_for_timeout(800)
    print("node rows:", page.locator(".el-table__row").count())
    page.locator(".el-table__row").first.click()
    page.wait_for_timeout(800)
    page.screenshot(path=f"{SHOTS}/node-detail.png", full_page=True)
    print("dialog visible:", page.locator(".el-dialog").is_visible())

    browser.close()

print("errors:", len(errors))
for e in errors[:20]:
    print(" -", e)
sys.exit(1 if any("pageerror" in e for e in errors) else 0)
