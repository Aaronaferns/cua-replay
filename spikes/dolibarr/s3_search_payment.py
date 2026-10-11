"""Spike S3 (Dolibarr): near-miss search rows, and the payment endpoints for a double-post oracle."""
import asyncio
import json
import pathlib
import re
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://localhost:8085"
API = "http://127.0.0.1:8085/api/index.php"
OUT = pathlib.Path(__file__).parent / "out"


def api(path):
    req = urllib.request.Request(f"{API}{path}", headers={"DOLAPIKEY": "demokey", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode(errors="replace")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"{BASE}/index.php")
        await page.fill("input[name=username]", "admin")
        await page.fill("input[name=password]", "spike-admin")
        await page.click("input[type=submit], button[type=submit]")
        await page.wait_for_load_state("load")
        await page.goto(f"{BASE}/societe/list.php?search_customer_code=CU1910-0002")
        snap = await page.aria_snapshot(mode="ai")
        (OUT / "12_search_nearmiss.snapshot.txt").write_text(snap, encoding="utf-8")
        rows = [l.strip() for l in snap.splitlines() if re.search(r"Company Corp|CU1910|row \"|record", l)]
        print("[near-miss rows]", rows[:8])
        await browser.close()

    print("[payments of 217]", api("/invoices/217/payments"))
    print("[payments of 2 (paid)]", api("/invoices/2/payments"))


asyncio.run(main())
