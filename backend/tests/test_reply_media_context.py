"""An opt-in candidate pool must not turn unknown media into answerable text."""
import pytest

from rapport.datasets.corpus import _reply_context


def message(sender, kind="text", text="合成文字"):
    return {"sender": sender, "type": kind, "text": text}


def test_default_still_rejects_older_media():
    context = [message("me", "image"), message("me"), message("other")]
    assert _reply_context(context) is None
    assert _reply_context(context, allow_media_prefix=True) == context[1:]
    assert context[0]["type"] == "image"


@pytest.mark.parametrize("kind", ["image", "sticker", "voice", "file", "video"])
def test_mixed_incoming_turn_is_never_trimmed_to_look_text_only(kind):
    context = [message("me"), message("other", kind), message("other", text="你看看这个")]
    assert _reply_context(context, allow_media_prefix=True) is None


@pytest.mark.parametrize("bad", [message("system"), message("unknown"), message("me", "unknown")])
def test_unknown_roles_and_types_are_not_discarded(bad):
    context = [bad, message("me"), message("other")]
    assert _reply_context(context, allow_media_prefix=True) is None


def test_only_the_suffix_after_the_last_media_is_used():
    context = [message("other", "image"), message("me"), message("me", "sticker"),
               message("me"), message("other"), message("other")]
    assert _reply_context(context, allow_media_prefix=True) == context[3:]


def test_pure_text_context_unchanged_in_both_modes():
    context = [message("me"), message("other")]
    assert _reply_context(context) == context
    assert _reply_context(context, allow_media_prefix=True) == context
