import random

import pytest

from backend.safety_rules import ordering_violations, plan_ramp_checkpoints

RATE = 10.0


def first_violation(cur, checkpoints, min_delta=20.0):
    """Ramp every channel at the same V/s toward each checkpoint in turn. The ordering margins are
    piecewise linear in time, so their extremes sit at channel arrival times: check exactly there."""
    state = dict(cur)
    for cp in checkpoints:
        dist = {n: abs(cp[n] - state[n]) for n in cp}
        times = sorted({0.0, *(d / RATE for d in dist.values())})
        for t in times:
            at_t = {n: state[n] + (1 if cp[n] >= state[n] else -1) * min(RATE * t, dist[n]) for n in cp}
            bad = ordering_violations(at_t, min_delta, tol=1e-6, floor=0.0)
            if bad:
                return at_t, bad
        state = dict(cp)
    return None


def random_state(rng, zero_ok=True, with_ff=False):
    g = 0.0 if (zero_ok and rng.random() < 0.2) else float(rng.randrange(0, 1000, 5))
    a = g + rng.choice([20, 20, 30, 50, 100, rng.uniform(20, 400)])
    c = a + rng.choice([0, 0.5, 20, 100, rng.uniform(0, 600)])
    s = {"Grid": g, "Anode": a, "Cathode": c}
    if with_ff:
        s["FF1"] = g + rng.uniform(20, 300)
    return s


@pytest.mark.parametrize("offset", [20.0, 50.0, 100.0])
@pytest.mark.parametrize("with_ff", [False, True])
def test_no_ordering_violation_at_any_instant_random_pairs(offset, with_ff):
    rng = random.Random(1234)
    for _ in range(400):
        cur = random_state(rng, with_ff=with_ff)
        if rng.random() < 0.15:
            cur = {n: 0.0 for n in cur}                      # power-on state
        tgt = random_state(rng, zero_ok=False, with_ff=with_ff)
        plan = plan_ramp_checkpoints(cur, tgt, offset=offset)
        assert plan[-1] == tgt or tgt == cur
        assert first_violation(cur, plan) is None, (cur, tgt, plan, first_violation(cur, plan))


def test_no_violation_through_staging_waypoint():
    rng = random.Random(99)
    for _ in range(300):
        cur = random_state(rng)
        tgt = random_state(rng, zero_ok=False)
        wp = {"Grid": 300.0, "Anode": 350.0, "Cathode": max(350.0, tgt["Cathode"])}
        plan = plan_ramp_checkpoints(cur, tgt, waypoints=[wp])
        assert first_violation(cur, plan) is None, (cur, tgt, plan)


def test_naive_direct_ramp_would_violate():
    """Sanity check that the property test can fail: ramping 0 -> target in one move breaks the rule."""
    cur = {"Grid": 0.0, "Anode": 0.0, "Cathode": 0.0}
    tgt = {"Grid": 300.0, "Anode": 350.0, "Cathode": 500.0}
    assert first_violation(cur, [tgt]) is not None


def test_zero_to_target_matches_the_described_procedure():
    cur = {"Grid": 0.0, "Anode": 0.0, "Cathode": 0.0}
    tgt = {"Grid": 300.0, "Anode": 350.0, "Cathode": 500.0}
    assert plan_ramp_checkpoints(cur, tgt, offset=50.0) == [
        {"Grid": 0.0, "Anode": 50.0, "Cathode": 50.0},        # Anode/Cathode lead first
        {"Grid": 300.0, "Anode": 350.0, "Cathode": 350.0},    # Grid reaches target with Anode/Cathode = Grid + offset
        {"Grid": 300.0, "Anode": 350.0, "Cathode": 500.0},    # then the final Anode/Cathode ramp
    ]
    assert plan_ramp_checkpoints(cur, tgt, offset=20.0)[0] == {"Grid": 0.0, "Anode": 20.0, "Cathode": 20.0}


def test_anode_already_high_is_not_pulled_down_while_grid_rises():
    cur = {"Grid": 100.0, "Anode": 300.0, "Cathode": 400.0}
    tgt = {"Grid": 200.0, "Anode": 350.0, "Cathode": 500.0}
    assert plan_ramp_checkpoints(cur, tgt, offset=50.0) == [
        {"Grid": 200.0, "Anode": 300.0, "Cathode": 400.0}, tgt]


def test_grid_down_is_a_single_move():
    cur = {"Grid": 400.0, "Anode": 450.0, "Cathode": 500.0}
    tgt = {"Grid": 300.0, "Anode": 350.0, "Cathode": 400.0}
    assert plan_ramp_checkpoints(cur, tgt) == [tgt]


def test_no_move_needed_gives_empty_plan():
    s = {"Grid": 300.0, "Anode": 350.0, "Cathode": 500.0}
    assert plan_ramp_checkpoints(s, dict(s)) == []


@pytest.mark.parametrize("bad_cur", [
    {"Grid": 325.0, "Anode": 325.0, "Cathode": 375.0},       # anode == grid (old manual table)
    {"Grid": 300.0, "Anode": 319.0, "Cathode": 400.0},
    {"Grid": 300.0, "Anode": 350.0, "Cathode": 340.0},
])
def test_refuses_to_plan_from_a_state_that_already_breaks_the_rule(bad_cur):
    with pytest.raises(ValueError, match="current state"):
        plan_ramp_checkpoints(bad_cur, {"Grid": 400.0, "Anode": 450.0, "Cathode": 500.0})


def test_refuses_bad_target_waypoint_offset_or_channel_mismatch():
    cur = {"Grid": 0.0, "Anode": 0.0, "Cathode": 0.0}
    with pytest.raises(ValueError, match="target"):
        plan_ramp_checkpoints(cur, {"Grid": 300.0, "Anode": 310.0, "Cathode": 500.0})
    with pytest.raises(ValueError, match="offset"):
        plan_ramp_checkpoints(cur, {"Grid": 300.0, "Anode": 350.0, "Cathode": 500.0}, offset=10.0)
    with pytest.raises(ValueError, match="channel sets"):
        plan_ramp_checkpoints(cur, {"Grid": 300.0, "Anode": 350.0})


# ---------------------------------------------------------------- ordering_violations
def test_ordering_boundaries():
    assert ordering_violations({"Grid": 300.0, "Anode": 320.0, "Cathode": 320.0}) == []
    assert ordering_violations({"Grid": 300.0, "Anode": 319.99, "Cathode": 400.0})
    assert ordering_violations({"Grid": 300.0, "Anode": 315.0, "Cathode": 400.0}, tol=5.0) == []
    assert ordering_violations({"Grid": 300.0, "Anode": 350.0, "Cathode": 349.0})
    assert ordering_violations({"Grid": 300.0, "Anode": 350.0, "Cathode": 349.0}, tol=1.5) == []


def test_strict_cathode_only_for_targets():
    s = {"Grid": 300.0, "Anode": 350.0, "Cathode": 350.0}
    assert ordering_violations(s) == []
    assert ordering_violations(s, strict_cathode=True)


def test_exempt_while_grid_at_or_below_floor():
    assert ordering_violations({"Grid": 0.0, "Anode": 0.0, "Cathode": 0.0}) == []
    assert ordering_violations({"Grid": 4.0, "Anode": 0.0, "Cathode": 0.0}, floor=5.0) == []
    assert ordering_violations({"Grid": 6.0, "Anode": 0.0, "Cathode": 0.0}, floor=5.0)
    assert ordering_violations({"Grid": 0.0, "Anode": 0.0, "Cathode": 0.0}, floor=-1.0)   # targets: always enforced


def test_channels_other_than_anode_cathode_follow_the_anode_rule():
    assert ordering_violations({"Grid": 300.0, "Anode": 350.0, "FF1": 310.0})
    assert ordering_violations({"Grid": 300.0, "Anode": 350.0, "FF1": 330.0}) == []
