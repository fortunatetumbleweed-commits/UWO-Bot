"""Feature / functional tests that run against REAL CAPTURED FRAMES.

These are not unit tests (user, 2026-08-31). They exercise perception end to end against
pixels the bot actually saw, so they answer "does this still read a real screen correctly"
rather than "does this function behave".

They depend on `tests/stage_suite/frames/`, which is NOT in the repo (see .gitignore). Every
one of them skips when its frame is absent:

    if not os.path.exists(STAGE): self.skipTest("stage frame not available")

so a fresh checkout runs clean — quietly. If this whole package skips, the frames are missing,
not the behaviour.

Whole files were moved rather than split (user's call), so some pure unit-test classes travel
with them. That is the accepted cost of the move.
"""
