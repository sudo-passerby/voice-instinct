"""Decide yes/no/unclear from the user's actual words; the LLM never decides approval."""

import re

_NO = {"no", "nope", "don't", "dont", "never", "nevermind", "negative",
       # any negation refuses: "not even once", "let's not", "I won't", "can't allow it" (review 2026-11-10)
       "not", "nah", "nahi", "na", "mat", "cannot", "deny", "refuse", "skip", "leave"}
_NO_UNLESS_YES = {"stop", "cancel"}  # "cancel that" refuses, "yes, cancel it" approves a cancellation
_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "approved", "affirmative",
        "haan", "han", "haa", "ji", "fine", "alright"}
_YES_PHRASES = ("go ahead", "do it", "please do", "theek hai", "thik hai")
# A question or a hesitation is never consent: asking again is the safe outcome.
_UNSURE = {"what", "why", "how", "which", "where", "when", "who", "wait", "hold", "hang", "think", "maybe"}


def classify(reply: str) -> str:
    text = re.sub(r"[^a-z' ]", " ", reply.lower())
    words = set(text.split())
    yes = bool(words & _YES) or any(p in text for p in _YES_PHRASES)
    if re.search(r"\bnot (sure|certain)\b|\bdon'?t know\b", text):
        return "unclear"  # a hesitation, not a refusal: ask again
    if words & _NO or any(w.endswith("n't") for w in words) or "never mind" in text or (words & _NO_UNLESS_YES and not yes):
        return "no"  # a refusal wins: "yeah no" must not approve
    if "?" in reply or words & _UNSURE:
        return "unclear"
    return "yes" if yes else "unclear"


_ALWAYS = {"always", "anytime", "forever", "permanently"}
_ONE_OFF = re.compile(r"\b(once|this time|for now|today|only|just now)\b")  # a yes that is not standing
# A reply made only of the question's own option words ("just this once", "always") answers it on its own;
# anything else must be a clear yes (classify) to grant: an unknown phrasing re-asks, never grants.
_OPTION_WORDS = {"just", "only", "this", "time", "once", "always", "anytime", "forever", "please", "for", "now"}
_NOT_ALWAYS_ONCE = re.compile(r"^not always,? (just|only) (this time|once)$")


def _grant(reply: str):
    """(verdict, one_off): "once" / "always" / "no" / "unclear", and whether the user qualified a yes as one-off."""
    text = " ".join(re.sub(r"[^a-z' ]", " ", reply.lower()).split())
    if _NOT_ALWAYS_ONCE.match(text):
        return "once", True
    verdict = classify(reply)
    if verdict == "no":
        return "no", False
    words = set(text.split())
    if "?" in reply or words & _UNSURE:
        return "unclear", False
    if verdict != "yes" and not (words and words <= _OPTION_WORDS and words & {"once", "always", "anytime", "forever", "time"}):
        return "unclear", False
    one_off = bool(_ONE_OFF.search(text))
    if (words & _ALWAYS or "from now on" in text or "any time" in text) and not one_off:
        return "always", False
    return "once", one_off


def grant_answer(reply: str) -> str:
    """A folder question's answer (folders.py): "once", "always", "no" or "unclear", from the user's own words.
    Only a clear yes, or a reply of the question's own option words, grants; any negation refuses, anything else
    re-asks. A yes is once unless it says always, anytime or from now on without a one-off qualifier."""
    return _grant(reply)[0]


def standing_answer(reply: str) -> str:
    """The answer to allow_folder's question, which already asks for always: a plain yes or "always" is
    "always"; a one-off yes ("just this once", "for now", "today only") is "once", which saves nothing."""
    verdict, one_off = _grant(reply)
    return "always" if verdict == "once" and not one_off else verdict


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
