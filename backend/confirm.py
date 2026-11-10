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
_NOT_ALWAYS = re.compile(r"\bnot (always|forever|permanently|any ?time)\b")
# "not this time", "not once", "not now", "not yet": a refusal, never a one-off yes.
_NOT_NOW = re.compile(r"\bnot (now|yet|today|once|this once|this time)\b")
_ONCE = re.compile(r"\b(once|this time)\b")


def _grant(reply: str):
    """(verdict, explicit_once): the verdict as grant_answer gives it, and whether the user said once outright."""
    if classify(reply) == "no":
        return "no", False
    text = " ".join(re.sub(r"[^a-z' ]", " ", reply.lower()).split())
    words = set(text.split())
    if "?" in reply or words & _UNSURE:
        return "unclear", False
    if _NOT_NOW.search(text):
        return "no", False
    once = bool(_ONCE.search(text))  # not negated: _NOT_NOW returned above
    if _NOT_ALWAYS.search(text):
        # "not always, just this time" is once; "not always" on its own refuses.
        return ("once", True) if once or classify(reply) == "yes" else ("no", False)
    if words & _ALWAYS or "from now on" in text or "any time" in text:
        return "always", False
    if once:
        return "once", True
    return ("once", False) if classify(reply) == "yes" else ("unclear", False)


def grant_answer(reply: str) -> str:
    """A folder question's answer (folders.py): "once", "always", "no" or "unclear", from the user's own words.
    A plain yes is once; "always", "anytime", "from now on" make it standing. Refusals and doubts win as above,
    and a negated "once" or "this time" ("not this time", "not now") is a no."""
    return _grant(reply)[0]


def standing_answer(reply: str) -> str:
    """The answer to allow_folder's question, which already asks for always: a plain yes or "always" is
    "always"; an explicit "just this once" is "once", which saves nothing."""
    verdict, explicit_once = _grant(reply)
    return "always" if verdict == "once" and not explicit_once else verdict


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
