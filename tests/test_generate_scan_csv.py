import generate_scan_csv as cli
from backend.step_csv import parse_steps_csv

ARGS = [
    "--channel-map", "Grid=0,Anode=4,Cathode=5",
    "--grid-min", "400", "--grid-max", "420", "--grid-step", "10",
    "--anode-offset-min", "20", "--anode-offset-max", "40", "--anode-offset-step", "10",
    "--cathode-offset-min", "800", "--cathode-offset-max", "900", "--cathode-offset-step", "100",
    "--hold-s", "300",
]


def test_writes_a_loadable_csv_and_reports_a_summary(tmp_path, capsys):
    out = tmp_path / "scan.csv"
    assert cli.main([str(out), *ARGS]) == 0
    out_text = capsys.readouterr().out
    assert "Wrote 18 steps (3 x 3 x 2 Grid x Anode-offset x Cathode-offset)" in out_text
    assert "First step: Grid=400 Anode=420 Cathode=1220" in out_text
    assert "Last step:  Grid=420 Anode=460 Cathode=1360" in out_text

    parsed, errors = parse_steps_csv(out.read_text())
    assert errors == []
    assert len(parsed) == 18
    assert all(s.daq is True for s in parsed)   # default --daq Y


def test_daq_none_omits_the_column(tmp_path):
    out = tmp_path / "scan.csv"
    assert cli.main([str(out), *ARGS, "--daq", "none"]) == 0
    parsed, errors = parse_steps_csv(out.read_text())
    assert errors == [] and all(s.daq is None for s in parsed)


def test_daq_n_forces_skip(tmp_path):
    out = tmp_path / "scan.csv"
    assert cli.main([str(out), *ARGS, "--daq", "N"]) == 0
    parsed, errors = parse_steps_csv(out.read_text())
    assert errors == [] and all(s.daq is False for s in parsed)


def test_refuses_to_overwrite_without_force(tmp_path):
    out = tmp_path / "scan.csv"
    out.write_text("preexisting content\n")
    assert cli.main([str(out), *ARGS]) == 3
    assert out.read_text() == "preexisting content\n"   # untouched
    assert cli.main([str(out), *ARGS, "--force"]) == 0
    assert out.read_text() != "preexisting content\n"


def test_rule_breaking_range_is_refused_before_writing_anything(tmp_path, capsys):
    out = tmp_path / "scan.csv"
    args = [a for a in ARGS]
    args[args.index("20")] = "10"   # anode offset min below the 20 V min-delta default
    assert cli.main([str(out), *args]) == 2
    assert not out.exists()
    assert "electrode rule" in capsys.readouterr().err


def test_channel_map_must_be_exactly_grid_anode_cathode(tmp_path):
    out = tmp_path / "scan.csv"
    args = [a if a != "Grid=0,Anode=4,Cathode=5" else "Grid=0,Anode=4" for a in ARGS]
    assert cli.main([str(out), *args]) == 1
    assert not out.exists()


def test_zero_hold_is_refused(tmp_path):
    out = tmp_path / "scan.csv"
    args = [a for a in ARGS]
    args[args.index("300")] = "0"
    assert cli.main([str(out), *args]) == 2
    assert not out.exists()
