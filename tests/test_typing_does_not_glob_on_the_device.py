# tests/test_typing_does_not_glob_on_the_device.py
#
# "It had no destination set, but it tapped a letter 'd' in the search box, why" (user,
# 2026-09-04).
#
# Because `adb shell` RUNS A SHELL ON THE DEVICE. `input_text` builds its command as an
# ARGUMENT LIST, which avoids the LOCAL shell and is easy to mistake for safety — but adb
# hands the whole command to a shell on the phone, and that one expands globs against ITS
# working directory, which is `/`.
#
# Live: a recovery tried to sail to an unresolved home port named '?'. `?` is not in
# `_KEYCODE`, so it took the `input text` fallback, and on the device:
#
#     $ pwd                -> /
#     $ ls -d ?            -> d          (Android's standard /d symlink)
#     $ input text ?       -> input text d
#
# The search box received the letter **d**, the port list filtered to `Diu`, and nothing in
# the log said why. The stray 'd' was the only evidence.
#
# `?`, `*` and `[` all expand; quoting covers them together.

import unittest
from unittest.mock import patch


def _sent(text):
    """The argv lists `input_text` hands to adb.

    conftest replaces `actions.adb_actions.input_text` with a recorder so no test can reach
    the phone, so the real one is fetched from the module's source rather than the patched
    attribute — this test is ABOUT what that function builds, and the stub would hide it.
    `_adb` is still patched, so nothing is sent anywhere."""
    import importlib
    import actions.adb_actions as mod
    real = importlib.reload(mod).input_text
    calls = []
    with patch.object(mod, "_adb", side_effect=lambda a, **k: calls.append(a)), \
         patch.object(mod.time, "sleep"), patch.object(mod, "_acted"):
        real(text)
    return calls


class AWildcardMustNotReachTheDeviceShell(unittest.TestCase):
    def test_a_question_mark_is_quoted(self):
        """The exact character that produced the stray 'd'."""
        argv = [c for c in _sent("?") if "text" in c]
        self.assertTrue(argv, "nothing was typed")
        self.assertNotIn("?", argv[0], "raw '?' reaches the device shell and globs to 'd'")
        self.assertIn("'?'", argv[0])

    def test_the_other_glob_characters_too(self):
        for ch in ("*", "["):
            with self.subTest(char=ch):
                argv = [c for c in _sent(ch) if "text" in c][0]
                self.assertNotEqual(argv[-1], ch, f"raw {ch!r} reaches the device shell")


class TheOrDINARYPATHISUNCHANGED(unittest.TestCase):
    """Letters go through per-character keyevents and never touch `input text` at all — the
    Unity text-field workaround this function exists for."""

    def test_letters_still_use_keyevents(self):
        calls = _sent("faro")
        self.assertTrue(all("keyevent" in c for c in calls),
                        "a letter took the shell fallback")
        self.assertEqual([c[-1] for c in calls],
                         ["KEYCODE_F", "KEYCODE_A", "KEYCODE_R", "KEYCODE_O"])

    def test_a_space_goes_through_its_keycode(self):
        """`' '` is in `_KEYCODE`, so a space never reaches the shell fallback at all.

        Which means the fallback's `char.replace(" ", "%s")` — the `input text` convention for
        a space — CANNOT FIRE: it is per-character, and the only character it would rewrite is
        mapped. Pre-existing dead code, noted rather than removed."""
        calls = _sent("1 2")
        self.assertIn(["shell", "input", "keyevent", "KEYCODE_SPACE"], calls)
        digits = [c for c in calls if "text" in c]
        self.assertEqual([c[-1] for c in digits], ["1", "2"],
                         "digits take the fallback; they have no keycode here")


if __name__ == "__main__":
    unittest.main()
