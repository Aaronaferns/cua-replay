"""Spike S4 (Dolibarr): login CAPTCHA as the 'only a human can do this' handoff trigger."""
import asyncio
import re

from playwright.async_api import async_playwright

BASE = "http://localhost:8085"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"{BASE}/index.php")
        snap = await page.aria_snapshot(mode="ai")
        print("[login page]", [l.strip() for l in snap.splitlines() if re.search(r"textbox|img|button", l)])
        await page.fill("input[name=username]", "admin")
        await page.fill("input[name=password]", "spike-admin")
        await page.click("input[type=submit], button[type=submit]")
        await page.wait_for_load_state("load")
        snap = await page.aria_snapshot(mode="ai")
        msgs = [l.strip() for l in snap.splitlines() if re.search(r"(?i)code|captcha|bad|wrong|error", l)]
        print("[scripted login without code] url:", page.url, "| still on login form:", 'textbox "Password"' in snap)
        print("   messages:", msgs[:4])
        await browser.close()


asyncio.run(main())
