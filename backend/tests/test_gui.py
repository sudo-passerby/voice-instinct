from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import Config
from gui import make_gui_router


def test_gui_config_serves_what_the_orb_needs_from_config():
    app = FastAPI()
    app.include_router(make_gui_router(Config(name="Juno", gui_size=200, gui_position="top-left", awake_timeout=12)))
    r = TestClient(app).get("/gui-config")
    assert r.status_code == 200
    # all the orb reads (main.js: placement, and the name in the tray tooltip)
    assert r.json() == {"size": 200, "position": "top-left", "name": "Juno"}


def feed(script):
    """Run `script(registry, feed)` on a loop with an errand feed listening; returns what the orb was sent."""
    import asyncio

    from gui import ErrandFeed
    from registry import Registry

    sent = []

    async def send(msg):
        sent.append(msg)

    async def run():
        reg = Registry()
        f = ErrandFeed(reg, send)
        await script(reg, f)
        await asyncio.sleep(0)  # the feed sends from the loop, after the change that caused it
        await asyncio.sleep(0)
        return reg, f

    asyncio.run(run())
    return sent


def test_errand_feed_tells_the_orb_where_things_stand_when_it_connects():
    async def script(reg, f):
        reg.upsert_session("e1", origin="delegated", state="running")
        f.start()
        await f.push(force=True)
        await f.push(force=True)  # a reconnecting orb has forgotten: told again though nothing changed

    assert feed(script) == [{"type": "errands", "running": 1, "asking": False}] * 2


def test_errand_feed_sends_each_change_once_and_in_order():
    async def script(reg, f):
        f.start()
        await f.push(force=True)
        reg.upsert_session("e1", origin="delegated", state="running")
        reg.upsert_session("e1", cwd="/tmp/x")  # a change that is not the orb's business: nothing sent
        q = reg.add_question("e1", "permission", "run a command")
        reg.resolve_question(q, "answered", "allow")
        reg.set_state("e1", "idle")

    assert [(m["running"], m["asking"]) for m in feed(script)] == [(0, False), (1, False), (1, True), (1, False), (0, False)]


def test_errand_feed_stops_listening_and_survives_a_send_that_fails():
    import asyncio

    from gui import ErrandFeed
    from registry import Registry

    async def run():
        reg = Registry()
        calls = []

        async def send(msg):
            calls.append(msg)
            raise RuntimeError("the session is gone")

        f = ErrandFeed(reg, send)
        f.start()
        reg.upsert_session("e1", origin="delegated", state="running")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        f.stop()
        reg.set_state("e1", "idle")
        await asyncio.sleep(0)
        return calls, reg.listeners

    calls, listeners = asyncio.run(run())
    assert len(calls) == 1 and listeners == []


def test_a_registry_change_outside_the_event_loop_does_not_raise():
    from gui import ErrandFeed
    from registry import Registry

    reg = Registry()
    ErrandFeed(reg, None).start()
    reg.upsert_session("e1", origin="delegated", state="running")  # startup code, no loop: nothing to send to yet


def test_errand_feed_started_twice_still_sends_each_change_once():
    async def script(reg, f):
        f.start()
        f.start()
        reg.upsert_session("e1", origin="delegated", state="running")
        assert len(reg.listeners) == 1

    assert len(feed(script)) == 1
