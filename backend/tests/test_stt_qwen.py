import http.server
import json
import socket
import threading
import time

import numpy as np
import pytest
from loguru import logger

import qwen_asr_wire
import services
from config import Config


def _serve(handler_fn):
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            code, out = handler_fn(self.path, body)
            self.send_response(code)
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _transcribe(client, audio):
    s = client.create_stream()
    s.accept_waveform(16000, audio)
    client.decode_stream(s)
    return s.result.text


def test_the_client_posts_audio_and_terms_and_returns_the_text():
    audio = np.sin(np.linspace(0, 200, 16000)).astype(np.float32) * 0.5
    seen = {}

    def handle(path, body):
        seen["path"] = path
        samples, sr, terms = qwen_asr_wire.decode(body)
        seen.update(sr=sr, terms=terms, err=float(np.abs(samples - audio).max()))
        return 200, {"text": "Nova, call Priya."}

    srv = _serve(handle)
    try:
        client = services.QwenASRClient(["Nova", "Priya"], port=srv.server_port)
        assert _transcribe(client, audio) == "Nova, call Priya."
    finally:
        srv.shutdown()
    assert seen["path"] == "/transcribe" and seen["sr"] == 16000 and seen["terms"] == ["Nova", "Priya"]
    assert seen["err"] <= 1 / 32767


def test_a_down_service_gives_empty_text_and_one_warning():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    msgs = []
    sink = logger.add(lambda m: msgs.append(m.record["message"]), level="WARNING")
    try:
        client = services.QwenASRClient(["Nova"], port=port)
        for _ in range(2):
            t = time.monotonic()
            assert _transcribe(client, np.zeros(1600, np.float32)) == ""
            assert time.monotonic() - t < 2
    finally:
        logger.remove(sink)
    assert len(msgs) == 1 and "voice-instinct-qwen.service" in msgs[0]


def test_a_server_error_gives_empty_text():
    srv = _serve(lambda p, b: (500, {"error": "boom"}))
    try:
        client = services.QwenASRClient(["Nova"], port=srv.server_port)
        assert _transcribe(client, np.zeros(1600, np.float32)) == ""
    finally:
        srv.shutdown()


def test_a_non_string_text_gives_empty_text():
    srv = _serve(lambda p, b: (200, {"text": None}))
    try:
        client = services.QwenASRClient(["Nova"], port=srv.server_port)
        assert _transcribe(client, np.zeros(1600, np.float32)) == ""
    finally:
        srv.shutdown()


class _Fake:
    def create_stream(self):
        raise AssertionError


def test_make_stt_gives_qwen_the_segmented_service(tmp_path):
    (tmp_path / "Qwen3-ASR-1.7B-hf").mkdir()
    cfg = Config(stt_engine="qwen3-asr-gpu", models_dir=str(tmp_path))
    svc = services.make_stt(cfg, recognizer=_Fake())
    assert isinstance(svc, services.ParakeetSTTService)


def test_a_missing_qwen_model_says_to_run_the_installer(tmp_path):
    cfg = Config(stt_engine="qwen3-asr-gpu", models_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match="qwen3-asr-gpu speech model is not installed"):
        services.make_stt(cfg)


TERMS = ["Nova", "Deepak", "Priya", "Swiggy", "Koramangala", "Toit", "Pipecat", "Kokoro", "Parakeet"]


@pytest.mark.parametrize("echo", [
    "Nova, Deepak, Priya, Swiggy, Koramangala, Toit, Pipecat, Kokoro, Parakeet.",  # quiet audio, 2026-11-10
    "Deepak, Nova, Priya, Swiggy, Koramangala, Toit, Pipecat, Kokoro, Parakeet.",
    "nova deepak priya",
])
def test_the_key_terms_read_back_on_quiet_audio_are_dropped(echo):
    srv = _serve(lambda p, b: (200, {"text": echo}))
    try:
        assert _transcribe(services.QwenASRClient(TERMS, port=srv.server_port), np.zeros(1600, np.float32)) == ""
    finally:
        srv.shutdown()


@pytest.mark.parametrize("said", ["Nova.", "Deepak.", "Nova, Deepak", "Nova, call Priya.", "Nova, check my Swiggy order."])
def test_real_speech_with_key_terms_is_kept(said):
    srv = _serve(lambda p, b: (200, {"text": said}))
    try:
        assert _transcribe(services.QwenASRClient(TERMS, port=srv.server_port), np.zeros(1600, np.float32)) == said
    finally:
        srv.shutdown()
