"""Spike S1 (Dolibarr): AI snapshots on real screens, acting by ref, stale refs, normalize()."""
import asyncio
import pathlib
import re
import time

from playwright.async_api import async_playwright

BASE = "http://localhost:8085"
OUT = pathlib.Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)


async def snap(page, name):
    t = time.perf_counter()
    text = await page.aria_snapshot(mode="ai")
    ms = (time.perf_counter() - t) * 1000
    (OUT / f"{name}.snapshot.txt").write_text(text, encoding="utf-8")
    refs = re.findall(r"\[ref=([^\]]+)\]", text)
    print(f"[{name}] url={page.url} chars={len(text)} refs={len(refs)} took={ms:.0f}ms")
    return text


def find_ref(text, pattern):
    for line in text.splitlines():
        if re.search(pattern, line):
            m = re.search(r"\[ref=([^\]]+)\]", line)
            if m:
                return m.group(1), line.strip()
    return None, None


async def ref_count(page, ref, timeout=500):
    loc = page.locator(f"aria-ref={ref}")
    t = time.perf_counter()
    try:
        n = await loc.count()
    except Exception as e:  # noqa: BLE001
        return f"error {type(e).__name__}: {str(e)[:120]}"
    return f"count={n} in {(time.perf_counter() - t) * 1000:.0f}ms"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1366, "height": 900}, device_scale_factor=1)

        await page.goto(f"{BASE}/index.php")
        login = await snap(page, "01_login")
        # Act purely by refs from the snapshot, the way the agent will.
        user_ref, user_line = find_ref(login, r"textbox")
        print("  user field:", user_line)
        await page.locator(f"aria-ref={user_ref}").fill("admin")
        pw_ref, pw_line = find_ref(login, r"textbox.*[Pp]assword|password")
        print("  password field:", pw_line)
        if pw_ref is None:
            await page.locator("input[name=password]").fill("spike-admin")
        else:
            await page.locator(f"aria-ref={pw_ref}").fill("spike-admin")
        btn_ref, btn_line = find_ref(login, r"button")
        print("  login button:", btn_line)
        await page.locator(f"aria-ref={btn_ref}").click()
        await page.wait_for_load_state("load")
        home = await snap(page, "02_home")

        # Stale ref behaviour: a ref from the login page after navigation.
        print("  stale login ref after navigation:", await ref_count(page, user_ref))

        await page.goto(f"{BASE}/societe/list.php?leftmenu=customers&type=c")
        lst = await snap(page, "03_customer_list")
        await page.goto(f"{BASE}/comm/card.php?socid=10")
        await snap(page, "04_customer_card")
        await page.goto(f"{BASE}/compta/facture/list.php?search_status=0")
        await snap(page, "05_draft_invoice_list")

        # normalize() on a few elements from the customer list page.
        await page.goto(f"{BASE}/societe/list.php?leftmenu=customers&type=c")
        lst = await page.aria_snapshot(mode="ai")
        for pat in (r"textbox", r"button", r"link \"NLTechno\"", r"combobox"):
            ref, line = find_ref(lst, pat)
            if ref:
                try:
                    norm = await page.locator(f"aria-ref={ref}").normalize()
                    print(f"  normalize({line[:70]}) -> {norm}")
                except Exception as e:  # noqa: BLE001
                    print(f"  normalize({line[:70]}) failed: {e}")

        # Boxes mode
        boxed = await page.aria_snapshot(mode="ai", boxes=True)
        print("  boxes sample:", [l.strip() for l in boxed.splitlines() if "box=" in l][:2])
        await browser.close()


asyncio.run(main())
