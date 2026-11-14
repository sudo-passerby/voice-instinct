"""Push-to-talk in a real Pipecat pipeline (STT -> user aggregator, Smart Turn v3): pauses never end
the turn, and the second press ends it with the last words in it."""

import asyncio

from pipecat.frames.frames import InputAudioRawFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.turns.user_turn_strategies import UserTurnStrategies, default_user_turn_start_strategies

from ptt import PushToTalk, PushToTalkStop
from services import ParakeetSTTService
from wake import make_wake

SPEECH = b"\x01\x00" * 1600  # 100 ms at 16 kHz
SILENCE = b"\x00\x00" * 1600


class Numbered:
    """sherpa-onnx OfflineRecognizer as ParakeetSTTService uses it: segment n is heard as "part n"."""

    def __init__(self):
        self.n = 0

    def create_stream(self):
        rec = self

        class S:
            def accept_waveform(self, sample_rate, samples):
                rec.n += 1
                self.result = type("R", (), {"text": f"part {rec.n}"})()
        return S()

    def decode_stream(self, s):
        pass


def audio(chunk, n):
    return [InputAudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1) for _ in range(n)]


def talk(n=5):
    return [VADUserStartedSpeakingFrame(), *audio(SPEECH, n), VADUserStoppedSpeakingFrame()]


async def converse(frames, presses):
    """`presses`: seconds after the start at which Win+P is pressed."""
    ctx, sent = LLMContext(), []

    async def send(msg):
        sent.append(msg)

    stt = ParakeetSTTService(recognizer=Numbered())
    stt.PAUSE_SECS = 0.2
    wake = make_wake(["hey nova"], 10, send, hold=lambda: ptt.active)  # wired as in bot.py
    ptt = PushToTalk(wake, stt, None)
    user, _ = LLMContextAggregatorPair(ctx, user_params=LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(start=[wake, *default_user_turn_start_strategies()],
                                                stop=[PushToTalkStop(ptt)])))

    async def press_at():
        for t in presses:
            await asyncio.sleep(t)
            await ptt.press()
    asyncio.get_running_loop().create_task(press_at())
    await run_test(Pipeline([stt, user]), frames_to_send=[SleepFrame(sleep=0.1), *frames])
    return [m["content"] for m in ctx.get_messages() if m["role"] == "user"], ptt


def test_a_long_pause_does_not_end_the_turn_and_the_second_press_does():
    # Press, speak, stop for 1.5 s (a segment is cut and Smart Turn would decide), speak again and press
    # while still talking: one turn with both parts, the unfinished second segment included.
    frames = [*talk(), *audio(SILENCE, 15), SleepFrame(sleep=1.5),
              VADUserStartedSpeakingFrame(), *audio(SPEECH, 5), SleepFrame(sleep=0.5),
              VADUserStoppedSpeakingFrame(), SleepFrame(sleep=0.5)]
    users, ptt = asyncio.run(converse(frames, presses=[0.05, 1.8]))
    assert users == ["part 1 part 2"]
    assert ptt.state == "idle"


def test_pressing_twice_without_speaking_leaves_nothing_paused():
    users, ptt = asyncio.run(converse([SleepFrame(sleep=0.4)], presses=[0.05, 0.1]))
    assert users == [] and ptt.state == "idle"


def test_without_push_to_talk_speech_still_needs_the_wake_phrase():
    users, _ = asyncio.run(converse([*talk(), SleepFrame(sleep=0.5)], presses=[]))
    assert users == []
