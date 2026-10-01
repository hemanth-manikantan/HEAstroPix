import pytest

from backend.scan_csv import generate_scan_steps, scan_csv_text, self_check
from backend.step_csv import parse_steps_csv, map_to_logical

MAP = {"Grid": 0, "Anode": 4, "Cathode": 5}


def small_matrix(**overrides):
    kw = dict(grid_min=400, grid_max=420, grid_step=10,              # 400, 410, 420          (3)
             anode_offset_min=20, anode_offset_max=40, anode_offset_step=10,   # 20, 30, 40     (3)
             cathode_offset_min=800, cathode_offset_max=900, cathode_offset_step=100,  # 800, 900 (2)
             hold_s=300)
    kw.update(overrides)
    return generate_scan_steps(**kw)


# ------------------------------------------------------------------ generate_scan_steps
def test_matrix_shape_and_ordering():
    steps = small_matrix()
    assert len(steps) == 3 * 3 * 2
    assert {s.grid for s in steps} == {400, 410, 420}
    assert {round(s.anode - s.grid) for s in steps} == {20, 30, 40}
    assert {round(s.cathode - s.anode) for s in steps} == {800, 900}
    # Grid-major, Anode-mid, Cathode-minor
    assert (steps[0].grid, steps[0].anode, steps[0].cathode, steps[0].hold_s) == (400, 420, 1220, 300)
    assert (steps[-1].grid, steps[-1].anode, steps[-1].cathode) == (420, 460, 1360)


def test_users_exact_example_matrix_size():
    # Grid 400-450/10 (6), Anode offset 20-150/10 (14), Cathode offset 800-1200/100 (5)
    steps = generate_scan_steps(grid_min=400, grid_max=450, grid_step=10,
                                anode_offset_min=20, anode_offset_max=150, anode_offset_step=10,
                                cathode_offset_min=800, cathode_offset_max=1200, cathode_offset_step=100,
                                hold_s=300)
    assert len(steps) == 6 * 14 * 5
    assert all(s.hold_s == 300 for s in steps)


@pytest.mark.parametrize("field", ["grid", "anode_offset", "cathode_offset"])
def test_step_le_zero_is_rejected(field):
    with pytest.raises(ValueError, match="step must be > 0"):
        small_matrix(**{f"{field}_step": 0})


@pytest.mark.parametrize("field", ["grid", "anode_offset", "cathode_offset"])
def test_max_below_min_is_rejected(field):
    with pytest.raises(ValueError, match="must be >="):
        small_matrix(**{f"{field}_max": -1000})


def test_zero_or_negative_hold_is_rejected():
    with pytest.raises(ValueError, match="hold time"):
        small_matrix(hold_s=0)


def test_anode_offset_below_min_delta_is_rejected_before_any_step_is_returned():
    # anode offset 10 < the default 20 V min-delta: every single combination breaks the rule
    with pytest.raises(ValueError, match="electrode rule"):
        small_matrix(anode_offset_min=10, anode_offset_max=10, anode_offset_step=10)


def test_min_delta_is_actually_applied_not_just_a_default():
    # the same scan is fine once the caller also lowers min_delta to match
    steps = small_matrix(anode_offset_min=10, anode_offset_max=10, anode_offset_step=10, min_delta=10)
    assert len(steps) == 1 * 3 * 2


def test_cathode_always_above_anode_by_construction():
    steps = small_matrix()
    assert all(s.cathode > s.anode for s in steps)


# ------------------------------------------------------------------ scan_csv_text / self_check
def test_generated_csv_round_trips_cleanly_through_run_steps_own_parser():
    steps = small_matrix()
    text = scan_csv_text(steps, MAP, daq="Y")
    assert self_check(text, MAP) == []

    parsed, errors = parse_steps_csv(text)
    assert errors == []
    assert len(parsed) == len(steps)
    assert all(s.daq is True for s in parsed)

    logical, errors = map_to_logical(parsed, {"Grid": "Ch0", "Anode": "Ch4", "Cathode": "Ch5"})
    assert errors == []
    assert logical[0][0] == {"Grid": 400.0, "Anode": 420.0, "Cathode": 1220.0}
    assert logical[0][1] == 300.0


@pytest.mark.parametrize("daq,expected", [("Y", True), ("N", False), (None, None)])
def test_daq_column_modes(daq, expected):
    steps = small_matrix()
    parsed, errors = parse_steps_csv(scan_csv_text(steps, MAP, daq=daq))
    assert errors == []
    assert all(s.daq is expected for s in parsed)


def test_comment_header_is_preserved_and_ignored_by_the_parser():
    steps = small_matrix()
    text = scan_csv_text(steps, MAP, comment="line one\nline two")
    assert text.startswith("# line one\n# line two\n")
    parsed, errors = parse_steps_csv(text)
    assert errors == [] and len(parsed) == len(steps)


def test_missing_electrode_in_channel_map_is_rejected():
    with pytest.raises(ValueError, match="Grid, Anode and Cathode"):
        scan_csv_text(small_matrix(), {"Grid": 0, "Anode": 4})
