"""Spike: inspect Dolibarr demo data through its REST API (ground-truth channel)."""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8085/api/index.php"
KEY = sys.argv[1] if len(sys.argv) > 1 else "demokey"


def get(path: str):
    req = urllib.request.Request(f"{BASE}{path}", headers={"DOLAPIKEY": KEY, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200]


st, tps = get("/thirdparties?limit=100&sortfield=t.rowid")
print("thirdparties", st, len(tps) if isinstance(tps, list) else tps)
for t in tps[:12]:
    print("  ", t["id"], t.get("code_client"), "|", t["name"], "| client:", t.get("client"))

st, inv = get("/invoices?limit=200&sortfield=t.rowid")
print("invoices", st, len(inv) if isinstance(inv, list) else inv)
by_status = {}
for i in inv if isinstance(inv, list) else []:
    by_status[i["statut"]] = by_status.get(i["statut"], 0) + 1
print("   by status (0 draft,1 validated,2 paid,3 abandoned):", by_status)
for i in (inv or [])[:6]:
    print("  ", i["id"], i["ref"], "socid", i["socid"], "status", i["statut"], "total_ttc", i["total_ttc"], "paye", i.get("paye"))

st, users = get("/users?limit=50")
print("users", st, len(users) if isinstance(users, list) else users)
for u in users if isinstance(users, list) else []:
    print("   ", u["id"], u["login"], "admin" if u.get("admin") == "1" else "", "status", u.get("statut"))

st, banks = get("/bankaccounts")
print("bankaccounts", st, [(b["id"], b["ref"], b["label"]) for b in banks] if isinstance(banks, list) else banks)
