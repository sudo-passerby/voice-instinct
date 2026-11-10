"""#25/#26 with the real front model (run with -m claude): Gemini picks the right memory tool from Nova's
instructions, and tells a fact to remember from a reminder."""

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from config import REPO_ROOT, Config
from memory import PROMPT, Memory, make_memory_tools
from pipecat.frames.frames import ErrorFrame, FunctionCallsStartedFrame, LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import run_test
from registry import Registry
from reminders import make_reminder_tools
from services import build_llm

pytestmark = pytest.mark.claude
load_dotenv(REPO_ROOT / ".env")

SYSTEM = (PROMPT + "It is 2026-10-16 09:00 local time. For reminders, call set_reminder with the exact local time."
          "\n\nWhat you know about the user:\n# Noted recently\n- 2026-10-16: Is vegetarian")


def _first_call(said, tmp_path):
    """The real tools Nova has (bot.py), so Gemini sees their real names, docstrings and parameters. A temp memory
    and an in-memory registry: a call that runs touches nothing of the user's."""
    memory = Memory(tmp_path / "memory")
    memory.ensure()
    tools = [*make_reminder_tools(Registry(), None), *make_memory_tools(memory)]
    llm = build_llm(Config(), SYSTEM)
    for f in tools:
        llm.register_direct_function(f)
    ctx = LLMContext(messages=[{"role": "user", "content": said}], tools=tools)
    down, up = asyncio.run(run_test(llm, frames_to_send=[LLMContextFrame(ctx)]))
    assert not [f for f in up + down if isinstance(f, ErrorFrame)]
    (calls,) = [f for f in down if isinstance(f, FunctionCallsStartedFrame)]
    return calls.function_calls[0].function_name, calls.function_calls[0].arguments


@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="no GEMINI_API_KEY")
@pytest.mark.parametrize("said, tool, word", [
    ("Remember that my sister Priya lives in Pune.", "remember", "priya"),
    ("Remember to call mum at 5 pm today.", "set_reminder", "mum"),
    ("Forget that I'm vegetarian.", "forget", "vegetarian"),
    ("What's my sister's name again?", "recall", "sister"),
])
def test_gemini_picks_the_memory_tool_the_user_meant(said, tool, word, tmp_path):
    name, args = _first_call(said, tmp_path)
    assert name == tool, (name, args)
    assert word in json.dumps(args).lower()
