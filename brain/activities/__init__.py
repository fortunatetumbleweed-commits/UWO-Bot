"""Activities — the worlds the bot can be resumed in and do work in.

An activity does LOCALIZED perception on a screen it already knows it is on, runs bounded
goal-directed work to completion, and hands back. It never causes a transition, and it never
claims to know where the bot ended up. See docs/architecture_DRAFT.md.
"""
