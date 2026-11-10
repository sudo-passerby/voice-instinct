"""Decide yes/no/unclear from the user's actual words; the LLM never decides approval."""

import re

_NO = {"no", "nope", "don't", "dont", "never", "nevermind", "negative"}
_NO_UNLESS_YES = {"stop", "cancel"}  # "cancel that" refuses, "yes, cancel it" approves a cancellation
_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "approved", "affirmative"}
_YES_PHRASES = ("go ahead", "do it", "please do")
# A question or a hesitation is never consent: asking again is the safe outcome.
_UNSURE = {"what", "why", "how", "which", "where", "when", "who", "wait", "hold", "hang", "think", "maybe"}


def classify(reply: str) -> str:
    text = re.sub(r"[^a-z' ]", " ", reply.lower())
    words = set(text.split())
    yes = bool(words & _YES) or any(p in text for p in _YES_PHRASES)
    if words & _NO or "never mind" in text or (words & _NO_UNLESS_YES and not yes):
        return "no"  # a refusal wins: "yeah no" must not approve
    if "?" in reply or words & _UNSURE:
        return "unclear"
    return "yes" if yes else "unclear"


_ALWAYS = {"always", "anytime", "forever", "permanently"}
_ONCE = {"once"}


def grant_answer(reply: str) -> str:
    """A folder question's answer (folders.py): "once", "always", "no" or "unclear", from the user's own words.
    A plain yes is once; "always", "anytime", "from now on" make it standing. Refusals and doubts win as above."""
    verdict = classify(reply)
    if verdict == "no":
        return "no"
    text = re.sub(r"[^a-z' ]", " ", reply.lower())
    words = set(text.split())
    if "?" in reply or words & _UNSURE:
        return "unclear"
    if re.search(r"\bnot (always|forever|permanently|any ?time)\b", text):
        return "once"  # "not always, just this time"
    if words & _ALWAYS or "from now on" in text or "any time" in text:
        return "always"
    if verdict == "yes" or words & _ONCE or "this time" in text:
        return "once"
    return "unclear"


def last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def user_turns(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "user")
