"""Decide yes/no/unclear from the user's actual words; the LLM never decides approval."""

import re

_NO = {"no", "nope", "don't", "dont", "never", "nevermind", "negative", "not", "nah", "naah", "nahi", "nahin", "nai",
       "noo", "nooo", "nay", "mat", "cannot", "deny", "refuse", "skip"}
_UNSURE = {"what", "why", "how", "which", "where", "when", "who", "wait", "hold", "hang", "think", "maybe", "sure?"}
# Consent is an allowlist (2026-11-10 fuzzing): every word of the reply must be one of these, or Nova asks again.
# A word list of refusals always misses one ("ok later", "ok rehne do", "ok only if it's cheap", "is it fine").
_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "okey", "haan", "fine", "alright", "approved",
        "affirmative", "confirm", "goahead", "doit", "pleasedo", "theekhai", "kardo", "haanji", "jihaan"}
# On a question about cancelling or stopping, approving it needs an explicit yes ("yes, cancel it"; "ok stop" is a
# no). On any other question they mean no: "Pay ₹500?" "yes... stop".
_CANCEL = {"cancel", "stop"}
_ABOUT_CANCEL = re.compile(r"\b(cancel\w*|stop\w*|unsubscribe\w*)\b", re.I)
_EXPLICIT_YES = {"yes", "yeah", "yep", "yup", "sure", "haan", "haanji", "jihaan"}
_ONCE_WORDS = {"once", "thistime", "onetime", "ekbaar", "isbaar"}
_ALWAYS_WORDS = {"always", "anytime", "forever", "permanently", "hamesha", "fromnowon"}
# Words that only fill out a yes: the errand's own objects, politeness, the question's options.
_FILLER = {"it", "that", "this", "them", "the", "a", "my", "and", "please", "thanks", "thankyou", "now", "just",
           "only", "go", "do", "proceed", "continue", "book", "buy", "pay", "order", "send", "allow", "approve",
           "subscription", "plan", "booking", "ticket", "table", "cab", "uh", "um", "so", "then", "one",
           "sirf", "for", "folder", "files"}  # never "bas" ("enough") or "you" ("you sure")
_PHRASES = (("go ahead", "goahead"), ("do it", "doit"), ("please do", "pleasedo"), ("theek hai", "theekhai"),
            ("thik hai", "theekhai"), ("kar do", "kardo"), ("haan ji", "haanji"), ("ji haan", "jihaan"),
            ("thank you", "thankyou"), ("this time", "thistime"), ("one time", "onetime"), ("ek baar", "ekbaar"), ("is baar", "isbaar"),
            ("from now on", "fromnowon"), ("any time", "anytime"), ("never mind", "nevermind"),
            ("for now", "fornow"))  # unknown, so unclear: "for" is filler, but "sure, for now" was pinned as unclear


def name_words(wake_phrases) -> set[str]:
    """The words of the assistant's name (its wake phrases) and "hey": "Nova, yes" is a yes. Never a consent word,
    so a name like "OK Computer" cannot turn "ok" into nothing."""
    words = {"hey", *(w for p in wake_phrases for w in _words(p))}
    return words - (_YES | _NO | _UNSURE | _CANCEL | _EXPLICIT_YES | _ONCE_WORDS | _ALWAYS_WORDS | _FILLER)


def _words(reply: str, names=()) -> list[str]:
    text = reply.lower().replace("\u2019", "'").replace("\u2018", "'")
    # Digits stay a word of their own ("yes 500" is unclear), never silently dropped.
    text = " ".join(re.sub(r"[^a-z0-9' ]", " ", re.sub(r"(\d+)", r" \1 ", text)).split())
    for phrase, token in _PHRASES:
        text = re.sub(rf"\b{phrase}\b", token, text)
    return [w for w in text.split() if w not in names]


# A step that may spend money or commit the user (hooks.py's purchase guard). Whole words: "add to cart", "reorder"
# and "bookmark" stay unmatched. Keep README.md's list in step with this one.
MONEY_WORDS = re.compile(r"\b(pay|payment|buy|purchase|complete purchase|order|checkout|check out|subscribe"
                         r"|subscription|trial|upgrade|renew|confirm|confirm booking|book|booking|book now"
                         r"|complete booking|reserve|reservation|request ride|get tickets|donate|bid|place bid)\b",
                         re.I)
_CANCELS = re.compile(r"^\W*(cancel|stop|unsubscribe|end)\b", re.I)
# What a cancellation cancels: these money words name it, every other one pays ("Stop & Pay ₹500").
_CANCELLED = {"payment", "order", "subscription", "trial", "booking", "reservation"}


def names_a_cancellation(target_name) -> bool:
    """A browser element whose real name starts with cancel, stop, unsubscribe or end (whole words) and pays for
    nothing: the "Cancel subscription" button, not "Pay & stop ads" or "Cancel anytime – Subscribe ₹499"."""
    name = str(target_name or "")
    m = _CANCELS.match(name)
    return bool(m) and all(w.group(0).lower() in _CANCELLED for w in MONEY_WORDS.finditer(name, m.end()))


def _verdict(reply: str, question: str = "", names=(), about_cancel=None):
    """(verdict, words): "yes" only when every word is a known yes, option or filler word and one is a yes or an
    option; any negation is "no"; a question, a hesitation or any unknown word is "unclear" (ask again).
    question: the text asked, so "cancel" and "stop" approve only a cancellation or a stop; about_cancel, when
    given, decides that instead (a browser step: from its target's name, never the prompt). names: name_words()."""
    words = _words(reply, names)
    w = set(words)
    if re.search(r"\bnot (sure|certain)\b|\bdon'?t know\b", " ".join(words)):
        return "unclear", words
    # Not from a page address: "Pay now on https://x/stop-and-shop" is no question about stopping.
    if about_cancel is None:
        about_cancel = bool(_ABOUT_CANCEL.search(re.sub(r"\S+://\S+", " ", question or "")))
    cancel_ok = about_cancel and w & _EXPLICIT_YES
    if w & _NO or any(x.endswith("n't") for x in w) or (w & _CANCEL and not cancel_ok):
        return "no", words
    if "?" in reply or w & _UNSURE:
        return "unclear", words
    known = _YES | _CANCEL | _FILLER | _ONCE_WORDS | _ALWAYS_WORDS
    if not w or not w <= known or not w & (_YES | _ONCE_WORDS | _ALWAYS_WORDS):
        return "unclear", words
    return "yes", words


def classify(reply: str, question: str = "", names=(), about_cancel=None) -> str:
    return _verdict(reply, question, names, about_cancel)[0]


def _grant(reply: str, names=()):
    """(verdict, bare): "once" / "always" / "no" / "unclear", and whether it was a bare yes with no option words."""
    if re.fullmatch(r"\s*not always,? (just|only) (this time|once)\W*", reply.lower()):
        return "once", False
    verdict, words = _verdict(reply, names=names)
    if verdict != "yes":
        return verdict, False
    w = set(words)
    if w & _ALWAYS_WORDS and not w & _ONCE_WORDS:
        return "always", False
    bare = not w & (_ONCE_WORDS | _ALWAYS_WORDS | {"only", "now", "just", "sirf"}) and len(words) <= 3
    return "once", bare


def grant_answer(reply: str, names=()) -> str:
    """A folder question's answer (folders.py): "once", "always", "no" or "unclear", from the user's own words.
    Every word must be a known yes or option word (classify); "always", "hamesha", "from now on" make it standing."""
    return _grant(reply, names)[0]


def standing_answer(reply: str, names=()) -> str:
    """The answer to allow_folder's question, which already asks for always: "always" only when the user says
    always (or the like) or gives a bare yes of up to three words; any other yes ("yes one time", "haan ek baar")
    is "once", which saves nothing. "ok for now" is "unclear" (asked again)."""
    verdict, bare = _grant(reply, names)
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
