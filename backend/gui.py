"""What the desktop orb is told: the route it reads at start-up, so config.toml stays the single source of
truth, and the errands it shows as bodies circling the star."""

import asyncio

from fastapi import APIRouter
from loguru import logger


def make_gui_router(cfg) -> APIRouter:
    router = APIRouter()

    @router.get("/gui-config")
    async def gui_config():
        return {"size": cfg.gui_size, "position": cfg.gui_position, "name": cfg.name}

    return router


class ErrandFeed:
    """Tells one session's orb how many errands are running and whether a question waits, as
    {"type": "errands", "running": n, "asking": bool}: when the orb connects (push(force=True)) and after every
    registry change that alters it. `send` is the session's send_gui."""

    def __init__(self, registry, send):
        self.reg, self.send = registry, send
        self._last = None
        self._bg = set()

    def start(self):
        if self._changed not in self.reg.listeners:
            self.reg.listeners.append(self._changed)

    def stop(self):
        if self._changed in self.reg.listeners:
            self.reg.listeners.remove(self._changed)

    def _changed(self):
        status = self.reg.errand_status()  # read now, so each change is sent as it was, in order
        if status == self._last:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._send(status))
        except RuntimeError:  # no loop (start-up code): the orb is told when it connects
            return
        self._last = status
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def push(self, force=False):
        status = self.reg.errand_status()
        if status != self._last or force:
            self._last = status
            await self._send(status)

    async def _send(self, status):
        try:
            await self.send(status)
        except Exception:  # a session on its way out; the orb is told again when it reconnects
            logger.warning("could not tell the orb about errands")
