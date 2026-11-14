"""Push-to-talk (`voice-instinct talk`, e.g. on Win+P): the first press wakes Nova and the turn stays open
through any pause; the second press ends it. In between, Smart Turn's silence check is paused.

The second press asks the STT for the end (ParakeetSTTService.end_turn): it transcribes what is still
buffered and only then proposes the stop, so the last words are in the turn. The stop strategy below
resolves that documented ProposedUserStoppedSpeakingFrame.
"""

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import ProposedUserStoppedSpeakingFrame, StartFrame, STTMetadataFrame
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy


class PushToTalk:
    def __init__(self, wake, stt, queue):
        self.state = "idle"  # idle, talking, ending (the second press, until the stop goes through)
        self._wake, self._stt, self._queue = wake, stt, queue

    @property
    def active(self) -> bool:
        return self.state != "idle"

    async def press(self):
        if self.state == "idle":
            self.state = "talking"
            self._wake.wake("talk")
        elif self.state == "talking":
            self.state = "ending"
            if hasattr(self._stt, "end_turn"):
                await self._stt.end_turn()
            else:  # an STT without segments (Nemotron): its transcript is already through
                await self._queue([ProposedUserStoppedSpeakingFrame()])
        else:
            self.state = "idle"  # a third press while a stop never came: never leave Smart Turn paused


class PushToTalkStop(TurnAnalyzerUserTurnStopStrategy):
    """Pipecat's default stop strategy (Smart Turn v3), paused while push-to-talk is on."""

    def __init__(self, ptt: PushToTalk, **kwargs):
        super().__init__(turn_analyzer=LocalSmartTurnAnalyzerV3(), **kwargs)
        self._ptt = ptt

    @property
    def resolves_proposed_turn_stop_frames(self) -> bool:
        return True

    async def process_frame(self, frame):
        if isinstance(frame, ProposedUserStoppedSpeakingFrame) and self._ptt.state == "ending":
            self._ptt.state = "idle"
            await self.report_user_turn_stopped()  # without an open turn (nothing said) Pipecat ignores it
        elif not self._ptt.active or isinstance(frame, (StartFrame, STTMetadataFrame)):
            return await super().process_frame(frame)
