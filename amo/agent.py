"""The assistant: builds context from memory + live business data, runs the
local model with tools, and logs every exchange so it can learn from it."""

from __future__ import annotations

import json
import re
import logging
import threading
import time
from typing import Any, Callable

from . import tools
from .config import settings
from . import fastpath
from .confirm import CONFIRM, instant_reply
from .voice.acks import kind_of
from .voice.tts import active_personality
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
- Dates: the current date and time are in the note at the top of {owner}'s latest message. Convert relative dates ("Friday", "tomorrow at 7") to 'YYYY-MM-DD HH:MM' before calling tools.
- You can change or delete anything (goals, clients, sessions, payments, inventory, trades, rules,
  Cravvr tasks, memories) with update_record / delete_record. Only say something is saved, changed or
  deleted after the tool succeeded.
- For anything current or that you're not sure of (news, facts, prices, people), use web_search
  (and read_webpage for detail), then answer briefly and name your source. Don't guess.
- For trading charts and analysis use strat_analysis — it computes The Strat levels from live data.
- If a tool returns an error (e.g. an ambiguous client), ask a short clarifying question.
{voice_hint}{personality}{persona}
What you know about {owner}:
{core}
{favorites}{reflection}"""

# The system prompt above stays byte-for-byte the same from turn to turn, so Ollama can reuse the
# work it already did on it (prompt caching) and start answering in about a second instead of
# re-reading thousands of tokens. Everything that changes every turn — the time, memories relevant
# to this message, the current mood — goes in a short note at the top of the latest message instead.
TURN_NOTE = "({note})\n\n"

PERSONALITIES = {
    "default": "",
    "computer": (
        "\nPersonality: you are a dry, deadpan, posh British computer with a sarcastic wit. "
        "Crisp, lightly condescending, theatrically unimpressed — but loyal, and you always get the "
        "task exactly right. At most one short quip per reply. Never mock {owner}'s goals, money "
        "worries, health or anything serious, and never use *actions in asterisks*.\n"
    ),
    "chill": (
        "\nPersonality: laid-back and calm, like a cool friend from the studio who has everything handled. "
        "Short and easy-going, a little casual slang now and then (\"bet\", \"say less\", \"locked in\", "
        "\"no stress\", \"you're good\") — never forced, never more than one per reply. Unbothered, never "
        "sarcastic, never preachy. Stay accurate with numbers, dates and money, and when something matters "
        "(a broken trading rule, money owed) say it straight, calmly. Never use *actions in asterisks*.\n"
    ),
    "mood": "",  # built each turn from mood.py
    "jarvis": (
        "\nPersonality: a polished, unflappable British AI butler. Courteous, understated, quietly "
        "witty, always one step ahead. Address {owner} by name now and then.\n"
    ),
}

VOICE_HINT = (
    "- This is a VOICE conversation: answer in 1-3 short spoken sentences. No markdown, lists, "
    "tables or emoji. Say numbers naturally.\n"
)


def _persona() -> str:
    try:
        from .persona import prompt_block

        return prompt_block()
    except Exception:  # noqa: BLE001
        return ""


def _favorites_line(db: Database) -> str:
    try:
        from .profile import prompt_line

        return prompt_line(db)
    except Exception:  # noqa: BLE001
        return ""


def _format_memories(rows: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{r['category']}] {r['content']}" for r in rows)


class Agent:
    def __init__(self, db: Database | None = None):
        self.db = db or get_db()
        self.memory = Memory(self.db)
        self._turn = None

    def _call_tool(self, name: str, args: Any) -> str:
        """Run a tool, timed for the speed report."""
        if self._turn is None:
            return tools.call(name, args)
        with self._turn.timer("tool"):
            return tools.call(name, args)

    # ------------------------------------------------------------ context
    def build_system_prompt(self, user_text: str = "", channel: str = "webui") -> str:
        """The stable part of the prompt (see TURN_NOTE for what changes every turn)."""
        core = self.memory.core(12)
        latest = self.db.one("SELECT summary FROM reflections ORDER BY id DESC LIMIT 1")
        name = active_personality()
        if name == "mood":  # how to adapt is stable; the mood right now goes in the turn note
            from . import mood

            personality = mood.BASE_PROMPT.format(owner=settings.owner_name)
        else:
            personality = PERSONALITIES.get(name, "").format(owner=settings.owner_name)
        return SYSTEM_PROMPT.format(
            assistant=settings.assistant_name,
            owner=settings.owner_name,
            voice_hint=VOICE_HINT if channel == "voice" else "",
            personality=personality,
            persona=_persona(),
            core=_format_memories(core) or "- (nothing yet — learn as you go)",
            favorites=_favorites_line(self.db),
            reflection=("\nYour latest weekly reflection (patterns you noticed):\n" + latest["summary"] + "\n")
            if latest else "",
        )

    def turn_note(self, user_text: str) -> str:
        """What changes every turn: the time, the mood right now, memories relevant to this message."""
        now = local_now()
        parts = [f"Now: {now:%A} {now.date().isoformat()}, {now:%H:%M}"]
        if active_personality() == "mood":
            from . import mood

            m, why = mood.read(user_text, self.db, now)
            mood.set_current(m, why, self.db)
            parts.append(mood.note(m, why))
        if user_text.strip():
            core_ids = {m["id"] for m in self.memory.core(12)}
            relevant = [m for m in self.memory.search(user_text, limit=settings.memory_context_limit, touch=False)
                        if m["id"] not in core_ids]
            if relevant:
                parts.append("Possibly relevant: " + " | ".join(m["content"] for m in relevant))
            from .knowledge import context_for

            docs = context_for(user_text, self.db)
            if docs:
                parts.append(docs)
        return TURN_NOTE.format(note=". ".join(parts))

    # ------------------------------------------------------------ chat
    def chat(
        self,
        messages: list[dict[str, Any]],
        channel: str = "webui",
        model: str | None = None,
        learn: bool | None = None,
        on_text: Callable[[str], None] | None = None,
        turn: Any = None,
    ) -> dict[str, Any]:
        """Run one assistant turn, timed for the speed report (perf.py). Pass `turn` to add your own
        timings (speech-to-text, first sound) — then you save it; otherwise it's saved here."""
        from .perf import Turn

        own = turn is None
        last = next((m.get("content") for m in reversed(messages) if m.get("role") == "user"), "")
        turn = turn or Turn(channel, last if isinstance(last, str) else "")
        outer, self._turn = getattr(self, "_turn", None), turn

        def timed_text(piece: str) -> None:
            turn.mark("first_text")
            on_text(piece)

        try:
            out = self._chat(messages, channel, model, learn, timed_text if on_text else None)
        except Exception:
            turn.info["ok"] = False
            if own:
                turn.save(self.db, model=model or turn.info.get("model") or settings.chat_model)
            raise
        finally:
            self._turn = outer
        turn.mark("first_text")
        if "path" not in turn.info:
            turn.info["path"] = ("model" if turn.info["model_calls"] else
                                 "fastpath" if out["tool_calls"] else "instant")
        turn.info["tools"] = [t["name"] for t in out["tool_calls"]]
        turn.info["ok"] = turn.info.get("ok", True) and not any(
            isinstance(t.get("result"), dict) and "error" in t["result"] for t in out["tool_calls"])
        if own and (out["content"] or "").startswith(("Sorry — I didn't manage", "Sorry, I couldn't")):
            from .selfreview import log_correction

            log_correction("failed", turn.request, "", self.db, reply=out["content"])
        if own:
            turn.save(self.db, model=model or turn.info.get("model") or settings.chat_model)
        return out

    def _chat(
        self,
        messages: list[dict[str, Any]],
        channel: str = "webui",
        model: str | None = None,
        learn: bool | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Run one assistant turn. `messages` is the chat history WITHOUT a system prompt
        (any client-supplied system message is kept, appended after ours).

        on_text: if given, receives the reply as it's written (piece by piece for answers, so
        voice can start speaking the first sentence early; all at once for saved actions, which
        are verified before anything is said).

        Returns {"content": str, "tool_calls": [{"name", "arguments", "result"}]}.
        """
        history = [m for m in messages if m.get("role") in ("user", "assistant", "system", "tool")]
        user_text = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        if isinstance(user_text, list):  # OpenAI multi-part content
            user_text = " ".join(p.get("text", "") for p in user_text if isinstance(p, dict))
            history = [_flatten(m) for m in history]

        # "Undo that" / "no, I said Thursday": take back what was just saved, then redo it right.
        from . import undo

        undone = None
        wrapped = False
        from .quick_answers import answer as quick_answer
        from .selfreview import log_correction

        earlier = [m["content"] for m in history[:-1] if m["role"] == "user" and isinstance(m["content"], str)]
        previous_request = earlier[-1] if earlier else ""
        from .selfreview import WRONG_RE

        if user_text and WRONG_RE.match(user_text.strip()):  # "that's wrong" → into the mistake log
            last_reply = next((m["content"] for m in reversed(history[:-1])
                               if m["role"] == "assistant" and isinstance(m["content"], str)), "")
            log_correction("wrong", previous_request, "", self.db, reply=last_reply)
        from .coding import recent_fix

        if (user_text and undo.UNDO_RE.match(user_text.strip())
                and not (re.search(r"\b(?:change|edit)", user_text, re.I) and recent_fix(self.db))):
            content = undo.undo_last(self.db)
            if content.startswith("Undid"):
                log_correction("undo", previous_request, "", self.db)
            if on_text:
                on_text(content)
            self._log(channel, user_text, content)
            return {"content": content, "tool_calls": []}
        meant = fastpath.correction(user_text) if user_text else None
        from .pronounce import PATTERNS as PRONOUNCE

        if meant is not None and any(rx.match(meant.rstrip(".!")) for rx in PRONOUNCE):
            meant = None  # "no, I said it's pronounced…" — a pronunciation lesson, nothing to undo
        if meant is not None:
            undone = undo.undo_recent(self.db)
            log_correction("misheard", previous_request, meant, self.db)
            if not meant:  # "you misheard me" — ask what they said
                content = ((undone + " ") if undone else "") + "Sorry — what did you say?"
                if on_text:
                    on_text(content)
                self._log(channel, user_text, content)
                return {"content": content, "tool_calls": []}
            if not (self._shortcut(meant, "") or quick_answer(meant, now=local_now())):
                wrapped = True  # the AI redoes it — the shortcuts below must not read this wrapper
                meant = (f"Correction to what I asked before: {meant}. "
                         + ("You already undid the earlier save, so do my previous request again with this change."
                            if undone else "Answer or redo my previous request with this change."))
            user_text = meant
            last_user = max(i for i, m in enumerate(history) if m["role"] == "user")
            history = [*history[:last_user], {"role": "user", "content": meant}, *history[last_user + 1:]]
            if on_text and undone:
                on_text(undone + " ")

        if model is None:  # two-tier brain: the model that's best at this kind of request
            from . import brain

            kind, model = brain.pick(history)
            if self._turn is not None:
                self._turn.info["kind"] = kind

        def build() -> list[dict[str, Any]]:
            """The prompt — only built when the AI is actually needed (memory search, documents, mood)."""
            c: list[dict[str, Any]] = [
                {"role": "system", "content": self.build_system_prompt(user_text, channel)},
                *history,
            ]
            if user_text and c[-1].get("role") == "user" and isinstance(c[-1].get("content"), str):
                c[-1] = {**c[-1], "content": self.turn_note(user_text) + c[-1]["content"]}
            return c

        convo: list[dict[str, Any]] = []
        llm = get_llm()
        recent_user = " ".join(
            m["content"] for m in history[-6:] if m["role"] == "user" and isinstance(m["content"], str)
        )
        schemas = tools.schemas(tools.route(recent_user))
        trace: list[dict[str, Any]] = []

        # 1) Common, unambiguous requests ("add a daily goal: …") are done directly.
        self._channel = channel
        self._on_text = on_text if (on_text and kind_of(user_text or "") != "action") else None
        self._final_streamed = False
        content = ""
        fast = None
        if user_text and not wrapped:
            from .integrations import pending_text

            quick = quick_answer(user_text, now=local_now())  # time, date, math, units, countdowns
            if quick:
                if on_text:
                    on_text(quick)
                self._log(channel, user_text, quick)
                return {"content": quick, "tool_calls": []}
            fast = fastpath.text_reply(user_text, pending_text(self.db) is not None)
            last_reply = next((m["content"] for m in reversed(history[:-1])
                               if m["role"] == "assistant" and isinstance(m["content"], str)), "")
            if not fast:  # "yes" / "skip" to something you asked the pocket pet while it was offline
                from . import pocket

                # …unless "yes" answers a different question AMO just asked ("want me to text Jay?")
                other_question = last_reply.rstrip().endswith("?") and "want me to do it?" not in last_reply
                if pocket.pending(self.db) and not other_question and (pocket.YES_RE.match(user_text.strip())
                                                                       or pocket.NO_RE.match(user_text.strip())):
                    return self._offline_answer(user_text, channel, on_text)
            if not fast:
                from .records import confirm_delete_all

                fast = confirm_delete_all(user_text, self.db) or self._shortcut(user_text, last_reply)
        if fast:
            name, args = fast
            result = json.loads(self._call_tool(name, args))
            if name == "show_chart" and "error" not in result:
                # "Pull up the chart" → show it AND give the read.
                result = json.loads(self._call_tool("strat_analysis", {"symbol": result["symbol"]}))
                name = "strat_analysis"
            content = instant_reply([{"name": name, "arguments": args, "result": result}]) or ""
            from .shortcuts import SAY

            if content:
                trace.append({"name": name, "arguments": args, "result": result})
            elif name in SAY and not (isinstance(result, dict) and "error" in result):
                content = SAY[name](result, args)  # "what are my goals" — read straight from your data
                trace.append({"name": name, "arguments": args, "result": result})
            elif name == "search_knowledge":
                if result.get("hits"):  # let the model answer from your files
                    note = " | ".join(f"From your file “{h['file']}”: {h['text'][:700]}" for h in result["hits"])
                    convo = build()
                    convo[-1] = {**convo[-1], "content": f"({note})\n\n" + convo[-1]["content"]}
                    trace.append({"name": name, "arguments": args, "result": result})
                else:
                    content = result.get("text") or result.get("error", "")
            elif name.startswith(("code_", "self_")):
                if name in ("code_help", "code_explain", "code_fix") and self._turn is not None:
                    self._turn.info["path"] = "coder"
                content = result.get("text") or result.get("error", "")
                if channel == "voice" and result.get("diff"):  # don't read a diff out loud
                    content = (f"I've got a fix ready for {', '.join(result['files'])} — {result['lines']} lines. "
                               "It's on the dashboard. Say apply the fix to make the change.")
                if "error" not in result:
                    trace.append({"name": name, "arguments": args, "result": result})
            elif name in ("list_timers", "cancel_timer", "timer_left", "find_notes"):
                content = result.get("text") or result.get("error", "")
                trace.append({"name": name, "arguments": args, "result": result})
            elif name in ("set_timer", "set_reminder", "set_pronunciation", "set_preference") and "error" in result:
                content = result["error"][:1].upper() + result["error"][1:]
            elif name == "look":
                content = result.get("answer") or result.get("error", "")
                if "error" not in result:
                    trace.append({"name": name, "arguments": args, "result": result})
            elif name in ("text_client", "send_text", "cancel_text") and "error" in result:
                content = result["error"]
            elif name == "delete_record" and "error" in result:
                content = result["error"]  # "couldn't find…" / "which one?" — ask straight away
            elif name in ("strat_analysis", "strat_compare", "show_chart", "watchlist_show",
                          "watchlist_scan", "check_trade", "trading_review") and "error" not in result:
                trace.append({"name": name, "arguments": args, "result": result})
                voice = channel == "voice"
                content = (result.get("summary" if voice else "thesis")
                           or result.get("spoken" if voice else "text")
                           or f"The {result.get('symbol')} chart is up on your dashboard.")
            elif name in ("strat_analysis", "strat_compare", "show_chart", "watchlist_add",
                          "watchlist_remove", "watchlist_show", "watchlist_scan", "check_trade", "trading_review"):
                content = result.get("error", "")
        if not content and user_text and not fast:
            from .coding import answer as code_answer, is_code_question

            if is_code_question(user_text):  # coding questions go to the code model
                if self._turn is not None:
                    self._turn.info["path"] = "coder"
                try:
                    content = code_answer(user_text, voice=channel == "voice", history=history[:-1])
                except LLMError as e:
                    content = (f"{e} Run: amo setup-coder" if "isn't downloaded" in str(e) else str(e))
        answered_by_model = not content
        if not content:  # no shortcut, or it failed: let the model handle it
            convo = convo or build()
            content = self._tool_loop(llm, convo, schemas, model, trace)

        # 2) The model claimed it saved something but never called a tool → make it actually do it.
        if (answered_by_model and user_text and not _saved_anything(trace) and kind_of(user_text) == "action"
                and _CLAIM.search(content)):
            log.warning("model claimed an action without a tool call; retrying: %r", content[:120])
            convo += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "You haven't actually saved anything — no tool was called. "
                                            "Call the right tool now to do what I asked."},
            ]
            content = self._tool_loop(llm, convo, schemas, model, trace)
            if not _saved_anything(trace):
                content = ("Sorry — I didn't manage to save that. Try saying it again a bit more directly, "
                           "for example: \"add a daily goal: make one beat\".")

        if on_text and not self._final_streamed and content:
            on_text(content)
        if undone:
            content = f"{undone} {content}"
        undo.record_turn(trace, self.db)
        self._log(channel, user_text, content)
        # Fact learning normally runs from the scheduler once you've gone quiet, so it never
        # competes with your next message for the CPU. learn=True forces it now (in background).
        if learn:
            threading.Thread(target=self._learn_safely, daemon=True).start()
        return {"content": content, "tool_calls": trace}

    def _shortcut(self, text: str, last_reply: str) -> tuple[str, dict[str, Any]] | None:
        """Requests handled directly, without the AI: timers, pronunciations, preferences, notes, code
        commands, your documents, then the common actions in fastpath."""
        from .coding import match as code_match
        from .knowledge import match as knowledge_match
        from .notes import match as notes_match
        from .profile import match as preference_match
        from .pronounce import match as pronounce_match
        from .timers import match as timer_match

        if fastpath.TEXT_RE.match(text.strip()):
            return fastpath.match(text)
        from .shortcuts import match as data_match

        return (timer_match(text, local_now()) or pronounce_match(text, last_reply) or preference_match(text)
                or notes_match(text) or code_match(text) or data_match(text, self.db) or knowledge_match(text)
                or fastpath.match(text))

    def _offline_answer(self, user_text: str, channel: str, on_text: Callable[[str], None] | None) -> dict[str, Any]:
        from . import pocket

        item = pocket.pop_pending(self.db)
        if pocket.YES_RE.match(user_text.strip()):
            out = self.chat([{"role": "user", "content": item["text"]}], channel=channel, turn=self._turn)
            content, calls = out["content"], out["tool_calls"]
        else:
            content, calls = "Okay, skipped.", []
        nxt = pocket.pending(self.db)
        if nxt:
            content += f" You also asked me to {nxt[0]['text'].rstrip('.?!')} — want me to do it?"
        if on_text:
            on_text(content)
        self._log(channel, user_text, content)
        return {"content": content, "tool_calls": calls}

    def _tool_loop(self, llm: Any, convo: list[dict[str, Any]], schemas: list[dict[str, Any]],
                   model: str | None, trace: list[dict[str, Any]]) -> str:
        """Let the model call tools until it answers. Appends to `trace`, returns the reply."""
        for _ in range(MAX_TOOL_ROUNDS):
            msg = self._model_turn(llm, convo, model, schemas)
            calls = msg.get("tool_calls") or []
            if not calls:
                self._final_streamed = bool(msg.get("_streamed"))
                return msg.get("content", "")
            convo.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": calls})
            round_trace: list[dict[str, Any]] = []
            for c in calls:
                fn = c.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments", {})
                result = self._call_tool(name, args)
                log.info("tool %s(%s) -> %s", name, args, result[:200])
                round_trace.append({"name": name, "arguments": args, "result": json.loads(result)})
                convo.append({"role": "tool", "content": result, "tool_name": name})
            trace += round_trace
            # Pure saves (booking, logging a trade…) don't need a second model pass to confirm.
            quick = instant_reply(round_trace)
            if quick:
                return quick
            # A Strat analysis is computed, not written by the model — return it as-is.
            for t in round_trace:
                if t["name"] == "strat_analysis" and "thesis" in t["result"]:
                    return t["result"]["summary" if self._channel == "voice" else "thesis"]
                if t["name"] in ("strat_compare", "watchlist_scan", "watchlist_show", "check_trade",
                                 "trading_review") and "text" in t["result"]:
                    return t["result"]["spoken" if self._channel == "voice" else "text"]
        # Ran out of tool rounds: ask for a final answer without tools.
        return llm.chat(convo + [{"role": "user", "content": "Summarize what you did."}],
                        model=model).get("content", "")

    def _model_turn(self, llm: Any, convo: list[dict[str, Any]], model: str | None,
                    schemas: list[dict[str, Any]]) -> dict[str, Any]:
        """One model call. When streaming, text is passed on as it arrives."""
        turn = getattr(self, "_turn", None)
        if turn:
            turn.info["model_calls"] += 1
            turn.info["model"] = model or settings.chat_model
        started = time.monotonic()
        try:
            if not self._on_text or not hasattr(llm, "stream_chat"):
                msg = llm.chat(convo, model=model, tools=schemas)
                if turn:
                    turn.mark("first_token")
                return msg
            content, calls, streamed = [], [], False
            for event in llm.stream_chat(convo, model=model, tools=schemas):
                if turn:
                    turn.mark("first_token")
                if event.get("tool_calls"):
                    calls += event["tool_calls"]
                elif event.get("content"):
                    content.append(event["content"])
                    if not calls:
                        self._on_text(event["content"])
                        streamed = True
            return {"role": "assistant", "content": "".join(content), "tool_calls": calls,
                    "_streamed": streamed and not calls}
        finally:
            if turn:
                turn.add("model", (time.monotonic() - started) * 1000)

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


_CLAIM = re.compile(
    r"\b(added|adding|i'?ve added|i'?ll add|booked|logged|saved|recorded|created|scheduled|tracked|"
    r"i'?ll track|tracking|noted|marked|updated|set up|all set|done)\b", re.I)


def _saved_anything(trace: list[dict[str, Any]]) -> bool:
    """Did any save-type tool succeed this turn?"""
    return any(t["name"] in CONFIRM and isinstance(t["result"], dict) and "error" not in t["result"]
               for t in trace)


def _flatten(m: dict[str, Any]) -> dict[str, Any]:
    if isinstance(m.get("content"), list):
        text = " ".join(p.get("text", "") for p in m["content"] if isinstance(p, dict))
        return {**m, "content": text}
    return m
