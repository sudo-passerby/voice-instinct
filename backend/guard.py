"""Only this app's own orb may talk to the backend from a browser, over HTTP or a WebSocket.

The runner allows every origin (CORS "*"), so any web page open on this machine, including one the
errand browser visits, could otherwise open a voice session and answer its own permission question.
The orb's main process stamps its requests with ORB_ORIGIN (frontend/main.js). Page scripts cannot
set Origin and ".invalid" never resolves, so no web page can have it. ("null" is not enough: any page
can make an opaque origin with a sandboxed iframe.) A request with no Origin at all is let through only on
/hook/ (the claude CLI's hooks send none, and their URL holds a secret token); on every other route any local
program could otherwise leave it out. main.js stamps the orb's own /gui-config fetch by hand.

The runner's /client playground is served from this server, so its origin is localhost: any local
page could claim that too, so it is refused unless VI_ALLOW_PLAYGROUND=1 is set when the backend
starts (for testing in a browser). It also lets requests without an Origin through, since a browser sends none
when it loads the /client page and on its same-origin GETs.
"""

import os

from starlette.responses import PlainTextResponse

ORB_ORIGIN = "http://orb.invalid"


class _OriginGuard:
    """Pure ASGI, so WebSocket handshakes are checked too (an @app.middleware("http") never sees them)."""

    def __init__(self, app, allowed, no_origin_anywhere=False):
        self.app, self.allowed, self.no_origin_anywhere = app, allowed, no_origin_anywhere

    def _ok(self, scope, origin):
        if origin is None:
            return self.no_origin_anywhere or (scope["type"] == "http" and scope.get("path", "").startswith("/hook/"))
        return origin.decode("latin-1") in self.allowed

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            origin = dict(scope.get("headers") or []).get(b"origin")
            if not self._ok(scope, origin):
                if scope["type"] == "websocket":  # closing before accept refuses the handshake (HTTP 403)
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await PlainTextResponse("forbidden origin", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def refuse_other_origins(app, port):
    allowed, playground = {ORB_ORIGIN}, os.environ.get("VI_ALLOW_PLAYGROUND") == "1"
    if playground:
        allowed |= {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    app.add_middleware(_OriginGuard, allowed=allowed, no_origin_anywhere=playground)
