"""Printing through the Mac's printing system (CUPS) — tested with stand-in lp / lpstat / cancel."""

import os
import stat

import pytest

from amo import printer
from amo.agent import Agent
from amo.config import settings

LPSTAT = """#!/bin/sh
cat <<'OUT'
printer HP_OfficeJet_Pro_9015e is idle.  enabled since Mon Oct 12 09:00:00 2026
printer Brother_Label is idle.  enabled since Mon Oct 12 09:00:00 2026
system default destination: HP_OfficeJet_Pro_9015e
device for HP_OfficeJet_Pro_9015e: ipp://HP-OfficeJet.local/ipp/print
device for Brother_Label: usb://Brother/QL
OUT
"""


@pytest.fixture()
def cups(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "lp.log"
    scripts = {
        "lpstat": LPSTAT,
        "lp": f'#!/bin/sh\necho "$@" >> {log}\nlast=""; for a; do last="$a"; done\ncp "$last" {tmp_path}/printed.txt\n'
              'echo "request id is HP_OfficeJet_Pro_9015e-7 (1 file(s))"\n',
        "cancel": f'#!/bin/sh\necho "cancel $@" >> {log}\n',
    }
    for name, body in scripts.items():
        f = bin_ / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(printer, "OUT", tmp_path / "printouts")
    monkeypatch.setattr(settings, "printer", "")
    return tmp_path


def test_sees_printers_and_picks_the_default(cups):
    names = [p["name"] for p in printer.printers()]
    assert names == ["HP_OfficeJet_Pro_9015e", "Brother_Label"]
    assert printer.pick() == "HP_OfficeJet_Pro_9015e"
    assert printer.pick("brother") == "Brother_Label"
    with pytest.raises(ValueError, match="don't see a printer called"):
        printer.pick("canon")


def test_print_that_prints_what_amo_said(cups, db, llm):
    out = Agent(db).chat([{"role": "user", "content": "give me a packing list for the session"},
                          {"role": "assistant", "content": "**Packing list:** mics, cables, hard drive."},
                          {"role": "user", "content": "print that"}])["content"]
    assert out == "Printing From AMO on HP OfficeJet Pro 9015e." and llm.calls == []
    printed = (cups / "printed.txt").read_text()
    assert printed.startswith("From AMO\n") and "Packing list: mics, cables, hard drive." in printed  # no ** on paper
    assert "-d HP_OfficeJet_Pro_9015e -n 1" in (cups / "lp.log").read_text()


def test_print_an_invoice_and_copies(cups, db, llm):
    from amo.crm import StudioCRM

    crm = StudioCRM(db)
    crm.add_client("Jay Carter", artist_name="Lil Jay")
    s = crm.book_session("Lil Jay", "2026-10-01 19:00", hours=3, rate=50)
    crm.complete_session(s["id"], paid=100)
    out = Agent(db).chat([{"role": "user", "content": "print 2 copies of Lil Jay's invoice"}])["content"]
    assert out.startswith("Printing Invoice — Lil Jay (2 copies)") and llm.calls == []
    text = (cups / "printed.txt").read_text()
    assert "Bill to: Jay Carter (Lil Jay)" in text and "Total due" in text and "$50.00" in text


def test_print_requests_are_understood():
    m = printer.match
    assert m("print my schedule for tomorrow") == ("print_document", {"what": "schedule tomorrow"})
    assert m("print my goals two-sided") == ("print_document", {"what": "goals", "two_sided": True})
    assert m("print setlist.pdf") == ("print_document", {"file": "setlist.pdf"})
    assert m("how much ink is left?") == ("printer_ink", {})
    assert m("cancel printing") == ("cancel_printing", {})
    assert m("print money") is None and m("the printer is loud") is None


def test_no_printer_explains_how_to_add_one(tmp_path, monkeypatch, db, llm):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    f = bin_ / "lpstat"
    f.write_text("#!/bin/sh\necho 'lpstat: No destinations added.' >&2\nexit 1\n")
    f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    out = Agent(db).chat([{"role": "user", "content": "print my goals"}])["content"]
    assert "Printers & Scanners" in out and llm.calls == []
