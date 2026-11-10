"""The Qwen3-ASR server's pure parts. No torch here: the backend env doesn't have it."""

import importlib

import numpy as np

import qwen_asr_wire


def test_request_round_trip_keeps_audio_and_unicode_terms():
    x = np.sin(np.linspace(0, 200, 16000)).astype(np.float32) * 0.5
    terms = ["José", "O'Brien", "Koramangala"]
    samples, rate, got = qwen_asr_wire.decode(qwen_asr_wire.encode(x, 16000, terms))
    assert got == terms and rate == 16000
    assert samples.dtype == np.float32 and np.max(np.abs(samples - x)) <= 1 / 32767


def test_extract_text_takes_what_follows_the_marker():
    from qwen_asr_server import extract_text
    assert extract_text("language English<asr_text>Nova, call Priya.<|im_end|>") == "Nova, call Priya."
    # The prefill was in the prompt, so the output may start with the words themselves.
    assert extract_text("Nova, hi<|im_end|>") == "Nova, hi"
    assert extract_text("<|endoftext|>") == ""


def test_messages_put_the_terms_in_the_system_message():
    from qwen_asr_server import PREFILL, messages
    audio = np.zeros(1600, np.float32)
    system, user = messages(audio, ["Nova", "Priya"])
    assert system == {"role": "system", "content": [{"type": "text", "text": "Nova, Priya"}]}
    assert user["role"] == "user" and user["content"][0]["type"] == "audio"
    assert user["content"][0]["audio"] is audio
    assert PREFILL == "language English<asr_text>"


def test_the_server_module_imports_without_torch():
    importlib.import_module("qwen_asr_server")


def test_a_body_over_4_mb_is_refused_before_it_is_read():
    from qwen_asr_server import MAX_BODY, body_fits
    assert MAX_BODY == 4 * 1024 * 1024
    assert body_fits("0") and body_fits(str(MAX_BODY))
    assert not body_fits(str(MAX_BODY + 1)) and not body_fits("-1")
    # a 20 s segment, the longest the client sends, fits easily
    assert body_fits(str(len(qwen_asr_wire.encode(np.zeros(16000 * 20, np.float32), 16000, ["Nova"] * 50))))
