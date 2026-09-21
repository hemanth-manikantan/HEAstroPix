import pytest

from backend.step_csv import parse_steps_csv, map_to_logical, grid_offset_violations

MAP3 = {"Grid": "Ch0", "Anode": "Ch1", "Cathode": "Ch2"}


def test_parses_pairs_and_hold():
    steps, errors = parse_steps_csv("0,300,1,350,2,350,600\n0,325,1,375,2,375,1800\n")
    assert errors == []
    assert [s.voltages for s in steps] == [{0: 300.0, 1: 350.0, 2: 350.0}, {0: 325.0, 1: 375.0, 2: 375.0}]
    assert [s.hold_s for s in steps] == [600.0, 1800.0]
    assert [s.line for s in steps] == [1, 2]


def test_comments_blank_lines_header_and_trailing_commas():
    text = "# comment\n\nch,V,ch,V,hold\n0,300,1,350,600,,\n"
    steps, errors = parse_steps_csv(text)
    assert errors == []
    assert len(steps) == 1 and steps[0].line == 4


@pytest.mark.parametrize("row, fragment", [
    ("0,300,1,350", "odd number"),            # no hold time
    ("0,300,", "odd number"),                  # trailing commas stripped -> 2 fields
    ("0,,1,350,600", "empty field"),
    ("7,300,600", "outside 0-5"),
    ("0,300,0,350,600", "twice"),
    ("0,abc,600", "not a number"),
    ("0,-5,600", "outside 0-9000"),
    ("0,9001,600", "outside 0-9000"),
    ("0,300,0", "must be > 0"),
    ("0,300,-10", "must be > 0"),
    ("0,300,soon", "hold time"),
    ("0,nan,600", "not a number"),
    ("0,300,inf", "hold time"),
    ("1.5,300,600", "not an integer"),
])
def test_rejects_bad_rows(row, fragment):
    steps, errors = parse_steps_csv(row + "\n")
    assert steps == []
    assert any(fragment in e for e in errors), errors


def test_one_bad_row_reports_line_number_and_no_steps_used():
    steps, errors = parse_steps_csv("0,300,600\n0,300,x\n")
    assert errors and errors[0].startswith("line 2:")


def test_empty_file():
    assert parse_steps_csv("# only comments\n")[1] == ["no steps found in file"]


def test_max_v_and_channel_count_are_parameters():
    _, errors = parse_steps_csv("3,100,60\n", n_channels=3)
    assert errors
    _, errors = parse_steps_csv("0,1500,60\n", max_v=1000.0)
    assert errors


def test_map_to_logical_orders_by_map_and_carries_hold():
    steps, _ = parse_steps_csv("2,350,1,350,0,300,60\n")
    out, errors = map_to_logical(steps, MAP3)
    assert errors == []
    assert out == [({"Grid": 300.0, "Anode": 350.0, "Cathode": 350.0}, 60.0)]
    assert list(out[0][0]) == ["Grid", "Anode", "Cathode"]


def test_map_rejects_unmapped_and_missing_channels():
    steps, _ = parse_steps_csv("0,300,1,350,60\n0,300,1,350,2,350,5,350,60\n")
    out, errors = map_to_logical(steps, MAP3)
    assert out == []
    assert "missing" in errors[0] and "line 1" in errors[0]
    assert "not in the current channel map" in errors[1] and "line 2" in errors[1]


def test_map_applies_builder_grid_offset_rule():
    steps, _ = parse_steps_csv("0,300,1,305,2,350,60\n")
    out, errors = map_to_logical(steps, MAP3)
    assert out == [] and "Anode" in errors[0]


def test_grid_offset_rule_matches_builder_boundary():
    assert grid_offset_violations({"Grid": 300.0, "Anode": 310.0}) == []
    assert grid_offset_violations({"Grid": 300.0, "Anode": 309.9}) == ["Anode"]
    assert grid_offset_violations({"Anode": 0.0}) == ["Anode"]  # no Grid -> Grid=0; same as the builder


def test_map_to_logical_accepts_a_custom_check():
    steps, _ = parse_steps_csv("0,300,1,350,2,350,60\n")
    out, errors = map_to_logical(steps, MAP3, check=lambda t: ["nope"])
    assert out == [] and errors == ["line 1: nope"]
    out, errors = map_to_logical(steps, MAP3, check=lambda t: [])
    assert errors == [] and len(out) == 1
