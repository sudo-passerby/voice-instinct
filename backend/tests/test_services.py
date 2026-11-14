import numpy as np

from config import Config
from pipecat.utils.types import NOT_GIVEN
from services import ParakeetSTTService, build_llm


class FakeRecognizer:
    def __init__(self):
        self.seen = []

    def create_stream(self):
        outer = self

        class S:
            result = type("R", (), {"text": "  hello world  "})()

            def accept_waveform(self, sr, audio):
                outer.seen.append((sr, len(audio)))

        return S()

    def decode_stream(self, stream):
        pass


def test_parakeet_declares_model_and_language_so_pipecat_validation_is_clean():
    # Runtime log in the first voice demo: "STTSettings: the following fields are NOT_GIVEN: model, language"
    svc = ParakeetSTTService(recognizer=FakeRecognizer())
    assert svc._settings.model is not NOT_GIVEN and svc._settings.language is not NOT_GIVEN


def test_parakeet_decodes_16k_pcm_through_the_recognizer():
    rec = FakeRecognizer()
    svc = ParakeetSTTService(recognizer=rec)
    pcm = (np.ones(1600, dtype=np.int16) * 1000).tobytes()
    assert svc._decode(pcm) == "hello world" and rec.seen == [(16000, 1600)]


def test_gemini_service_gets_model_and_system_instruction_via_settings(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    llm = build_llm(Config(provider="gemini", model="gemini-3.8-flash", api_key_env="TEST_KEY"), "BE BRIEF")
    assert llm._settings.model == "gemini-3.8-flash" and llm._settings.system_instruction == "BE BRIEF"


def test_ollama_and_openai_compatible_get_the_same_settings(monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    cfg = Config(provider="ollama", model="qwen3:8b", api_key_env="NO_SUCH_KEY")
    llm = build_llm(cfg, "BE BRIEF")
    assert llm._settings.model == "qwen3:8b" and llm._settings.system_instruction == "BE BRIEF"


def test_a_local_model_gets_the_same_prompt_and_never_thinks_aloud(monkeypatch):
    # With reasoning on qwen3.5:4b took 13 s; with Ollama's /v1 left to default it spoke its reasoning. Its own
    # extra tool rules were dropped (2026-11-14): with the same prompt as Gemini it scored as Gemini did, 82/87.
    llm = build_llm(Config(provider="ollama", model="qwen3.5:4b", api_key_env="NO_SUCH_KEY"), "BE BRIEF")
    assert llm._settings.system_instruction == "BE BRIEF"
    assert llm._settings.extra["reasoning_effort"] == "none"


def test_gemini_and_openai_keep_their_prompt_as_given(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    for provider in ("gemini", "openai"):
        llm = build_llm(Config(provider=provider, model="m", api_key_env="TEST_KEY"), "BE BRIEF")
        assert llm._settings.system_instruction == "BE BRIEF" and not llm._settings.extra


from pathlib import Path

from services import PARAKEET_NAME, kokoro_paths, parakeet_dir


def test_model_paths_come_from_models_dir():
    cfg = Config(models_dir="/models")
    assert parakeet_dir(cfg) == Path("/models") / PARAKEET_NAME
    assert kokoro_paths(cfg) == (Path("/models/kokoro/kokoro-v1.0.onnx"), Path("/models/kokoro/voices-v1.0.bin"))


import pytest

from config import STT_ENGINES


def test_every_engine_has_a_model_folder():
    from services import STT_MODELS
    assert set(STT_MODELS) == set(STT_ENGINES)


def test_a_missing_model_says_to_run_the_installer(tmp_path):
    from services import make_stt
    cfg = Config(models_dir=str(tmp_path), stt_engine="nemotron")
    with pytest.raises(RuntimeError, match=r"nemotron.*not installed.*\./install\.sh"):
        make_stt(cfg)


def test_parakeet_is_built_from_its_folder(tmp_path):
    from services import STT_MODELS, make_stt
    (tmp_path / STT_MODELS["parakeet"]).mkdir()
    stt = make_stt(Config(models_dir=str(tmp_path), stt_engine="parakeet"), recognizer=object())
    assert isinstance(stt, ParakeetSTTService)


class FakeKokoro:
    loads = []

    def __init__(self, model_path, voices_path):
        import threading
        FakeKokoro.loads.append(threading.current_thread() is threading.main_thread())
        self.voices = ["af_heart"]


def test_kokoro_loads_once_per_process_and_off_the_event_loop(tmp_path, monkeypatch):
    import asyncio

    import kokoro_onnx
    import services
    d = tmp_path / "kokoro"
    d.mkdir()
    (d / "kokoro-v1.0.onnx").touch()
    (d / "voices-v1.0.bin").touch()
    monkeypatch.setattr(kokoro_onnx, "Kokoro", FakeKokoro)
    services._load_kokoro.cache_clear()
    FakeKokoro.loads = []
    cfg = Config(models_dir=str(tmp_path))

    async def two_sessions():
        return await services.make_tts(cfg), await services.make_tts(cfg)

    a, b = asyncio.run(two_sessions())
    services._load_kokoro.cache_clear()
    assert FakeKokoro.loads == [False]  # one load, in a worker thread
    assert a is not b and a._kokoro is b._kokoro  # a service per session, one model


def test_the_shared_kokoro_service_is_set_up_like_pipecats_own(monkeypatch):
    # SharedKokoroTTSService skips KokoroTTSService.__init__ (it loads the model): pinned to Pipecat 1.12.
    import pipecat.services.kokoro.tts as kokoro_tts
    from services import SharedKokoroTTSService
    monkeypatch.setattr(kokoro_tts, "Kokoro", FakeKokoro)
    monkeypatch.setattr(kokoro_tts, "_ensure_model_files", lambda *a: None)
    theirs = kokoro_tts.KokoroTTSService(model_path="m", voices_path="v",
                                         settings=kokoro_tts.KokoroTTSService.Settings(voice="af_heart"))
    ours = SharedKokoroTTSService(FakeKokoro("m", "v"), "af_heart")
    assert ours._settings == theirs._settings
    assert set(vars(ours)) == set(vars(theirs))
    with pytest.raises(ValueError, match="voice"):
        SharedKokoroTTSService(FakeKokoro("m", "v"), "no_such_voice")


def test_a_local_model_gets_the_apps_own_messages_as_system(monkeypatch):
    # Ollama ignores the "developer" role that relay uses for a due reminder or an errand's question: qwen3.5:4b
    # then ended the conversation on a reminder and started an errand on a question, 0/3 each (2026-11-14); sent as
    # "system" it spoke the reminder 3/3.
    llm = build_llm(Config(provider="ollama", model="qwen3.5:4b", api_key_env="NO_SUCH_KEY"), "BE BRIEF")
    msgs = [{"role": "user", "content": "hi"}, {"role": "developer", "content": "Reminder: call mom"}]
    params = llm.build_chat_completion_params({"messages": msgs})
    assert [m["role"] for m in params["messages"]] == ["user", "system"] and msgs[1]["role"] == "developer"


def test_gemini_compatible_services_keep_the_developer_role(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    llm = build_llm(Config(provider="openai", model="m", api_key_env="TEST_KEY"), "BE BRIEF")
    params = llm.build_chat_completion_params({"messages": [{"role": "developer", "content": "x"}]})
    assert params["messages"][0]["role"] == "developer"
