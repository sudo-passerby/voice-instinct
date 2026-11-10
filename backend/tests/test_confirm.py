from confirm import classify, last_user_text


def test_yes():
    for s in ["yes", "Yeah, go ahead.", "sure", "ok do it"]:
        assert classify(s) == "yes", s


def test_no_wins_over_yes():
    for s in ["no", "No, don't do it", "yeah no", "ok but don't", "cancel that"]:
        assert classify(s) == "no", s


def test_unclear():
    for s in ["hmm maybe", "what would it do?", ""]:
        assert classify(s) == "unclear", s


def test_last_user_text_handles_str_and_parts():
    msgs = [
        {"role": "user", "content": "yes"},
        {"role": "assistant", "content": "Allow it?"},
        {"role": "user", "content": [{"type": "text", "text": "not sure"}]},
    ]
    assert last_user_text(msgs) == "not sure"
    assert last_user_text([]) == ""


def test_user_turns_counts_only_user_messages():
    from confirm import user_turns

    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
            {"role": "developer", "content": "c"}, {"role": "user", "content": "d"}]
    assert user_turns(msgs) == 2 and user_turns([]) == 0


def test_approving_a_cancellation_or_a_stop_is_a_yes():
    # Cancelling subscriptions is a core errand: "yes, cancel it" approves it.
    for s in ["Yes, cancel it.", "Yeah go ahead and cancel", "Yes, stop the subscription", "sure, cancel my plan"]:
        assert classify(s) == "yes", s


def test_a_question_or_a_hesitation_is_never_a_yes():
    # A clarifying question must not approve; asking again is the safe outcome.
    for s in ["Okay, what would that do?", "ok hold on", "okay... let me think", "yes? which one", "sure, wait"]:
        assert classify(s) == "unclear", s


def test_a_real_refusal_still_wins():
    for s in ["yes no", "yeah, don't", "ok never mind", "no, cancel it", "nope, stop"]:
        assert classify(s) == "no", s


def test_not_sure_is_a_hesitation_and_any_other_negation_refuses():
    for s in ["not sure", "I'm not certain", "I don't know"]:
        assert classify(s) == "unclear", s
    for s in ["not now", "nah", "I won't", "can't do that", "nahi", "skip it"]:
        assert classify(s) == "no", s


# Fuzzing, 2026-11-10: consent is an allowlist, so an unknown word asks again instead of approving.
def test_a_deferral_a_condition_or_a_question_without_a_question_mark_is_never_a_yes():
    for s in ["ok later", "do it tomorrow", "ok rehne do", "theek hai chhodo", "ok only if it's cheap", "is it fine",
              "you sure", "I'm fine", "ok bas", "can you confirm the price", "haan baad mein", "han", "ji"]:
        assert classify(s) != "yes", s


def test_curly_apostrophes_and_hindi_refusals_refuse():
    for s in ["ok don’t", "okay, don’t do it", "haan nahin", "mat karo", "fine cancel", "ok stop"]:
        assert classify(s) == "no", s


def test_hinglish_yeses_are_yes():
    for s in ["haan", "haan ji", "theek hai", "haan kar do", "yes thank you"]:
        assert classify(s) == "yes", s
