"""The assistant: builds context from memory + live business data, runs the
local model with tools, and logs every exchange so it can learn from it."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from . import tools
from .config import settings
from .confirm import instant_reply
from .db import Database, get_db, local_now, now_iso
from .llm import LLMError, get_llm
from .memory import Memory

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6

SYSTEM_PROMPT = """You are {assistant}, {owner}'s personal AI operating system. You run 100% locally on their own hardware.

You help run their life and businesses:
- a recording studio (clients, bookings, payments)
- Cravvr (their business project)
- a reselling operation (inventory, listings, profit)
- trading (journal, stats, and their personal trading rules — hold them to their rules)
- daily goals and revenue targets

How you work:
- Greetings, small talk and "what can you do?" → answer directly in your own voice, no tools.
  Briefly offer what you can do: book studio sessions and track clients and payments, log
  reselling buys and sales, journal trades, track goals and revenue, and remember anything {owner} tells you.
- Never say you have "no recall" or "no context", and never talk about your tools or memory system — just help.
- Use the tools to read and write real data. Never invent numbers, clients, trades or bookings — look them up.
- When {owner} tells you something durable (a preference, a person, a price, a rule, a plan), save it with `remember`.
- Be direct and brief, like a sharp chief of staff. Lead with the answer. No filler.
- Dates: today is {today} ({weekday}), local time {time}. Convert relative dates ("Friday", "tomorrow at 7") to 'YYYY-MM-DD HH:MM' before calling tools.
- If a tool returns an error (e.g. an ambiguous client), ask a short clarifying question.
{voice_hint}{personality}
What you know about {owner}:
{core}
{relevant}{reflection}"""

PERSONALITIES = {
    "default": "",
    "computer": (
        "\nPersonality: you are a dry, deadpan, posh British computer with a sarcastic wit. "
        "Crisp, lightly condescending, theatrically unimpressed — but loyal, and you always get the "
        "task exactly right. At most one short quip per reply. Never mock {owner}'s goals, money "
        "worries, health or anything serious, and never use *actions in asterisks*.\n"
    ),
    "jarvis": (
        "\nPersonality: a polished, unflappable British AI butler. Courteous, understated, quietly "
        "witty, always one step ahead. Address {owner} by name now and then.\n"
    ),
}

VOICE_HINT = (
    "- This is a VOICE conversation: answer in 1-3 short spoken sentences. No markdown, lists, "
    "tables or emoji. Say numbers naturally.\n"
)


def _format_memories(rows: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{r['category']}] {r['content']}" for r in rows)


class Agent:
    def __init__(self, db: Database | None = None):
        self.db = db or get_db()
        self.memory = Memory(self.db)

    # ------------------------------------------------------------ context
    def build_system_prompt(self, user_text: str, channel: str = "webui") -> str:
        core = self.memory.core(12)
        core_ids = {m["id"] for m in core}
        relevant = [m for m in self.memory.search(user_text, limit=settings.memory_context_limit)
                    if m["id"] not in core_ids] if user_text.strip() else []
        latest = self.db.one("SELECT summary FROM reflections ORDER BY id DESC LIMIT 1")
        now = local_now()
        return SYSTEM_PROMPT.format(
            assistant=settings.assistant_name,
            owner=settings.owner_name,
            today=now.date().isoformat(),
            weekday=now.strftime("%A"),
            time=now.strftime("%H:%M"),
            voice_hint=VOICE_HINT if channel == "voice" else "",
            personality=PERSONALITIES.get(settings.personality, "").format(owner=settings.owner_name),
            core=_format_memories(core) or "- (nothing yet — learn as you go)",
            relevant=("\nPossibly relevant memories:\n" + _format_memories(relevant) + "\n") if relevant else "",
            reflection=("\nYour latest weekly reflection (patterns you noticed):\n" + latest["summary"] + "\n")
            if latest else "",
        )

    # ------------------------------------------------------------ chat
    def chat(
        self,
        messages: list[dict[str, Any]],
        channel: str = "webui",
        model: str | None = None,
        learn: bool | None = None,
    ) -> dict[str, Any]:
        """Run one assistant turn. `messages` is the chat history WITHOUT a system prompt
        (any client-supplied system message is kept, appended after ours).

        Returns {"content": str, "tool_calls": [{"name", "arguments", "result"}]}.
        """
        history = [m for m in messages if m.get("role") in ("user", "assistant", "system", "tool")]
        user_text = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        if isinstance(user_text, list):  # OpenAI multi-part content
            user_text = " ".join(p.get("text", "") for p in user_text if isinstance(p, dict))
            history = [_flatten(m) for m in history]

        convo: list[dict[str, Any]] = [
            {"role": "system", "content": self.build_system_prompt(user_text, channel)},
            *history,
        ]
        llm = get_llm()
        recent_user = " ".join(
            m["content"] for m in history[-6:] if m["role"] == "user" and isinstance(m["content"], str)
        )
        schemas = tools.schemas(tools.route(recent_user))
        trace: list[dict[str, Any]] = []
        content = ""
        for _ in range(MAX_TOOL_ROUNDS):
            msg = llm.chat(convo, model=model, tools=schemas)
            calls = msg.get("tool_calls") or []
            if not calls:
                content = msg.get("content", "")
                break
            convo.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": calls})
            round_trace: list[dict[str, Any]] = []
            for c in calls:
                fn = c.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments", {})
                result = tools.call(name, args)
                log.info("tool %s(%s) -> %s", name, args, result[:200])
                round_trace.append({"name": name, "arguments": args, "result": json.loads(result)})
                convo.append({"role": "tool", "content": result, "tool_name": name})
            trace += round_trace
            # Pure saves (booking, logging a trade…) don't need a second model pass to confirm.
            quick = instant_reply(round_trace)
            if quick:
                content = quick
                break
        else:
            # Ran out of tool rounds: ask for a final answer without tools.
            content = llm.chat(convo + [{"role": "user", "content": "Summarize what you did."}],
                               model=model).get("content", "")

        self._log(channel, user_text, content)
        # Fact learning normally runs from the scheduler once you've gone quiet, so it never
        # competes with your next message for the CPU. learn=True forces it now (in background).
        if learn:
            threading.Thread(target=self._learn_safely, daemon=True).start()
        return {"content": content, "tool_calls": trace}

    def _log(self, channel: str, user_text: str, reply: str) -> None:
        ts = now_iso()
        with self.db.tx() as c:
            if user_text:
                c.execute("INSERT INTO conversations (channel, role, content, created_at) VALUES (?, 'user', ?, ?)",
                          (channel, user_text, ts))
            if reply:
                c.execute("INSERT INTO conversations (channel, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                          (channel, reply, ts))

    def _learn_safely(self) -> None:
        from .learning import learn_from_conversations

        try:
            learn_from_conversations(self.db)
        except (LLMError, ValueError) as e:
            log.warning("background learning skipped: %s", e)


def _flatten(m: dict[str, Any]) -> dict[str, Any]:
    if isinstance(m.get("content"), list):
        text = " ".join(p.get("text", "") for p in m["content"] if isinstance(p, dict))
        return {**m, "content": text}
    return m
