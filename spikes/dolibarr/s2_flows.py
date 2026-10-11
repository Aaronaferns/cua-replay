"""Spike S2 (Dolibarr): reproduce and verify the brief's runtime conditions on a real app."""
import asyncio
import json
import pathlib
import re
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://localhost:8085"
API = "http://127.0.0.1:8085/api/index.php"
OUT = pathlib.Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)


def api(path):
    req = urllib.request.Request(f"{API}{path}", headers={"DOLAPIKEY": "demokey", "Accept": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def main_text(snapshot: str, limit=25):
    """Lines that carry visible text, minus the left/top menu noise."""
    lines = [l for l in snapshot.splitlines() if re.search(r'(text:|heading|dialog|alert|cell "|button "|"[^"]{3,}")', l)]
    return "\n".join(l.strip()[:140] for l in lines[:limit])


async def login(page, user, pw):
    await page.goto(f"{BASE}/index.php")
    await page.fill("input[name=username]", user)
    await page.fill("input[name=password]", pw)
    await page.click("input[type=submit], button[type=submit]")
    await page.wait_for_load_state("load")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # 1. Permission denial: real user without rights opens an invoice.
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await login(page, "noperm", "spike-noperm")
        await page.goto(f"{BASE}/compta/facture/card.php?facid=216")
        snap = await page.aria_snapshot(mode="ai")
        (OUT / "10_noperm_invoice.snapshot.txt").write_text(snap, encoding="utf-8")
        hits = [l.strip() for l in snap.splitlines() if re.search(r"(?i)access|denied|forbidden|permission|not allowed", l)]
        print("[1 permission] url:", page.url, "\n   ", hits[:4])
        await ctx.close()

        ctx = await browser.new_context()
        page = await ctx.new_page()
        await login(page, "admin", "spike-admin")

        # 2. Search behaviour: code that does not exist but prefixes another one.
        for url in (f"{BASE}/societe/list.php?search_customer_code=CU1910-0002",
                    f"{BASE}/societe/list.php?sall=CU1910-0002",
                    f"{BASE}/societe/list.php?search_customer_code=ZZ-NO-SUCH"):
            await page.goto(url)
            snap = await page.aria_snapshot(mode="ai")
            rows = [l.strip() for l in snap.splitlines() if "CU1910" in l or "No record" in l or "No records" in l]
            print("[2 search]", url.split("?")[1], "->", page.url.replace(BASE, ""), rows[:3])

        # 3. Validate a draft through the real confirmation dialog.
        await page.goto(f"{BASE}/compta/facture/card.php?facid=216")
        await page.get_by_role("link", name=re.compile(r"^\s*Validate\s*$")).click()
        await page.wait_for_timeout(800)
        snap = await page.aria_snapshot(mode="ai")
        (OUT / "11_validate_dialog.snapshot.txt").write_text(snap, encoding="utf-8")
        dlg = [l.strip() for l in snap.splitlines() if re.search(r"dialog|Yes|validate this invoice|Validate invoice", l)]
        print("[3 dialog]", page.url.replace(BASE, ""), dlg[:6])
        await page.get_by_role("button", name="Yes").click()
        await page.wait_for_load_state("load")
        inv = api("/invoices/216")
        print("[3 commit] UI url:", page.url.replace(BASE, ""), "| REST: ref", inv["ref"], "status", inv["statut"])

        # 4. Lost response after commit: request reaches Dolibarr, browser sees a 504.
        seen = []

        async def lose_response(route):
            req = route.request
            if "confirm_valid" in req.url and "confirm=yes" in (req.url + (req.post_data or "")):
                resp = await route.fetch()  # the real commit happens here
                seen.append((req.method, resp.status))
                await route.fulfill(status=504, content_type="text/html",
                                    body="<html><head><title>504 Gateway Time-out</title></head>"
                                         "<body><h1>504 Gateway Time-out</h1></body></html>")
            else:
                await route.continue_()

        await page.route("**/compta/facture/card.php*", lose_response)
        await page.goto(f"{BASE}/compta/facture/card.php?facid=217")
        await page.get_by_role("link", name=re.compile(r"^\s*Validate\s*$")).click()
        await page.wait_for_timeout(800)
        await page.get_by_role("button", name="Yes").click()
        await page.wait_for_load_state("load")
        snap = await page.aria_snapshot(mode="ai")
        inv = api("/invoices/217")
        print("[4 lost response] intercepted:", seen, "| browser shows:", main_text(snap, 3).replace("\n", " / "),
              "| REST: ref", inv["ref"], "status", inv["statut"])
        await page.unroute("**/compta/facture/card.php*")

        # 5. Session expiry: drop the session cookie, then try to work.
        await ctx.clear_cookies()
        await page.goto(f"{BASE}/compta/facture/card.php?facid=218")
        snap = await page.aria_snapshot(mode="ai")
        print("[5 session expiry] ->", page.url.replace(BASE, ""), "| login form:", "textbox \"Password\"" in snap)
        await browser.close()


asyncio.run(main())
