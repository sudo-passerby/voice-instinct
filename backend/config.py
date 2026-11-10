"""Settings for both halves, read from config.toml at the repo root (sectioned; flat keys still work)."""

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POSITIONS = ("bottom-right", "bottom-left", "top-right", "top-left", "center")
STT_ENGINES = ("nemotron", "nemotron-multilingual", "parakeet",
               "whisper-tiny", "whisper-base", "whisper-small", "qwen3-asr-gpu")  # services.make_stt; install.sh's table
QWEN_ASR_PORT = 7861  # the local Qwen3-ASR server (own venv, Python 3.12, imports this); keep config.py stdlib-only
# Whisper's language, for now English only. English mode also covers Hinglish (written in Roman letters) and
# translates plain Hindi; Hindi mode ("hi") gave garbage in these int8 builds (tested 2026-10-16). A code sherpa-onnx
# doesn't know makes it exit the whole process on the first utterance, so load() refuses anything else.
WHISPER_LANGUAGES = frozenset({"en"})


def is_whisper(engine: str) -> bool:
    return engine.startswith("whisper-")


# config.toml section -> {key in file: Config field}
_SECTIONS = {
    "assistant": {"name": "name", "awake_timeout": "awake_timeout"},
    "model": {"provider": "provider", "model": "model", "api_key_env": "api_key_env", "base_url": "base_url"},
    "gui": {"size": "gui_size", "position": "gui_position"},
    "paths": {"models_dir": "models_dir", "data_dir": "data_dir", "workspace_dir": "workspace_dir",
              "backend_cwd": "backend_cwd"},
    "agent": {"permission_mode": "permission_mode", "backend_timeout": "backend_timeout",
              "permission_timeout": "permission_timeout"},
    "debug": {"browser_steps": "debug_browser_steps"},
    "stt": {"engine": "stt_engine", "language": "stt_language", "keyterms": "stt_keyterms"},
}


@dataclass
class Config:
    name: str = "Nova"
    awake_timeout: float = 10.0  # seconds of quiet before the orb hides
    provider: str = "gemini"  # gemini | openai | ollama
    model: str = "gemini-3.8-flash"
    base_url: str | None = None  # openai-compatible endpoint; ollama defaults to localhost
    api_key_env: str = "GEMINI_API_KEY"
    gui_size: int = 128
    gui_position: str = "top-right"
    models_dir: str = "~/.voice-instinct/models"
    backend_cwd: str = str(Path.home())
    backend_timeout: float = 300  # must stay well above permission_timeout
    data_dir: str = "~/.voice-instinct"
    workspace_dir: str = "~/.voice-instinct/workspace"  # default folder for tasks that name none
    permission_timeout: float = 90
    permission_mode: str = "auto"  # Claude Code mode for delegated runs; the sandbox bounds what auto may do
    debug_browser_steps: bool = False  # screenshot every errand browser step (steps.py)
    stt_engine: str = "nemotron"  # speech-to-text engine (services.make_stt); install.sh downloads only this one
    stt_language: str = "en"  # Whisper's language code ([stt] language)
    stt_keyterms: list[str] = field(default_factory=list)  # [stt] keyterms: names/words to bias recognition toward

    @property
    def keyterms(self) -> list[str]:
        """The assistant's name first, then [stt] keyterms, without repeats (case-insensitive)."""
        seen, out = set(), []
        for t in [self.name, *self.stt_keyterms]:
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    @property
    def wake_phrases(self) -> list[str]:
        """Only the name itself, wherever it is said ("Nova, what's on?", "hey nova"). The merged spellings Parakeet
        wrote for a quick "Hey Nova" ("Inova", "Anova") and Whisper's "no va" were dropped on 2026-11-10: Qwen writes
        "Nova", and the owner wants only the name to wake it."""
        return [" ".join(self.name.lower().split())]


def load(path=None) -> Config:
    p = Path(path) if path else REPO_ROOT / "config.toml"
    data = tomllib.loads(p.read_text()) if p.exists() else {}
    known = {f.name for f in fields(Config)}
    values = {k: v for k, v in data.items() if k in known}  # legacy flat keys
    for section, mapping in _SECTIONS.items():
        for key, field_name in mapping.items():
            if isinstance(data.get(section), dict) and key in data[section]:
                values[field_name] = data[section][key]
    cfg = Config(**values)
    if not isinstance(cfg.name, str) or not cfg.name.strip():
        raise ValueError("config.toml: [assistant] name must be non-empty text")
    cfg.name = cfg.name.strip()
    if isinstance(cfg.gui_size, bool) or not isinstance(cfg.gui_size, int) or not 48 <= cfg.gui_size <= 1024:
        raise ValueError("config.toml: [gui] size must be a whole number of pixels from 48 to 1024")
    if isinstance(cfg.awake_timeout, bool) or not isinstance(cfg.awake_timeout, (int, float)) or cfg.awake_timeout <= 0:
        raise ValueError("config.toml: [assistant] awake_timeout must be a number of seconds above 0")
    if cfg.provider not in ("gemini", "openai", "ollama"):
        raise ValueError("config.toml: [model] provider must be gemini, openai or ollama")
    if not 0 < cfg.permission_timeout < cfg.backend_timeout:
        raise ValueError("config.toml: [agent] permission_timeout must be above 0 and below backend_timeout")
    if cfg.gui_position not in POSITIONS:
        raise ValueError(f"config.toml: [gui] position must be one of {', '.join(POSITIONS)}")
    if not isinstance(cfg.debug_browser_steps, bool):
        raise ValueError("config.toml: [debug] browser_steps must be true or false")
    if cfg.stt_engine not in STT_ENGINES:
        raise ValueError(f"config.toml: [stt] engine must be one of {', '.join(STT_ENGINES)}")
    if cfg.stt_language not in WHISPER_LANGUAGES:  # never a string Whisper would choke on mid-session
        raise ValueError('config.toml: [stt] language must be "en" for now (it also covers Hinglish)')
    k = cfg.stt_keyterms
    if not isinstance(k, list) or len(k) > 50 or not all(isinstance(x, str) and 0 < len(x.strip()) <= 40 for x in k):
        raise ValueError("config.toml: [stt] keyterms must be a list of up to 50 words or short phrases (each 1 to 40 characters)")
    cfg.stt_keyterms = [x.strip() for x in k]
    return cfg
