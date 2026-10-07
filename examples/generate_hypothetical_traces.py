"""Expand deterministic workload phases into a synthetic request-count CSV."""

import argparse
import csv
import json
import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def page_ids(config):
    pages = config["pages"]
    if (not isinstance(pages, list) or not pages
            or any(not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in pages)
            or len(set(pages)) != len(pages)):
        raise ValueError("pages must be a nonempty list of unique IDs using letters, digits, _ or -")
    return pages


def generate(config):
    pages = page_ids(config)
    duration = config["window_ms"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("window_ms must be a positive finite number")
    rows = []
    for scenario, phases in config["scenarios"].items():
        if not scenario or not phases:
            raise ValueError("Each scenario needs a name and at least one phase")
        window = 1
        for phase in phases:
            if type(phase["windows"]) is not int or phase["windows"] < 1:
                raise ValueError(f"{scenario}: windows must be a positive integer")
            counts = phase["requests"]
            if not isinstance(counts, dict) or set(counts) != set(pages):
                raise ValueError(f"{scenario}: requests must specify every configured page exactly once")
            if any(type(value) is not int or value < 0 for value in counts.values()):
                raise ValueError(f"{scenario}: request counts must be nonnegative integers")
            for _ in range(phase["windows"]):
                rows.append({
                    "scenario": scenario,
                    "window": window,
                    "start_ms": (window - 1) * duration,
                    "end_ms": window * duration,
                    **{f"requests_{page}": counts[page] for page in pages},
                })
                window += 1
    if not rows:
        raise ValueError("At least one scenario is required")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "experiment.json")
    parser.add_argument("--output", type=Path, default=ROOT / "hypothetical_traces.csv")
    args = parser.parse_args()
    rows = generate(json.loads(args.config.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Generated {len(rows)} windows: {args.output}")


if __name__ == "__main__":
    main()
