"""Printing: AMO prints to any printer your Mac knows (HP Wi-Fi printers work through AirPrint).

One-time setup on the Mac: System Settings → Printers & Scanners → Add Printer… → pick the HP
(it shows up by itself on the same Wi-Fi). Then:

    amo printer              which printers AMO sees, the default, ink levels
    amo printer --test       print a test page
    amo print FILE           print a file

Or just say: "print that", "print my schedule for tomorrow", "print Jay's invoice",
"print my goals", "print today's brief", "print my notes", "print the setlist.pdf",
"how much ink is left?", "cancel printing".

Uses the standard printing system (CUPS: lp / lpstat / cancel), which the Raspberry Pi has too.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, settings
from .db import Database, get_db, local_now

OUT = PROJECT_ROOT / "data" / "printouts"
PRINTABLE = {".pdf", ".txt", ".md", ".jpg", ".jpeg", ".png", ".heic", ".gif", ".tif", ".tiff", ".csv", ".log",
             ".py", ".js", ".json", ".html", ".rtf", ".docx", ".doc"}
MAX_COPIES = 20


class PrinterUnavailable(ValueError):  # a setup problem to explain, not a bug
    pass


def _run(args: list[str], timeout: float = 20) -> str:
    if not shutil.which(args[0]):
        raise PrinterUnavailable("printing isn't set up on this computer (no CUPS)")
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise ValueError((r.stderr or r.stdout).strip()[:300] or f"{args[0]} failed")
    return r.stdout


# ------------------------------------------------------------------ which printers
def printers() -> list[dict[str, Any]]:
    """[{name, default, ready, uri}] — the printers this computer knows."""
    try:
        out = _run(["lpstat", "-p", "-d", "-v"])
    except ValueError as e:
        if "no destinations" in str(e).lower() or "no system default" in str(e).lower():
            return []
        raise
    default = (re.search(r"system default destination: (\S+)", out) or [None, None])[1]
    found: dict[str, dict[str, Any]] = {}
    for m in re.finditer(r"^printer (\S+) (.*)$", out, re.M):
        found[m.group(1)] = {"name": m.group(1), "default": m.group(1) == default,
                             "ready": "disabled" not in m.group(2), "status": m.group(2).strip()}
    for m in re.finditer(r"^device for (\S+): (\S+)", out, re.M):
        if m.group(1) in found:
            found[m.group(1)]["uri"] = m.group(2)
    return list(found.values())


def pick(name: str | None = None) -> str:
    """The printer to use: the one you named (part of its name is enough), AMO_PRINTER, the default, or the only one."""
    ps = printers()
    if not ps:
        raise PrinterUnavailable("I don't see a printer yet. On the Mac: System Settings → Printers & Scanners → "
                                 "Add Printer… and pick your HP (it needs to be on the same Wi-Fi).")
    want = (name or settings.printer or "").strip().lower()
    if want:
        hits = [p for p in ps if want.replace(" ", "_") in p["name"].lower() or want in p["name"].lower().replace("_", " ")]
        if len(hits) == 1:
            return hits[0]["name"]
        if not hits:
            raise ValueError(f"I don't see a printer called {name or want}. I have: "
                             + ", ".join(p["name"].replace("_", " ") for p in ps))
    default = [p for p in ps if p["default"]]
    if default:
        return default[0]["name"]
    if len(ps) == 1:
        return ps[0]["name"]
    raise ValueError("which printer? " + ", ".join(p["name"].replace("_", " ") for p in ps))


# ------------------------------------------------------------------ printing
def print_file(path: str | Path, printer: str | None = None, copies: int = 1, two_sided: bool = False,
               title: str | None = None) -> dict[str, Any]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"I can't find {path}")
    if p.suffix.lower() not in PRINTABLE:
        raise ValueError(f"I can't print {p.suffix or 'that kind of'} files")
    if p.suffix.lower() in (".docx", ".doc", ".rtf") and shutil.which("textutil"):
        # Word files: the Mac turns them into text first (keeps the words, not the layout)
        OUT.mkdir(parents=True, exist_ok=True)
        txt = OUT / (p.stem + ".txt")
        _run(["textutil", "-convert", "txt", "-output", str(txt), str(p)])
        p = txt
    copies = max(1, min(MAX_COPIES, int(copies or 1)))
    if settings.print_dry_run:
        return {"printed": True, "printer": "test printer", "job": "dry-run", "copies": copies, "file": p.name,
                "text": f"Printing {title or p.name} on test printer."}
    dest = pick(printer)
    args = ["lp", "-d", dest, "-n", str(copies), "-t", (title or p.name)[:60]]
    if two_sided:
        args += ["-o", "sides=two-sided-long-edge"]
    if p.suffix.lower() in (".txt", ".md", ".csv", ".log", ".py", ".js", ".json"):
        args += ["-o", "cpi=12", "-o", "lpi=7", "-o", "page-left=54", "-o", "page-top=54"]
    out = _run(args + [str(p)])
    job = (re.search(r"request id is (\S+)", out) or [None, ""])[1]
    return {"printed": True, "printer": dest.replace("_", " "), "job": job, "copies": copies, "file": p.name,
            "text": f"Printing {title or p.name}" + (f" ({copies} copies)" if copies > 1 else "")
                    + f" on {dest.replace('_', ' ')}."}


def print_text(text: str, title: str, printer: str | None = None, copies: int = 1) -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = local_now()
    safe = re.sub(r"[^\w-]+", "-", title.lower()).strip("-")[:40] or "printout"
    f = OUT / f"{stamp:%Y%m%d-%H%M%S}-{safe}.txt"
    header = f"{title}\n{'=' * min(len(title), 60)}\n{stamp:%A, %B} {stamp.day}, {stamp:%Y}\n\n"
    f.write_text(header + _plain(text).strip() + "\n")
    return print_file(f, printer, copies, title=title)


def _plain(text: str) -> str:
    """Markdown → clean text for paper."""
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    t = re.sub(r"(?m)^#{1,6}\s*", "", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    return t


def jobs() -> list[dict[str, str]]:
    try:
        out = _run(["lpstat", "-o"])
    except ValueError:
        return []
    return [{"job": m.group(1), "printer": m.group(1).rsplit("-", 1)[0]} for m in re.finditer(r"^(\S+-\d+)\s", out, re.M)]


def cancel_all(printer: str | None = None) -> dict[str, Any]:
    waiting = jobs()
    if not waiting:
        return {"cancelled": 0, "text": "Nothing's printing."}
    _run(["cancel", "-a"] + ([pick(printer)] if printer else []))
    return {"cancelled": len(waiting), "text": f"Cancelled {len(waiting)} print job{'s' if len(waiting) != 1 else ''}."}


def ink(printer: str | None = None) -> dict[str, Any]:
    """Ink / toner levels, asked from the printer itself (IPP). Best effort — not every printer says."""
    dest = pick(printer)
    uri = next((p.get("uri") for p in printers() if p["name"] == dest), None)
    levels: list[tuple[str, int]] = []
    test = Path("/usr/share/cups/ipptool/get-printer-attributes.test")
    if uri and uri.startswith(("ipp", "dnssd")) and shutil.which("ipptool") and test.exists():
        try:
            out = _run(["ipptool", "-T", "8", "-v", uri if uri.startswith("ipp") else uri, str(test)], timeout=15)
            names = re.search(r"marker-names \([^)]*\) = (.+)", out)
            vals = re.search(r"marker-levels \([^)]*\) = (.+)", out)
            if names and vals:
                levels = [(n.strip().replace("-", " "), int(v)) for n, v in
                          zip(names.group(1).split(","), vals.group(1).split(",")) if v.strip().lstrip("-").isdigit()]
        except (ValueError, subprocess.TimeoutExpired):
            pass
    if not levels:
        return {"printer": dest.replace("_", " "), "levels": [],
                "text": f"{dest.replace('_', ' ')} didn't say its ink levels — check the HP Smart app."}
    low = [n for n, v in levels if 0 <= v <= 15]
    say = ", ".join(f"{n} {v}%" for n, v in levels if v >= 0)
    return {"printer": dest.replace("_", " "), "levels": levels,
            "text": f"Ink: {say}." + (f" Running low on {', '.join(low)}." if low else "")}


# ------------------------------------------------------------------ things AMO can print
def invoice_text(client: str, db: Database | None = None) -> tuple[str, str]:
    """(title, text) — what a client owes: unpaid sessions and the balance."""
    from .crm import StudioCRM

    db = db or get_db()
    crm = StudioCRM(db)
    c = crm.resolve_client(client)
    who = c.get("artist_name") or c["name"]
    rows = [s for s in crm.unpaid_sessions() if s["client_name"] == c["name"]]
    lines = [f"Bill to: {c['name']}" + (f" ({c['artist_name']})" if c.get("artist_name") else ""),
             *( [f"Phone: {c['phone']}"] if c.get("phone") else []), "", f"{'Date':<14}{'Session':<34}{'Amount':>10}",
             "-" * 58]
    for s in rows:
        d = datetime.fromisoformat(s["starts_at"]) if len(s["starts_at"]) > 10 else datetime.fromisoformat(s["starts_at"][:10])
        owed = (s["total"] or 0) - (s["paid"] or 0)
        lines.append(f"{d:%b %d, %Y}  {'Studio session' + (' (part paid)' if s['paid'] else ''):<34}{_money(owed):>10}")
    total = sum((s["total"] or 0) - (s["paid"] or 0) for s in rows)
    if not rows:
        lines.append("Nothing owed — all paid up. Thank you!")
    lines += ["-" * 58, f"{'Total due':<48}{_money(total):>10}", "",
              f"Thank you — {settings.owner_name}"]
    return f"Invoice — {who}", "\n".join(lines)


def _money(v: float) -> str:
    return f"${v:,.2f}"


def content_for(what: str, db: Database | None = None, last_reply: str = "") -> tuple[str, str]:
    """(title, text) for something AMO keeps: that / schedule / goals / brief / notes / invoice:<client>."""
    from .shortcuts import SAY

    db = db or get_db()
    w = what.lower().strip()
    if w in ("that", "this", "it", "last", "reply"):
        if not last_reply.strip():
            raise ValueError("print what? I haven't said anything to print yet")
        return "From AMO", last_reply
    if w.startswith("invoice:"):
        return invoice_text(what.split(":", 1)[1], db)
    from . import tools

    import json

    def call(name: str, args: dict | None = None) -> Any:
        r = json.loads(tools.call(name, args or {}))
        if isinstance(r, dict) and "error" in r:
            raise ValueError(r["error"])
        return r

    if w.startswith("schedule"):
        day = "tomorrow" if "tomorrow" in w else "today"
        sessions = call("studio_schedule", {"day": day})
        lines = [f"{SAY['studio_schedule'](sessions, {'day': day})}", ""]
        for s in sessions:
            lines.append(f"- {s['starts_at'][11:16]}  {s.get('artist_name') or s.get('client_name')}  "
                         f"{s.get('service', '')}  {s.get('hours', 0):g}h"
                         + (f"  — {s['notes']}" if s.get("notes") else ""))
        return f"Studio schedule — {day}", "\n".join(lines)
    if w == "goals":
        goals = call("list_goals")
        return "Goals", "\n".join(f"[{'x' if g.get('done_today') else ' '}] {g['title']}  ({g['cadence']}"
                                  + (f", {g['streak']} in a row" if g.get("streak") else "") + ")" for g in goals) \
            or "No goals yet."
    if w == "brief":
        from .scheduler import morning_brief_text

        return "Today's brief", morning_brief_text(db)
    if w == "notes":
        from .notes import find

        r = find("", limit=20, db=db)
        return "Notes", "\n\n".join(f"- {n['text']}" for n in r.get("notes", [])) or "No notes yet."
    for name, title in (("unpaid_sessions", "Who owes me"), ("resale_inventory", "Inventory"),
                        ("open_positions", "Open positions"), ("trading_rules", "Trading rules"),
                        ("cravvr_tasks", "Cravvr tasks")):
        if w in (name, title.lower()):
            return title, SAY[name](call(name), {})
    raise ValueError(f"I don't know how to print “{what}”")


def find_file(ref: str) -> Path | None:
    """A file by name in your usual folders (Desktop, Documents, Downloads, the knowledge folder, printouts)."""
    p = Path(ref).expanduser()
    if p.is_absolute():
        return p if p.is_file() and _ok(p) else None
    home = Path.home()
    for base in (home / "Desktop", home / "Documents", home / "Downloads", Path(settings.knowledge_dir).expanduser(),
                 OUT):
        if not base.is_dir():
            continue
        exact = base / ref
        if exact.is_file() and _ok(exact):
            return exact
        hits = [f for f in base.rglob("*") if f.is_file() and f.name.lower() == ref.lower() and _ok(f)][:2]
        if not hits:
            hits = [f for f in base.rglob(f"*{ref}*") if f.is_file() and _ok(f)][:2]
        if len(hits) == 1:
            return hits[0]
    return None


def _ok(p: Path) -> bool:
    parts = p.resolve().parts
    return (p.suffix.lower() in PRINTABLE and not any(x.startswith(".") for x in parts)
            and p.name not in (".env",) and "Library" not in parts)


# ------------------------------------------------------------------ understanding requests
PRINT_RE = re.compile(
    r"^(?:(?:hey )?amo[ ,]*)?(?:please |can you |could you )?print(?: out)?\s+(?:me\s+)?(?:(?P<copies>\d+|two|three|four|five)\s+"
    r"(?:copies\s+of\s+|of\s+)?)?(?P<what>.+?)(?:\s+(?:two|double)[- ]sided)?(?:\s+(?:on|to)\s+(?:the\s+)?(?P<printer>[\w -]+?)"
    r"(?:\s+printer)?)?(?:\s+please)?[.!]*$", re.I)
INK_RE = re.compile(r"^(?:how much|how'?s the|what'?s the|check(?: the)?) (?:ink|toner)(?: is)?(?: left| level)?"
                    r"(?: in the printer)?\??$|^(?:am i|is the printer) (?:low on|out of) (?:ink|toner)\??$", re.I)
CANCEL_RE = re.compile(r"^(?:please )?(?:cancel|stop) (?:the )?print(?:ing| job| jobs)?[.!]*$", re.I)
_WORD_N = {"two": 2, "three": 3, "four": 4, "five": 5}


def match(text: str) -> tuple[str, dict[str, Any]] | None:
    t = (text or "").strip()
    if INK_RE.match(t):
        return "printer_ink", {}
    if CANCEL_RE.match(t):
        return "cancel_printing", {}
    m = PRINT_RE.match(t)
    if not m:
        return None
    what = m.group("what").strip().strip("\"'“”")
    w = what.lower()
    args: dict[str, Any] = {}
    if m.group("copies"):
        args["copies"] = _WORD_N.get(m.group("copies").lower()) or int(m.group("copies"))
    if re.search(r"(?:two|double)[- ]sided", t, re.I):
        args["two_sided"] = True
    if m.group("printer"):
        args["printer"] = m.group("printer").strip()
    inv = re.match(r"^(?:an? |the )?invoice for (?P<c>.+)$|^(?P<c2>.+?)(?:'s|s')? (?:invoice|bill)$", what, re.I)
    if inv:
        return "print_document", {**args, "what": "invoice:" + (inv.group("c") or inv.group("c2")).strip()}
    kinds = [(r"^(?:that|this|it)$", "that"), (r"schedule.*tomorrow|tomorrow'?s schedule", "schedule tomorrow"),
             (r"schedule", "schedule today"), (r"goals?$", "goals"), (r"(?:morning |today'?s )?brief(?:ing)?$", "brief"),
             (r"notes$", "notes"), (r"who owes me|unpaid", "unpaid_sessions"), (r"inventory$", "resale_inventory"),
             (r"(?:open )?positions$", "open_positions"), (r"(?:trading )?rules$", "trading_rules"),
             (r"cravvr", "cravvr_tasks")]
    for rx, key in kinds:
        if re.search(rx, w):
            return "print_document", {**args, "what": key}
    if re.search(r"\.\w{2,4}$", what) or "/" in what:  # a file name
        return "print_document", {**args, "file": what}
    return None
