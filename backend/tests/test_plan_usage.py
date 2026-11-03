"""The Claude subscription readout (plan_usage.py) and the status-line
recorder (statusline_record.py)."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ccpipe import config, plan_usage as pu, statusline_record as rec

# What `claude -p /usage --output-format stream-json --verbose` printed
# (2.1.317, 2026-11-03), trimmed to the lines plan_usage reads.
REPORT = {
    "session": {"total_cost_usd": 0, "model_usage": {}},
    "rate_limits": {
        "limits": [
            {"kind": "weekly_all", "group": "weekly", "percent": 40,
             "resets_at": "2026-11-06T06:00:00.513182+00:00", "scope": None,
             "severity": "normal", "is_active": True},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 5,
             "resets_at": "2026-11-06T05:59:59.513311+00:00",
             "scope": {"model": {"display_name": "Fable"}, "surface": None},
             "severity": "normal", "is_active": False},
        ],
        "extra_usage": {"is_enabled": False, "monthly_limit": 2500, "used_credits": 0,
                        "utilization": 0, "currency": "GBP"},
    },
}
TEXT = ("You are currently using your subscription to power your Claude Code usage\n\n"
        "Current week (all models): 40% used · resets Nov 6, 6am (UTC)\n")


def stream(report=REPORT, text=TEXT) -> str:
    lines = [{"type": "system", "subtype": "init"},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]},
              "usage_report": report},
             {"type": "result", "subtype": "success", "total_cost_usd": 0}]
    return "\n".join(json.dumps(x) for x in lines) + "\n"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(config.CONFIG_FILE_ENV, str(tmp_path / "config.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    config.reset_cache()
    for name, val in (("_usage", None), ("_attempt_at", 0.0), ("_next_at", 0.0), ("_failures", 0),
                      ("_error", None), ("_running", False), ("_forced", False), ("_wake", None),
                      ("_loaded", False), ("_records", {}), ("_pane_keys", {})):
        monkeypatch.setattr(pu, name, val)
    yield tmp_path
    config.reset_cache()


# ── the /usage report ────────────────────────────────────────────────────

def test_report_parsed_into_meters_and_credits():
    report, text = pu.parse_report(stream())
    assert report == REPORT and "Current week" in text
    meters, credits = pu.meters_from_report(report, 1000.0)
    assert [(m["kind"], m["label"], m["percent"], m["active"]) for m in meters] == [
        ("weekly_all", "week · all models", 40.0, True),
        ("weekly_scoped", "week · Fable", 5.0, False)]
    assert meters[0]["window"] == 7 * 24 * 3600 and meters[0]["at"] == 1000.0
    assert meters[0]["resets_at"] == pytest.approx(1793944800.513182)
    assert credits == {"enabled": False, "limit": 2500.0, "used": 0.0, "pct": 0.0, "currency": "GBP"}


def test_report_without_plan_limits():
    # API-key logins and tokens without the profile scope: text, no field.
    assert pu.parse_report(stream(report=None))[0] is None
    assert pu.meters_from_report({"session": {}, "rate_limits": None}, 0) is None
    assert pu.parse_report("not json\n{\"type\": \"result\"}\n") == (None, "")
    # Rows that don't parse are skipped, not fatal.
    meters, _ = pu.meters_from_report({"rate_limits": {"limits": [
        {"kind": "session", "group": "session", "percent": 12, "resets_at": None},
        {"kind": "", "percent": 3}, {"kind": "x", "percent": "lots"}, "junk"]}}, 0)
    assert [(m["kind"], m["label"], m["window"]) for m in meters] == [("session", "5-hour", 5 * 3600)]


# ── merging in the status line ───────────────────────────────────────────

def _usage(at=1000.0):
    meters, credits = pu.meters_from_report(REPORT, at)
    return {"at": at, "meters": meters, "credits": credits}


def test_newer_status_line_overlays_its_meters():
    now = 1_793_900_000.0
    sl = (2000.0, {"seven_day": {"used_percentage": 41, "resets_at": 1793944800},
                   "five_hour": {"used_percentage": 12.5, "resets_at": now + 3600}})
    meters = {m["kind"]: m for m in pu.merge(_usage(), sl, now)}
    assert meters["weekly_all"]["percent"] == 41 and meters["weekly_all"]["source"] == "statusline"
    assert meters["weekly_all"]["at"] == 2000.0
    assert meters["weekly_scoped"]["percent"] == 5 and meters["weekly_scoped"]["source"] == "usage"
    # A 5-hour window /usage didn't report is added from the status line.
    assert meters["session"]["percent"] == 12.5 and meters["session"]["label"] == "5-hour"


def test_older_status_line_is_ignored():
    now = 1_793_900_000.0
    sl = (500.0, {"seven_day": {"used_percentage": 1, "resets_at": 1793944800}})
    assert {m["kind"]: m["percent"] for m in pu.merge(_usage(), sl, now)}["weekly_all"] == 40


def test_status_line_alone_and_an_expired_window():
    now = 1_793_900_000.0
    sl = (2000.0, {"seven_day": {"used_percentage": 39, "resets_at": now - 1},
                   "five_hour": {"used_percentage": 20, "resets_at": now + 60}})
    meters = {m["kind"]: m for m in pu.merge(None, sl, now)}
    # After its reset time a window reads 0 until the next reading.
    assert meters["weekly_all"]["percent"] == 0 and meters["weekly_all"]["reset"] is True
    # No server pick: the all-models weekly meter is the headline.
    assert meters["weekly_all"]["active"] and not meters["session"]["active"]
    assert pu.merge(None, None, now) == []


# ── launching Claude ─────────────────────────────────────────────────────

def test_launch_command_runs_the_recorder(state):
    (state / "claude").mkdir()
    (state / "claude" / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "echo mine", "padding": 2, "refreshInterval": True}}))
    cmd = pu.claude_command("--continue")
    assert cmd.startswith("claude --settings '") and cmd.endswith(" --continue")
    flag = json.loads(cmd.split("'")[1])
    sl = flag["statusLine"]
    assert sl["type"] == "command" and sl["padding"] == 2 and "refreshInterval" not in sl
    assert "statusline_record.py" in sl["command"] and str(pu.statusline_dir()) in sl["command"]
    assert sl["command"].split()[:2] == [sys.executable, "-I"]


@pytest.mark.parametrize("cfg", [{"enabled": False}, {"statusline": False}])
def test_launch_command_without_the_recorder(state, cfg):
    (state / "config.json").write_text(json.dumps({"plan_usage": cfg}))
    config.reset_cache()
    assert pu.claude_command() == "claude"
    assert pu.claude_command("--resume x") == "claude --resume x"


def test_config_clamps():
    c = config.PlanUsageConfig.from_dict({"poll_minutes": 1, "enabled": "yes"})
    assert c.poll_minutes == 5 and c.enabled is True
    assert config.PlanUsageConfig.from_dict({"poll_minutes": 10_000}).poll_minutes == 240
    assert config.AppConfig.from_dict({}).to_dict()["plan_usage"] == {
        "enabled": True, "statusline": True, "poll_minutes": 15}


# ── the poller ───────────────────────────────────────────────────────────

def _fake_claude(tmp_path, out: str, code: int = 0, sleep: float = 0) -> list[str]:
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import sys, time, os\n"
        f"time.sleep({sleep})\n"
        "open(os.path.join(os.getcwd(), 'ran'), 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.stdout.write({out!r})\n"
        f"sys.exit({code})\n")
    return [sys.executable, str(script), "-p", "/usage"]


def test_poll_success_saves_and_reloads(state, monkeypatch):
    monkeypatch.setattr(pu, "_claude_argv", lambda: _fake_claude(state, stream()))
    asyncio.run(pu.poll_once())
    snap = pu.snapshot()
    assert [m["percent"] for m in snap["meters"]] == [40, 5]
    assert snap["credits"]["currency"] == "GBP" and snap["error"] is None and not snap["checking"]
    # It ran in ccpipe's own work directory, not anywhere of yours.
    assert (state / "xdg" / "ccpipe" / "plan-usage" / "ran").exists()
    saved = state / "xdg" / "ccpipe" / "plan-usage" / "usage.json"
    assert saved.exists() and stat.S_IMODE(saved.parent.stat().st_mode) == 0o700
    # A restart picks the reading (and the attempt time) back up.
    monkeypatch.setattr(pu, "_usage", None)
    monkeypatch.setattr(pu, "_loaded", False)
    monkeypatch.setattr(pu, "_attempt_at", 0.0)
    assert [m["percent"] for m in pu.snapshot()["meters"]] == [40, 5]
    assert pu._attempt_at > 0


def test_rate_limited_keeps_the_old_reading_and_backs_off(state, monkeypatch):
    monkeypatch.setattr(pu, "_claude_argv", lambda: _fake_claude(state, stream()))
    asyncio.run(pu.poll_once())
    first = pu._usage
    limited = stream(text="Showing last-known usage (fetched 40 min ago)\nrate limited until 3:14am")
    monkeypatch.setattr(pu, "_claude_argv", lambda: _fake_claude(state, limited))
    asyncio.run(pu.poll_once())
    assert pu._usage is first and "rate limited" in pu._error
    assert pu._next_at - time.time() == pytest.approx(15 * 60, abs=5)
    asyncio.run(pu.poll_once())
    assert pu._next_at - time.time() == pytest.approx(30 * 60, abs=5)    # doubles
    for _ in range(5):
        asyncio.run(pu.poll_once())
    assert pu._next_at - time.time() <= pu.BACKOFF_MAX_S + 1


@pytest.mark.parametrize("make", [
    lambda s: _fake_claude(s, "", code=1),
    lambda s: _fake_claude(s, stream(report=None)),
    lambda s: None,
])
def test_failures_back_off(state, monkeypatch, make):
    monkeypatch.setattr(pu, "_claude_argv", lambda: make(state))
    asyncio.run(pu.poll_once())
    assert pu._usage is None and pu._error and pu._failures == 1 and pu._next_at > time.time()
    assert pu.snapshot()["meters"] == [] and pu.snapshot()["error"] == pu._error


def test_a_hung_claude_is_killed(state, monkeypatch):
    monkeypatch.setattr(pu, "RUN_TIMEOUT_S", 0.5)
    monkeypatch.setattr(pu, "_claude_argv", lambda: _fake_claude(state, stream(), sleep=30))
    t0 = time.monotonic()
    asyncio.run(pu.poll_once())
    assert time.monotonic() - t0 < 5 and "timed out" in pu._error


def test_when_a_check_is_due(state, monkeypatch):
    now = 10_000.0
    demand = {"on": False}
    monkeypatch.setattr(pu.sysstats, "demanded", lambda: demand["on"])
    assert not pu._due(now)                         # nobody looking
    demand["on"] = True
    assert pu._due(now)
    monkeypatch.setattr(pu, "_attempt_at", now - 60)
    assert not pu._due(now)                         # MIN_GAP_S
    monkeypatch.setattr(pu, "_attempt_at", now - 5 * 60)
    assert not pu._due(now)                         # within poll_minutes
    monkeypatch.setattr(pu, "_forced", True)
    assert pu._due(now)                             # the pop-up asked
    monkeypatch.setattr(pu, "_next_at", now + 10)
    assert not pu._due(now)                         # backing off
    monkeypatch.setattr(pu, "_next_at", 0.0)
    monkeypatch.setattr(pu, "_running", True)
    assert not pu._due(now)


def test_refresh_only_when_the_reading_is_old(state, monkeypatch):
    monkeypatch.setattr(pu, "_usage", {"at": time.time() - 60, "meters": [], "credits": None})
    assert pu.request_refresh() is False and pu._forced is False
    monkeypatch.setattr(pu, "_usage", {"at": time.time() - pu.FRESH_S - 1, "meters": [], "credits": None})
    assert pu.request_refresh() is True and pu._forced is True


def test_turned_off(state, monkeypatch):
    (state / "config.json").write_text(json.dumps({"plan_usage": {"enabled": False}}))
    config.reset_cache()
    assert pu.snapshot() is None and pu.request_refresh() is False
    assert asyncio.run(pu.session_context("work")) is None


# ── status-line readings ─────────────────────────────────────────────────

def _record(state, name, data, age=0.0):
    d = pu.statusline_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(data))
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def test_newest_reading_with_limits_wins(state):
    _record(state, "1_1.json", {"rate_limits": {"seven_day": {"used_percentage": 10}}}, age=300)
    _record(state, "1_2.json", {"rate_limits": {"seven_day": {"used_percentage": 20}}}, age=60)
    _record(state, "1_3.json", {"context_window": {"used_percentage": 3}}, age=1)   # no limits yet
    _record(state, "1_4.json", {"rate_limits": {}}, age=0)
    (pu.statusline_dir() / "junk.json").write_text("{")
    at, rl = pu.latest_statusline()
    assert rl["seven_day"]["used_percentage"] == 20 and time.time() - at == pytest.approx(60, abs=2)


def test_old_readings_are_pruned(state):
    old = _record(state, "1_1.json", {}, age=pu.RECORD_MAX_AGE_S + 10)
    new = _record(state, "1_2.json", {}, age=10)
    pu._prune_records(time.time())
    assert not old.exists() and new.exists()


def test_session_context_from_its_pane(state, monkeypatch):
    _record(state, "4242_7.json", {"context_window": {"used_percentage": 34, "context_window_size": 1_000_000},
                                   "model": {"display_name": "Opus 5.5"}})
    asked = []

    async def query(cmd, timeout=0.5):
        asked.append(cmd)
        return ["4242 %7"]

    monkeypatch.setattr(pu.control_client, "query", query)
    ctx = asyncio.run(pu.session_context("work"))
    assert ctx["percent"] == 34 and ctx["window"] == 1_000_000 and ctx["model"] == "Opus 5.5"
    assert asked == ["display-message -p -t '=work:' '#{pid} #{pane_id}'"]
    asyncio.run(pu.session_context("work"))
    assert len(asked) == 1                                    # cached
    assert asyncio.run(pu.session_context("bad name;")) is None


def test_session_context_falls_back_to_tmux(state, monkeypatch):
    async def down(cmd, timeout=0.5):
        return None

    async def tmux_out(*args):
        return "99 %3\n"

    monkeypatch.setattr(pu.control_client, "query", down)
    monkeypatch.setattr(pu.tmux, "_tmux_out", tmux_out)
    assert asyncio.run(pu.session_context("work")) is None    # no reading yet
    monkeypatch.setattr(pu, "_pane_keys", {})
    _record(state, "99_3.json", {"context_window": {"used_percentage": 8}})
    assert asyncio.run(pu.session_context("work"))["percent"] == 8


# ── the recorder ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("env,want", [
    ({"TMUX": "/tmp/tmux-1000/default,4242,0", "TMUX_PANE": "%7"}, "4242_7.json"),
    ({"TMUX": "/tmp/tmux-1000/default,4242,0"}, None),
    ({"TMUX_PANE": "%7"}, None),
    ({"TMUX": "x,notapid,0", "TMUX_PANE": "%7"}, None),
    ({"TMUX": "x,1,0", "TMUX_PANE": "%7/../../x"}, None),
])
def test_record_name(env, want):
    assert rec.record_name(env) == want


def _run_recorder(tmp_path, data: dict, env_extra: dict, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TMUX", "CCPIPE_STATUSLINE"))}
    env.update(env_extra)
    return subprocess.run([sys.executable, "-I", rec.__file__, str(tmp_path / "out")],
                          input=json.dumps(data).encode(), capture_output=True, env=env,
                          cwd=cwd or tmp_path, timeout=20)


def test_recorder_saves_and_runs_your_own_command(tmp_path):
    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "read x; echo \"mine:$x\" | cut -c1-12"}}))
    data = {"workspace": {"project_dir": str(tmp_path / "proj")}, "rate_limits": {"seven_day": {"used_percentage": 3}}}
    r = _run_recorder(tmp_path, data, {"TMUX": "s,77,0", "TMUX_PANE": "%5", "CLAUDE_CONFIG_DIR": str(home)})
    assert r.returncode == 0 and r.stdout == b'mine:{"works\n'
    saved = tmp_path / "out" / "77_5.json"
    assert json.loads(saved.read_text()) == data
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "out").stat().st_mode) == 0o700


def test_recorder_prefers_the_projects_status_line(tmp_path):
    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "echo user"}}))
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(json.dumps({"statusLine": {"command": "echo project"}}))
    data = {"workspace": {"project_dir": str(proj)}}
    r = _run_recorder(tmp_path, data, {"CLAUDE_CONFIG_DIR": str(home)})
    assert r.stdout == b"project\n"
    (proj / ".claude" / "settings.local.json").write_text(json.dumps({"statusLine": {"command": "echo local"}}))
    assert _run_recorder(tmp_path, data, {"CLAUDE_CONFIG_DIR": str(home)}).stdout == b"local\n"
    assert not (tmp_path / "out").exists()                 # outside tmux: nothing saved


def test_recorder_without_a_command_of_your_own(tmp_path):
    home = tmp_path / "claude"
    home.mkdir()
    r = _run_recorder(tmp_path, {}, {"CLAUDE_CONFIG_DIR": str(home), "TMUX": "s,1,0", "TMUX_PANE": "%1"})
    assert r.returncode == 0 and r.stdout == b""
    # Pointing your own status line at the recorder can't loop.
    (home / "settings.json").write_text(json.dumps(
        {"statusLine": {"command": f"{sys.executable} -I {rec.__file__} {tmp_path / 'out'}"}}))
    r = _run_recorder(tmp_path, {}, {"CLAUDE_CONFIG_DIR": str(home)})
    assert r.returncode == 0 and r.stdout == b""


def test_recorder_ignores_the_sessions_directory(tmp_path):
    # Claude starts it in the session's cwd; -I keeps a planted json.py out.
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / "json.py").write_text("raise SystemExit('planted module ran')\n")
    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"statusLine": {"command": "echo ok"}}))
    r = _run_recorder(tmp_path, {}, {"CLAUDE_CONFIG_DIR": str(home)}, cwd=cwd)
    assert r.returncode == 0 and r.stdout == b"ok\n"


# ── /api/system ──────────────────────────────────────────────────────────

def test_api_system_carries_plan_and_context(state, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    s = tmp_path / "s"
    s.mkdir()
    monkeypatch.setenv("CCPIPE_SESSION_SECRET_FILE", str(s / "secret"))
    monkeypatch.setenv("CCPIPE_CREDENTIALS_FILE", str(s / "credentials"))
    monkeypatch.setenv("CCPIPE_AUTH_USERNAME", "alice")
    monkeypatch.setenv("CCPIPE_AUTH_PASSWORD", "letmein")
    import ccpipe.auth as auth
    import ccpipe.main as m
    import ccpipe.routes.auth as routes_auth
    auth.reset_cached_credential()
    routes_auth.reset_throttle_state()
    monkeypatch.setattr(auth, "_revoked", None)
    importlib.reload(m)
    c = TestClient(m.app)
    H = {"X-Requested-By": "ccpipe"}
    assert c.post("/api/auth/login", headers=H, json={"username": "alice", "password": "letmein"}).status_code == 200
    monkeypatch.setattr(pu, "_usage", _usage(at=time.time()))
    monkeypatch.setattr(pu, "_loaded", True)

    async def query(cmd, timeout=0.5):
        return ["5 %1"]

    monkeypatch.setattr(pu.control_client, "query", query)
    _record(state, "5_1.json", {"context_window": {"used_percentage": 12}})
    body = c.get("/api/system?session=work").json()
    assert [m["percent"] for m in body["plan"]["meters"]] == [40, 5]
    assert body["session"]["context"]["percent"] == 12
    assert c.get("/api/system").json()["plan"]["credits"]["limit"] == 2500
    assert c.post("/api/plan-usage/refresh").status_code == 403          # no CSRF header
    r = c.post("/api/plan-usage/refresh", headers=H)
    assert r.status_code == 200 and r.json()["checking"] is False     # the reading is fresh
