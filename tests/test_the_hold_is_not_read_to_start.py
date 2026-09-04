"""A mission does not open by reading a hold the plan then throws away."""


def test_capacity_is_remembered_because_it_is_the_ship():
    """LIVE 2026-08-29. A run started while the fleet stood in a village and died on tick 2:
    "cannot open the main menu from 'village' (no ☰ there)".

    The opening move was `ReadHold`, which reads the fleet through the ☰ — a control that
    exists on the overworlds and nowhere else. It came back with two numbers, and the plan
    uses ONE of them: `free_space_for_barter(capacity, 0)` passes the cargo as literal zero,
    deliberately (user, 2026-08-27: subtracting it was circular and made the hold its own
    obstacle). Capacity is a property of the SHIP and constant for the mission.

    So the read fetched a constant, discarded the other value, and could only be done in
    half the places a mission might start. It is remembered now instead.
    """
    from memory.observed_facts import recall, remember

    remember("fleet_capacity", 4952)
    seen = recall("fleet_capacity")
    assert seen is not None and seen[0] == 4952


def test_the_hold_read_still_stamps_it_when_it_does_run():
    """The remembering has to happen where the reading does, or the fallback never fills."""
    import inspect

    from brain.activities import sea

    src = inspect.getsource(sea.read_the_hold)
    assert "fleet_capacity" in src, "a successful hold read must stamp the capacity"
