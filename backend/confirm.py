"""Decide yes/no/unclear from the user's actual words; the LLM never decides approval."""

import re

_NO = {"no", "nope", "don't", "dont", "never", "nevermind", "negative", "not", "nah", "naah", "nahi", "nahin", "nai",
       "noo", "nooo", "nay", "mat", "cannot", "deny", "refuse", "skip"}
_UNSURE = {"what", "why", "how", "which", "where", "when", "who", "wait", "hold", "hang", "think", "maybe", "sure?"}
# Consent is an allowlist (2026-11-10 fuzzing): every word of the reply must be one of these, or Nova asks again.
# A word list of refusals always misses one ("ok later", "ok rehne do", "ok only if it's cheap", "is it fine").
_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "okey", "haan", "fine", "alright", "approved",
        "affirmative", "confirm", "goahead", "doit", "pleasedo", "theekhai", "kardo", "haanji", "jihaan"}
_CANCEL = {"cancel", "stop"}  # approving a cancellation needs an explicit yes: "yes, cancel it"; "ok stop" re-asks
_EXPLICIT_YES = {"yes", "yeah", "yep", "yup", "sure", "haan", "haanji", "jihaan"}
_ONCE_WORDS = {"once", "thistime", "onetime", "ekbaar", "isbaar"}
_ALWAYS_WORDS = {"always", "anytime", "forever", "permanently", "hamesha", "fromnowon"}
# Words that only fill out a yes: the errand's own objects, politeness, the question's options.
_FILLER = {"it", "that", "this", "them", "the", "a", "my", "and", "please", "thanks", "thankyou", "now", "just",
           "only", "go", "do", "proceed", "continue", "book", "buy", "pay", "order", "send", "allow", "approve",
           "subscription", "plan", "booking", "ticket", "table", "cab", "uh", "um", "so", "then", "one",
           "sirf"}  # never "bas" ("enough") or "you" ("you sure")
_PHRASES = (("go ahead", "goahead"), ("do it", "doit"), ("please do", "pleasedo"), ("theek hai", "theekhai"),
            ("thik hai", "theekhai"), ("kar do", "kardo"), ("haan ji", "haanji"), ("ji haan", "jihaan"),
            ("thank you", "thankyou"), ("this time", "thistime"), ("one time", "onetime"), ("ek baar", "ekbaar"), ("is baar", "isbaar"),
            ("from now on", "fromnowon"), ("any time", "anytime"), ("never mind", "nevermind"))


def _words(reply: str) -> list[str]:
    text = reply.lower().replace("\u2019", "'").replace("\u2018", "'")
    text = " ".join(re.sub(r"[^a-z' ]", " ", text).split())
    for phrase, token in _PHRASES:
        text = re.sub(rf"\b{phrase}\b", token, text)
    return text.split()


def _verdict(reply: str):
    """(verdict, words): "yes" only when every word is a known yes, option or filler word and one is a yes or an
    option; any negation is "no"; a question, a hesitation or any unknown word is "unclear" (ask again)."""
    words = _words(reply)
    w = set(words)
    if re.search(r"\bnot (sure|certain)\b|\bdon'?t know\b", " ".join(words)):
        return "unclear", words
    if w & _NO or any(x.endswith("n't") for x in w) or (w & _CANCEL and not w & _EXPLICIT_YES):
        return "no", words
    if "?" in reply or w & _UNSURE:
        return "unclear", words
    known = _YES | _CANCEL | _FILLER | _ONCE_WORDS | _ALWAYS_WORDS
    if not w or not w <= known or not w & (_YES | _ONCE_WORDS | _ALWAYS_WORDS):
        return "unclear", words
    return "yes", words


def classify(reply: str) -> str:
    return _verdict(reply)[0]


def _grant(reply: str):
    """(verdict, bare): "once" / "always" / "no" / "unclear", and whether it was a bare yes with no option words."""
    if re.fullmatch(r"\s*not always,? (just|only) (this time|once)\W*", reply.lower()):
        return "once", False
    verdict, words = _verdict(reply)
    if verdict != "yes":
        return verdict, False
    w = set(words)
    if w & _ALWAYS_WORDS and not w & _ONCE_WORDS:
        return "always", False
    bare = not w & (_ONCE_WORDS | _ALWAYS_WORDS | {"only", "now", "just", "sirf"}) and len(words) <= 3
    return "once", bare


def grant_answer(reply: str) -> str:
    """A folder question's answer (folders.py): "once", "always", "no" or "unclear", from the user's own words.
    Every word must be a known yes or option word (classify); "always", "hamesha", "from now on" make it standing."""
    return _grant(reply)[0]


def standing_answer(reply: str) -> str:
    """The answer to allow_folder's question, which already asks for always: "always" only when the user says
    always (or the like) or gives a bare yes of up to three words; any other yes ("yes one time", "haan ek baar",
    "ok for now") is "once", which saves nothing."""
    verdict, bare = _grant(reply)
    return "always" if verdict == "always" or (verdict == "once" and bare) else verdict


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
