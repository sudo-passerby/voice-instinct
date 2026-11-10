import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from reminders import next_due, spoken_time

NY, IN = ZoneInfo("America/New_York"), ZoneInfo("Asia/Kolkata")


def ts(y, mo, d, h, mi, tz):
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def test_a_time_a_dst_change_skipped_comes_back_the_next_day():
    # 2:30 does not exist on 8 Mar 2026 in New York (clocks go 2:00 -> 3:00): that day it fires at 3:30, and
    # every day after at 2:30 again, not 3:30 for good.
    now = [ts(2026, 3, 7, 2, 30, NY)]
    reg = Registry(clock=lambda: now[0])
    r = Reminders(reg, FakeRelay(), None, locked=lambda: False, clock=lambda: now[0], tz=NY)
    reg.add_reminder("pills", now[0], "daily")
    seen = []
    for _ in range(3):
        tick(r)
        now[0] = reg.pending_reminders()[0]["due"]
        seen.append(datetime.fromtimestamp(now[0], NY).strftime("%d %H:%M"))
    assert seen == ["08 03:30", "09 02:30", "10 02:30"]


def test_daily_keeps_the_wall_clock_time_across_dst():
    due = ts(2026, 10, 31, 9, 0, NY)  # DST ends at 2am on 1 Nov in New York: a 25-hour day
    nxt = next_due(due, "daily", due, NY)
    assert datetime.fromtimestamp(nxt, NY).hour == 9 and nxt - due == 25 * 3600


def test_weekly_is_seven_days_later():
    due = ts(2026, 10, 16, 15, 0, IN)
    assert next_due(due, "weekly", due, IN) == ts(2026, 10, 23, 15, 0, IN)


def test_after_a_long_sleep_the_next_time_is_in_the_future():
    due = ts(2026, 10, 10, 9, 0, IN)
    now = ts(2026, 10, 16, 12, 0, IN)
    assert next_due(due, "daily", now, IN) == ts(2026, 10, 17, 9, 0, IN)


def test_spoken_time_is_relative_to_today():
    now = ts(2026, 10, 16, 1, 0, IN)
    assert spoken_time(ts(2026, 10, 16, 15, 0, IN), IN, now) == "3:00 pm"
    assert spoken_time(ts(2026, 10, 17, 9, 5, IN), IN, now) == "tomorrow at 9:05 am"
    assert spoken_time(ts(2026, 10, 20, 15, 0, IN), IN, now) == "Tuesday at 3:00 pm"
    assert spoken_time(ts(2026, 11, 20, 15, 0, IN), IN, now) == "20 Nov at 3:00 pm"


import asyncio

from registry import Registry
from reminders import Reminders


class FakeRelay:
    def __init__(self, attached=True):
        self.attached, self.said = attached, []

    def say(self, text, on_spoken=None, on_dropped=None, wanted=None):
        self.said.append(text)
        self.pending = (on_spoken, on_dropped)
        if not getattr(self, "hold", False) and on_spoken:
            on_spoken()  # spoken at once, as a quiet relay would


class FakeTelegram:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def send_message(self, text):
        self.sent.append(text)
        return self.ok


def make(now, attached=True, locked=False, tg=True, ok=True):
    reg = Registry(clock=lambda: now[0])
    relay, telegram = FakeRelay(attached), (FakeTelegram(ok) if tg else None)
    r = Reminders(reg, relay, telegram, locked=lambda: locked, clock=lambda: now[0], tz=IN)
    return reg, relay, telegram, r


def tick(r):
    asyncio.run(r.tick())


def test_present_it_is_spoken_once():
    now = [ts(2026, 10, 16, 15, 0, IN)]
    reg, relay, tg, r = make(now)
    reg.add_reminder("call mom", now[0], "none")
    tick(r)
    tick(r)
    assert len(relay.said) == 1 and "call mom" in relay.said[0] and tg.sent == []


def test_away_it_goes_to_telegram():
    now = [ts(2026, 10, 16, 15, 0, IN)]
    reg, relay, tg, r = make(now, locked=True)
    reg.add_reminder("call mom", now[0], "none")
    tick(r)
    assert relay.said == [] and tg.sent == ["⏰ call mom (3:00 pm)"]


def test_away_without_telegram_it_waits_then_is_spoken():
    now = [ts(2026, 10, 16, 15, 0, IN)]
    reg, relay, _, r = make(now, attached=False, tg=False)
    reg.add_reminder("call mom", now[0], "none")
    tick(r)
    assert reg.pending_reminders()  # put back, not lost
    relay.attached = True
    now[0] += 600
    tick(r)
    assert len(relay.said) == 1 and "was due at 3:00 pm" in relay.said[0]


def test_a_telegram_failure_is_retried():
    now = [ts(2026, 10, 16, 15, 0, IN)]
    reg, _, tg, r = make(now, attached=False, ok=False)
    reg.add_reminder("call mom", now[0], "none")
    tick(r)
    tg.ok = True
    now[0] += 15
    tick(r)
    assert len(tg.sent) == 2 and reg.pending_reminders() == []


def test_telegram_failures_back_off_up_to_ten_minutes():
    now = [ts(2026, 10, 16, 15, 0, IN)]
    reg, _, tg, r = make(now, attached=False, ok=False)
    reg.add_reminder("call mom", now[0], "none")
    tries = []
    for _ in range(4 * 60 * 2):  # two hours of 15 s ticks
        n = len(tg.sent)
        tick(r)
        if len(tg.sent) > n:
            tries.append(now[0])
        now[0] += 15
    gaps = [b - a for a, b in zip(tries, tries[1:])]
    assert gaps[:6] == [15, 30, 60, 120, 240, 480] and set(gaps[6:]) == {600}
    assert len(reg.pending_reminders()) == 1  # still kept


def test_a_reminder_telegram_refuses_is_not_retried_there_but_said_when_the_user_is_back():
    now = [ts(2026, 10, 16, 15, 0, IN)]
    reg, relay, tg, r = make(now, attached=False, ok=None)  # None: a 4xx such as a wrong chat id
    reg.add_reminder("call mom", now[0], "none")
    for _ in range(10):
        tick(r)
        now[0] += 600
    assert len(tg.sent) == 1 and len(reg.pending_reminders()) == 1
    relay.attached = True
    tick(r)
    assert len(relay.said) == 1 and reg.pending_reminders() == []


def test_an_undeliverable_daily_reminder_does_not_multiply():
    now = [ts(2026, 10, 16, 9, 0, IN)]
    reg, relay, _, r = make(now, attached=False, tg=False)
    reg.add_reminder("water", now[0], "daily")
    for _ in range(3):
        tick(r)
    assert len(reg.pending_reminders()) == 1


def test_a_daily_reminder_after_a_long_sleep_fires_once_then_moves_on():
    now = [ts(2026, 10, 16, 12, 0, IN)]
    reg, relay, _, r = make(now)
    reg.add_reminder("water", ts(2026, 10, 10, 9, 0, IN), "daily")
    tick(r)
    assert len(relay.said) == 1
    assert [x["due"] for x in reg.pending_reminders()] == [ts(2026, 10, 17, 9, 0, IN)]


def test_a_cancelled_reminder_never_fires():
    now = [ts(2026, 10, 16, 15, 0, IN)]
    reg, relay, tg, r = make(now)
    reg.cancel_reminder(reg.add_reminder("x", now[0], "none"))
    tick(r)
    assert relay.said == [] and tg.sent == []


from types import SimpleNamespace

from reminders import make_reminder_tools


def call(tool, **kw):
    got = []

    async def cb(result, **_):
        got.append(result)
    asyncio.run(tool(SimpleNamespace(result_callback=cb), **kw))
    return got[0]


def tools(now, tg=True):
    reg = Registry(clock=lambda: now)
    t = FakeTelegram() if tg else None
    return reg, t, {f.__name__: f for f in make_reminder_tools(reg, t, clock=lambda: now, tz=IN)}


def test_set_list_cancel():
    now = ts(2026, 10, 16, 1, 0, IN)
    reg, _, t = tools(now)
    r = call(t["set_reminder"], what="call mom", at="2026-10-16T15:00")
    assert r["status"] == "set" and r["when"] == "3:00 pm"
    listed = call(t["list_reminders"])["reminders"]
    assert listed[0]["what"] == "call mom" and listed[0]["id"] == r["id"]
    assert call(t["cancel_reminder"], reminder_id=r["id"])["status"] == "cancelled"
    assert call(t["list_reminders"])["reminders"] == []


@pytest.mark.parametrize("kw", [
    {"what": "x", "at": "2026-10-16T00:30"},           # one-off in the past
    {"what": "x", "at": "tomorrow-ish"},               # not a time
    {"what": "x", "at": "2026-10-16T15:00", "repeat": "hourly"},
    {"what": " ", "at": "2026-10-16T15:00"},
])
def test_bad_reminders_are_refused(kw):
    _, _, t = tools(ts(2026, 10, 16, 1, 0, IN))
    assert call(t["set_reminder"], **kw)["status"] == "refused"


def test_a_daily_reminder_may_start_in_the_past_and_rolls_forward():
    now = ts(2026, 10, 16, 10, 0, IN)
    _, _, t = tools(now)
    r = call(t["set_reminder"], what="water", at="2026-10-16T09:00", repeat="daily")
    assert r["status"] == "set" and r["when"] == "tomorrow at 9:00 am"


def test_send_telegram_only_with_config():
    now = ts(2026, 10, 16, 1, 0, IN)
    _, tg, t = tools(now)
    assert call(t["send_telegram"], text="hi")["status"] == "sent" and tg.sent == ["hi"]
    _, _, t2 = tools(now, tg=False)
    assert call(t2["send_telegram"], text="hi")["status"] == "not_configured"


def test_the_tools_build_valid_schemas():
    from pipecat.processors.aggregators.llm_context import LLMContext
    names = {s.name for s in LLMContext._normalize_and_validate_tools(
        make_reminder_tools(Registry(), None)).standard_tools}
    assert names == {"set_reminder", "list_reminders", "cancel_reminder", "send_telegram"}



def test_a_spoken_reminder_dropped_before_it_was_said_is_retried():
    now = [ts(2026, 10, 16, 15, 0, IN)]
    reg, relay, tg, r = make(now)
    relay.hold = True  # queued for a quiet moment that never comes
    reg.add_reminder("water", now[0], "daily")
    tick(r)
    assert reg.pending_reminders() == []  # in flight, and no next occurrence yet
    relay.pending[1]()  # the session detached
    relay.attached = False
    tick(r)
    assert tg.sent and len(reg.pending_reminders()) == 1  # sent by Telegram, then the next daily one


def test_a_restart_puts_reminders_in_flight_back():
    now = [ts(2026, 10, 16, 15, 0, IN)]
    reg, relay, tg, r = make(now)
    relay.hold = True
    reg.add_reminder("water", now[0], "none")
    tick(r)
    r2 = Reminders(reg, FakeRelay(attached=False), tg, locked=lambda: False, clock=lambda: now[0], tz=IN)
    tick(r2)  # a fresh scheduler, as after a bot restart
    assert tg.sent == ["⏰ water (3:00 pm)"]


def test_a_daily_reminder_moves_on_in_its_own_row():
    now = [ts(2026, 10, 16, 9, 0, IN)]
    reg, relay, _, r = make(now)
    rid = reg.add_reminder("water", now[0], "daily")
    tick(r)
    assert [(x["id"], x["due"]) for x in reg.pending_reminders()] == [(rid, ts(2026, 10, 17, 9, 0, IN))]
    assert reg.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1


# With the real Relay (M-12): a spoken reminder counts as delivered only once the turn that says it has ended.
from relay import Relay


class FakeTask:
    def __init__(self):
        self.frames = []

    async def queue_frames(self, frames):
        self.frames += frames


def with_relay(now, tg=None):
    reg = Registry(clock=lambda: now[0])
    relay, task = Relay(reg), FakeTask()
    relay.attach(task, None)
    return reg, relay, task, Reminders(reg, relay, tg, locked=lambda: False, clock=lambda: now[0], tz=IN)


def test_a_reminder_said_in_an_uninterrupted_turn_is_delivered_then_and_not_before():
    async def go():
        now = [ts(2026, 10, 16, 9, 0, IN)]
        reg, relay, task, r = with_relay(now)
        rid = reg.add_reminder("water", now[0], "daily")
        await r.tick()
        await asyncio.sleep(0)
        handed = len(task.frames), reg.get_reminder(rid)["status"]
        relay.bot_turn(True)  # on_assistant_turn_started
        speaking = reg.get_reminder(rid)["status"]
        relay.bot_turn(False, spoken=True)  # on_assistant_turn_stopped, not interrupted
        return handed, speaking, reg.get_reminder(rid)

    handed, speaking, row = asyncio.run(go())
    assert handed == (1, "fired") and speaking == "fired"
    assert row["status"] == "pending" and row["due"] == ts(2026, 10, 17, 9, 0, IN)  # said: the next day's


def test_an_interrupted_reminder_is_put_back_and_said_again():
    async def go():
        now = [ts(2026, 10, 16, 15, 0, IN)]
        reg, relay, task, r = with_relay(now)
        rid = reg.add_reminder("call mom", now[0], "none")
        await r.tick()
        await asyncio.sleep(0)
        relay.bot_turn(True)
        relay.user_turn(True)  # barge-in
        relay.bot_turn(False, spoken=False)  # Pipecat: interrupted
        back = reg.get_reminder(rid)["status"]
        relay.user_turn(False)
        await r.tick()
        await asyncio.sleep(0)
        return back, len(task.frames)

    assert asyncio.run(go()) == ("pending", 2)


def test_a_reminder_whose_turn_never_started_is_put_back_when_the_user_speaks_first():
    async def go():
        now = [ts(2026, 10, 16, 15, 0, IN)]
        reg, relay, task, r = with_relay(now)
        rid = reg.add_reminder("call mom", now[0], "none")
        await r.tick()
        await asyncio.sleep(0)
        relay.user_turn(True)  # the reply to the note is cancelled before it starts
        relay.user_turn(False)
        relay.bot_turn(True)  # the reply to what the user said
        relay.bot_turn(False, spoken=True)
        return reg.get_reminder(rid)["status"]

    assert asyncio.run(go()) == "pending"


def test_a_turn_that_says_nothing_does_not_deliver_it():
    async def go():
        now = [ts(2026, 10, 16, 15, 0, IN)]
        reg, relay, task, r = with_relay(now)
        rid = reg.add_reminder("call mom", now[0], "none")
        await r.tick()
        await asyncio.sleep(0)
        relay.bot_turn(True)
        relay.bot_turn(False, spoken=False)  # zero tokens
        return reg.get_reminder(rid)["status"]

    assert asyncio.run(go()) == "pending"


def test_a_waiting_reminder_is_listed_and_can_be_cancelled_before_it_is_said():
    async def go():
        now = [ts(2026, 10, 16, 9, 0, IN)]
        reg, relay, task, r = with_relay(now)
        rid = reg.add_reminder("water", now[0], "daily")
        relay.bot_turn(True)  # Nova is mid-reply: the reminder waits, fired
        await r.tick()
        t = {f.__name__: f for f in make_reminder_tools(reg, None, clock=lambda: now[0], tz=IN)}
        listed = [x["id"] for x in call_async(t["list_reminders"])["reminders"]]
        cancelled = call_async(t["cancel_reminder"], reminder_id=rid)["status"]
        relay.bot_turn(False)  # the quiet moment comes
        await asyncio.sleep(0.01)
        return listed, cancelled, task.frames, reg.open_reminders()

    listed, cancelled, frames, left = asyncio.run(go())
    assert listed == [1] and cancelled == "cancelled"
    assert frames == [] and left == []  # never said, and no next day's


def test_a_reminder_cancelled_while_it_is_being_said_does_not_repeat():
    async def go():
        now = [ts(2026, 10, 16, 9, 0, IN)]
        reg, relay, task, r = with_relay(now)
        rid = reg.add_reminder("water", now[0], "daily")
        await r.tick()
        await asyncio.sleep(0)
        relay.bot_turn(True)
        reg.cancel_reminder(rid)
        relay.bot_turn(False, spoken=True)
        return reg.open_reminders(), reg.get_reminder(rid)["status"]

    assert asyncio.run(go()) == ([], "cancelled")


def call_async(tool, **kw):
    """call() for a test already inside an event loop: the reminder tools never await anything but the callback."""
    got = []

    async def cb(result, **_):
        got.append(result)
    coro = tool(SimpleNamespace(result_callback=cb), **kw)
    try:
        coro.send(None)
    except StopIteration:
        pass
    return got[0]

