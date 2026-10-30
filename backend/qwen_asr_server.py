"""Qwen3-ASR-1.7B on the GPU, as a local HTTP service for QwenASRClient (services.py).
Runs in its own venv (torch + transformers); bot.py never imports it. Usage: python qwen_asr_server.py <model_dir>

POST /transcribe  body: qwen_asr_wire.encode(...)  ->  {"text": "..."} (always a string, "" when nothing heard)
GET  /health      200 once the model is loaded and warmed up (the server only listens after that)."""

import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import qwen_asr_wire
from config import QWEN_ASR_PORT

# The prompt ends with the language preamble, so Qwen doesn't spend tokens writing it (measured: same text, faster).
PREFILL = "language English<asr_text>"


def messages(audio, keyterms: list[str]) -> list[dict]:
    """The key terms are the system message text, per the model's chat template."""
    return [{"role": "system", "content": [{"type": "text", "text": ", ".join(keyterms)}]},
            {"role": "user", "content": [{"type": "audio", "audio": audio}]}]


def extract_text(raw: str) -> str:
    """Decoded with special tokens kept: the text after <asr_text>, up to <|im_end|>. Skipping special tokens
    instead glues "English" onto the first word (measured), so never regex words away."""
    raw = raw.split("<asr_text>", 1)[-1].split("<|im_end|>", 1)[0]
    return re.sub(r"<\|[^|>]*\|>", "", raw).strip()


def load(model_dir: str):
    """The measured fast settings: fp16, sdpa, torch.compile(default, dynamic), on cuda (ROCm or NVIDIA)."""
    import torch
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

    proc = AutoProcessor.from_pretrained(model_dir)
    model = AutoModelForSpeechSeq2Seq.from_pretrained(model_dir, dtype=torch.float16,
                                                      attn_implementation="sdpa").to("cuda").eval()
    model.forward = torch.compile(model.forward, mode="default", dynamic=True)  # no static cache/reduce-overhead

    def transcribe(audio: np.ndarray, sample_rate: int, keyterms: list[str]) -> str:
        if audio.size == 0:
            return ""
        text = proc.apply_chat_template(messages(audio, keyterms), add_generation_prompt=True,
                                        tokenize=False) + PREFILL
        inputs = proc(text=text, audio=[audio], sampling_rate=sample_rate, return_tensors="pt").to("cuda")
        # The features come out fp32; fp16 weights need fp16 input, else generate crashes on a dtype mismatch.
        inputs = {k: (v.to(model.dtype) if v.is_floating_point() else v) for k, v in inputs.items()}
        with torch.inference_mode():
            ids = model.generate(**inputs, max_new_tokens=256, do_sample=False)
        return extract_text(proc.batch_decode(ids[:, inputs["input_ids"].shape[1]:], skip_special_tokens=False)[0])

    return transcribe


def main(model_dir: str, port: int = QWEN_ASR_PORT):
    transcribe = load(model_dir)
    rng = np.random.default_rng(0)
    for seconds in (2, 8, 20):  # compile the shapes a segment can have (capped at 20 s) before listening
        t0 = time.perf_counter()
        transcribe((rng.standard_normal(16000 * seconds) * 0.003).astype(np.float32), 16000, ["Nova"])
        print(f"warm-up {seconds} s: {time.perf_counter() - t0:.1f} s", flush=True)
    lock = threading.Lock()  # one GPU model: one request at a time

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply(200, {"ok": True}) if self.path == "/health" else self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/transcribe":
                return self._reply(404, {"error": "not found"})
            try:
                audio, rate, terms = qwen_asr_wire.decode(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    text = transcribe(audio, rate, terms)
            except Exception as e:  # the client turns any non-200 into "" and a warning
                print(f"transcribe failed: {e!r}", file=sys.stderr, flush=True)
                return self._reply(500, {"error": str(e)})
            self._reply(200, {"text": text})

    print(f"listening on 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main(sys.argv[1])
