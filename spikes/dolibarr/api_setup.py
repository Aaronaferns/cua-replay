"""Spike: prepare fixtures via REST (noperm password + API key), list draft invoices."""
import json
import urllib.request

BASE = "http://127.0.0.1:8085/api/index.php"


def call(method, path, key="demokey", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                 headers={"DOLAPIKEY": key, "Accept": "application/json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode(errors="replace")


print("set noperm password:", call("PUT", "/users/22", body={"pass": "spike-noperm"}))
st, tok = call("GET", "/login?login=noperm&password=spike-noperm", key="")
print("noperm login:", st, tok if st != 200 else "token ok")
noperm_key = tok["success"]["token"] if st == 200 else None
if noperm_key:
    print("noperm GET /invoices:", call("GET", "/invoices?limit=1", key=noperm_key)[0])
    print("noperm GET /thirdparties:", call("GET", "/thirdparties?limit=3", key=noperm_key))

st, inv = call("GET", "/invoices?limit=200&sqlfilters=(t.fk_statut:=:0)")
print("draft invoices:", st)
for i in inv[:10]:
    print("  ", i["id"], i["ref"], "socid", i["socid"], "total_ttc", i["total_ttc"], "lines", len(i.get("lines", [])))
st, tp = call("GET", "/thirdparties?limit=5&sqlfilters=(t.code_client:like:'CU1910-0002%')")
print("codes like CU1910-0002%:", st, [(t["id"], t["code_client"], t["name"]) for t in tp] if st == 200 else tp)
