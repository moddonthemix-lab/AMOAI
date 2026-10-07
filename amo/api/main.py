"""AMO API server.

- OpenAI-compatible endpoints so Open WebUI (and anything else) can use AMO
  as a model, with voice: /v1/models, /v1/chat/completions,
  /v1/audio/transcriptions (Whisper), /v1/audio/speech (Piper)
- REST endpoints for every tracker (/api/...)
- A built-in dashboard at /

Run: amo serve   (or: uvicorn amo.api.main:app --host 0.0.0.0 --port 8765)
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from .. import __version__
from ..agent import Agent
from ..config import settings
from ..crm import StudioCRM
from ..cravvr import Cravvr
from ..db import get_db
from ..finance import Finance, dashboard
from ..goals import Goals
from ..llm import LLMError, get_llm
from ..memory import Memory
from ..notify import mark_read
from ..reselling import Reselling
from ..trading import TradingJournal

log = logging.getLogger("amo")
STATIC = Path(__file__).parent / "static"
MODEL_ID = "amo"


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = None
    if getattr(app.state, "run_scheduler", True):
        from ..scheduler import Scheduler

        scheduler = Scheduler()
        scheduler.start()
    yield
    if scheduler:
        scheduler.stop()


app = FastAPI(title="AMO", version=__version__, lifespan=lifespan)


def auth(authorization: Optional[str] = Header(default=None)) -> None:
    if not settings.api_key:
        return
    if authorization != f"Bearer {settings.api_key}":
        raise HTTPException(401, "invalid or missing API key")


def _ok(value: Any) -> Any:
    if value is None:
        raise HTTPException(404, "not found")
    return value


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ======================================================== OpenAI-compatible
class ChatMessage(BaseModel):
    role: str
    content: Any = ""


class ChatRequest(BaseModel):
    model: str = MODEL_ID
    messages: list[ChatMessage]
    stream: bool = False
    # Open WebUI passes extras (temperature, etc.); accept and ignore them.
    model_config = {"extra": "allow"}


@app.get("/v1/models", dependencies=[Depends(auth)])
def list_models():
    return {"object": "list", "data": [
        {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "local",
         "name": settings.assistant_name},
    ]}


@app.post("/v1/chat/completions", dependencies=[Depends(auth)])
async def chat_completions(req: ChatRequest):
    messages = [m.model_dump() for m in req.messages]
    # Open WebUI's background tasks (title/tag/follow-up generation) shouldn't hit tools,
    # memory or learning; pass them straight to the model.
    last = str(messages[-1].get("content", "")) if messages else ""
    is_task = last.lstrip().startswith("### Task:")
    try:
        if is_task:
            msg = await run_in_threadpool(get_llm().chat, messages, settings.fast_model)
            content = msg.get("content", "")
        else:
            result = await run_in_threadpool(Agent().chat, messages, "webui")
            content = result["content"]
    except LLMError as e:
        raise HTTPException(502, f"{e}. Is Ollama running at {settings.ollama_url}?") from e

    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    if not req.stream:
        return {
            "id": cid, "object": "chat.completion", "created": created, "model": MODEL_ID,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def sse():
        def chunk(delta: dict, finish: str | None = None) -> str:
            return "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }) + "\n\n"

        yield chunk({"role": "assistant"})
        words = content.split(" ")
        for i in range(0, len(words), 4):
            piece = " ".join(words[i:i + 4]) + (" " if i + 4 < len(words) else "")
            yield chunk({"content": piece})
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


@app.post("/v1/audio/transcriptions", dependencies=[Depends(auth)])
async def transcriptions(file: UploadFile = File(...), model: str = Form("whisper-1"),
                         language: Optional[str] = Form(None)):
    from ..voice.stt import VoiceUnavailable, transcribe

    data = await file.read()
    try:
        text = await run_in_threadpool(transcribe, data, language or "en")
    except VoiceUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return {"text": text}


class SpeechRequest(BaseModel):
    input: str
    model: str = "piper"
    voice: str = "default"
    response_format: str = "wav"


@app.post("/v1/audio/speech", dependencies=[Depends(auth)])
async def speech(req: SpeechRequest):
    from ..voice.stt import VoiceUnavailable
    from ..voice.tts import synthesize

    try:
        wav = await run_in_threadpool(synthesize, req.input)
    except VoiceUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return Response(wav, media_type="audio/wav")


# ======================================================== native chat
class AskRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    channel: Literal["webui", "voice", "cli", "device"] = "cli"


@app.post("/api/ask", dependencies=[Depends(auth)])
async def ask(req: AskRequest):
    msgs = [m.model_dump() for m in req.history] + [{"role": "user", "content": req.message}]
    try:
        return await run_in_threadpool(Agent().chat, msgs, req.channel)
    except LLMError as e:
        raise HTTPException(502, str(e)) from e


# ======================================================== status
@app.get("/api/health")
def health():
    models = get_llm().list_models()
    return {
        "status": "ok",
        "version": __version__,
        "ollama": bool(models),
        "chat_model_installed": any(m.split(":")[0] == settings.chat_model.split(":")[0] for m in models),
        "models": models,
        "memories": Memory(get_db(), use_embeddings=False).stats(),
    }


@app.get("/api/dashboard", dependencies=[Depends(auth)])
def get_dashboard():
    return dashboard(get_db())


@app.get("/api/brief", dependencies=[Depends(auth)])
def brief():
    from ..scheduler import morning_brief_text

    return {"text": morning_brief_text(get_db())}


@app.get("/api/revenue", dependencies=[Depends(auth)])
def get_revenue(period: str = "month"):
    fin = Finance(get_db())
    return {**_guard(fin.revenue, period), "daily": fin.daily_series(30)}


@app.get("/api/notifications", dependencies=[Depends(auth)])
def notifications(unread_only: bool = False, limit: int = 30):
    sql = "SELECT * FROM notifications" + (" WHERE read = 0" if unread_only else "")
    return get_db().all(sql + " ORDER BY id DESC LIMIT ?", (limit,))


@app.post("/api/notifications/read", dependencies=[Depends(auth)])
def notifications_read(id: Optional[int] = None):
    return {"updated": mark_read(id)}


# ======================================================== memory
class MemoryIn(BaseModel):
    content: str
    category: str = "general"
    importance: int = 3


class MemoryPatch(BaseModel):
    content: Optional[str] = None
    category: Optional[str] = None
    importance: Optional[int] = None


@app.get("/api/memories", dependencies=[Depends(auth)])
def memories(q: Optional[str] = None, category: Optional[str] = None, limit: int = 100):
    mem = Memory(get_db())
    if q:
        return mem.search(q, limit=limit, category=category, touch=False)
    return mem.list(category, limit)


@app.post("/api/memories", dependencies=[Depends(auth)])
def add_memory(m: MemoryIn):
    return _guard(Memory(get_db()).add, m.content, m.category, m.importance, "manual")


@app.patch("/api/memories/{mem_id}", dependencies=[Depends(auth)])
def patch_memory(mem_id: int, m: MemoryPatch):
    mem = Memory(get_db(), use_embeddings=False)
    mem.update(mem_id, **m.model_dump(exclude_none=True))
    return _ok(mem.get(mem_id))


@app.delete("/api/memories/{mem_id}", dependencies=[Depends(auth)])
def delete_memory(mem_id: int):
    return {"forgotten": Memory(get_db(), use_embeddings=False).forget(mem_id)}


@app.post("/api/learn", dependencies=[Depends(auth)])
async def learn_now():
    from ..learning import learn_from_conversations

    try:
        return {"learned": await run_in_threadpool(learn_from_conversations, get_db())}
    except LLMError as e:
        raise HTTPException(502, str(e)) from e


@app.post("/api/reflect", dependencies=[Depends(auth)])
async def reflect_now():
    from ..learning import weekly_reflection

    try:
        return await run_in_threadpool(weekly_reflection, get_db())
    except (LLMError, ValueError) as e:
        raise HTTPException(502, str(e)) from e


@app.get("/api/reflections", dependencies=[Depends(auth)])
def reflections(limit: int = 10):
    return get_db().all("SELECT * FROM reflections ORDER BY id DESC LIMIT ?", (limit,))


# ======================================================== studio CRM
class ClientIn(BaseModel):
    name: str
    artist_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    instagram: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class SessionIn(BaseModel):
    client: str
    starts_at: str
    hours: float = 2
    service: str = "recording"
    rate: float = 0
    notes: Optional[str] = None


class SessionPatch(BaseModel):
    starts_at: Optional[str] = None
    hours: Optional[float] = None
    service: Optional[str] = None
    rate: Optional[float] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class PaymentIn(BaseModel):
    amount: float
    client: Optional[str] = None
    session_id: Optional[int] = None
    method: Optional[str] = None
    business: str = "studio"
    note: Optional[str] = None
    paid_at: Optional[str] = None


@app.get("/api/clients", dependencies=[Depends(auth)])
def clients(q: Optional[str] = None, status: Optional[str] = None):
    crm = StudioCRM(get_db())
    return crm.find_client(q) if q else crm.list_clients(status)


@app.post("/api/clients", dependencies=[Depends(auth)])
def create_client(c: ClientIn):
    d = c.model_dump(exclude_none=True)
    return StudioCRM(get_db()).add_client(d.pop("name"), **d)


@app.get("/api/clients/{client_id}", dependencies=[Depends(auth)])
def get_client(client_id: int):
    crm = StudioCRM(get_db())
    c = _ok(crm.get_client(client_id))
    c["sessions"] = get_db().all(
        "SELECT *, hours * rate AS total FROM studio_sessions WHERE client_id = ? ORDER BY starts_at DESC",
        (client_id,))
    return c


@app.patch("/api/clients/{client_id}", dependencies=[Depends(auth)])
def patch_client(client_id: int, c: ClientIn):
    return _ok(StudioCRM(get_db()).update_client(client_id, **c.model_dump(exclude_none=True)))


@app.get("/api/sessions", dependencies=[Depends(auth)])
def sessions(start: Optional[str] = None, end: Optional[str] = None, days: int = 14):
    crm = StudioCRM(get_db())
    if start and end:
        return crm.sessions_between(start, end, include_cancelled=True)
    return crm.upcoming(days)


@app.post("/api/sessions", dependencies=[Depends(auth)])
def create_session(s: SessionIn):
    d = s.model_dump()
    return _guard(StudioCRM(get_db()).book_session, d.pop("client"), d.pop("starts_at"), **d)


@app.patch("/api/sessions/{session_id}", dependencies=[Depends(auth)])
def patch_session(session_id: int, s: SessionPatch):
    return _ok(_guard(StudioCRM(get_db()).update_session, session_id, **s.model_dump(exclude_none=True)))


@app.get("/api/payments", dependencies=[Depends(auth)])
def payments(limit: int = 50):
    return get_db().all(
        "SELECT p.*, c.name AS client_name FROM payments p LEFT JOIN clients c ON c.id = p.client_id "
        "ORDER BY paid_at DESC LIMIT ?", (limit,))


@app.post("/api/payments", dependencies=[Depends(auth)])
def create_payment(p: PaymentIn):
    d = p.model_dump()
    return _guard(StudioCRM(get_db()).record_payment, d.pop("amount"), **d)


@app.get("/api/unpaid", dependencies=[Depends(auth)])
def unpaid():
    return StudioCRM(get_db()).unpaid_sessions()


# ======================================================== reselling
class ItemIn(BaseModel):
    name: str
    cost: float = 0
    sku: Optional[str] = None
    category: Optional[str] = None
    source: Optional[str] = None
    list_price: Optional[float] = None
    platform: Optional[str] = None
    bought_at: Optional[str] = None
    notes: Optional[str] = None


class SoldIn(BaseModel):
    sold_price: float
    platform: Optional[str] = None
    fees: Optional[float] = None
    shipping: Optional[float] = None
    sold_at: Optional[str] = None


@app.get("/api/resale", dependencies=[Depends(auth)])
def resale(status: Optional[str] = None, q: Optional[str] = None):
    r = Reselling(get_db())
    if q:
        return r.find(q)
    if status == "sold":
        return get_db().all(
            "SELECT *, (COALESCE(sold_price,0) - cost - fees - shipping) AS profit FROM resale_items "
            "WHERE status = 'sold' ORDER BY sold_at DESC LIMIT 100")
    return r.inventory(status)


@app.post("/api/resale", dependencies=[Depends(auth)])
def create_item(i: ItemIn):
    d = i.model_dump(exclude_none=True)
    return _guard(Reselling(get_db()).add_item, d.pop("name"), d.pop("cost"), **d)


@app.post("/api/resale/{item_id}/sold", dependencies=[Depends(auth)])
def sell_item(item_id: int, s: SoldIn):
    return _guard(Reselling(get_db()).mark_sold, item_id, **s.model_dump())


@app.get("/api/resale/summary", dependencies=[Depends(auth)])
def resale_summary(since: Optional[str] = None):
    return Reselling(get_db()).summary(since)


# ======================================================== trading
class TradeIn(BaseModel):
    symbol: str
    side: str
    entry: float
    quantity: float = 1
    stop: Optional[float] = None
    target: Optional[float] = None
    setup: Optional[str] = None
    fees: Optional[float] = None
    opened_at: Optional[str] = None
    emotion: Optional[str] = None
    notes: Optional[str] = None


class CloseIn(BaseModel):
    exit: float
    followed_rules: Optional[bool] = None
    emotion: Optional[str] = None
    notes: Optional[str] = None
    fees: Optional[float] = None
    closed_at: Optional[str] = None


class RuleIn(BaseModel):
    rule: str


@app.get("/api/trades", dependencies=[Depends(auth)])
def trades(open_only: bool = False, limit: int = 50):
    tj = TradingJournal(get_db())
    return tj.open_positions() if open_only else tj.recent(limit)


@app.post("/api/trades", dependencies=[Depends(auth)])
def create_trade(t: TradeIn):
    d = t.model_dump(exclude_none=True)
    return _guard(TradingJournal(get_db()).open_trade, d.pop("symbol"), d.pop("side"), d.pop("entry"), **d)


@app.post("/api/trades/{trade_id}/close", dependencies=[Depends(auth)])
def close_trade(trade_id: int, c: CloseIn):
    return _guard(TradingJournal(get_db()).close_trade, trade_id, **c.model_dump())


@app.get("/api/trades/stats", dependencies=[Depends(auth)])
def trade_stats(since: Optional[str] = None):
    return TradingJournal(get_db()).stats(since)


@app.get("/api/rules", dependencies=[Depends(auth)])
def rules():
    return TradingJournal(get_db()).rules()


@app.post("/api/rules", dependencies=[Depends(auth)])
def add_rule(r: RuleIn):
    return TradingJournal(get_db()).add_rule(r.rule)


@app.delete("/api/rules/{rule_id}", dependencies=[Depends(auth)])
def delete_rule(rule_id: int):
    return {"removed": TradingJournal(get_db()).remove_rule(rule_id)}


# ======================================================== goals & cravvr
class GoalIn(BaseModel):
    title: str
    area: str = "general"
    cadence: str = "daily"
    target: Optional[float] = None
    due_date: Optional[str] = None


class CheckIn(BaseModel):
    done: bool = True
    value: Optional[float] = None
    note: Optional[str] = None
    day: Optional[str] = None


class TaskIn(BaseModel):
    title: str
    priority: int = 3
    due_date: Optional[str] = None
    notes: Optional[str] = None


class TaskStatus(BaseModel):
    status: str


@app.get("/api/goals", dependencies=[Depends(auth)])
def goals(area: Optional[str] = None):
    return Goals(get_db()).active(area)


@app.post("/api/goals", dependencies=[Depends(auth)])
def create_goal(g: GoalIn):
    d = g.model_dump()
    return _guard(Goals(get_db()).add, d.pop("title"), **d)


@app.post("/api/goals/{goal_id}/checkin", dependencies=[Depends(auth)])
def goal_checkin(goal_id: int, c: CheckIn):
    return _guard(Goals(get_db()).check_in, goal_id, **c.model_dump())


@app.delete("/api/goals/{goal_id}", dependencies=[Depends(auth)])
def archive_goal(goal_id: int):
    return _ok(Goals(get_db()).update(goal_id, active=0))


@app.get("/api/cravvr", dependencies=[Depends(auth)])
def cravvr_tasks():
    return Cravvr(get_db()).open_tasks()


@app.post("/api/cravvr", dependencies=[Depends(auth)])
def create_cravvr_task(t: TaskIn):
    d = t.model_dump()
    return _guard(Cravvr(get_db()).add_task, d.pop("title"), **d)


@app.patch("/api/cravvr/{task_id}", dependencies=[Depends(auth)])
def patch_cravvr_task(task_id: int, s: TaskStatus):
    return _ok(_guard(Cravvr(get_db()).set_status, task_id, s.status))


# ======================================================== dashboard UI
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")
