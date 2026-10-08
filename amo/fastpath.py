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


_WL = r"(?:my\s+|the\s+)?watch\s?list"
WATCH_ADD_RE = re.compile(_LEAD + r"(?:add|put)\s+(?P<list>.+?)\s+(?:to|on|onto)\s+" + _WL + r"$", re.I)
WATCH_VERB_RE = re.compile(_LEAD + r"(?:watch|keep an eye on|track|monitor)\s+(?P<list>.+?)$", re.I)
WATCH_REMOVE_RE = re.compile(
    _LEAD + r"(?:remove|delete|take|drop|stop watching)\s+(?P<list>.+?)(?:\s+(?:from|off)\s+" + _WL + r")?$", re.I)
WATCH_SHOW_RE = re.compile(_LEAD + r"(?:what'?s|what is|show(?: me)?|list|read(?: me)?)\s+(?:on\s+)?" + _WL + r"$", re.I)
WATCH_SCAN_RE = re.compile(
    _LEAD + r"(?:how'?s|how is|scan|check|run|any setups on|what'?s setting up on)\s+" + _WL
    + r"(?:\s+(?:looking|doing))?$", re.I)


def _watch_match(t: str) -> tuple[str, dict[str, Any]] | None:
    d = _clean_sentence(t)
    if WATCH_SHOW_RE.match(d):
        return "watchlist_show", {}
    if WATCH_SCAN_RE.match(d):
        return "watchlist_scan", {}
    m = WATCH_ADD_RE.match(d)
    if m:
        syms = _tickers(m.group("list"), d + " stock")
        return ("watchlist_add", {"symbols": ", ".join(syms)}) if syms else None
    if re.search(r"watch\s?list", d, re.I) or d.lower().startswith("stop watching"):
        m = WATCH_REMOVE_RE.match(d)
        if m:
            syms = _tickers(m.group("list"), d + " stock")
            return ("watchlist_remove", {"symbols": ", ".join(syms)}) if syms else None
    m = WATCH_VERB_RE.match(d)
    if m:  # "watch Tesla" — only real tickers/companies, so "watch the game" isn't a stock
        syms = _tickers(m.group("list"), "")
        return ("watchlist_add", {"symbols": ", ".join(syms)}) if syms else None
    return None


_NUM = r"\$?([\d,]+(?:\.\d+)?)"
CHECK_RE = re.compile(
    _LEAD + r"(?:should i|can i|is it (?:a )?good (?:time )?to|i'?m thinking (?:of|about)|thinking (?:of|about)|"
    r"i want to|i'?m about to|about to)\s+(?:go(?:ing)?\s+)?"
    r"(?P<side>long|short|buy(?:ing)?|sell(?:ing)?|short(?:ing)?)\s+(?:on\s+|in\s+|some\s+)?"
    r"(?P<sym>(?:(?!(?:at|with|stop|target|around|here|now|today|right)\b)[A-Za-z&.\-]+\s*){1,3}?)"
    r"(?=\s*(?:at\b|@|with\b|around\b|here\b|now\b|today\b|right\b|,|$|[?.!]))"
    r"(?:\s*(?:at|@|around)\s+" + _NUM + r")?(?:.*?\bstop\s+(?:at\s+)?" + _NUM + r")?"
    r"(?:.*?\btarget\s+(?:at\s+|of\s+)?" + _NUM + r")?(?:\s*(?:right\s+)?(?:here|now|today))?[?.!]*$", re.I)
REVIEW_RE = re.compile(
    _LEAD + r"(?:how'?s|how is|how was|how did|how am i doing (?:with|on)|review|give me a review of|run a review of)\s+"
    r"(?:i\s+)?(?:my\s+)?(?:trading|trades|trade)(?:\s+(?:this|for the|so far this)?\s*(?P<period>today|week|month|year))?"
    r"(?:\s+(?:going|looking|doing|been))?[?.!]*$|" + _LEAD + r"trading review(?:\s+(?:for\s+)?(?:this\s+)?"
    r"(?P<period2>today|week|month|year))?[?.!]*$", re.I)


def _check_match(t: str) -> tuple[str, dict[str, Any]] | None:
    m = CHECK_RE.match(_clean_sentence(t).replace(" at the ", " at "))
    if not m:
        return None
    syms = _tickers(m.group("sym"), t + " stock")
    if not syms:
        return None
    side = "short" if m.group("side").lower().startswith(("sell", "short")) else "long"
    args: dict[str, Any] = {"symbol": syms[0], "side": side}
    for i, name in ((3, "entry"), (4, "stop"), (5, "target")):
        if m.group(i):
            args[name] = float(m.group(i).replace(",", ""))
    return "check_trade", args


TEXT_RE = re.compile(
    _LEAD + r"(?:text|message|imessage|send (?:a )?(?:text|message) to)\s+(?P<client>.+?)\s+"
    r"(?:saying|that|to say|and say|and tell (?:him|her|them)|telling (?:him|her|them)|:)\s+(?P<msg>.+)$", re.I)
SEND_RE = re.compile(r"^(?:yes|yeah|yep|ok(?:ay)?)?[\s,]*(?:send it|send|send that|go ahead(?: and send(?: it)?)?|do it|"
                     r"yes,? send(?: it)?|ship it)[.!]*$", re.I)
CANCEL_TEXT_RE = re.compile(r"^(?:no,?\s*)?(?:cancel(?: it| that| the text)?|don'?t send(?: it)?|scrap (?:it|that))[.!]*$", re.I)


# "No, I said Thursday" / "you misheard me" / "that's not what I said — book Jay"
_CORR_LEAD = r"(?:(?:no+|nope|wait|hold on|hang on|stop|sorry|actually|hey amo|amo)\b[ ,.!-]*)"
CORRECTION_RE = re.compile(
    rf"^(?:{_CORR_LEAD}*(?:that'?s not what i (?:said|meant|asked(?: for)?)|that'?s not it|you misheard(?: me)?|"
    r"you heard (?:me )?wrong|you got (?:it|that|me) wrong|i didn'?t say(?: that)?|i meant(?! to\b))"
    rf"|{_CORR_LEAD}+(?:i said|i asked(?: for| you)?))\b[ ,.:!-]*(?P<rest>.*)$", re.I)


def correction(text: str) -> str | None:
    """None if this isn't a correction; otherwise what the user actually meant ("" = not said yet)."""
    t = text.strip()
    m = CORRECTION_RE.match(t)
    if not m:
        return None
    for _ in range(3):  # "that's not what I said, I said Thursday" → "Thursday"
        rest = m.group("rest").strip(" ,.!?-")
        m = CORRECTION_RE.match(rest) or re.match(r"^(?:i said|i asked(?: for)?)\b[ ,.:!-]*(?P<rest>.*)$", rest, re.I)
        if not m:
            break
    return rest


def text_reply(text: str, has_pending: bool) -> tuple[str, dict[str, Any]] | None:
    """'send it' / 'cancel' only mean something while a text draft is waiting."""
    t = _clean_sentence(text)
    if has_pending and SEND_RE.match(t):
        return "send_text", {}
    if has_pending and CANCEL_TEXT_RE.match(t):
        return "cancel_text", {}
    return None


def match(text: str) -> tuple[str, dict[str, Any]] | None:
    t = text.strip()
    m = TEXT_RE.match(t.rstrip())
    if m:
        msg = m.group("msg").strip()
        msg = msg[:1].upper() + msg[1:]
        return "text_client", {"client": m.group("client").strip(" ,"), "message": msg}
    rv = REVIEW_RE.match(_clean_sentence(t))
    if rv:
        return "trading_review", {"period": (rv.group("period") or rv.group("period2") or "week").lower()}
    ck = _check_match(t)
    if ck:
        return ck
    wl = _watch_match(t)
    if wl:
        return wl
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
