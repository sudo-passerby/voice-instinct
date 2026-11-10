import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from guard import ORB_ORIGIN, refuse_other_origins


def client(monkeypatch, playground=False):
    if playground:
        monkeypatch.setenv("VI_ALLOW_PLAYGROUND", "1")
    else:
        monkeypatch.delenv("VI_ALLOW_PLAYGROUND", raising=False)
    app = FastAPI()

    @app.post("/api/offer")
    async def offer():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_text("hi")
        await socket.close()

    refuse_other_origins(app, port=7860)
    return TestClient(app)


def test_a_web_page_elsewhere_cannot_open_a_voice_session(monkeypatch):
    # The errand browser renders untrusted pages; one could otherwise connect and answer its own question.
    c = client(monkeypatch)
    assert c.post("/api/offer", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/offer", headers={"Origin": "http://localhost:4000"}).status_code == 403
    # Any page can make an opaque origin (a sandboxed iframe, a data: page): "null" proves nothing.
    assert c.post("/api/offer", headers={"Origin": "null"}).status_code == 403


@pytest.mark.parametrize("origin", ["http://localhost:7860", "http://127.0.0.1:7860"])
def test_localhost_origins_are_refused_unless_the_playground_is_switched_on(monkeypatch, origin):
    # Any local page (one the errand browser was sent to, a dev server) can claim a localhost origin.
    assert client(monkeypatch).post("/api/offer", headers={"Origin": origin}).status_code == 403
    assert client(monkeypatch, playground=True).post("/api/offer", headers={"Origin": origin}).status_code == 200


def test_the_orb_and_non_browser_callers_still_connect(monkeypatch):
    c = client(monkeypatch)
    assert c.post("/api/offer", headers={"Origin": ORB_ORIGIN}).status_code == 200  # the orb (main.js sets it)
    assert c.post("/api/offer").status_code == 200  # no Origin: hooks from the claude CLI


def test_websockets_are_guarded_too(monkeypatch):
    c = client(monkeypatch)
    for origin in ("https://evil.example", "http://localhost:7860"):
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={"Origin": origin}) as s:
                s.receive_text()
    with c.websocket_connect("/ws", headers={"Origin": ORB_ORIGIN}) as s:
        assert s.receive_text() == "hi"
