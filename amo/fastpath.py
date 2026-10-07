"""Requests common and unambiguous enough to handle without the AI model.

Small local models occasionally *say* they saved something without calling the tool.
For the most common requests AMO just does it directly — instant and reliable.
"""

from __future__ import annotations

import re
from typing import Any

_LEAD = r"^(?:hey\s+amo,?\s*|amo,?\s*)?(?:please\s+|can you\s+|could you\s+|go ahead and\s+|i want to\s+)?"

GOAL_RE = re.compile(
    _LEAD + r"(?:add|create|set|make|start|new)\s+(?:me\s+)?(?:up\s+)?(?:a\s+|an\s+)?(?:new\s+)?"
    r"(?P<cad>daily|weekly|monthly)?\s*goal\s*(?:to\s+|:|-|—|called\s+|of\s+|for\s+|that\s+)?\s*(?P<title>.+?)[.!]*$",
    re.I,
)
REMEMBER_RE = re.compile(
    _LEAD + r"(?:remember|make a note|note down|take a note)\s*(?:that|:)?\s+(?P<fact>.{6,}?)[.!]*$", re.I
)


_DEL = r"(?:delete|remove|get rid of|drop|erase|scrap|clear|wipe|take off|cross off)"
_KINDS_SINGULAR = r"(?:(?P<cad>daily|weekly|monthly)\s+)?(?P<kind>goal|client|cravvr task|task|resale item|item|trading rule|rule|trade|session|booking|payment|memory)"
_KINDS_PLURAL = (r"(?:(?P<cad>daily|weekly|monthly)\s+)?(?P<kind>goals|clients|cravvr tasks|tasks|resale items|items|"
                 r"trading rules|rules|trades|sessions|bookings|payments|memories)")
DELETE_ALL_RE = re.compile(  # "delete all my daily goals", "clear my goals"
    _LEAD + _DEL + r"\s+(?:all\s+)?(?:of\s+)?(?:my\s+|the\s+)?" + _KINDS_PLURAL + r"$", re.I)
DELETE_RE = re.compile(  # "delete the goal make one beat", "delete my daily goal: make one beat"
    _LEAD + _DEL + r"\s+(?:the\s+|my\s+|that\s+|this\s+)?" + _KINDS_SINGULAR
    + r"\b\s*(?:called\s+|named\s+|for\s+|about\s+|:|-|—)?\s*(?P<name>.*)$", re.I)
DELETE_AFTER_RE = re.compile(  # "delete the make one beat goal"
    _LEAD + _DEL + r"\s+(?:the\s+|my\s+)?(?P<name>.+?)\s+" + _KINDS_SINGULAR + r"$", re.I)
DELETE_FROM_RE = re.compile(  # "remove make one beat from my goals"
    _LEAD + _DEL + r"\s+(?P<name>.+?)\s+(?:from|off)\s+(?:my\s+|the\s+)?" + _KINDS_PLURAL + r"(?:\s+list)?$", re.I)


def _clean_sentence(text: str) -> str:
    """Speech-to-text adds commas and full stops in odd places — drop them."""
    t = re.sub(r"[,;]+", " ", text)
    t = re.sub(r"\s+", " ", t).strip()
    return t.rstrip(" .!?")


# "delete that trade" refers to the conversation — leave those to the AI, which has the context.
_CONTEXT_WORDS = {"that", "this", "it", "them", "those", "these", "last", "last one", "the last one",
                  "latest", "the latest", "one", "that one", "this one", "previous", "same"}


_KIND_MAP = {"task": "cravvr", "cravvr task": "cravvr", "item": "resale", "resale item": "resale",
             "booking": "sessions", "rule": "rules", "trading rule": "rules"}


def _kind(word: str) -> str:
    w = word.lower()
    if "goal" in w:
        return "goals"
    singular = w[:-1] if w.endswith("s") and w not in ("goals",) else w
    if w == "memories":
        singular = "memory"
    return _KIND_MAP.get(singular, singular)


_SYM = r"\$?(?P<sym>[A-Za-z][A-Za-z0-9.=\-^&]{0,9}(?: futures)?)"
STRAT_RE = re.compile(
    _LEAD + r"(?:give me |run |do |what'?s |what is )?(?:a |an |the |my )?(?:strat|thesis|analysis|breakdown|read)"
    r"(?: analysis| thesis| read| breakdown)?\s+(?:on|for|of)\s+(?:the\s+)?" + _SYM + r"(?:\s+chart)?[.!?]*$", re.I)
STRAT_VERB_RE = re.compile(  # "analyze SPY", "strat SPY"
    _LEAD + r"(?:analy[sz]e|strat)\s+(?:the\s+)?" + _SYM + r"[.!?]*$", re.I)
STRAT_NOUN_RE = re.compile(  # "SPY thesis"
    _LEAD + _SYM + r"\s+(?:strat|thesis|analysis|breakdown)[.!?]*$", re.I)
CHART_FOR_RE = re.compile(  # "pull up the weekly chart for NQ"
    _LEAD + r"(?:pull up|show|open|bring up|load|put up|display)\s+(?:me\s+)?(?:the\s+|a\s+)?"
    r"(?:(?P<tf>daily|weekly|monthly|hourly|60 ?minute)\s+)?(?:chart|graph)\s+(?:for|on|of)\s+(?:the\s+)?"
    + _SYM + r"[.!?]*$", re.I)
CHART_SYM_RE = re.compile(  # "pull up the SPY weekly chart"
    _LEAD + r"(?:pull up|show|open|bring up|load)\s+(?:me\s+)?(?:the\s+)?" + _SYM
    + r"\s+(?:(?P<tf>daily|weekly|monthly|hourly)\s+)?chart[.!?]*$", re.I)
_TF = {"daily": "D", "weekly": "W", "monthly": "M", "hourly": "60", "60 minute": "60", "60minute": "60"}
_NOT_SYMBOLS = {"it", "that", "this", "me", "my", "the", "market", "markets", "chart"}




def _symbol(m: re.Match) -> str | None:
    sym = (m.group("sym") or "").replace(" futures", "").strip()
    return None if sym.lower() in _NOT_SYMBOLS else sym


# "what's your input on Amazon or Tesla setups", "how does NVDA look", "any setups on Apple"
_TRADEY = re.compile(r"set ?ups?|strat|chart|stock|shares|trade|trading|long|short|calls|puts|levels|ticker", re.I)
_FILLER = re.compile(r"\b(?:the|my|your|their|stock|stocks|shares|set ?ups?|strat|chart|charts|right now|today|"
                     r"this week|ticker|trade|look|looking|play|plays)\b|'s\b", re.I)
TAKE_RE = re.compile(
    _LEAD + r"(?:what'?s|what is|what are|give me|got any|any|do you have)?\s*(?:your\s+)?"
    r"(?:input|take|thoughts?|opinion|read|view|outlook|analysis)\s+(?:on|about|for|of)\s+(?P<list>.+?)[?.!]*$", re.I)
THINK_RE = re.compile(
    _LEAD + r"what do you think (?:about|of)\s+(?P<list>.+?)[?.!]*$", re.I)
LOOK_RE = re.compile(
    _LEAD + r"how (?:does|do|is|are|'s)\s+(?P<list>.+?)\s+(?:look|looking|setting up|set up)(?:\s+.*)?[?.!]*$", re.I)
SETUPS_RE = re.compile(
    _LEAD + r"(?:any|what are the|what'?s the|show me|find|check)?\s*(?:good\s+)?(?:strat\s+)?set ?ups?\s+"
    r"(?:on|for|in)\s+(?P<list>.+?)[?.!]*$", re.I)


def _tickers(raw: str, sentence: str) -> list[str]:
    from .market import ALIASES

    tradey = bool(_TRADEY.search(sentence))
    out = []
    for part in re.split(r",|\bor\b|\band\b|&|/|\bvs\.?\b|\bversus\b", raw, flags=re.I):
        cand = _FILLER.sub(" ", part).strip(" ?.!$'\"")
        cand = re.sub(r"\s+", " ", cand)
        if not cand or len(cand.split()) > 3:
            continue
        known = cand.upper() in ALIASES or re.fullmatch(r"[A-Z]{1,5}", cand) is not None
        if known or (tradey and re.fullmatch(r"[A-Za-z.&\- ]{1,24}", cand)):
            out.append(cand)
    return out


def match(text: str) -> tuple[str, dict[str, Any]] | None:
    t = text.strip()
    for rx in (TAKE_RE, THINK_RE, LOOK_RE, SETUPS_RE):
        m = rx.match(t)
        if m:
            syms = _tickers(m.group("list"), t)
            if len(syms) == 1:
                return "strat_analysis", {"symbol": syms[0]}
            if len(syms) > 1:
                return "strat_compare", {"symbols": ", ".join(syms)}
    for rx in (STRAT_RE, STRAT_VERB_RE, STRAT_NOUN_RE):
        m = rx.match(t)
        if m and _symbol(m):
            return "strat_analysis", {"symbol": _symbol(m)}
    for rx in (CHART_FOR_RE, CHART_SYM_RE):
        m = rx.match(t)
        if m and _symbol(m):
            tf = (m.group("tf") or "daily").lower()
            return "show_chart", {"symbol": _symbol(m), "timeframe": _TF.get(tf, "D")}
    d = _clean_sentence(t)
    m = DELETE_ALL_RE.match(d)
    if m:
        args = {"kind": _kind(m.group("kind"))}
        if m.group("cad"):
            args["cadence"] = m.group("cad").lower()
        return "delete_all_records", args
    for rx in (DELETE_FROM_RE, DELETE_RE, DELETE_AFTER_RE):
        m = rx.match(d)
        if not m:
            continue
        name = m.group("name").strip(" :-—\"'“”").strip()
        if name.lower() in _CONTEXT_WORDS:
            return None  # "delete that" — the AI has the conversation context
        if len(name) == 1 or len(name.split()) > 10:
            continue
        return "delete_record", {"kind": _kind(m.group("kind")), "item": name}
    m = GOAL_RE.match(t)
    if m and len(m.group("title").split()) <= 12:
        title = m.group("title").strip(" :-—")
        return "add_goal", {"title": title[:1].upper() + title[1:], "cadence": (m.group("cad") or "daily").lower()}
    m = REMEMBER_RE.match(t)
    if m:
        return "remember", {"content": m.group("fact").strip()}
    return None
