"""A tiny in-memory stand-in for the parts of Dolibarr's REST API that `seed.py` uses, so the seed logic (idempotency,
request shapes, key replacement, access checks) is tested offline. It mimics the behaviour recorded in the spike:
list endpoints answer 404 when empty, `demokey` is the demo admin key, and users without rights get 403."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FakeDolibarr:
    def __init__(self) -> None:
        self.keys = {"demokey": "admin"}  # api key / token -> login
        self.users = {1: {"id": 1, "login": "admin"}, 22: {"id": 22, "login": "noperm"}}
        self.passwords: dict[str, str] = {}
        self.rights: dict[str, str] = {"admin": "write", "noperm": "none"}  # login -> none | read | write
        self.thirdparties: dict[int, dict] = {
            21: {"id": 21, "name": "Company Corp 2", "code_client": "CU1910-00021", "status": "1"}}
        self.invoices: dict[int, dict] = {}
        self.banks = {1: {"id": 1, "ref": "BANK1", "label": "Demo", "currency_code": "EUR", "country_id": "1"}}
        self.next_id = 100
        self.calls: list[tuple[str, str]] = []
        self.api_key_put_works = True
        self.server: ThreadingHTTPServer | None = None

    # -- server --------------------------------------------------------------------------------------------------

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # noqa: D401
                pass

            def _do(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null") if length else None
                status, data = fake.handle(method, self.path, self.headers.get("DOLAPIKEY"), body)
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None: self._do("GET")  # noqa: E704
            def do_POST(self) -> None: self._do("POST")  # noqa: E704
            def do_PUT(self) -> None: self._do("PUT")  # noqa: E704

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}/api/index.php"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()

    # -- behaviour -----------------------------------------------------------------------------------------------

    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    @staticmethod
    def _filter(rows: list[dict], sqlfilters: str | None) -> list[dict]:
        if not sqlfilters:
            return rows
        m = re.fullmatch(r"\(t\.(\w+):=:'([^']*)'\)", sqlfilters)
        assert m, f"unsupported filter {sqlfilters}"
        return [r for r in rows if str(r.get(m.group(1))) == m.group(2)]

    def handle(self, method: str, raw_path: str, key: str | None, body):
        parts = urlsplit(raw_path)
        path = parts.path.removeprefix("/api/index.php")
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append((method, path))
        if path == "/login" and method == "POST":
            login = body["login"]
            if self.passwords.get(login) != body["password"]:
                return 401, {"error": {"code": 401}}
            token = f"tok-{login}"
            self.keys[token] = login
            return 200, {"success": {"code": 200, "token": token}}
        login = self.keys.get(key or "")
        if login is None:
            return 401, {"error": {"code": 401}}
        level = self.rights.get(login, "none")
        if level == "none":
            return 403, {"error": {"code": 403}}

        def listing(rows: list[dict]):
            rows = self._filter(rows, q.get("sqlfilters"))
            if "thirdparty_ids" in q:
                rows = [r for r in rows if str(r["socid"]) == q["thirdparty_ids"]]
            return (200, rows) if rows else (404, {"error": {"code": 404}})

        if method == "GET":
            if path == "/users":
                return listing(list(self.users.values()))
            if path == "/thirdparties":
                return listing(list(self.thirdparties.values()))
            if path == "/invoices":
                return listing(list(self.invoices.values()))
            if path == "/bankaccounts":
                return listing(list(self.banks.values()))
            if m := re.fullmatch(r"/invoices/(\d+)/payments", path):
                return 404, {"error": {"code": 404}}
        if level != "write":
            return 403, {"error": {"code": 403}}
        if method == "POST" and path == "/users":
            uid = self._new_id()
            self.users[uid] = {"id": uid, "login": body["login"]}
            self.rights[body["login"]] = "none"
            return 200, uid
        if method == "PUT" and (m := re.fullmatch(r"/users/(\d+)", path)):
            user = self.users[int(m.group(1))]
            if "pass" in body:
                self.passwords[user["login"]] = body["pass"]
            if "api_key" in body and self.api_key_put_works:
                self.keys = {k: v for k, v in self.keys.items() if v != user["login"]}
                self.keys[body["api_key"]] = user["login"]
            return 200, user
        if method == "POST" and path == "/thirdparties":
            if not body.get("code_client") or len(body["code_client"]) < 11:
                return 500, {"error": {"code": 500}}
            tid = self._new_id()
            self.thirdparties[tid] = {"id": tid, "status": "1", **{k: body[k] for k in ("name", "code_client")}}
            return 200, tid
        if method == "POST" and path == "/invoices":
            iid = self._new_id()
            total = sum(line["qty"] * line["subprice"] for line in body["lines"])
            self.invoices[iid] = {"id": iid, "ref": f"(PROV{iid})", "ref_client": body["ref_client"],
                                  "socid": str(body["socid"]), "statut": "0", "paye": "0", "total_ttc": f"{total:.8f}"}
            return 200, iid
        if method == "POST" and (m := re.fullmatch(r"/invoices/(\d+)/validate", path)):
            self.invoices[int(m.group(1))]["statut"] = "1"
            return 200, self.invoices[int(m.group(1))]
        if method == "POST" and path == "/bankaccounts":
            bid = self._new_id()
            self.banks[bid] = {"id": bid, **{k: body[k] for k in ("ref", "label")}}
            return 200, bid
        return 404, {"error": {"code": 404}}

    # -- what the fake database would answer for the rights SQL ---------------------------------------------------

    def grant(self, login: str, level: str) -> None:
        self.rights[login] = level
