import pytest
from fastapi.testclient import TestClient

from amo.api.main import app
from amo.scheduler import morning_brief_text, tick


@pytest.fixture()
def client(db, llm):
    app.state.run_scheduler = False
    with TestClient(app) as c:
        yield c


def test_openai_compat_chat(client, llm):
    assert client.get("/v1/models").json()["data"][0]["id"] == "amo"
    llm.script({"role": "assistant", "content": "Hey, what's up?"})
    r = client.post("/v1/chat/completions", json={"model": "amo", "messages": [{"role": "user", "content": "yo"}]})
    assert r.json()["choices"][0]["message"]["content"] == "Hey, what's up?"

    llm.script({"role": "assistant", "content": "one two three four five six"})
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "stream": True})
    body = r.text
    assert body.strip().endswith("data: [DONE]") and "five" in body


def test_rest_flow(client):
    c = client.post("/api/clients", json={"name": "Ana Lopez", "phone": "555"}).json()
    s = client.post("/api/sessions", json={"client": "Ana", "starts_at": "2030-02-01 18:00", "rate": 60}).json()
    assert s["client_id"] == c["id"]
    client.patch(f"/api/sessions/{s['id']}", json={"status": "completed"})
    assert client.get("/api/unpaid").json()[0]["total"] == 120
    assert client.post("/api/sessions", json={"client": "Zed", "starts_at": "2030-02-01"}).status_code == 400

    t = client.post("/api/trades", json={"symbol": "AAPL", "side": "long", "entry": 100, "quantity": 5}).json()
    assert client.post(f"/api/trades/{t['id']}/close", json={"exit": 110}).json()["pnl"] == 50

    g = client.post("/api/goals", json={"title": "Post on IG"}).json()
    assert client.post(f"/api/goals/{g['id']}/checkin", json={}).json()["done_today"]

    m = client.post("/api/memories", json={"content": "Favorite DAW is FL Studio", "category": "preference"}).json()
    assert client.get("/api/memories", params={"q": "DAW"}).json()[0]["id"] == m["id"]

    d = client.get("/api/dashboard").json()
    assert d["trading"]["today_pnl"] == 50 and d["goals"][0]["done"]
    assert client.get("/").status_code == 200


def test_scheduler_brief_and_reminders(db, llm):
    from datetime import datetime, timedelta

    from amo.crm import StudioCRM

    crm = StudioCRM(db)
    crm.add_client("Jay", phone="555-1")
    now = datetime(2030, 3, 4, 15, 0)
    crm.book_session("Jay", (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"))
    ran = tick(db, now)
    assert "reminders" in ran and "brief" in ran
    assert tick(db, now) == []  # nothing runs twice
    assert db.scalar("SELECT COUNT(*) FROM notifications") == 2
    assert "Month so far" in morning_brief_text(db)


def test_api_key_and_localhost_trust(client, monkeypatch):
    from amo.config import settings

    monkeypatch.setattr(settings, "api_key", "secret")
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/dashboard", headers={"Authorization": "Bearer secret"}).status_code == 200
    # Requests from the same computer skip the key.
    from fastapi.testclient import TestClient
    from amo.api.main import app

    with TestClient(app, client=("127.0.0.1", 5000)) as local:
        assert local.get("/api/dashboard").status_code == 200
    monkeypatch.setattr(settings, "trust_localhost", False)
    with TestClient(app, client=("127.0.0.1", 5000)) as local:
        assert local.get("/api/dashboard").status_code == 401
