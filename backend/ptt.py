"""Push-to-talk (`voice-instinct talk`, e.g. on Win+P): the first press wakes Nova and the turn stays open
through any pause; the second press ends it. In between, Smart Turn's silence check is paused.

The second press asks the STT for the end (`end_turn` in services.py): it transcribes what is still
buffered and only then proposes the stop, so the last words are in the turn. The stop strategy below
resolves that documented ProposedUserStoppedSpeakingFrame.
"""

import time

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (ProposedUserStoppedSpeakingFrame, StartFrame, STTMetadataFrame,
                                   TranscriptionFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy


class PushToTalk:
    # A press never followed by another (muted, walked away) must not keep Nova awake without the
    # wake phrase forever: this long without speech, it turns itself off.
    QUIET_SECS = 60.0

    def __init__(self, wake, stt):
        self._state = "idle"  # idle, talking, ending (the second press, until the stop goes through)
        self._wake, self._stt = wake, stt
        self._heard = 0.0
        # Hiding the orb or saying bye puts Nova to sleep (wake.sleep, past the hold): push-to-talk ends too.
        wake.add_event_handler("on_wake_phrase_timeout", lambda *_: self.reset())

    @property
    def state(self) -> str:
        if self._state != "idle" and time.monotonic() - self._heard > self.QUIET_SECS:
            self._state = "idle"
        return self._state

    @property
    def active(self) -> bool:
        return self.state != "idle"

    def heard(self):
        self._heard = time.monotonic()

    def reset(self):
        self._state = "idle"

    async def press(self):
        if self.state == "idle":
            self._state = "talking"
            self.heard()
            self._wake.wake("talk")
        elif self.state == "talking":
            self._state = "ending"
            await self._stt.end_turn()
        else:
            self.reset()  # a third press while a stop never came: never leave Smart Turn paused


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
            self._ptt.reset()
            await self.report_user_turn_stopped()  # without an open turn (nothing said) Pipecat ignores it
        elif not self._ptt.active or isinstance(frame, (StartFrame, STTMetadataFrame)):
            return await super().process_frame(frame)
        elif isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame, TranscriptionFrame)):
            self._ptt.heard()
