# brain/goals — Goal-driven task execution.
#
# Each goal class implements a tick-driven dispatch table:
#   perceive() → (state, goal) → one action → return to perceive()
#
# The task runner drives the tick loop.  Goals never contain internal
# polling loops — they dispatch one action per tick and return.
