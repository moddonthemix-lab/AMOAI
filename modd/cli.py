"""Command line: talk to Modd, or manage data directly without the model."""

from __future__ import annotations

import argparse
import json
import sys

from .assistant import Assistant
from .config import Config
from .db import AREAS, Store
from .llm import LLMError, Ollama


def _money(x) -> str:
    return "-" if x is None else f"${x:,.2f}"


def _print_rows(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, "") if r.get(c) is not None else "")) for r in rows))
              for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c) if r.get(c) is not None else "").ljust(widths[c]) for c in cols))


def print_dashboard(d: dict) -> None:
    print(f"== {d['date']} ==")
    print("\nGoals:")
    for g in d["goals"] or [{"done": 0, "id": "", "text": "(none — add one with `modd goal add`)"}]:
        print(f"  [{'x' if g['done'] else ' '}] {g['id']} {g['text']}")
    print("\nStudio today:")
    for s in d["sessions_today"] or []:
        print(f"  #{s['id']} {s['starts_at']}  {s['client']}  {s['hours']}h  {s['status']}")
    if not d["sessions_today"]:
        print("  (no sessions)")
    if d["unpaid_sessions"]:
        owed = sum(s["amount_due"] - s["paid"] for s in d["unpaid_sessions"])
        print(f"  Unpaid: {len(d['unpaid_sessions'])} sessions, {_money(owed)} owed")
    print(f"\nOpen trades: {len(d['open_trades'])}   Reselling inventory: {d['inventory_count']}")
    for label, r in (("Today", d["revenue_today"]), ("This month", d["revenue_month"])):
        print(f"{label}: {_money(r['total'])}  (studio {_money(r['studio'])}, "
              f"reselling {_money(r['reselling'])}, trading {_money(r['trading'])})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="modd", description="Your local personal assistant.")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("chat", help="Talk to Modd in the terminal")
    v = sub.add_parser("voice", help="Talk to Modd out loud (push Enter to speak)")
    v.add_argument("--hands-free", action="store_true", help="Listen continuously, no Enter needed")
    s = sub.add_parser("serve", help="Run the JSON API for dashboards/devices")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    sub.add_parser("today", help="Show today's dashboard")
    sub.add_parser("doctor", help="Check that Ollama and the model are available")

    r = sub.add_parser("remember", help="Save a memory")
    r.add_argument("text", nargs="+")
    r.add_argument("--area", choices=AREAS, default="general")
    r = sub.add_parser("recall", help="Search memory")
    r.add_argument("query", nargs="*")
    r = sub.add_parser("forget", help="Delete a memory by id")
    r.add_argument("id", type=int)

    c = sub.add_parser("client", help="Studio CRM").add_subparsers(dest="action", required=True)
    a = c.add_parser("add")
    a.add_argument("name")
    a.add_argument("--phone", default="")
    a.add_argument("--email", default="")
    a.add_argument("--notes", default="")
    c.add_parser("list")

    ss = sub.add_parser("session", help="Studio sessions").add_subparsers(dest="action", required=True)
    a = ss.add_parser("book")
    a.add_argument("client")
    a.add_argument("starts_at", help="e.g. 2026-10-07T18:00")
    a.add_argument("--hours", type=float, default=1)
    a.add_argument("--rate", type=float, default=0)
    a.add_argument("--notes", default="")
    a = ss.add_parser("done")
    a.add_argument("id", type=int)
    a.add_argument("--paid", type=float)
    a = ss.add_parser("cancel")
    a.add_argument("id", type=int)
    a = ss.add_parser("list")
    a.add_argument("--start")
    a.add_argument("--end")
    ss.add_parser("unpaid")

    it = sub.add_parser("item", help="Reselling tracker").add_subparsers(dest="action", required=True)
    a = it.add_parser("add")
    a.add_argument("name")
    a.add_argument("cost", type=float)
    a.add_argument("--platform", default="")
    a.add_argument("--list-price", type=float)
    a.add_argument("--notes", default="")
    a = it.add_parser("sell")
    a.add_argument("id", type=int)
    a.add_argument("price", type=float)
    a.add_argument("--fees", type=float, default=0)
    a.add_argument("--shipping", type=float, default=0)
    a.add_argument("--platform")
    a = it.add_parser("list")
    a.add_argument("--status", choices=["inventory", "listed", "sold"])

    t = sub.add_parser("trade", help="Trading journal").add_subparsers(dest="action", required=True)
    a = t.add_parser("log")
    a.add_argument("symbol")
    a.add_argument("qty", type=float)
    a.add_argument("entry", type=float)
    a.add_argument("--side", choices=["long", "short"], default="long")
    a.add_argument("--exit", type=float)
    a.add_argument("--fees", type=float, default=0)
    a.add_argument("--setup", default="")
    a.add_argument("--emotion", default="")
    a.add_argument("--notes", default="")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--followed-rules", dest="followed_rules", action="store_true", default=None)
    g.add_argument("--broke-rules", dest="followed_rules", action="store_false")
    a = t.add_parser("close")
    a.add_argument("id", type=int)
    a.add_argument("exit", type=float)
    a.add_argument("--fees", type=float)
    a = t.add_parser("list")
    a.add_argument("--open", action="store_true")
    a = t.add_parser("stats")
    a.add_argument("--since")

    ru = sub.add_parser("rule", help="Trading rules").add_subparsers(dest="action", required=True)
    a = ru.add_parser("add")
    a.add_argument("text", nargs="+")
    ru.add_parser("list")

    go = sub.add_parser("goal", help="Daily goals").add_subparsers(dest="action", required=True)
    a = go.add_parser("add")
    a.add_argument("text", nargs="+")
    a.add_argument("--area", choices=AREAS, default="general")
    a.add_argument("--day")
    a = go.add_parser("done")
    a.add_argument("id", type=int)
    a = go.add_parser("list")
    a.add_argument("--day")

    rv = sub.add_parser("revenue", help="Revenue across all areas")
    rv.add_argument("period", nargs="?", default="month", choices=["day", "week", "month", "year"])
    return p


def chat_loop(assistant: Assistant) -> None:
    name = assistant.config.assistant_name
    print(f"{name} is listening. /reset clears the conversation, /quit exits.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text in ("/quit", "/exit"):
            return
        if text == "/reset":
            assistant.reset()
            print("(conversation cleared — long-term memory kept)")
            continue
        try:
            print(f"\n{name.lower()}> {assistant.ask(text)}")
        except LLMError as e:
            print(f"error: {e}", file=sys.stderr)


def voice_loop(assistant: Assistant, config: Config, hands_free: bool) -> None:
    from .voice import Voice, VoiceUnavailable

    voice = Voice(config.whisper_model, config.piper_voice)
    name = config.assistant_name
    print(f"{name} voice mode. {'Just talk.' if hands_free else 'Press Enter, then speak.'} Ctrl+C to quit.")
    try:
        while True:
            if not hands_free:
                input("\n[Enter to talk] ")
            print("listening…")
            text = voice.listen()
            if not text:
                continue
            print(f"you> {text}")
            try:
                reply = assistant.ask(text)
            except LLMError as e:
                print(f"error: {e}", file=sys.stderr)
                continue
            print(f"{name.lower()}> {reply}")
            voice.say(reply)
    except VoiceUnavailable as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    except (EOFError, KeyboardInterrupt):
        print()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config()
    config.ensure_home()
    db = Store(config.db_path)
    llm = Ollama(config.ollama_url, config.model)
    cmd, action = args.cmd, getattr(args, "action", None)

    if cmd in (None, "chat"):
        chat_loop(Assistant(db, llm, config))
    elif cmd == "voice":
        voice_loop(Assistant(db, llm, config), config, args.hands_free)
    elif cmd == "serve":
        from .server import serve
        serve(db, Assistant(db, llm, config), args.host or config.api_host,
              args.port or config.api_port, config.api_token)
    elif cmd == "doctor":
        print(f"data:   {config.db_path}")
        print(f"ollama: {config.ollama_url}  model: {config.model}")
        if llm.available():
            print("OK — model is installed and Ollama is reachable.")
        else:
            print(f"Not ready. Install Ollama (https://ollama.com) then run: ollama pull {config.model}")
            return 1
    elif cmd == "today":
        print_dashboard(db.dashboard())
    elif cmd == "remember":
        print(f"saved #{db.remember(' '.join(args.text), args.area)}")
    elif cmd == "recall":
        _print_rows(db.recall(" ".join(args.query), limit=20), ["id", "area", "created_at", "content"])
    elif cmd == "forget":
        print("deleted" if db.forget(args.id) else "not found")
    elif cmd == "client":
        if action == "add":
            print(f"client #{db.add_client(args.name, args.phone, args.email, args.notes)}")
        else:
            _print_rows(db.list_clients(), ["id", "name", "phone", "sessions", "total_paid", "last_session"])
    elif cmd == "session":
        if action == "book":
            print(f"session #{db.book_session(args.client, args.starts_at, args.hours, args.rate, args.notes)}")
        elif action == "done":
            print("updated" if db.update_session(args.id, "done", args.paid) else "not found")
        elif action == "cancel":
            print("updated" if db.update_session(args.id, "cancelled") else "not found")
        else:
            rows = db.unpaid_sessions() if action == "unpaid" else db.sessions(args.start, args.end)
            _print_rows(rows, ["id", "starts_at", "client", "hours", "rate", "amount_due", "paid", "status"])
    elif cmd == "item":
        if action == "add":
            print(f"item #{db.add_item(args.name, args.cost, args.platform, args.list_price, args.notes)}")
        elif action == "sell":
            item = db.sell_item(args.id, args.price, args.fees, args.shipping, args.platform)
            print(f"sold — profit {_money(item['profit'])}" if item else "not found")
        else:
            _print_rows(db.items(args.status),
                        ["id", "name", "platform", "status", "cost", "list_price", "sold_price", "profit"])
    elif cmd == "trade":
        if action == "log":
            tid = db.log_trade(args.symbol, args.qty, args.entry, args.side, args.exit, args.fees,
                               args.setup, args.followed_rules, args.emotion, args.notes)
            t = db.get_trade(tid)
            print(f"trade #{tid}" + (f" — P&L {_money(t['pnl'])}" if t["pnl"] is not None else " (open)"))
        elif action == "close":
            t = db.close_trade(args.id, args.exit, args.fees)
            print(f"closed — P&L {_money(t['pnl'])}" if t else "not found")
        elif action == "stats":
            print(json.dumps(db.trade_stats(args.since), indent=2))
        else:
            _print_rows(db.trades(open_only=args.open),
                        ["id", "opened_at", "symbol", "side", "qty", "entry", "exit", "pnl", "setup"])
    elif cmd == "rule":
        if action == "add":
            print(f"rule #{db.add_rule(' '.join(args.text))}")
        else:
            _print_rows(db.rules(), ["id", "rule"])
    elif cmd == "goal":
        if action == "add":
            print(f"goal #{db.add_goal(' '.join(args.text), args.area, args.day)}")
        elif action == "done":
            print("nice." if db.complete_goal(args.id) else "not found")
        else:
            _print_rows(db.goals(args.day), ["id", "area", "done", "text"])
    elif cmd == "revenue":
        print(json.dumps(db.revenue_for(args.period), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
