"""Fake Turso /v2/pipeline endpoint for offline tests.

Implements just enough of Turso's HTTP pipeline (Hrana)
protocol to exercise the license server's Turso backend
without a Turso account: POST /v2/pipeline with batched
execute/close requests, backed by a REAL sqlite file, so SQL
semantics (constraints, AUTOINCREMENT, lastrowid) are genuine.

Run standalone:
  python3 tests/fake_turso.py [port] [dbfile]

Typed-value mapping mirrors the real protocol: integers travel
as strings, floats as JSON numbers, blobs base64-encoded.
Statement errors come back as HTTP 200 with a per-request
{"type": "error", ...} result, like the real API. A missing
Bearer token gets a 401.
"""
import base64
import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOCK = threading.Lock()
_CONN = None


def _decode_arg(a):
    t = (a or {}).get("type")
    v = (a or {}).get("value")
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    if t == "blob":
        return base64.b64decode(v)
    if t == "null":
        return None
    return v


def _encode_cell(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        return {"type": "blob",
                "value": base64.b64encode(bytes(v)).decode("ascii")}
    return {"type": "text", "value": str(v)}


def _execute(stmt):
    sql = stmt.get("sql") or ""
    args = [_decode_arg(a) for a in stmt.get("args") or []]
    cur = _CONN.execute(sql, args)
    cols = []
    rows = []
    if cur.description:
        cols = [{"name": d[0], "decltype": None}
                for d in cur.description]
        rows = [[_encode_cell(v) for v in row]
                for row in cur.fetchall()]
    return {
        "cols": cols,
        "rows": rows,
        "affected_row_count": cur.rowcount if cur.rowcount > 0 else 0,
        "last_insert_rowid": (str(cur.lastrowid)
                              if cur.lastrowid else None),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep test output clean
        pass

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.rstrip("/") != "/v2/pipeline":
            return self._send_json(404, {"error": "not found"})
        if not (self.headers.get("Authorization") or ""
                ).startswith("Bearer "):
            return self._send_json(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(
                self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            return self._send_json(400, {"error": str(exc)})
        results = []
        with _LOCK:
            for req in payload.get("requests") or []:
                if req.get("type") == "execute":
                    try:
                        result = _execute(req.get("stmt") or {})
                        results.append({
                            "type": "ok",
                            "response": {"type": "execute",
                                         "result": result}})
                    except Exception as exc:  # sqlite error etc.
                        results.append({
                            "type": "error",
                            "error": {"message": str(exc),
                                      "code": "SQLITE_ERROR"}})
                elif req.get("type") == "close":
                    results.append({
                        "type": "ok",
                        "response": {"type": "close"}})
                else:
                    results.append({
                        "type": "error",
                        "error": {"message": "unknown request type",
                                  "code": "UNKNOWN"}})
        self._send_json(200, {"baton": None, "base_url": None,
                              "results": results})


def main():
    global _CONN
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8431
    dbfile = sys.argv[2] if len(sys.argv) > 2 else ":memory:"
    _CONN = sqlite3.connect(dbfile, check_same_thread=False,
                            isolation_level=None)  # autocommit
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("fake turso on 127.0.0.1:%d (db: %s)" % (port, dbfile),
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
