import json

import pytest
from conftest import tool_call
from fastapi.testclient import TestClient

from amo import records
from amo.agent import Agent
from amo.api.main import app


def test_full_lifecycle_for_every_kind(db):
    records.create("clients", {"name": "Jay Carter", "artist_name": "Lil Jay"}, db)
    samples = {
        "goals": {"title": "Make one beat", "cadence": "daily"},
        "clients": {"name": "Ana Lopez", "phone": "555"},
        "sessions": {"client": "Lil Jay", "starts_at": "2030-01-04 19:00", "hours": 3, "rate": "$50"},
        "payments": {"amount": "120", "client_id": "Jay Carter", "method": "cash app"},
        "resale": {"name": "Jordan 4 Bred", "cost": 210},
        "trades": {"symbol": "NQ", "side": "long", "entry": 18000},
        "rules": {"rule": "No trades in the first 5 minutes"},
        "cravvr": {"title": "Finalize menu", "priority": 4},
        "memories": {"content": "Marcus engineers on Tuesdays", "category": "person"},
    }
    for kind, data in samples.items():
        row = records.create(kind, data, db)
        assert row["id"], kind
        assert any(r["id"] == row["id"] for r in records.list_records(kind, db=db)), kind
        records.delete(kind, row["id"], db)
        assert not any(r["id"] == row["id"] for r in records.list_records(kind, db=db)), kind


def test_update_by_name_and_validation(db):
    records.create("goals", {"title": "Make one beat"}, db)
    g = records.update("goals", "one beat", {"cadence": "Weekly", "title": "Make two beats"}, db)
    assert g["cadence"] == "weekly" and g["title"] == "Make two beats"
    with pytest.raises(ValueError, match="must be one of"):
        records.update("goals", "two beats", {"cadence": "hourly"}, db)
    with pytest.raises(ValueError, match="no field"):
        records.update("goals", "two beats", {"colour": "red"}, db)
    item = records.create("resale", {"name": "PS5", "cost": 350}, db)
    sold = records.update("resale", item["id"], {"sold_price": 450}, db)
    assert sold["status"] == "sold" and sold["profit"] == 100


def test_ambiguous_names_ask_which_one(db):
    records.create("goals", {"title": "Make one beat"}, db)
    records.create("goals", {"title": "Sell one beat"}, db)
    with pytest.raises(ValueError, match="more than one goal"):
        records.delete("goals", "beat", db)
    assert records.delete("goals", "Sell one beat", db)["name"] == "Sell one beat"


def test_voice_delete_goal_without_the_model(db, llm):
    records.create("goals", {"title": "Make one beat"}, db)
    out = Agent(db).chat([{"role": "user", "content": "Delete the goal make one beat"}])
    assert llm.calls == [] and out["content"] == "Deleted goal “Make one beat”."
    assert records.list_records("goals", db=db) == []


def test_model_can_update_anything(db, llm):
    records.create("clients", {"name": "Jay Carter"}, db)
    llm.script(tool_call("update_record", kind="clients", item="Jay", changes=json.dumps({"phone": "555-0199"})))
    out = Agent(db).chat([{"role": "user", "content": "change Jay's phone to 555-0199"}])
    assert out["content"] == "Updated client “Jay Carter”: phone → 555-0199."
    assert records.resolve("clients", "Jay", db)["phone"] == "555-0199"


def test_dashboard_api_and_live_version(db, llm):
    app.state.run_scheduler = False
    with TestClient(app) as c:
        v0 = c.get("/api/version").json()["v"]
        g = c.post("/api/records/goals", json={"title": "Post a reel", "cadence": "daily"}).json()
        assert c.get("/api/version").json()["v"] > v0
        assert c.patch(f"/api/records/goals/{g['id']}", json={"cadence": "weekly"}).json()["cadence"] == "weekly"
        assert c.post("/api/records/goals", json={"cadence": "daily"}).status_code == 400  # title required
        assert c.delete(f"/api/records/goals/{g['id']}").json()["deleted"]
        assert c.get("/api/records/goals").json() == []
        assert "fields" in c.get("/api/schema").json()["resale"]
