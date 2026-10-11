"""Coding help (explain / fix with your OK / undo) and self-repair (tested on a copy, rollback)."""

import textwrap

import pytest

from amo import coding, selfrepair

BUGGY = textwrap.dedent('''\
    def average(xs):
        return sum(xs) / len(xs)
''')
TESTS = textwrap.dedent('''\
    from amo.calc import average


    def test_average():
        assert average([2, 4]) == 3


    def test_empty():
        assert average([]) == 0
''')
GOOD_FIX = textwrap.dedent('''\
    FILE: amo/calc.py
    <<<<<<< SEARCH
        return sum(xs) / len(xs)
    =======
        return sum(xs) / len(xs) if xs else 0
    >>>>>>> REPLACE
''')
BAD_FIX = GOOD_FIX.replace("sum(xs) / len(xs) if xs else 0", "0")


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "amo").mkdir()
    (tmp_path / "amo" / "__init__.py").write_text("")
    (tmp_path / "amo" / "calc.py").write_text(BUGGY)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(TESTS.replace("def test_empty", "def _later_test_empty"))
    return tmp_path


def crash(project):
    """Run the buggy code from the project so the traceback points into it."""
    ns = {}
    code = compile((project / "amo" / "calc.py").read_text(), str(project / "amo" / "calc.py"), "exec")
    exec(code, ns)
    try:
        ns["average"]([])
    except ZeroDivisionError as e:
        return e


# ------------------------------------------------------------------ edits
def test_parse_and_preview_edits(project):
    edits = coding.parse_edits(GOOD_FIX)
    assert edits[0].file == "amo/calc.py" and "if xs else 0" in edits[0].replace
    new = coding.preview(project, edits)
    assert "if xs else 0" in new["amo/calc.py"]
    assert coding.changed_lines(coding.diff(project, new)) == 2
    with pytest.raises(ValueError):
        coding.preview(project, coding.parse_edits(GOOD_FIX.replace("sum(xs) / len(xs)\n=", "nope\n=")))


def test_code_questions_are_recognised():
    assert coding.is_code_question("why does my python script crash")
    assert coding.is_code_question("TypeError: x is undefined")
    assert not coding.is_code_question("what's my best trading method")
    assert not coding.is_code_question("what's the door code")
    assert coding.match("fix the bug in main.py") == ("code_fix", {"path": "main.py", "problem": ""})
    assert coding.match("apply the fix") == ("code_apply", {})
    assert coding.match("roll back your last fix") == ("self_rollback", {})
    assert coding.match("fix yourself") == ("self_repair", {})


def test_fix_your_code_with_ok_and_undo(db, llm, project, monkeypatch):
    from amo.agent import Agent
    from amo.config import settings

    monkeypatch.setattr(settings, "code_dirs", str(project))
    (project / "pyproject.toml").write_text("[project]\nname='x'\n")
    llm.script({"role": "assistant", "content": GOOD_FIX})
    a = Agent(db)
    out = a.chat([{"role": "user", "content": f"fix {project}/amo/calc.py - it crashes on an empty list"}])["content"]
    assert out.startswith("Here's the fix for amo/calc.py (2 lines)") and "```diff" in out
    assert llm.calls[-1]["model"] == settings.coder_model
    assert (project / "amo" / "calc.py").read_text() == BUGGY  # nothing changed yet
    assert a.chat([{"role": "user", "content": "apply the fix"}])["content"].startswith("Done — changed amo/calc.py")
    assert "if xs else 0" in (project / "amo" / "calc.py").read_text()
    assert a.chat([{"role": "user", "content": "undo the fix"}])["content"].startswith("Put back amo/calc.py")
    assert (project / "amo" / "calc.py").read_text() == BUGGY


def test_coding_questions_go_to_the_code_model(db, llm):
    from amo.agent import Agent
    from amo.config import settings

    llm.script({"role": "assistant", "content": "Use `if xs` before dividing."})
    out = Agent(db).chat([{"role": "user", "content": "ZeroDivisionError: division by zero in my python script, why?"}])
    assert out["content"] == "Use `if xs` before dividing." and llm.calls[-1]["model"] == settings.coder_model


# ------------------------------------------------------------------ self-repair
def test_crashes_are_recorded(db, project):
    e = crash(project)
    assert selfrepair.record(e, db, project)
    assert selfrepair.record(e, db, project)
    row = db.one("SELECT * FROM errors")
    assert row["signature"] == "ZeroDivisionError:amo/calc.py:average" and row["count"] == 2 and row["line"] == 2


def test_self_repair_applies_a_tested_fix_and_rolls_back(db, project):
    selfrepair.record(crash(project), db, project)
    err = selfrepair.due(db, selfrepair.local_now())[0]
    r = selfrepair.repair(err, db, project, coder=lambda p: GOOD_FIX, restart=False)
    assert r["result"] == "applied", r
    assert "if xs else 0" in (project / "amo" / "calc.py").read_text()
    assert db.scalar("SELECT status FROM errors") == "fixed"
    out = selfrepair.rollback(db, restart=False)
    assert out["files"] == ["amo/calc.py"] and (project / "amo" / "calc.py").read_text() == BUGGY


def test_self_repair_refuses_a_fix_that_fails_tests(db, project):
    """A 'fix' that breaks something else (here: averages come out wrong) is thrown away."""
    selfrepair.record(crash(project), db, project)
    err = selfrepair.due(db, selfrepair.local_now())[0]
    r = selfrepair.repair(err, db, project, coder=lambda p: BAD_FIX, restart=False)
    assert r["result"] == "tests_failed"
    assert (project / "amo" / "calc.py").read_text() == BUGGY  # untouched
    selfrepair.repair(err | {"attempts": 1}, db, project, coder=lambda p: BAD_FIX, restart=False)
    assert db.scalar("SELECT status FROM errors") == "failed"  # gives up after two tries


def test_self_repair_never_edits_protected_files(db, project):
    (project / "amo" / "db.py").write_text("x = 1\n")
    db.insert("errors", {"signature": "X:amo/db.py:f", "kind": "KeyError", "message": "", "traceback": "",
                         "file": "amo/db.py", "line": 1, "first_seen": "2026-01-01", "last_seen": "2026-01-01"})
    err = db.one("SELECT * FROM errors")
    assert selfrepair.repair(err, db, project, coder=lambda p: GOOD_FIX, restart=False)["result"] == "ignored"


TOOLS = textwrap.dedent('''\
    import json
    from amo.calc import average


    def call(name, args):
        try:
            return json.dumps({"avg": average(args["xs"])})
        except Exception as e:
            return json.dumps({"error": f"something broke inside me ({e})"})
''')
NO_OP_FIX = GOOD_FIX.replace("sum(xs) / len(xs) if xs else 0", "sum(xs)/len(xs)")


def test_fix_must_make_the_crash_go_away(db, project):
    """Tests passing isn't enough: the request that crashed is replayed on a copy of the data."""
    import json

    (project / "amo" / "tools.py").write_text(TOOLS)
    selfrepair.record(crash(project), db, project, repro={"tool": "avg", "args": {"xs": []}})
    err = selfrepair.due(db, selfrepair.local_now())[0]
    assert json.loads(err["repro"]) == {"tool": "avg", "args": {"xs": []}}
    r = selfrepair.repair(err, db, project, coder=lambda p: NO_OP_FIX, restart=False)
    assert r["result"] == "tests_failed" and "still happens" in r["detail"]
    r = selfrepair.repair(db.one("SELECT * FROM errors"), db, project, coder=lambda p: GOOD_FIX, restart=False)
    assert r["result"] == "applied"


def test_code_tools_never_touch_secrets_or_amo_data(project, monkeypatch):
    from amo.config import settings

    monkeypatch.setattr(settings, "code_dirs", str(project))
    (project / ".env").write_text("KEY=secret")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("x")
    assert coding.find_path("amo/calc.py") == (project / "amo" / "calc.py").resolve()
    assert coding.find_path(".env") is None and coding.find_path(str(project / ".env")) is None
    assert coding.find_path(".git/config") is None
    assert coding.find_path("../etc/passwd") is None
    assert not coding.allowed(coding.PROJECT_ROOT / "data" / "amo.db")


def test_a_fix_is_not_applied_if_the_file_changed(db, llm, project, monkeypatch):
    from amo.config import settings

    monkeypatch.setattr(settings, "code_dirs", str(project))
    (project / "pyproject.toml").write_text("[project]\nname='x'\n")
    llm.script({"role": "assistant", "content": GOOD_FIX})
    assert coding.propose_fix(f"{project}/amo/calc.py", "crashes on empty", db=db)["proposed"]
    (project / "amo" / "calc.py").write_text(BUGGY + "\n# my own edit\n")
    with pytest.raises(ValueError, match="changed since"):
        coding.apply_pending(db)
    assert "# my own edit" in (project / "amo" / "calc.py").read_text()


def test_self_repair_sandbox_has_what_the_tests_need(project):
    (project / "evals").mkdir()
    (project / "evals" / "cases.json").write_text("[]")
    box = selfrepair._sandbox(project)
    try:
        assert (box / "evals" / "cases.json").is_file() and (box / "amo" / "calc.py").is_file()
    finally:
        import shutil

        shutil.rmtree(box)


def test_self_repair_never_replays_outside_actions(tmp_path):
    for tool in ("send_text", "hardware_control", "code_apply", "self_rollback", "send_notification"):
        assert not selfrepair.can_replay(tool)
        fixed, why = selfrepair.reproduce(tmp_path, {"tool": tool, "args": {}})
        assert fixed and "not replayed" in why
    assert selfrepair.can_replay("finance_summary")
