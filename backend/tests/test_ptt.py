"""Push-to-talk in a real Pipecat pipeline (STT -> user aggregator, Smart Turn v3): pauses never end
the turn, and the second press ends it with the last words in it."""

import asyncio
import time

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

    def __init__(self, decode_secs=0.0):
        self.n, self.decode_secs = 0, decode_secs

    def create_stream(self):
        rec = self

        class S:
            def accept_waveform(self, sample_rate, samples):
                rec.n += 1
                self.result = type("R", (), {"text": f"part {rec.n}"})()
        return S()

    def decode_stream(self, s):
        time.sleep(self.decode_secs)  # runs in a thread, like the real model


def audio(chunk, n):
    return [InputAudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1) for _ in range(n)]


def talk(n=5):
    return [VADUserStartedSpeakingFrame(), *audio(SPEECH, n), VADUserStoppedSpeakingFrame()]


async def converse(frames, presses, decode_secs=0.0, quiet=None, rec=None):
    """`presses`: seconds after the previous press at which Win+P is pressed, or a callable(wake, ptt)."""
    ctx, sent = LLMContext(), []

    async def send(msg):
        sent.append(msg)

    stt = ParakeetSTTService(recognizer=rec or Numbered(decode_secs))
    stt.PAUSE_SECS = 0.2
    wake = make_wake(["hey nova"], 10, send, hold=lambda: ptt.active, manual=lambda: ptt.manual)  # as in bot.py
    ptt = PushToTalk(wake, stt, stt.queue_frame)
    if quiet:
        ptt.QUIET_SECS = quiet
    user, _ = LLMContextAggregatorPair(ctx, user_params=LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(start=[wake, *default_user_turn_start_strategies()],
                                                stop=[PushToTalkStop(ptt)])))
    stops = []  # (seconds since start, what the turn said) for every turn end

    @user.event_handler("on_user_turn_stopped")
    async def _stopped(aggregator, strategy, message):
        stops.append((time.monotonic() - t0, message.content))
    t0 = time.monotonic()

    async def press_at():
        for t, act in presses:
            await asyncio.sleep(t)
            await (act(wake, ptt) if act else ptt.press())
    asyncio.get_running_loop().create_task(press_at())
    down, _ = await run_test(Pipeline([stt, user]), frames_to_send=[SleepFrame(sleep=0.1), *frames])
    converse.stops, converse.down = stops, down
    return [m["content"] for m in ctx.get_messages() if m["role"] == "user"], ptt


def test_a_long_pause_does_not_end_the_turn_and_the_second_press_does():
    # Press, speak, stop for 1.5 s (a segment is cut and Smart Turn would decide), speak again and press
    # while still talking: one turn with both parts, the unfinished second segment included.
    frames = [*talk(), *audio(SILENCE, 15), SleepFrame(sleep=1.5),
              VADUserStartedSpeakingFrame(), *audio(SPEECH, 5), SleepFrame(sleep=0.5),
              VADUserStoppedSpeakingFrame(), SleepFrame(sleep=0.5)]
    users, ptt = asyncio.run(converse(frames, presses=[(0.05, None), (1.75, None)]))
    assert users == ["part 1 part 2"]
    assert ptt.state == "idle"


def test_pressing_twice_without_speaking_leaves_nothing_paused():
    users, ptt = asyncio.run(converse([SleepFrame(sleep=0.4)], presses=[(0.05, None), (0.05, None)]))
    assert users == [] and ptt.state == "idle"


def test_without_push_to_talk_speech_still_needs_the_wake_phrase():
    users, _ = asyncio.run(converse([*talk(), SleepFrame(sleep=0.5)], presses=[]))
    assert users == []


def test_a_pause_longer_than_pipecats_stop_timeout_does_not_end_the_turn():
    frames = [*talk(), *audio(SILENCE, 15), SleepFrame(sleep=6.5), *talk(), SleepFrame(sleep=1.5)]
    users, ptt = asyncio.run(converse(frames, presses=[(0.05, None), (6.9, None)]))
    assert users == ["part 1 part 2"] and ptt.state == "idle"


def test_a_press_while_the_last_words_are_decoding_waits_for_them():
    # Speak, pause past the cut, press while the model is still decoding: one turn, with the words.
    frames = [*talk(), SleepFrame(sleep=0.35), SleepFrame(sleep=1.2)]
    users, ptt = asyncio.run(converse(frames, presses=[(0.05, None), (0.55, None)], decode_secs=0.6))
    assert users == ["part 1"] and ptt.state == "idle"
    assert [text for _, text in converse.stops] == ["part 1"]  # one turn end, by the press, with the words


async def hide(wake, ptt):
    wake.sleep()


def test_hiding_the_orb_ends_push_to_talk_and_its_turn():
    users, ptt = asyncio.run(converse([SleepFrame(sleep=6.0)], presses=[(0.05, None), (0.1, hide)]))
    assert ptt.state == "idle" and not ptt.active and [text for _, text in converse.stops] == [""]


def test_a_press_never_followed_by_another_turns_itself_off_and_closes_its_turn():
    # The press opened a turn; without this it stayed open for good (Pipecat waits while the user "speaks").
    frames = [*audio(SILENCE, 5), SleepFrame(sleep=0.6), *audio(SILENCE, 5), SleepFrame(sleep=6.0)]  # the mic keeps sending
    users, ptt = asyncio.run(converse(frames, presses=[(0.05, None)], quiet=0.3))
    assert ptt.state == "idle" and [text for _, text in converse.stops] == [""]


class Said(Numbered):
    """Every segment is heard as the same sentence."""

    def __init__(self, text):
        super().__init__()
        self.text = text

    def create_stream(self):
        rec = self

        class S:
            def accept_waveform(self, sample_rate, samples):
                self.result = type("R", (), {"text": rec.text})()
        return S()


def test_hey_nova_still_works_without_ever_pressing_the_key():
    # Push-to-talk is an extra way in: with the key unused, the wake phrase opens the turn and Smart Turn ends it.
    ctx, sent = LLMContext(), []

    async def send(msg):
        sent.append(msg)

    async def go():
        stt = ParakeetSTTService(recognizer=Said("Hey Nova, what's the time?"))
        stt.PAUSE_SECS = 0.2
        wake = make_wake(["hey nova"], 10, send, hold=lambda: ptt.active)
        ptt = PushToTalk(wake, stt, stt.queue_frame)
        user, _ = LLMContextAggregatorPair(ctx, user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(start=[wake, *default_user_turn_start_strategies()],
                                                    stop=[PushToTalkStop(ptt)])))
        await run_test(Pipeline([stt, user]), frames_to_send=[*talk(), *audio(SILENCE, 10), SleepFrame(sleep=2.0)])
    asyncio.run(go())
    assert [m["content"] for m in ctx.get_messages() if m["role"] == "user"] == ["Hey Nova, what's the time?"]
    assert sent[0] == {"type": "wake", "reason": "phrase"}


class Script(Numbered):
    """Segment n is heard as texts[n]."""

    def __init__(self, *texts):
        super().__init__()
        self.texts = texts

    def create_stream(self):
        rec = self

        class S:
            def accept_waveform(self, sample_rate, samples):
                rec.n += 1
                self.result = type("R", (), {"text": rec.texts[rec.n - 1]})()
        return S()


def gap(secs=0.8):
    return [*audio(SILENCE, 5), SleepFrame(sleep=secs)]


def test_after_a_push_to_talk_turn_background_speech_is_ignored_but_its_name_is_not():
    rec = Script("book a table", "the tv in the next room", "hey nova what time is it")
    frames = [*talk(), *gap(0.5), *talk(), *gap(), *talk(), *gap(1.5)]
    users, ptt = asyncio.run(converse(frames, presses=[(0.05, None), (0.5, None)], rec=rec))
    assert users == ["book a table", "hey nova what time is it"]
    assert not ptt.manual  # its name: back to the hands-free conversation


def test_after_hey_nova_background_speech_still_counts_as_a_reply():
    # The hands-free conversation, unchanged: once called by name, the next thing said is a reply.
    rec = Script("hey nova book a table", "the tv in the next room")
    frames = [*talk(), *gap(), *talk(), *gap(1.5)]
    users, _ = asyncio.run(converse(frames, presses=[], rec=rec))
    assert "the tv in the next room" in " ".join(users)  # heard (no bot reply here, so in the same turn)


def test_once_nova_sleeps_after_push_to_talk_its_usual_wakes_work_again():
    async def sleep_then_reply(wake, ptt):
        wake.sleep()
        wake.wake("reply")  # an errand's result later on
    rec = Script("book a table", "yes please")
    frames = [*talk(), *gap(0.6), *talk(), *gap(1.5)]
    users, ptt = asyncio.run(converse(frames, presses=[(0.05, None), (0.5, None), (0.2, sleep_then_reply)], rec=rec))
    assert users == ["book a table", "yes please"] and not ptt.manual


def test_a_press_while_nova_answers_interrupts_it_at_once():
    # Nothing said yet: the press alone opens the turn, which interrupts the answer (Pipecat broadcasts it).
    from pipecat.frames.frames import InterruptionFrame
    asyncio.run(converse([SleepFrame(sleep=0.4)], presses=[(0.05, None)]))
    assert any(isinstance(f, InterruptionFrame) for f in converse.down)
    assert converse.stops == []  # and the turn stays open until the second press


def test_push_to_talk_mode_for_the_session_ignores_background_speech_even_after_hey_nova():
    # "Hey Nova, switch to push to talk": from then on, only a press or its name starts a turn.
    from ptt import SESSION
    SESSION["push_to_talk"] = True
    try:
        rec = Script("hey nova book a table", "the tv in the next room", "hey nova what time is it")
        frames = [*talk(), *gap(), *talk(), *gap(), *talk(), *gap(1.5)]
        users, ptt = asyncio.run(converse(frames, presses=[], rec=rec))
    finally:
        SESSION["push_to_talk"] = False
    assert "the tv in the next room" not in " ".join(users) and "what time is it" in " ".join(users)


def test_switching_modes_by_voice():
    from types import SimpleNamespace
    from ptt import SESSION, make_listening_tool
    out = []

    async def cb(r, **kw):
        out.append(r)
    ptt = PushToTalk(make_wake(["hey nova"], 10, None), None, None)
    tool = make_listening_tool(ptt)
    asyncio.run(tool(SimpleNamespace(result_callback=cb), "push_to_talk"))
    assert SESSION["push_to_talk"] and ptt.manual
    asyncio.run(tool(SimpleNamespace(result_callback=cb), "automatic"))
    assert not SESSION["push_to_talk"] and not ptt.manual
    asyncio.run(tool(SimpleNamespace(result_callback=cb), "loud"))
    assert out[-1]["status"] == "error" and not SESSION["push_to_talk"]
