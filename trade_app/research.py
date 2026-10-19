"""Offline command line tools for point-in-time research and replay."""

import argparse
import json
import math
import os
from pathlib import Path
import sys

from .datasets import MAX_TIMESTAMP, load_dataset
from .decision_trace import build_replay_trace
from .research_runs import run_replay_experiment, run_shadow


MAX_INPUT_BYTES = 100 * 1024 * 1024


class ResearchError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ResearchError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ResearchError(f"non-finite JSON number is not allowed: {value}")


def _read_json(path, expected, optional=False):
    if optional and path is None:
        return None
    source = Path(path)
    try:
        if not source.is_file():
            raise ResearchError(f"input must be a regular file: {source}")
        size = source.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise ResearchError(f"input exceeds {MAX_INPUT_BYTES} bytes: {source}")
        raw = source.read_bytes()
        if len(raw) > MAX_INPUT_BYTES:
            raise ResearchError(f"input exceeds {MAX_INPUT_BYTES} bytes: {source}")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except ResearchError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchError(f"cannot read JSON input {source}: {exc}") from None
    if not isinstance(value, expected):
        label = "object" if expected is dict else "array"
        raise ResearchError(f"{source} top-level value must be an {label}")
    return value


def _dataset(path):
    payload = _read_json(path, dict)
    try:
        return load_dataset(payload)
    except (TypeError, ValueError) as exc:
        raise ResearchError(f"invalid dataset: {exc}") from None


def _positive_epoch(value, name="as-of"):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"{name} must be a positive epoch integer") from None
    if str(result) != str(value) or result < 1 or result > MAX_TIMESTAMP:
        raise argparse.ArgumentTypeError(f"{name} must be a positive epoch integer")
    return result


def _minutes(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("minutes must be an integer in 0..1440") from None
    if str(number) != str(value) or number < 0 or number > 1440:
        raise argparse.ArgumentTypeError("minutes must be an integer in 0..1440")
    return number


def _serialize(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")) + "\n"
    except (TypeError, ValueError, OverflowError) as exc:
        raise ResearchError(f"result cannot be encoded as finite JSON: {exc}") from None


def _write_new(path, serialized):
    if path is None:
        sys.stdout.write(serialized)
        return
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as output:
            output.write(serialized)
    except FileExistsError:
        raise ResearchError(f"output already exists: {path}") from None
    except OSError as exc:
        raise ResearchError(f"cannot create output {path}: {exc}") from None


def _write_many_new(outputs):
    opened, created = [], []
    try:
        for path, serialized in outputs:
            handle = open(path, "x", encoding="utf-8", newline="\n")
            opened.append(handle)
            created.append(path)
        for handle, (_, serialized) in zip(opened, outputs):
            handle.write(serialized)
    except FileExistsError as exc:
        for handle in opened:
            handle.close()
        for path in created:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise ResearchError(f"output already exists: {exc.filename}") from None
    except OSError as exc:
        for handle in opened:
            handle.close()
        for path in created:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise ResearchError(f"cannot create outputs: {exc}") from None
    else:
        for handle in opened:
            handle.close()


def _check_distinct_outputs(first, second):
    if first and second and Path(first).resolve() == Path(second).resolve():
        raise ResearchError("output and trace-output resolve to the same path")


def _manifest(dataset):
    data = dataset.to_dict()
    quotes = data["quotes"]
    return {"dataset_hash": dataset.dataset_hash, "source": data["source"],
            "instrument": data["instrument"],
            "counts": {"quotes": len(quotes), "bars": {k: len(v) for k, v in data["bars"].items()},
                       "evidence": len(data["evidence"])},
            "time_bounds": {"quotes": [quotes[0]["time"], quotes[-1]["time"]],
                            "bars": {k: ([v[0]["time"], v[-1]["time"]] if v else None)
                                     for k, v in data["bars"].items()},
                            "evidence": ([min(e["published_at"] for e in data["evidence"]),
                                          max(e["available_at"] for e in data["evidence"])]
                                         if data["evidence"] else None)}}


def _build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m trade_app.research")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a dataset and print its manifest")
    validate.add_argument("--dataset", required=True)
    snap = commands.add_parser("snapshot", help="export a point-in-time dataset snapshot")
    snap.add_argument("--dataset", required=True); snap.add_argument("--as-of", required=True, type=_positive_epoch)
    snap.add_argument("--output")
    replay = commands.add_parser("replay", help="run an offline historical replay")
    replay.add_argument("--dataset", required=True); replay.add_argument("--proposals", required=True)
    replay.add_argument("--settings"); replay.add_argument("--balance", type=float, default=10000.0)
    replay.add_argument("--latency", type=int, default=0); replay.add_argument("--leave-open", action="store_true")
    replay.add_argument("--calendar-before", type=_minutes, default=0); replay.add_argument("--calendar-after", type=_minutes, default=0)
    replay.add_argument("--output"); replay.add_argument("--trace-output")
    shadow = commands.add_parser("shadow", help="validate advisory proposals without execution")
    shadow.add_argument("--dataset", required=True); shadow.add_argument("--proposals", required=True)
    shadow.add_argument("--as-of", required=True, type=_positive_epoch)
    shadow.add_argument("--calendar-before", type=_minutes, default=0); shadow.add_argument("--calendar-after", type=_minutes, default=0)
    shadow.add_argument("--output")
    evaluate = commands.add_parser("evaluate", help="run causal walk-forward strategy evaluation")
    evaluate.add_argument("--dataset", required=True); evaluate.add_argument("--strategies")
    evaluate.add_argument("--train-bars", type=int, default=600)
    evaluate.add_argument("--validation-bars", type=int, default=250)
    evaluate.add_argument("--test-bars", type=int, default=100)
    evaluate.add_argument("--purge-bars", type=int, default=1)
    evaluate.add_argument("--embargo-bars", type=int, default=1)
    evaluate.add_argument("--balance", type=float, default=10000.0)
    evaluate.add_argument("--latency", type=int, default=0)
    evaluate.add_argument("--calendar-before", type=_minutes, default=0)
    evaluate.add_argument("--calendar-after", type=_minutes, default=0)
    evaluate.add_argument("--settings")
    evaluate.add_argument("--output")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        dataset = _dataset(args.dataset)
        if args.command == "validate":
            serialized = _serialize(_manifest(dataset))
            _write_new(None, serialized)
            return 0
        if args.command == "snapshot":
            serialized = _serialize(dataset.snapshot(args.as_of))
            _write_new(args.output, serialized)
            return 0
        if args.command == "shadow":
            proposals = _read_json(args.proposals, list)
            result = run_shadow(dataset, proposals, args.as_of, args.calendar_before, args.calendar_after)
            _write_new(args.output, _serialize(result))
            return 0
        if args.command == "evaluate":
            from .evaluation import evaluate_dataset
            settings = _read_json(args.settings, dict, optional=True)
            strategies = args.strategies.split(",") if args.strategies else None
            result = evaluate_dataset(dataset, strategies=strategies, settings=settings,
                train_bars=args.train_bars, validation_bars=args.validation_bars,
                test_bars=args.test_bars, purge_bars=args.purge_bars,
                embargo_bars=args.embargo_bars, initial_balance=args.balance,
                latency_seconds=args.latency, calendar_before_minutes=args.calendar_before,
                calendar_after_minutes=args.calendar_after)
            _write_new(args.output, _serialize(result))
            return 0
        _check_distinct_outputs(args.output, args.trace_output)
        proposals = _read_json(args.proposals, list)
        settings = _read_json(args.settings, dict, optional=True)
        if isinstance(args.latency, bool) or args.latency < 0:
            raise ResearchError("latency must be a non-negative integer")
        if not math.isfinite(args.balance) or args.balance <= 0:
            raise ResearchError("balance must be a positive finite number")
        result = run_replay_experiment(dataset, proposals, settings=settings, initial_balance=args.balance,
            latency_seconds=args.latency, close_at_end=not args.leave_open,
            calendar_before_minutes=args.calendar_before, calendar_after_minutes=args.calendar_after)
        output_text = _serialize(result)
        trace_text = _serialize(build_replay_trace(dataset, result)) if args.trace_output else None
        if args.output:
            outputs = [(args.output, output_text)]
            if trace_text is not None:
                outputs.append((args.trace_output, trace_text))
            _write_many_new(outputs)
            sys.stdout.write(_serialize({"output": args.output, "run_hash": result["run_hash"],
                "accepted": len(result["accepted"]), "rejected": len(result["rejected"]),
                "closed_trades": result["metrics"]["closed_trades"]}))
        elif args.trace_output:
            _write_new(args.trace_output, trace_text)
            sys.stdout.write(output_text)
        else:
            sys.stdout.write(output_text)
        return 0
    except (ResearchError, OSError, TypeError, ValueError, OverflowError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
