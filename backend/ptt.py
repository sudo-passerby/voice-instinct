"""Push-to-talk (`voice-instinct talk`, e.g. on Win+P): the first press wakes Nova and opens the turn
(interrupting Nova if it is answering), the turn stays open through any pause, and the second press ends
it. In between, Smart Turn's silence check is paused.

The second press asks the STT for the end (`end_turn` in services.py): it transcribes what is still
buffered and only then proposes the stop, so the last words are in the turn. The stop strategy below
resolves that documented ProposedUserStoppedSpeakingFrame.
"""

import asyncio
import time

from loguru import logger

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (ProposedUserStartedSpeakingFrame, ProposedUserStoppedSpeakingFrame, StartFrame, STTMetadataFrame,
                                   TranscriptionFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy


# The user's choice for the session (set_listening_mode): push-to-talk owns every conversation, not only
# the one after a press. It lasts until they switch back or Nova restarts; reconnects keep it.
SESSION = {"push_to_talk": False}


class PushToTalk:
    # A press never followed by another (muted, walked away) must not keep Nova awake without the
    # wake phrase forever: this long without speech, it turns itself off.
    QUIET_SECS = 60.0

    def __init__(self, wake, stt, queue):
        self._state = "idle"  # idle, talking, ending (the second press, until the stop goes through)
        self._wake, self._stt, self._queue = wake, stt, queue  # queue: async (frame) into the pipeline
        self._heard = 0.0
        self._manual = False  # after a push-to-talk turn, until Nova sleeps or hears its name (wake.py)
        # Hiding the orb or saying bye puts Nova to sleep (wake.sleep, past the hold): push-to-talk ends too.
        wake.add_event_handler("on_wake_phrase_timeout", lambda *_: self.cancel("asleep", manual=False))
        wake.add_event_handler("on_wake_phrase_detected", self._woke)

    def _woke(self, _wake, reason):
        if reason not in self.REASONS:
            self._manual = False  # the user called Nova by name: the usual hands-free conversation

    REASONS = ("talk", "reply", "question", "shown")  # wake.wake() reasons; anything else is the wake phrase

    @property
    def manual(self) -> bool:
        """Push-to-talk owns the conversation: no turn without a press or Nova's name."""
        return (self._manual or SESSION["push_to_talk"]) and self.state == "idle"

    @property
    def state(self) -> str:
        if self._state != "idle" and time.monotonic() - self._heard > self.QUIET_SECS:
            self.cancel("nothing heard")
        return self._state

    @property
    def active(self) -> bool:
        return self.state != "idle"

    def heard(self):
        self._heard = time.monotonic()

    def stopped(self):
        """The second press's stop went through: the turn has ended, and push-to-talk owns what follows."""
        self._state, self._manual = "idle", True

    def cancel(self, why="", manual=None):
        """Push-to-talk ends without the second press (quiet too long, Nova put to sleep, a third press).
        The press told Pipecat the user is speaking, and it never ends a turn while they are, so say they
        stopped: the turn then ends on Pipecat's own stop timeout, and Smart Turn is back."""
        if manual is not None:
            self._manual = manual
        if self._state == "idle":
            return
        logger.info(f"push-to-talk: off ({why})")
        self._state = "idle"
        try:
            asyncio.get_running_loop().create_task(self._queue(ProposedUserStoppedSpeakingFrame()))
        except RuntimeError:
            pass  # no pipeline running: no turn to end

    async def press(self):
        logger.info(f"push-to-talk: press ({self.state})")  # markers only: never what was said
        if self.state == "idle":
            self._state = "talking"
            self.heard()
            self._wake.wake("talk")
            # The press opens the turn at once: the awake wake strategy resolves this documented proposal
            # into a turn start, which interrupts Nova if it is still answering.
            await self._queue(ProposedUserStartedSpeakingFrame())
        elif self.state == "talking":
            self._state = "ending"
            await self._stt.end_turn()
        else:
            self.cancel("third press")  # a third press while a stop never came: never leave Smart Turn paused


class PushToTalkStop(TurnAnalyzerUserTurnStopStrategy):
    """Pipecat's default stop strategy (Smart Turn v3), paused while push-to-talk is on."""

    def __init__(self, ptt: PushToTalk, **kwargs):
        super().__init__(turn_analyzer=LocalSmartTurnAnalyzerV3(), **kwargs)
        self._ptt = ptt

    @property
    def resolves_proposed_turn_stop_frames(self) -> bool:
        return True

    @property
    def decision_scheduled(self) -> bool:
        # Pipecat's 5 s stop timeout would end the turn in a pause; the press decides (bounded by QUIET_SECS).
        return self._ptt.active or super().decision_scheduled

    async def process_frame(self, frame):
        if isinstance(frame, ProposedUserStoppedSpeakingFrame) and self._ptt.state == "ending":
            self._ptt.stopped()
            await self.report_user_turn_stopped()  # without an open turn (nothing said) Pipecat ignores it
        elif not self._ptt.active or isinstance(frame, (StartFrame, STTMetadataFrame)):
            return await super().process_frame(frame)
        elif isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame, TranscriptionFrame)):
            self._ptt.heard()


def make_listening_tool(ptt: PushToTalk):
    async def set_listening_mode(params, mode: str):
        """Switch how the user talks to you, only when they ask to: "push_to_talk" (they press a key to talk, and
        you answer only a key press or your name, never background speech, even after an errand's result) or
        "automatic" (hands-free: after your name they can reply without it). It lasts until they ask again.

        Args:
            mode: "push_to_talk" or "automatic".
        """
        if mode not in ("push_to_talk", "automatic"):
            await params.result_callback({"status": "error", "error": 'mode must be "push_to_talk" or "automatic"'})
            return
        SESSION["push_to_talk"] = mode == "push_to_talk"
        logger.info(f"listening mode: {mode}")
        if mode == "automatic":
            ptt.cancel("automatic", manual=False)
        await params.result_callback({"status": "ok", "mode": mode, "instruction": (
            "Tell the user in one short sentence: push-to-talk is on, press the key to talk, or say your name."
            if mode == "push_to_talk" else "Tell the user in one short sentence: you are hands-free again.")})
    return set_listening_mode
