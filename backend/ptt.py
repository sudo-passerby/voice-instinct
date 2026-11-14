"""Push-to-talk (`voice-instinct talk`, e.g. on Win+P): the first press wakes Nova and opens the turn
(interrupting Nova if it is answering), the turn stays open through any pause, and the second press ends
it. In between, Smart Turn's silence check is paused.

The second press asks the STT for the end (`end_turn` in services.py): it transcribes what is still
buffered and only then proposes the stop, so the last words are in the turn. The stop strategy below
resolves that documented ProposedUserStoppedSpeakingFrame.
"""

import time

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (ProposedUserStartedSpeakingFrame, ProposedUserStoppedSpeakingFrame, StartFrame, STTMetadataFrame,
                                   TranscriptionFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy


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
        wake.add_event_handler("on_wake_phrase_timeout", lambda *_: self.reset(manual=False))
        wake.add_event_handler("on_wake_phrase_detected", self._woke)

    def _woke(self, _wake, reason):
        if reason not in self.REASONS:
            self._manual = False  # the user called Nova by name: the usual hands-free conversation

    REASONS = ("talk", "reply", "question", "shown")  # wake.wake() reasons; anything else is the wake phrase

    @property
    def manual(self) -> bool:
        """Push-to-talk owns the conversation: no turn without a press or Nova's name."""
        return self._manual and self.state == "idle"

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

    def reset(self, manual=None):
        self._state = "idle"
        if manual is not None:
            self._manual = manual

    async def press(self):
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
            self._ptt.reset(manual=True)
            await self.report_user_turn_stopped()  # without an open turn (nothing said) Pipecat ignores it
        elif not self._ptt.active or isinstance(frame, (StartFrame, STTMetadataFrame)):
            return await super().process_frame(frame)
        elif isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame, TranscriptionFrame)):
            self._ptt.heard()
