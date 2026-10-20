"""Tushare daily points: cookie handling, sign/view/guess Flames, session loss and CLI import.

All HTTP is a fake transport; nothing contacts tushare.pro. Response shapes follow
the public web bundle (2026-10-19, logged out); repeated-sign wording comes from the
public shyz2006/tushare-checkin script and stays a T0 item until verified logged in.
"""

import io
import json
import logging
import random
import stat
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest

from keepflame.capabilities import CapabilityRegistry
from keepflame.capabilities.http import HttpResponse, HttpTransportError
from keepflame.capabilities.secrets import FileSecretResolver
from keepflame.cli import main as cli_main
from keepflame.core.config import ConfigError, FlameConfig, ScheduleConfig, load_config
from keepflame.core.models import KeepStatus
from keepflame.core.orchestrator import Orchestrator
from keepflame.flames.tushare import TushareKeeper
from keepflame.flames.tushare.cli import import_cookie
from keepflame.flames.tushare.cookies import CookieError, SessionCookie, load_file, parse_import, write_file
from keepflame.flames.tushare.keeper import (
    GUESS_CURRENT_URL, GUESS_VOTE_URL, LOGIN_URL, NEW_DATA_PAGE_URL, SIGN_URL, TASKS_URL, VIEW_URL,
    _Settings, majority_vote,
)
from keepflame.notifier.slack import render_lifecycle_message
from tests.helpers import FakeNotifier, FixedClock, MemoryStorage, StaticRegistry

ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 20, 9, 30, tzinfo=ZONE).astimezone(timezone.utc)
COOKIE = "uid=424242; session=SYNTHETIC-SECRET-VALUE; csrftoken=SYNTHETIC-CSRF"


class FakeHttp:
    """Routes (method, url) to a response, a callable, or a list consumed in order."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests = []

    def send(self, request, *, timeout_seconds):
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url))
        if isinstance(handler, list):
            handler = handler.pop(0) if len(handler) > 1 else handler[0]
        if handler is None:
            raise HttpTransportError("unrouted synthetic request")
        if callable(handler):
            return handler(request)
        return handler

    def calls(self):
        return [(r.method, r.url) for r in self.requests]


def respond(payload, status=200, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
    return lambda request: HttpResponse(status, request.url, {k.casefold(): v for k, v in (headers or {}).items()}, body)


def tasks(sign=False, view=False):
    return respond({"code": 0, "message": "success", "data": {"datas": [
        {"task_code": "USER_PROFILE", "task_name": "完善资料", "is_completed": True},
        {"task_code": "DAILY_SIGN", "task_name": "每日签到", "is_completed": sign, "limit_times": 1},
        {"task_code": "VIEW_NEW_DATA", "task_name": "浏览上新数据", "is_completed": view, "limit_times": 0},
    ]}})


def current(status=1, up=120, down=80, vote=None):
    return respond({"code": 0, "data": {
        "period": {"trade_date": "20261020", "status": status, "up_votes": up, "down_votes": down,
                   "result": None, "title": "10月20日上证指数涨跌"},
        "user_guess": {"vote": vote, "win_rate": -1},
    }})


GONE = respond({"code": 40201, "message": "未找到登录信息"})
PAGE = respond(b"<html><title>ChangeLog</title></html>")


def flame(tmp_path, task="sign", flame_id=None, **settings) -> FlameConfig:
    values = {"cookie_file": str(tmp_path / "tushare.cookie"), "task": task, "request_pause_seconds": 0}
    values.update(settings)
    return FlameConfig(flame_id or f"tushare-{task}", "tushare", "primary", True, ScheduleConfig(
        "Asia/Shanghai", time(9), 60, time(20), time(23), first_window_end=time(10, 30)), values)


def store_cookie(tmp_path, header=COOKIE):
    write_file(tmp_path / "tushare.cookie", SessionCookie(header))


def run(tmp_path, http, *, task="sign", storage=None, notifier=None, config=None, now=NOW, choose=None):
    storage = storage or MemoryStorage()
    notifier = notifier or FakeNotifier()
    keeper = TushareKeeper(sleeper=lambda _s: None, choose=choose or (lambda _sides: pytest.fail("no tie expected")))
    runner = Orchestrator(storage=storage, registry=StaticRegistry(keeper),
                          capabilities=CapabilityRegistry({"http": http}), secrets=FileSecretResolver(),
                          notifier=notifier, clock=FixedClock(now))
    outcome = runner.run_flame(config or flame(tmp_path, task), now=now)
    return outcome, storage, notifier


def dumped(outcome, notifier) -> str:
    return json.dumps([outcome.record.result.metadata, outcome.record.result.message,
                       [str(e) for e in notifier.events]], default=str, ensure_ascii=False)


def assert_only_tushare(http):
    assert http.requests
    for request in http.requests:
        parts = urlsplit(request.url)
        assert parts.scheme == "https" and parts.hostname == "tushare.pro"


# --- cookies -----------------------------------------------------------------------

def test_cookie_import_formats_keep_only_tushare_cookies():
    assert parse_import("Cookie: a=1; b=2").header == "a=1; b=2"
    exported = json.dumps([{"name": "uid", "value": "1", "domain": ".tushare.pro"},
                           {"name": "sid", "value": "x", "domain": "tushare.pro"},
                           {"name": "other", "value": "leak", "domain": ".example.com"}])
    assert parse_import(exported).header == "uid=1; sid=x"
    only_other = json.dumps([{"name": "other", "value": "leak", "domain": "example.com"}])
    for bad in ("", "a=1\nb=2", "novalue", "a=1; b", "[1]", "[", "x" * 9000, only_other):
        with pytest.raises(CookieError) as error:
            parse_import(bad)
        assert "a=1" not in str(error.value) and "leak" not in str(error.value)


def test_cookie_file_is_written_atomically_private_and_round_trips(tmp_path):
    path = tmp_path / "tushare.cookie"
    path.write_text("old")
    path.chmod(0o644)
    write_file(path, SessionCookie(COOKIE))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_file(FileSecretResolver().read_text(str(path))) == SessionCookie(COOKIE)
    assert json.loads(path.read_text())["origin"] == "https://tushare.pro"
    assert [p.name for p in tmp_path.iterdir()] == ["tushare.cookie"]
    link = tmp_path / "link.cookie"
    link.symlink_to(path)
    with pytest.raises(CookieError):
        write_file(link, SessionCookie(COOKIE))
    with pytest.raises(CookieError):
        load_file('{"cookie": "a=1", "origin": "https://evil.example"}')
    assert load_file("a=1; b=2") == SessionCookie("a=1; b=2")


# --- sign ----------------------------------------------------------------------------

def test_sign_success_claims_one_point_and_sends_the_cookie_only_to_tushare(tmp_path):
    store_cookie(tmp_path)
    http = FakeHttp({("GET", TASKS_URL): [tasks(sign=False), tasks(sign=True)],
                     ("POST", SIGN_URL): respond({"code": 0, "data": {"success": True, "message": "签到成功"}})})
    outcome, _, notifier = run(tmp_path, http)
    result = outcome.record.result
    assert result.status is KeepStatus.SUCCESS and result.message == "签到成功"
    assert result.reward.to_dict() == {"amount": "1", "unit": "积分", "source": "claimed"}
    assert result.metadata["sign_state"] == "completed_now" and result.metadata["remote_completed"] is True
    assert http.calls() == [("GET", TASKS_URL), ("POST", SIGN_URL), ("GET", TASKS_URL)]
    assert_only_tushare(http)
    post = http.requests[1]
    assert post.headers["Cookie"] == COOKIE and post.body == b""
    assert post.headers["Origin"] == "https://tushare.pro" and post.headers["Referer"] == "https://tushare.pro/weborder/"
    assert "SYNTHETIC" not in dumped(outcome, notifier)
    assert outcome.state.next_action_at.astimezone(ZONE).date().isoformat() == "2026-10-21"


def test_sign_already_completed_in_task_list_makes_no_action_call(tmp_path):
    store_cookie(tmp_path)
    http = FakeHttp({("GET", TASKS_URL): tasks(sign=True)})
    outcome, _, _ = run(tmp_path, http)
    result = outcome.record.result
    assert result.status is KeepStatus.SUCCESS and result.reward is None
    assert result.metadata["sign_state"] == "already_completed"
    assert http.calls() == [("GET", TASKS_URL)]


@pytest.mark.parametrize("message", ["今日签到已达上限", "任务已完成", "您今天已经签到过了"])
def test_repeated_sign_wording_counts_as_already_completed(tmp_path, message):
    store_cookie(tmp_path)
    http = FakeHttp({("GET", TASKS_URL): tasks(sign=False),
                     ("POST", SIGN_URL): respond({"code": 0, "data": {"success": False, "message": message}})})
    outcome, _, _ = run(tmp_path, http)
    assert outcome.record.result.status is KeepStatus.SUCCESS
    assert outcome.record.result.metadata["sign_state"] == "already_completed"


@pytest.mark.parametrize("routes, reason", [
    ({("POST", SIGN_URL): respond({"code": 0, "data": {"success": False, "message": "活动未开始"}})},
     "sign_not_successful"),
    ({("POST", SIGN_URL): respond({"code": 50001, "message": "系统繁忙"})}, "sign_rejected"),
    ({("POST", SIGN_URL): respond(b"<html>oops</html>")}, "sign_not_json"),
])
def test_unrecognized_sign_outcomes_fail_for_bounded_retry(tmp_path, routes, reason):
    store_cookie(tmp_path)
    http = FakeHttp({("GET", TASKS_URL): tasks(sign=False), **routes})
    outcome, _, _ = run(tmp_path, http)
    assert outcome.record.result.status is KeepStatus.FAILED
    assert outcome.record.result.metadata["reason"] == reason


def test_sign_that_is_not_credited_in_the_task_list_fails(tmp_path):
    store_cookie(tmp_path)
    http = FakeHttp({("GET", TASKS_URL): tasks(sign=False),
                     ("POST", SIGN_URL): respond({"code": 0, "data": {"success": True, "message": "签到成功"}})})
    outcome, _, _ = run(tmp_path, http)
    assert outcome.record.result.status is KeepStatus.FAILED
    assert outcome.record.result.metadata["reason"] == "sign_not_credited"


# --- session loss --------------------------------------------------------------------

@pytest.mark.parametrize("task, routes", [
    ("sign", {("GET", TASKS_URL): GONE}),
    ("sign", {("GET", TASKS_URL): tasks(), ("POST", SIGN_URL): GONE}),
    ("sign", {("GET", TASKS_URL): respond({}, 401)}),
    ("view", {("GET", TASKS_URL): respond(b"", 302, {"Location": "/weborder/#/login"})}),
    ("guess", {("GET", GUESS_CURRENT_URL): GONE}),
])
def test_session_loss_waits_for_a_human_with_the_login_link_and_import_command(tmp_path, task, routes):
    store_cookie(tmp_path)
    http = FakeHttp(routes)
    outcome, _, notifier = run(tmp_path, http, task=task)
    result = outcome.record.result
    assert result.status is KeepStatus.WAITING_HUMAN and result.metadata["reason"] == "session_expired"
    assert result.human_action.url == "https://tushare.pro/weborder/"
    assert LOGIN_URL == "https://tushare.pro/weborder/#/login" and LOGIN_URL in result.human_action.instructions
    assert f"keepflame tushare import-cookie --flame tushare-{task}" in result.human_action.instructions
    assert [e["event"] for e in notifier.events] == ["human_action_required"]
    text = render_lifecycle_message(event="human_action_required", notification_id="n", flame_id=f"tushare-{task}",
                                    account_id="primary", result=result)
    assert "Action-URL: https://tushare.pro/weborder/\n" in text and LOGIN_URL in text and "import-cookie" in text
    assert "SYNTHETIC" not in text + dumped(outcome, notifier)
    assert not any(method == "POST" and url != SIGN_URL for method, url in http.calls())


@pytest.mark.parametrize("prepare, reason", [
    (lambda p: None, "cookie_unreadable"),
    (lambda p: (p.write_text(json.dumps({"cookie": COOKIE})), p.chmod(0o644)), "cookie_unreadable"),
    (lambda p: (p.write_text("not a cookie"), p.chmod(0o600)), "cookie_invalid"),
])
def test_cookie_problems_wait_for_a_human_without_network(tmp_path, prepare, reason):
    prepare(tmp_path / "tushare.cookie")
    http = FakeHttp({})
    outcome, _, _ = run(tmp_path, http)
    assert outcome.record.result.status is KeepStatus.WAITING_HUMAN
    assert outcome.record.result.metadata["reason"] == reason and http.requests == []


def test_unchanged_cookie_while_waiting_makes_no_requests_until_reimport(tmp_path):
    store_cookie(tmp_path)
    routes = {("GET", TASKS_URL): GONE}
    first, storage, notifier = run(tmp_path, FakeHttp(routes))
    quiet = FakeHttp(routes)
    later = NOW + timedelta(hours=1)
    second, storage, notifier = run(tmp_path, quiet, storage=storage, notifier=notifier, now=later)
    assert quiet.requests == [] and second.record.result.metadata["network"] == "skipped_unchanged_import"
    assert second.record.result.human_action == first.record.result.human_action
    store_cookie(tmp_path, header="uid=424242; session=SYNTHETIC-FRESH")
    fresh = FakeHttp({("GET", TASKS_URL): [tasks(sign=False), tasks(sign=True)],
                      ("POST", SIGN_URL): respond({"code": 0, "data": {"success": True, "message": "签到成功"}})})
    third, _, notifier = run(tmp_path, fresh, storage=storage, notifier=notifier, now=later + timedelta(hours=1))
    assert third.record.result.status is KeepStatus.SUCCESS
    assert "flame_recovered" in [e["event"] for e in notifier.events]


def test_rate_limit_and_transport_errors_are_retryable_failures(tmp_path):
    store_cookie(tmp_path)
    routes = {("GET", TASKS_URL): tasks(), ("POST", SIGN_URL): respond(b"busy", 503, {"Retry-After": "7200"})}
    outcome, _, _ = run(tmp_path, FakeHttp(routes))
    assert outcome.record.result.metadata["reason"] == "sign_http_error"
    assert outcome.state.next_action_at >= NOW + timedelta(hours=2)
    del routes[("POST", SIGN_URL)]
    outcome, _, _ = run(tmp_path, FakeHttp(routes))
    assert outcome.record.result.metadata["reason"] == "sign_transport_error"


# --- view ----------------------------------------------------------------------------

def test_view_opens_the_new_data_page_then_claims_two_points(tmp_path):
    store_cookie(tmp_path)
    http = FakeHttp({("GET", TASKS_URL): [tasks(view=False), tasks(view=True)], ("GET", NEW_DATA_PAGE_URL): PAGE,
                     ("POST", VIEW_URL): respond({"code": 0, "data": {}})})
    outcome, _, _ = run(tmp_path, http, task="view")
    result = outcome.record.result
    assert result.status is KeepStatus.SUCCESS and result.reward.amount == "2"
    assert result.metadata["view_state"] == "completed_now" and result.metadata["task_code"] == "VIEW_NEW_DATA"
    assert http.calls() == [("GET", TASKS_URL), ("GET", NEW_DATA_PAGE_URL), ("POST", VIEW_URL), ("GET", TASKS_URL)]
    assert NEW_DATA_PAGE_URL == "https://tushare.pro/document/1?doc_id=9"
    assert "Origin" not in http.requests[1].headers
    assert_only_tushare(http)


def test_view_already_completed_skips_page_and_action(tmp_path):
    store_cookie(tmp_path)
    http = FakeHttp({("GET", TASKS_URL): tasks(view=True)})
    outcome, _, _ = run(tmp_path, http, task="view")
    assert outcome.record.result.metadata["view_state"] == "already_completed"
    assert http.calls() == [("GET", TASKS_URL)]


def test_view_without_verification_does_not_guess_an_unknown_reply(tmp_path):
    store_cookie(tmp_path)
    http = FakeHttp({("GET", TASKS_URL): tasks(), ("GET", NEW_DATA_PAGE_URL): PAGE,
                     ("POST", VIEW_URL): respond({"code": 0, "data": {}})})
    outcome, _, _ = run(tmp_path, http, config=flame(tmp_path, "view", verify_after_action=False))
    assert outcome.record.result.metadata["reason"] == "view_unrecognized_response"


def test_view_page_failure_stops_before_the_action(tmp_path):
    store_cookie(tmp_path)
    http = FakeHttp({("GET", TASKS_URL): tasks(), ("GET", NEW_DATA_PAGE_URL): respond(b"", 500)})
    outcome, _, _ = run(tmp_path, http, task="view")
    assert outcome.record.result.metadata["reason"] == "page_http_error"
    assert ("POST", VIEW_URL) not in http.calls()


# --- guess ---------------------------------------------------------------------------

@pytest.mark.parametrize("up, down, expected", [(120, 80, 1), (30, 31, 2), ("905", "17", 1), ("0", 3, 2)])
def test_guess_votes_with_the_majority(tmp_path, up, down, expected):
    store_cookie(tmp_path)
    after = respond({"code": 0, "data": {"period": {"status": 1}, "user_guess": {"vote": expected}}})
    http = FakeHttp({("GET", GUESS_CURRENT_URL): current(up=up, down=down), ("POST", GUESS_VOTE_URL): after})
    outcome, _, notifier = run(tmp_path, http, task="guess")
    result = outcome.record.result
    assert result.status is KeepStatus.SUCCESS and result.metadata["guess_state"] == "voted_now"
    assert result.metadata["vote"] == expected and result.metadata["vote_confirmed"] is True
    assert result.metadata["tie"] is False and result.metadata["tie_policy"] is None
    assert result.metadata["vote_direction"] == ("up" if expected == 1 else "down")
    assert result.reward.amount == "1" and result.metadata["trade_date"] == "20261020"
    vote = http.requests[-1]
    assert json.loads(vote.body) == {"vote": expected} and vote.headers["Content-Type"].startswith("application/json")
    assert_only_tushare(http)
    assert "SYNTHETIC" not in dumped(outcome, notifier)


@pytest.mark.parametrize("routes_current, state", [
    (current(vote=2), "already_voted"),
    (current(status=2), "not_open"),
    (current(status=3, vote=None), "not_open"),
])
def test_guess_skips_when_already_voted_or_not_open(tmp_path, routes_current, state):
    store_cookie(tmp_path)
    http = FakeHttp({("GET", GUESS_CURRENT_URL): routes_current})
    outcome, _, _ = run(tmp_path, http, task="guess")
    result = outcome.record.result
    assert result.status is KeepStatus.SUCCESS and result.metadata["guess_state"] == state
    assert result.reward is None and http.calls() == [("GET", GUESS_CURRENT_URL)]


@pytest.mark.parametrize("picked", [1, 2])
def test_guess_tie_always_votes_a_randomly_picked_side(tmp_path, picked, caplog):
    store_cookie(tmp_path)
    offered = []

    def choose(sides):
        offered.append(tuple(sides))
        return picked

    after = respond({"code": 0, "data": {"user_guess": {"vote": picked}}})
    http = FakeHttp({("GET", GUESS_CURRENT_URL): current(up=40, down="40"), ("POST", GUESS_VOTE_URL): after})
    with caplog.at_level(logging.INFO, logger="keepflame.flames.tushare.keeper"):
        outcome, _, notifier = run(tmp_path, http, task="guess", choose=choose)
    result = outcome.record.result
    assert offered == [(1, 2)]
    assert result.status is KeepStatus.SUCCESS and result.metadata["guess_state"] == "voted_now"
    assert json.loads(http.requests[-1].body) == {"vote": picked}
    assert result.metadata["tie"] is True and result.metadata["tie_policy"] == "random"
    assert result.metadata["vote_direction"] == ("up" if picked == 1 else "down")
    assert "on a tie" in result.message and result.reward.amount == "1"
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("tushare_guess_vote ")]
    logged = json.loads(line.split(" ", 1)[1])
    assert logged == {"flame_id": "tushare-guess", "trade_date": "20261020", "vote": picked,
                      "direction": "up" if picked == 1 else "down", "up_votes": 40, "down_votes": 40,
                      "tie": True, "tie_policy": "random"}
    assert "SYNTHETIC" not in line + dumped(outcome, notifier)


def test_guess_tie_seeded_rng_reaches_both_sides(tmp_path):
    store_cookie(tmp_path)
    rng = random.Random(20261019)
    seen = set()
    for _ in range(32):
        http = FakeHttp({("GET", GUESS_CURRENT_URL): current(up=0, down=0),
                         ("POST", GUESS_VOTE_URL): respond({"code": 0, "data": {}})})
        outcome, _, _ = run(tmp_path, http, task="guess", choose=rng.choice)
        assert outcome.record.result.status is KeepStatus.SUCCESS
        seen.add(json.loads(http.requests[-1].body)["vote"])
    assert seen == {1, 2}


def test_guess_majority_ignores_the_tie_chooser_and_pinned_side_overrides_random(tmp_path, caplog):
    store_cookie(tmp_path)
    http = FakeHttp({("GET", GUESS_CURRENT_URL): current(up=10, down=11),
                     ("POST", GUESS_VOTE_URL): respond({"code": 0, "data": {}})})
    with caplog.at_level(logging.INFO, logger="keepflame.flames.tushare.keeper"):
        outcome, _, _ = run(tmp_path, http, task="guess")  # chooser fails the test if consulted
    assert json.loads(http.requests[-1].body) == {"vote": 2} and outcome.record.result.metadata["tie"] is False
    assert '"tie": false' in caplog.text and '"direction": "down"' in caplog.text
    http = FakeHttp({("GET", GUESS_CURRENT_URL): current(up=0, down=0),
                     ("POST", GUESS_VOTE_URL): respond({"code": 0, "data": {}})})
    outcome, _, _ = run(tmp_path, http, config=flame(tmp_path, "guess", guess_tie="down"))
    assert outcome.record.result.status is KeepStatus.SUCCESS
    assert json.loads(http.requests[-1].body) == {"vote": 2}
    assert outcome.record.result.metadata["tie_policy"] == "down"
    assert outcome.record.result.metadata["vote_confirmed"] is None


@pytest.mark.parametrize("routes, reason", [
    ({("GET", GUESS_CURRENT_URL): respond({"code": 0, "data": {}})}, "guess_period_missing"),
    ({("GET", GUESS_CURRENT_URL): current(up=None)}, "guess_votes_unreadable"),
    ({("GET", GUESS_CURRENT_URL): current(),
      ("POST", GUESS_VOTE_URL): respond({"code": 0, "data": {"user_guess": {"vote": 2}}})}, "guess_vote_mismatch"),
    ({("GET", GUESS_CURRENT_URL): current(),
      ("POST", GUESS_VOTE_URL): respond({"code": 40001, "message": "竞猜已结束"})}, "vote_rejected"),
])
def test_guess_failures(tmp_path, routes, reason):
    store_cookie(tmp_path)
    outcome, _, _ = run(tmp_path, FakeHttp(routes), task="guess")
    assert outcome.record.result.status is KeepStatus.FAILED
    assert outcome.record.result.metadata["reason"] == reason


def test_majority_vote_rules():
    assert majority_vote(3, 1) == (1, False) and majority_vote(1, 3) == (2, False)
    assert majority_vote(5, 5, choose=lambda sides: sides[-1]) == (2, True)
    assert majority_vote(5, 5, choose=lambda sides: sides[0]) == (1, True)
    assert majority_vote(5, 5, "up") == (1, True) and majority_vote("2", "2", "down") == (2, True)
    assert majority_vote(5, 5)[0] in (1, 2)  # default: real randomness, still always a vote
    with pytest.raises(ValueError):
        majority_vote(5, 5, choose=lambda sides: 3)
    for bad in ((None, 1), (True, 1), (-1, 2), ("x", 1)):
        with pytest.raises(ValueError):
            majority_vote(*bad)


# --- settings ------------------------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"cookie_file": "relative.cookie"}, {"task": "checkin"}, {"task": None},
    {"guess_tie": "up"},  # only valid for task = "guess"
    {"task_code": "daily sign"}, {"timeout_seconds": 1}, {"verify_after_action": "yes"},
])
def test_invalid_settings_pause_instead_of_guessing(tmp_path, overrides):
    overrides = dict(overrides)
    config = flame(tmp_path, overrides.pop("task", "sign"), **overrides)
    if config.settings["task"] is None:
        config.settings.pop("task")
    outcome, _, _ = run(tmp_path, FakeHttp({}), config=config)
    assert outcome.record.result.status is KeepStatus.FAILED
    assert outcome.record.result.metadata["automation_paused"] is True


# --- CLI -----------------------------------------------------------------------------

CONFIG = """
[storage]
path = "{tmp}/state.sqlite3"
{flames}
"""
FLAME = """
[[flames]]
id = "tushare-{task}"
keeper = "tushare"
account_id = "primary"
enabled = true

[flames.schedule]
timezone = "Asia/Shanghai"
first_window_start = "09:00"
first_window_end = "10:30"
recheck_minutes = 60
reminder_at = "20:00"
stop_at = "23:00"

[flames.settings]
task = "{task}"
cookie_file = "{tmp}/{file}"
"""


def config_file(tmp_path, files=("tushare.cookie",) * 3):
    flames = "".join(FLAME.format(task=task, tmp=tmp_path, file=file) for task, file in zip(("sign", "view", "guess"), files))
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(tmp=tmp_path, flames=flames))
    return path


class Pipe(io.StringIO):
    def isatty(self):
        return False


def test_import_cookie_once_serves_all_three_flames_and_prints_no_values(tmp_path):
    out = io.StringIO()
    args = SimpleNamespace(config=config_file(tmp_path), flame=None, from_clipboard=False)
    assert import_cookie(args, stdin=Pipe(COOKIE + "\n"), out=out) == 0
    record = json.loads(out.getvalue())
    assert record["flame_ids"] == ["tushare-sign", "tushare-view", "tushare-guess"]
    assert record["cookie_names"] == ["uid", "session", "csrftoken"] and "SYNTHETIC" not in out.getvalue()
    path = tmp_path / "tushare.cookie"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_file(path.read_text()) == SessionCookie(COOKIE)


def test_import_cookie_needs_flame_when_cookie_files_differ(tmp_path):
    path = config_file(tmp_path, files=("a.cookie", "b.cookie", "b.cookie"))
    with pytest.raises(ConfigError):
        import_cookie(SimpleNamespace(config=path, flame=None, from_clipboard=False), stdin=Pipe(COOKIE), out=io.StringIO())
    out = io.StringIO()
    assert import_cookie(SimpleNamespace(config=path, flame="tushare-view", from_clipboard=False),
                         stdin=Pipe(COOKIE), out=out) == 0
    assert json.loads(out.getvalue())["flame_ids"] == ["tushare-view", "tushare-guess"]
    assert (tmp_path / "b.cookie").exists() and not (tmp_path / "a.cookie").exists()


def test_import_cookie_cli_rejects_bad_input_without_echoing_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", Pipe("session=SYNTHETIC-SECRET-VALUE\nmore=1"))
    code = cli_main(["tushare", "import-cookie", "--config", str(config_file(tmp_path))])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code != 0 and "CookieError" in output and "SYNTHETIC" not in output
    assert not (tmp_path / "tushare.cookie").exists()


def test_example_config_loads_three_disabled_flames_sharing_one_cookie():
    path = Path(__file__).resolve().parents[1] / "config" / "keepflame.tushare.example.toml"
    config = load_config(path)
    by_id = {f.id: f for f in config.flames}
    assert set(by_id) == {"tushare-sign", "tushare-view", "tushare-guess"}
    assert all(f.keeper == "tushare" and f.enabled is False for f in config.flames)
    assert len({f.settings["cookie_file"] for f in config.flames}) == 1
    assert [_Settings(by_id[i].settings).task for i in ("tushare-sign", "tushare-view", "tushare-guess")] == ["sign", "view", "guess"]
    assert by_id["tushare-view"].schedule.after_flame == by_id["tushare-guess"].schedule.after_flame == "tushare-sign"
    guess = by_id["tushare-guess"].schedule
    latest = datetime.combine(datetime(2026, 10, 20), guess.first_window_end) + timedelta(
        minutes=guess.min_gap_minutes + sum(guess.retry_minutes))
    assert latest.time() < time(13)  # retries finish before voting closes
    assert config.slack is None or config.slack.enabled is False


def test_keeper_is_registered_as_an_entry_point():
    from keepflame.core.plugins import EntryPointKeeperRegistry

    assert isinstance(EntryPointKeeperRegistry().resolve("tushare"), TushareKeeper)


QUOTED = 'uid="SYNTHETIC|quoted:value=="; sid=plain'


def test_rfc6265_quoted_cookie_value_is_accepted_and_kept_verbatim(tmp_path):
    assert parse_import(QUOTED).header == QUOTED
    assert parse_import("Cookie: " + QUOTED + ";").header == QUOTED
    assert parse_import('uid=""').header == 'uid=""'
    exported = json.dumps([{"name": "uid", "value": '"SYNTHETIC|quoted:value=="', "domain": ".tushare.pro"},
                           {"name": "sid", "value": "plain", "domain": "tushare.pro"}])
    assert parse_import(exported).header == QUOTED
    path = tmp_path / "tushare.cookie"
    write_file(path, parse_import(QUOTED))
    assert load_file(FileSecretResolver().read_text(str(path))).header == QUOTED


def test_quoted_cookie_value_import_and_send_keep_the_quotes(tmp_path):
    store_cookie(tmp_path, header=QUOTED)
    http = FakeHttp({("GET", TASKS_URL): tasks(sign=True)})
    outcome, _, _ = run(tmp_path, http)
    assert outcome.record.result.status is KeepStatus.SUCCESS
    assert http.requests[0].headers["Cookie"] == QUOTED
    out = io.StringIO()
    args = SimpleNamespace(config=config_file(tmp_path), flame=None, from_clipboard=False)
    assert import_cookie(args, stdin=Pipe(QUOTED + "\n"), out=out) == 0
    assert load_file((tmp_path / "tushare.cookie").read_text()).header == QUOTED
    assert "SYNTHETIC" not in out.getvalue()


@pytest.mark.parametrize("bad", ['uid="abc', 'uid=abc"', 'uid="a"b"', 'uid=a"b', 'uid="a b"', 'uid="a\\b"',
                                 'uid="a,b"', 'uid=""x""', 'uid="'])
def test_unbalanced_or_embedded_quotes_are_still_rejected(bad):
    with pytest.raises(CookieError):
        parse_import(bad)
    with pytest.raises(CookieError):
        parse_import(json.dumps([{"name": "uid", "value": bad.split("=", 1)[1], "domain": "tushare.pro"}]))
