"""The request body between the backend client and the Qwen3-ASR server. Stdlib + numpy only: the server
(its own Python 3.12 venv) imports this file too."""

import base64
import json

import numpy as np


def encode(samples: np.ndarray, sample_rate: int, keyterms: list[str]) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    return json.dumps({"sample_rate": sample_rate, "pcm16": base64.b64encode(pcm.tobytes()).decode(),
                       "keyterms": list(keyterms)}).encode("utf-8")


def decode(body: bytes) -> tuple[np.ndarray, int, list[str]]:
    d = json.loads(body.decode("utf-8"))
    pcm = np.frombuffer(base64.b64decode(d["pcm16"]), dtype="<i2")
    return pcm.astype(np.float32) / 32767, int(d["sample_rate"]), list(d.get("keyterms", []))
