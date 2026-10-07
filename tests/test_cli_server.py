import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from modd.cli import main
from modd.db import Store
from modd.server import make_handler


def test_cli_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODD_HOME", str(tmp_path))
    assert main(["goal", "add", "Ship", "Cravvr", "menu", "--area", "cravvr"]) == 0
    assert main(["trade", "log", "spy", "1", "400", "--exit", "405", "--broke-rules"]) == 0
    assert main(["item", "add", "Hoodie", "20"]) == 0
    assert main(["item", "sell", "1", "60", "--fees", "5"]) == 0
    assert main(["today"]) == 0
    out = capsys.readouterr().out
    assert "Ship Cravvr menu" in out and "P&L $5.00" in out and "profit $35.00" in out
    assert (tmp_path / "modd.db").exists()


def _serve(store, token=""):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, None, token))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _req(url, method="GET", body=None, token=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_dashboard_and_goals():
    store = Store()
    httpd, base = _serve(store)
    try:
        assert _req(base + "/api/health") == (200, {"ok": True})
        status, body = _req(base + "/api/goals", "POST", {"text": "Call 3 clients"})
        assert status == 201
        assert _req(f"{base}/api/goals/{body['goal_id']}/done", "POST")[1] == {"updated": True}
        dash = _req(base + "/api/dashboard")[1]
        assert dash["goals"][0]["done"] == 1
        assert _req(base + "/api/ask", "POST", {"text": "hi"})[0] == 503
        assert _req(base + "/api/nope")[0] == 404
    finally:
        httpd.shutdown()


def test_api_token():
    httpd, base = _serve(Store(), token="s3cret")
    try:
        assert _req(base + "/api/dashboard")[0] == 401
        assert _req(base + "/api/dashboard", token="s3cret")[0] == 200
    finally:
        httpd.shutdown()
