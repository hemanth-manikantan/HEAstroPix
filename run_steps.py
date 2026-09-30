#!/usr/bin/env python
"""Run a CSV step sequence (ramp -> settle -> hold/DAQ -> next step -> ramp down) without the UI.

CSV rows: ch,V,ch,V,...,hold_s[,DAQ Y/N]   (see example_steps.csv)

  --dry-run    validate the CSV + channel map and print the plan. No hardware, no caen_libs.
  --simulate   run the whole sequence against a fake crate in virtual time (DAQ not executed).
  (neither)    REAL HARDWARE. Shows the live crate state and asks you to type 'yes' first.

Exit codes: 0 done | 1 aborted (safe state verified) | 2 refused/config error (HV untouched)
            3 safety trip (safe state verified) | 4 COULD NOT VERIFY SAFE STATE - check the crate
Run it inside tmux/screen: if the process is SIGKILLed the HV is left ON and unmonitored.
"""
import argparse
import importlib.util
import logging
import shlex
import signal
import sys
from datetime import datetime
from pathlib import Path

from backend.safety_rules import ordering_violations
from run_daq import check_daq_inputs
from backend.step_csv import parse_steps_csv, map_to_logical
from backend.step_runner import (StepRunner, RealClock, build_daq_argv, describe_plan,
                                 EXIT_REFUSED)


def parse_channel_map(text):
    try:
        pairs = [item.split("=") for item in text.split(",")]
        mapping = {name.strip(): int(ch) for name, ch in pairs}
    except ValueError:
        raise argparse.ArgumentTypeError("use NAME=CH pairs, e.g. Grid=0,Anode=1,Cathode=2")
    if len(set(mapping.values())) != len(mapping) or not all(0 <= c <= 5 for c in mapping.values()):
        raise argparse.ArgumentTypeError("channels must be distinct and within 0-5")
    return mapping


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("steps_csv")
    p.add_argument("--channel-map", required=True, type=parse_channel_map,
                   help="REQUIRED, no default (wrong mapping = wrong electrode). e.g. Grid=0,Anode=1,Cathode=2")
    p.add_argument("--ip", default="192.168.0.1")
    p.add_argument("--daq-cmd", help="command run during each hold; placeholders {step} {duration} {outdir} "
                                     "and lowercase channel names e.g. {grid}. Exit code 0 = success.")
    p.add_argument("--daq-backup", help="Timepix3 DAC backup (.TPX3), full path; with --daq-mask and --daq-eq, runs run_daq.py "
                                        "for each step for the CSV hold time (alternative to --daq-cmd)")
    p.add_argument("--daq-mask", help="Timepix3 mask (.h5), full path")
    p.add_argument("--daq-eq", help="Timepix3 equalisation (.h5), full path")
    p.add_argument("--daq-grace-s", type=float, default=120.0, help="extra time allowed past the hold before the DAQ is killed")
    p.add_argument("--daq-continuous", action="store_true",
                   help="extend an acquiring step's DAQ to also cover the ramp into it, not just its hold, for "
                        "continuous monitoring during ramp-up/down. Requires --daq-backup+--daq-mask+--daq-eq "
                        "(not a raw --daq-cmd). The final ramp-to-zero is never covered.")
    p.add_argument("--daq-startup-grace-s", type=float, default=5.0,
                   help="with --daq-continuous: seconds to wait after launching DAQ, checking it stays alive, "
                        "before starting to ramp; tune to your hardware's actual chip-init time")
    p.add_argument("--power-on", action="store_true", help="allow the script to power ON mapped channels (at 0 V) if they are OFF")
    p.add_argument("--keep-power", action="store_true", help="leave channels powered ON (at 0 V) at the end")
    p.add_argument("--min-delta", type=float, default=20.0,
                   help="Anode/Cathode (all non-Grid channels) must always be at least this far above Grid (V)")
    p.add_argument("--stage-offset", type=float, default=50.0,
                   help="while Grid ramps up, Anode/Cathode lead it by this much (V); must be >= --min-delta")
    p.add_argument("--delta-meas-tol", type=float, default=5.0,
                   help="VMon noise allowance when checking the --min-delta rule on MEASURED voltages (V)")
    p.add_argument("--max-current-ua", type=float, default=5.0)
    p.add_argument("--v-tol", type=float, default=10.0, help="|VMon-target| for a step to count as settled (V)")
    p.add_argument("--settle-s", type=float, default=5.0, help="time VMon must stay within --v-tol before holding")
    p.add_argument("--hold-v-tol", type=float, default=50.0, help="trip if VMon drifts this far from target during a hold (V)")
    p.add_argument("--zero-tol", type=float, default=5.0, help="|VMon| below this counts as 0 V (V)")
    p.add_argument("--on-trip", choices=["shutdown", "ramp"], default="shutdown",
                   help="shutdown = backend shutdown() (V=0, 2 s, Pw off; same as the UI). ramp = controlled ramp at 10 V/s")
    p.add_argument("--outdir", help="where run.log, steps.csv, hv_log_*.csv and step_data_files.csv are written "
                                    "(default: ~/HEAstroPix_runs/<timestamp>, outside the project checkout)")
    p.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--simulate", action="store_true")
    return p


def setup_logging(outdir):
    logging.raiseExceptions = False  # a closed terminal must not break the safety path
    log = logging.getLogger("hv_steps")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%H:%M:%S")
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(outdir / "run.log")):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    return log


def main(argv=None):
    args = build_parser().parse_args(argv)

    steps_parsed, errors = parse_steps_csv(Path(args.steps_csv).read_text(encoding="utf-8-sig"))
    steps = []
    if args.stage_offset < args.min_delta:
        errors.append(f"--stage-offset ({args.stage_offset:g}) must be >= --min-delta ({args.min_delta:g})")
    if not errors:
        rule = lambda t: ordering_violations(t, args.min_delta, tol=0.0, floor=-1.0, strict_cathode=True)
        steps, errors = map_to_logical(steps_parsed, {n: f"Ch{i}" for n, i in args.channel_map.items()}, check=rule)
    if "Grid" not in args.channel_map:
        errors.append("channel map must include Grid")
    daq_files = [args.daq_backup, args.daq_mask, args.daq_eq]
    if args.daq_continuous and not all(daq_files):
        errors.append("--daq-continuous requires --daq-backup, --daq-mask and --daq-eq (not a raw --daq-cmd)")
    if any(daq_files):
        if args.daq_cmd:
            errors.append("use either --daq-cmd or --daq-backup/--daq-mask/--daq-eq, not both")
        elif not all(daq_files):
            errors.append("--daq-backup, --daq-mask and --daq-eq must all be given together")
        else:
            errors += check_daq_inputs(*daq_files)
            cmd_parts = [sys.executable, str(Path(__file__).with_name("run_daq.py")),
                        "--backup", args.daq_backup, "--mask", args.daq_mask, "--equalisation", args.daq_eq]
            # continuous mode is one subprocess for the WHOLE sequence: no per-step {step}/{duration} to fill in
            cmd_parts += ["--continuous"] if args.daq_continuous else ["--duration", "{duration}", "--step", "{step}"]
            args.daq_cmd = shlex.join(cmd_parts)
    # --simulate never executes any DAQ command, so a command's validity there is moot (see describe_plan below
    # for what WOULD run for real; here we only need to know whether one is configured for that real run).
    real_daq_cmd = None if args.simulate else args.daq_cmd
    if real_daq_cmd:
        try:
            build_daq_argv(real_daq_cmd, step=1, duration="1", outdir=".", **{n.lower(): "0" for n in args.channel_map})
        except ValueError as e:
            errors.append(str(e))
    for k, (_, _, want_daq) in enumerate(steps, 1):
        # under --simulate no DAQ command ever runs anyway (see below), so a CSV forcing Y is not an error there
        if want_daq is True and not real_daq_cmd and not args.simulate:
            errors.append(f"step {k}: CSV column forces a DAQ acquisition (Y) but no --daq-cmd/"
                          "--daq-backup+--daq-mask+--daq-eq was given")
    if errors:
        print("Cannot run:\n" + "\n".join(f"  - {e}" for e in errors), file=sys.stderr)
        return EXIT_REFUSED

    try:
        lines, total = describe_plan(steps, args.channel_map, args.settle_s, args.stage_offset, args.min_delta,
                                     daq_cmd=args.daq_cmd, daq_continuous=args.daq_continuous,
                                     daq_startup_grace_s=args.daq_startup_grace_s)
    except ValueError as e:
        print(f"Cannot run:\n  - {e}", file=sys.stderr)
        return EXIT_REFUSED
    print("\n".join(lines))
    print(f"Rule: Anode/Cathode >= Grid + {args.min_delta:g} V at all times (measured tol {args.delta_meas_tol:g} V); "
          f"Cathode >= Anode; while Grid rises they lead it by {args.stage_offset:g} V")
    print(f"Approx. duration: {total / 3600:.2f} h | max current {args.max_current_ua} uA | settle +/-{args.v_tol} V for {args.settle_s} s "
          f"| hold drift trip {args.hold_v_tol} V | on trip: {args.on_trip}")
    print(f"DAQ: {args.daq_cmd or 'none (hold only)'}" + (" [not executed in --simulate]" if args.simulate else ""))
    if args.dry_run:
        return 0

    if not args.simulate and importlib.util.find_spec("caen_libs") is None:
        print(f"caen_libs is not installed in this Python ({sys.executable}). Use the environment that runs "
              "the dashboard (hypex2). Nothing was connected.", file=sys.stderr)
        return EXIT_REFUSED

    outdir = Path(args.outdir or Path.home() / "HEAstroPix_runs" / datetime.now().strftime("%Y%m%d_%H%M%S"))
    outdir.mkdir(parents=True, exist_ok=False)
    (outdir / "steps.csv").write_text(Path(args.steps_csv).read_text(encoding="utf-8-sig"))
    log = setup_logging(outdir)
    log.info("outputs in %s", outdir)

    if args.simulate:
        from backend.simulated_hv import SimulatedDetectorHV, SimClock, install_clock
        clock = SimClock()
        install_clock(clock)
        hv = SimulatedDetectorHV(clock=clock)
        daq_cmd, daq_continuous = None, False  # --simulate never runs a real DAQ command
        # A CSV forcing Y is allowed under --simulate (checked above), but StepRunner's own constructor still
        # requires daq_cmd whenever a step is True; since nothing runs anyway, drop to "no override" here only.
        runner_steps = [(t, h, None if want is True else want) for t, h, want in steps]
    else:
        from backend.hvlogic import DetectorHV
        clock, hv, daq_cmd, daq_continuous = RealClock(), DetectorHV(args.ip), args.daq_cmd, args.daq_continuous
        runner_steps = steps

    runner = StepRunner(
        hv, args.channel_map, runner_steps, outdir, clock=clock, log=log, max_current_ua=args.max_current_ua,
        v_tol=args.v_tol, hold_v_tol=args.hold_v_tol, settle_s=args.settle_s, zero_tol=args.zero_tol,
        on_trip=args.on_trip, daq_cmd=daq_cmd, daq_grace_s=args.daq_grace_s,
        power_on=args.power_on or args.simulate, keep_power=args.keep_power,
        min_delta=args.min_delta, stage_offset=args.stage_offset, delta_meas_tol=args.delta_meas_tol,
        daq_continuous=daq_continuous, daq_startup_grace_s=args.daq_startup_grace_s)

    def handler(signum, frame):
        if not runner.hv_active:
            raise KeyboardInterrupt  # nothing energised yet: plain exit
        runner.request_abort(signal.Signals(signum).name)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handler)

    def confirm():
        if args.yes or args.simulate:
            return True
        try:
            return input("Type 'yes' to start (Ctrl-C afterwards ramps down to 0 V): ").strip().lower() == "yes"
        except EOFError:
            return False

    try:
        code = runner.run(confirm)
    except KeyboardInterrupt:
        log.info("interrupted before any HV was applied")
        code = 130
    log.info("exit code %d", code)
    if code == 4:
        log.critical("!!! SAFE STATE NOT VERIFIED - CHECK THE CRATE BEFORE ANYTHING ELSE !!!")
    return code


if __name__ == "__main__":
    sys.exit(main())
