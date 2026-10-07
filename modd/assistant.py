"""The assistant loop: build context from memory, talk to the model, run tools."""

from __future__ import annotations

import json
from datetime import datetime

from . import tools
from .config import Config
from .db import Store
from .llm import Ollama

MAX_TOOL_ROUNDS = 6
MAX_HISTORY = 30  # messages kept in the rolling window

PERSONA = """You are {name}, {user}'s private, always-on personal assistant — think Jarvis, not a chatbot.
You run 100% locally. You help run:
- the recording studio (clients, sessions, payments),
- Cravvr (the food/tech business),
- the reselling operation (inventory, sales, profit),
- trading (journal, rules, discipline).

How you work:
- Be brief and direct; you may be speaking out loud. No markdown tables unless asked.
- Use tools to read or change data instead of guessing numbers. Never invent figures.
- When {user} tells you something worth keeping (a preference, a client detail, a decision,
  a goal), save it with `remember` without being asked.
- Hold {user} to their trading rules. If a trade breaks a rule, say so plainly.

Now: {now}
"""


class Assistant:
    def __init__(self, store: Store, llm: Ollama, config: Config | None = None):
        self.db = store
        self.llm = llm
        self.config = config or Config()
        self.history: list[dict] = []

    def system_prompt(self, user_text: str) -> str:
        c = self.config
        parts = [PERSONA.format(name=c.assistant_name, user=c.user_name,
                                now=datetime.now().strftime("%A %Y-%m-%d %H:%M"))]
        rules = self.db.rules()
        if rules:
            parts.append("Trading rules:\n" + "\n".join(f"- {r['rule']}" for r in rules))
        memories = self.db.recall(user_text, limit=8)
        if memories:
            parts.append("Relevant memories:\n" + "\n".join(
                f"- [{m['area']}] {m['content']} ({m['created_at'][:10]})" for m in memories))
        dash = self.db.dashboard()
        snapshot = {
            "goals_today": [f"{'x' if g['done'] else ' '} #{g['id']} {g['text']}" for g in dash["goals"]],
            "sessions_today": [f"#{s['id']} {s['starts_at']} {s['client']}" for s in dash["sessions_today"]],
            "unpaid_sessions": len(dash["unpaid_sessions"]),
            "open_trades": [f"#{t['id']} {t['side']} {t['qty']} {t['symbol']} @ {t['entry']}"
                            for t in dash["open_trades"]],
            "revenue_month": dash["revenue_month"],
        }
        parts.append("Today's snapshot:\n" + json.dumps(snapshot))
        return "\n\n".join(parts)

    def ask(self, user_text: str) -> str:
        self.history.append({"role": "user", "content": user_text})
        messages = [{"role": "system", "content": self.system_prompt(user_text)}, *self.history]

        for _ in range(MAX_TOOL_ROUNDS):
            msg = self.llm.chat(messages, tools.schemas())
            calls = msg.get("tool_calls") or []
            messages.append(msg)
            if not calls:
                break
            for call in calls:
                fn = call.get("function", {})
                result = tools.call(self.db, fn.get("name", ""), fn.get("arguments"))
                messages.append({"role": "tool", "content": result, "tool_name": fn.get("name", "")})
        else:
            msg = {"role": "assistant", "content": "I got stuck in a loop of tool calls — try rephrasing?"}
            messages.append(msg)

        reply = (msg.get("content") or "").strip()
        # Keep tool traffic out of the rolling history; the data lives in the DB anyway.
        self.history.append({"role": "assistant", "content": reply})
        self.history = self.history[-MAX_HISTORY:]
        return reply

    def reset(self) -> None:
        self.history.clear()
