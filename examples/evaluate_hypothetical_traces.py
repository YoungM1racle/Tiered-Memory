"""Evaluate synthetic request counts with the serial model in updates.md.

Run with Python 3; uses only the standard library. This is an illustrative
cost calculation, not a DAMON parser or a timed/resource-contention simulator.
"""

import argparse
import csv
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from generate_hypothetical_traces import page_ids

ROOT = Path(__file__).resolve().parent

def load_model(config):
    model = dict(config["model"])
    model["pages"] = page_ids(config)
    for key in ("page_bytes", "migration_budget_bytes", "dram_capacity_bytes", "cxl_capacity_bytes"):
        minimum = 1 if key == "page_bytes" else 0
        if type(model[key]) is not int or model[key] < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
    initial = model["initial_dram_pages"]
    if (not isinstance(initial, list) or any(p not in model["pages"] for p in initial)
            or len(set(initial)) != len(initial)):
        raise ValueError("initial_dram_pages must list unique configured page IDs")
    if len(initial) * model["page_bytes"] > model["dram_capacity_bytes"]:
        raise ValueError("Initial DRAM pages exceed DRAM capacity")
    if (len(model["pages"]) - len(initial)) * model["page_bytes"] > model["cxl_capacity_bytes"]:
        raise ValueError("Initial CXL pages exceed CXL capacity; all other pages start in CXL")
    for key in ("dram_latency_us", "cxl_latency_us", "copy_bandwidth_bytes_s",
                "promotion_overhead_us", "demotion_overhead_us"):
        value = Decimal(str(model[key]))
        if not value.is_finite() or value < 0:
            raise ValueError(f"{key} must be finite and nonnegative")
        model[key] = value
    if model["copy_bandwidth_bytes_s"] == 0:
        raise ValueError("copy_bandwidth_bytes_s must be positive")
    return model


def evaluate(windows, policy, model):
    if policy not in ("static", "last_window_hotness"):
        raise ValueError(f"Unknown policy: {policy}")
    pages = model["pages"]
    dram = set(model["initial_dram_pages"])
    page_bytes = model["page_bytes"]
    copy_us = Decimal(page_bytes) / model["copy_bandwidth_bytes_s"] * 1_000_000
    promotion_us = model["promotion_overhead_us"] + copy_us
    demotion_us = model["demotion_overhead_us"] + copy_us
    access_us = Decimal(0)
    migration_us = Decimal(0)
    promotions = demotions = 0
    log = []
    for index, window in enumerate(windows):
        counts = {page: int(window[f"requests_{page}"]) for page in pages}
        serving_pages = [p for p in pages if p in dram]
        window_access = sum(
            counts[page] * model["dram_latency_us" if page in dram else "cxl_latency_us"]
            for page in counts
        )
        access_us += window_access
        actions = []
        window_migration = Decimal(0)
        # Observe this completed window, then move for the next one.
        # Keep the current placement on ties; do not move at the horizon.
        if policy == "last_window_hotness" and index + 1 < len(windows):
            budget = model["migration_budget_bytes"]
            # Stable sort: equal-hotness candidates follow the configured page order.
            candidates = sorted((p for p in pages if p not in dram), key=lambda p: -counts[p])
            for candidate in candidates:
                if counts[candidate] == 0:
                    break
                free_dram = (len(dram) + 1) * page_bytes <= model["dram_capacity_bytes"]
                victim = None
                if not free_dram:
                    if not dram:
                        actions.append("blocked: DRAM cannot hold a page")
                        break
                    victim = min((p for p in pages if p in dram), key=lambda p: counts[p])
                    if counts[candidate] <= counts[victim]:
                        break  # Keep existing residents on ties.
                needed_bytes = page_bytes * (2 if victim is not None else 1)
                if budget < needed_bytes:
                    actions.append("blocked: migration budget")
                    break
                # Demote first when DRAM is full. Never assume a free swap
                # between two full tiers or release a source before copying.
                if victim is not None:
                    if (len(pages) - len(dram) + 1) * page_bytes > model["cxl_capacity_bytes"]:
                        actions.append("blocked: CXL has no demotion headroom")
                        break
                    dram.remove(victim)
                    actions.append(f"demote {victim}")
                    window_migration += demotion_us
                    demotions += 1
                dram.add(candidate)
                actions.append(f"promote {candidate}")
                window_migration += promotion_us
                promotions += 1
                budget -= needed_bytes
        migration_us += window_migration
        log.append({
            "window": window["window"],
            "start_ms": window["start_ms"],
            "end_ms": window["end_ms"],
            "dram_pages_during_window": json.dumps(serving_pages),
            "dram_bytes_during_window": len(serving_pages) * page_bytes,
            "cxl_bytes_during_window": (len(pages) - len(serving_pages)) * page_bytes,
            "access_us": window_access,
            "action_after_window": "; ".join(actions) or "none",
            "migration_us": window_migration,
            "dram_pages_after_window": json.dumps([p for p in pages if p in dram]),
        })
    return {
        "access_us": access_us,
        "migration_us": migration_us,
        "total_us": access_us + migration_us,
        "promotions": promotions,
        "demotions": demotions,
        "migration_payload_bytes": (promotions + demotions) * page_bytes,
    }, log


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "experiment.json")
    parser.add_argument("--trace", type=Path, default=ROOT / "hypothetical_traces.csv")
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    args = parser.parse_args()
    model = load_model(json.loads(args.config.read_text()))
    traces = defaultdict(list)
    with args.trace.open(newline="") as stream:
        reader = csv.DictReader(stream)
        expected = {"scenario", "window", "start_ms", "end_ms"} | {f"requests_{p}" for p in model["pages"]}
        if reader.fieldnames is None or set(reader.fieldnames) != expected or len(reader.fieldnames) != len(expected):
            raise ValueError("Trace columns must match the configured pages; regenerate the trace")
        for row in reader:
            previous = traces[row["scenario"]]
            if int(row["window"]) != len(previous) + 1:
                raise ValueError("Each scenario must have consecutive windows starting at 1")
            start, end = Decimal(row["start_ms"]), Decimal(row["end_ms"])
            expected_start = Decimal(previous[-1]["end_ms"]) if previous else Decimal(0)
            if not start.is_finite() or not end.is_finite() or start != expected_start or end <= start:
                raise ValueError("Trace windows must be finite, positive-duration and contiguous from 0")
            if any(int(row[f"requests_{p}"]) < 0 for p in model["pages"]):
                raise ValueError("Request counts must be nonnegative")
            traces[row["scenario"]].append(row)
    if not traces:
        raise ValueError("Trace is empty; run generate_hypothetical_traces.py first")

    summaries, events = [], []
    for scenario, windows in traces.items():
        for policy in ("static", "last_window_hotness"):
            summary, log = evaluate(windows, policy, model)
            summaries.append({"scenario": scenario, "policy": policy, **summary})
            events.extend({"scenario": scenario, "policy": policy, **row} for row in log)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "hypothetical_results.csv", summaries)
    write_csv(args.output_dir / "hypothetical_window_log.csv", events)
    for row in summaries:
        print(f"{row['scenario']:20} {row['policy']:20} "
              f"access={row['access_us']:5} us  "
              f"migration={row['migration_us']:4} us  "
              f"total={row['total_us']:5} us")


if __name__ == "__main__":
    main()
