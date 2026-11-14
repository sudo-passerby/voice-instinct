"""Pipecat services: local speech-to-text (Nemotron streaming, Parakeet or Whisper through sherpa-onnx, or a client
for the local Qwen3-ASR GPU server; Pipecat has no built-in one for these), Kokoro text-to-speech, and the
front-LLM builder."""

import asyncio
import functools
import json
import os
import re
import time
import urllib.request
from collections.abc import AsyncGenerator
from pathlib import Path

import numpy as np
from loguru import logger

from pipecat.frames.frames import (Frame, InterimTranscriptionFrame, ProposedUserStoppedSpeakingFrame, TranscriptionFrame,
                                   UserStoppedSpeakingFrame)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import SegmentedSTTService, STTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

import qwen_asr_wire
import whisper_text
from config import QWEN_ASR_PORT, is_whisper

PARAKEET_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"


def parakeet_dir(cfg) -> Path:
    return Path(cfg.models_dir).expanduser() / PARAKEET_NAME


def kokoro_paths(cfg) -> tuple[Path, Path]:
    d = Path(cfg.models_dir).expanduser() / "kokoro"
    return d / "kokoro-v1.0.onnx", d / "voices-v1.0.bin"


@functools.cache  # one copy for every session: reconnects must not load the model again
def _load_kokoro(model_path: Path, voices_path: Path):
    from kokoro_onnx import Kokoro
    from pipecat.services.kokoro.tts import _ensure_model_files

    _ensure_model_files(model_path, voices_path)  # downloads them if missing, as KokoroTTSService does
    return Kokoro(str(model_path), str(voices_path))


class SharedKokoroTTSService(KokoroTTSService):
    """KokoroTTSService on a model loaded once per process. Its own __init__ loads the model for every session,
    on the event loop; this one does the rest of that __init__ only (pinned to Pipecat 1.16 by test_services)."""

    def __init__(self, kokoro, voice: str, **kwargs):
        TTSService.__init__(self, push_start_frame=True, push_stop_frames=True,
                            settings=self.Settings(model=None, voice=voice, language=Language.EN, speed=1.0),
                            **kwargs)
        if voice not in kokoro.voices:
            raise ValueError(f"Kokoro TTS voice '{voice}' is not in the voices file")
        self._kokoro = kokoro  # no resampler of its own: since 1.16 TTSService resamples what run_tts yields


async def make_tts(cfg, voice: str = "af_heart") -> SharedKokoroTTSService:
    """A Kokoro service per session; the model is loaded in a thread the first time, so the loop keeps running."""
    return SharedKokoroTTSService(await asyncio.to_thread(_load_kokoro, *kokoro_paths(cfg)), voice)


@functools.cache  # one copy for every session: reconnects must not load the model again
def _load_recognizer(model_dir: Path):
    import sherpa_onnx

    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(model_dir / "encoder.int8.onnx"),
        decoder=str(model_dir / "decoder.int8.onnx"),
        joiner=str(model_dir / "joiner.int8.onnx"),
        tokens=str(model_dir / "tokens.txt"),
        num_threads=4,
        model_type="nemo_transducer",
    )


@functools.cache  # one copy for every session: reconnects must not load the model again
def _load_whisper_recognizer(model_dir: Path, size: str, language: str):
    import sherpa_onnx

    return sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=str(model_dir / f"{size}-encoder.int8.onnx"),
        decoder=str(model_dir / f"{size}-decoder.int8.onnx"),
        tokens=str(model_dir / f"{size}-tokens.txt"),
        language=language, task="transcribe", num_threads=4,
    )


@functools.cache  # one copy for every session; each session gets its own stream
def _load_online_recognizer(model_dir: Path):
    import sherpa_onnx

    def one(pattern):  # file names differ between builds (encoder.int8.onnx, encoder-….int8.onnx)
        (path,) = sorted(model_dir.glob(pattern))
        return str(path)

    return sherpa_onnx.OnlineRecognizer.from_transducer(
        encoder=one("encoder*.onnx"), decoder=one("decoder*.onnx"), joiner=one("joiner*.onnx"),
        tokens=str(model_dir / "tokens.txt"), num_threads=4, sample_rate=16000,
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=2.4,  # nothing said: reset the stream
        rule2_min_trailing_silence=0.8,  # the end of a sentence: finalize it
        rule3_min_utterance_length=20,  # a very long one: finalize anyway
    )


class ParakeetSTTService(SegmentedSTTService):
    """Parakeet TDT 0.6B v3 via sherpa-onnx; transcribes each VAD segment.

    Pipecat has no sherpa-onnx or local Parakeet service (its local STT options are Whisper and
    Moonshine), so this subclasses SegmentedSTTService. `wants_wav_segments = False` is the
    source-documented way to receive raw 16-bit PCM instead of a WAV container.
    """

    # ponytail: offline model, transcribes after VAD stop; swap for a streaming
    # cache-aware model if STT latency dominates.
    wants_wav_segments = False

    def __init__(self, model_dir: Path | None = None, recognizer=None, clean=None, model_name="parakeet-tdt-0.6b-v3",
                 **kwargs):
        super().__init__(
            sample_rate=16000,
            ttfs_p99_latency=1.3,  # the 0.8 s pause plus decoding
            settings=STTSettings(model=model_name, language=Language.EN),
            **kwargs,
        )
        self._rec = recognizer or _load_recognizer(model_dir)
        self._cut = None
        self._in_segment, self._segment_bytes = False, 0  # a segment under way, and its audio so far
        self._ended = self._decoded = 0  # segments handed to the base class, and transcribed (in order)
        self._stop_after = None  # push-to-talk: propose the turn's end once this many are transcribed
        self._clean = clean or (lambda text: text)  # str -> str on every decoded text (Whisper's filter)

    def can_generate_metrics(self) -> bool:
        return True

    # A VAD stop (0.2 s for Smart Turn) used to end every segment, so speech reached Parakeet a word at
    # a time and was misheard (#19). Now a stop ends the segment only after PAUSE_SECS of silence.
    # These override SegmentedSTTService's start/stop handlers (pinned to Pipecat 1.16 by test_stt_parakeet).
    # Until the cut its SpeechSegmentBuffer keeps everything since the segment started. Since 1.16 a transcript
    # whose user spoke again before it arrived is pushed unfinalized by SegmentedSTTService itself.
    PAUSE_SECS = 0.8

    async def _handle_user_started_speaking(self, frame):
        await self._cancel_cut()
        if not self._in_segment:  # a resume within the pause continues the same segment
            self._in_segment, self._segment_bytes = True, 0
        await super()._handle_user_started_speaking(frame)

    async def _handle_user_stopped_speaking(self, frame):
        if not self._in_segment:
            return  # push-to-talk already ended this segment (end_turn)
        await self._cancel_cut()
        self._cut = self.create_task(self._cut_after_pause(frame))

    async def _cut_after_pause(self, frame):
        await asyncio.sleep(self.PAUSE_SECS)
        self._cut = None
        await self._end_segment(frame)

    async def _end_segment(self, frame):
        self._in_segment, self._segment_bytes = False, 0
        self._ended += 1
        await SegmentedSTTService._handle_user_stopped_speaking(self, frame)

    async def _cancel_cut(self):
        if self._cut is not None:
            await self.cancel_task(self._cut)
            self._cut = None

    async def end_turn(self):
        """Push-to-talk's second press (ptt.py): transcribe what is buffered now, without waiting for the
        pause, and propose the end of the user's turn right after that transcript."""
        if self._in_segment:
            await self._cancel_cut()
            await self._end_segment(None)  # the base class never reads the frame
        if self._decoded >= self._ended:
            await self.push_frame(ProposedUserStoppedSpeakingFrame())
        else:
            self._stop_after = self._ended

    # A TV with gaps under PAUSE_SECS would make one ever-growing segment (4 min: 3.7 GB, and no wake
    # until it ended), so a segment is cut at this length whatever, like Nemotron's rule 3. Counted here,
    # not read from the base class's buffer (a private that 1.16 replaced).
    MAX_SEGMENT_SECS = 20

    async def process_audio_frame(self, frame, direction):
        await super().process_audio_frame(frame, direction)
        if not self._in_segment:
            return
        self._segment_bytes += len(frame.audio)
        if self._segment_bytes >= self.MAX_SEGMENT_SECS * self.sample_rate * 2:
            talking = self._cut is None  # no pause under way: they are still talking
            await self._cancel_cut()
            await self._end_segment(frame)  # it never reads the frame
            if talking:  # keep collecting the next segment; this one's transcript is pushed unfinalized
                await self._handle_user_started_speaking(frame)

    async def cancel(self, frame):
        await self._cancel_cut()
        await super().cancel(frame)

    async def stop(self, frame):
        await self._cancel_cut()
        await super().stop(frame)

    def _decode(self, pcm: bytes) -> str:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        stream = self._rec.create_stream()
        stream.accept_waveform(16000, audio)
        self._rec.decode_stream(stream)
        return self._clean(stream.result.text.strip())

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        self._decoded += 1
        await self.start_processing_metrics()
        text = await asyncio.to_thread(self._decode, audio)
        await self.stop_processing_metrics()
        if text:  # never logged: until the wake phrase this is whatever the room is saying
            yield TranscriptionFrame(text, self._user_id, time_now_iso8601())
        if self._stop_after is not None and self._decoded >= self._stop_after:
            self._stop_after = None
            yield ProposedUserStoppedSpeakingFrame()  # after the transcript, so the turn ends with it


class NemotronSTTService(STTService):
    """Nemotron Speech Streaming 0.6B via sherpa-onnx: one continuous stream, so the model hears whole
    sentences. Finals come on sherpa's endpoint (0.8 s of trailing silence) or at the end of the
    user's turn, never on the VAD's 0.2 s stops: those chopped speech into misheard pieces (#19)."""

    def __init__(self, model_dir: Path | None = None, recognizer=None, language: Language | None = Language.EN,
                 **kwargs):
        # Smart Turn waits this long for a final after the VAD stop. Measured: sherpa's endpoint fires
        # 0.74-1.34 s after speech ends, so 1.2 split "Hey Nova … <request>" into two turns. A final
        # is marked finalized and ends the turn at once, so the normal path never waits this out.
        super().__init__(sample_rate=16000, ttfs_p99_latency=1.8,
                         settings=STTSettings(model=model_dir.name if model_dir else "nemotron", language=language),
                         **kwargs)
        self._rec = recognizer or _load_online_recognizer(model_dir)
        self._stream = self._rec.create_stream()
        self._partial = ""
        # UserStoppedSpeakingFrame is a SystemFrame: it can arrive while a chunk is being decoded in a
        # thread, so the stream is only touched under this lock.
        self._lock = asyncio.Lock()

    def can_generate_metrics(self) -> bool:
        return True

    def _feed(self, pcm: bytes) -> tuple[str, bool]:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self._stream.accept_waveform(16000, samples)
        while self._rec.is_ready(self._stream):
            self._rec.decode_stream(self._stream)
        return self._rec.get_result(self._stream).strip(), self._rec.is_endpoint(self._stream)

    def _final(self) -> TranscriptionFrame | None:
        text, self._partial = self._partial, ""
        self._rec.reset(self._stream)
        if not text:
            return None
        frame = TranscriptionFrame(text, self._user_id, time_now_iso8601())
        frame.finalized = True  # the end-of-turn strategy releases the turn on it
        return frame

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        out = []
        async with self._lock:
            try:
                text, endpoint = await asyncio.to_thread(self._feed, audio)
            except Exception as e:  # keep listening: one bad chunk must not end the session
                self._partial = ""
                self._rec.reset(self._stream)
                error = e
            else:
                error = None
                if text and text != self._partial:  # never logged: before the wake phrase it is the room
                    self._partial = text
                    out.append(InterimTranscriptionFrame(text, self._user_id, time_now_iso8601()))
                if endpoint and (final := self._final()):
                    out.append(final)
        if error is not None:
            await self.push_error(f"{self}: transcription failed: {error}", exception=error)
        for frame in out:  # yielded outside the lock: downstream must never wait on it
            yield frame

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, UserStoppedSpeakingFrame):
            async with self._lock:
                final = self._final()
            if final:
                await self.push_frame(final)  # the turn ended: what was heard so far is the sentence
        await super().process_frame(frame, direction)


class _QwenStream:
    """Shaped like a sherpa-onnx offline stream: accept_waveform in, result.text out."""

    def __init__(self):
        self.audio, self.rate = np.zeros(0, np.float32), 16000
        self.result = type("Result", (), {"text": ""})()

    def accept_waveform(self, sample_rate, samples):
        self.rate, self.audio = sample_rate, np.asarray(samples, np.float32)


class QwenASRClient:
    """Stands in for a sherpa-onnx offline recognizer, but asks the local Qwen3-ASR server (GPU, own venv)."""

    def __init__(self, keyterms: list[str], port: int = QWEN_ASR_PORT, timeout: float = 15.0):
        self.keyterms, self.port, self.timeout = keyterms, port, timeout
        self._last_warned = float("-inf")
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open  # local: never a proxy

    def create_stream(self):
        return _QwenStream()

    def _echoes_keyterms(self, text: str) -> bool:
        """On quiet audio Qwen reads its context back ("Nova, Deepak, Priya, ..."), which would wake the assistant
        (2026-11-10). Three or more words, every one of them a key term, is that echo, not speech."""
        terms = {w for t in self.keyterms for w in t.lower().split()}
        words = re.findall(r"[^\W_]+", text.lower())
        return len(words) >= 3 and all(w in terms for w in words)

    def decode_stream(self, stream):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/transcribe",
                                         data=qwen_asr_wire.encode(stream.audio, stream.rate, self.keyterms),
                                         headers={"Content-Type": "application/json"})
            with self._open(req, timeout=self.timeout) as r:
                text = json.load(r)["text"]
            stream.result.text = text if isinstance(text, str) and not self._echoes_keyterms(text) else ""
        except Exception as e:  # down, slow, 500, bad JSON: a missed sentence, never a crashed pipeline
            stream.result.text = ""
            if time.monotonic() - self._last_warned > 30:
                self._last_warned = time.monotonic()
                logger.warning(f"Qwen speech service on 127.0.0.1:{self.port} not answering ({e}): "
                               "is voice-instinct-qwen.service running? (it takes about a minute to warm up)")


# The official sherpa-onnx int8 builds; install.sh downloads the configured one (same names there).
STT_MODELS = {
    "nemotron": "sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25",
    "nemotron-multilingual": "sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-160ms-int8-2026-06-11",
    "parakeet": PARAKEET_NAME,
    "whisper-tiny": "sherpa-onnx-whisper-tiny",
    "whisper-base": "sherpa-onnx-whisper-base",
    "whisper-small": "sherpa-onnx-whisper-small",
    "qwen3-asr-gpu": "Qwen3-ASR-1.7B-hf",
}


def stt_dir(cfg) -> Path:
    return Path(cfg.models_dir).expanduser() / STT_MODELS[cfg.stt_engine]


def make_stt(cfg, recognizer=None):
    """The configured speech-to-text service. recognizer: a fake for tests."""
    d = stt_dir(cfg)
    if recognizer is None and not d.is_dir():
        raise RuntimeError(f"The {cfg.stt_engine} speech model is not installed ({d}). Run ./install.sh.")
    if cfg.stt_engine == "parakeet":
        return ParakeetSTTService(model_dir=d, recognizer=recognizer)
    if is_whisper(cfg.stt_engine):
        size = cfg.stt_engine.removeprefix("whisper-")
        return ParakeetSTTService(model_dir=d, recognizer=recognizer or _load_whisper_recognizer(d, size, cfg.stt_language),
                                  clean=whisper_text.clean, model_name=cfg.stt_engine)
    if cfg.stt_engine == "qwen3-asr-gpu":  # no clean filter: Qwen doesn't invent text the way Whisper does
        return ParakeetSTTService(model_dir=d, recognizer=recognizer or QwenASRClient(cfg.keyterms),
                                  model_name="qwen3-asr-1.7b")
    return NemotronSTTService(model_dir=d, recognizer=recognizer,
                              language=Language.EN if cfg.stt_engine == "nemotron" else None)


class LocalLLMService(OpenAILLMService):
    """Ollama's /v1 ignores the "developer" role relay gives a due reminder or an errand's question (a small model
    then acted on the user's last words instead): it gets them as "system", which it follows."""

    def build_chat_completion_params(self, params_from_context):
        params = super().build_chat_completion_params(params_from_context)
        params["messages"] = [{**m, "role": "system"} if m.get("role") == "developer" else m
                              for m in params["messages"]]
        return params


def build_llm(cfg, system_instruction: str):
    """Front LLM. Model and system prompt go through Settings (the documented 1.x way)."""
    key = os.environ.get(cfg.api_key_env, "ollama")
    if cfg.provider == "gemini":
        return GoogleLLMService(
            api_key=key,
            settings=GoogleLLMService.Settings(model=cfg.model, system_instruction=system_instruction),
        )
    local = cfg.provider == "ollama"
    base = cfg.base_url or ("http://localhost:11434/v1" if local else None)
    return (LocalLLMService if local else OpenAILLMService)(
        api_key=key,
        base_url=base,
        # reasoning off: on Ollama's /v1 a thinking model otherwise reasons for seconds, or speaks its reasoning
        settings=OpenAILLMService.Settings(
            model=cfg.model, system_instruction=system_instruction,
            extra={"reasoning_effort": "none"} if local else {}),
    )
