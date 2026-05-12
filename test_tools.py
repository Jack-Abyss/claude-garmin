"""
Standalone smoke test: calls all 8 MCP tools and prints their output.

Bypasses the MCP protocol and invokes the Python functions directly.
Useful to verify that the Garmin login works and real data is flowing
before wiring the server into Claude Desktop.

Usage:
    python test_tools.py            # run all tools
    python test_tools.py recovery   # only get_recovery
    python test_tools.py fitness    # only get_fitness
    ...
"""
import json
import sys
import time

import garmin_mcp


def _run(name: str, fn, *args):
    print(f"\n{'=' * 70}")
    print(f"  {name}({', '.join(map(str, args))})")
    print(f"{'=' * 70}")
    t0 = time.time()
    try:
        result = fn(*args)
        dt = time.time() - t0
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n  -> OK in {dt:.1f}s")
        return result
    except Exception as e:
        dt = time.time() - t0
        print(f"  ERROR ({type(e).__name__}) after {dt:.1f}s: {e}")
        return None


TOOLS = {
    "recovery":      ("get_recovery",         garmin_mcp.get_recovery,         ()),
    "fitness":       ("get_fitness",          garmin_mcp.get_fitness,          ()),
    "load":          ("get_recent_load",      garmin_mcp.get_recent_load,      (28,)),
    "activities":    ("get_activities",       garmin_mcp.get_activities,       (14,)),
    "training_load": ("get_training_load",    garmin_mcp.get_training_load,    ()),
    "stress":        ("get_stress_data",      garmin_mcp.get_stress_data,      (7,)),
    "records":       ("get_personal_records", garmin_mcp.get_personal_records, ()),
}


def main() -> int:
    filt = sys.argv[1] if len(sys.argv) > 1 else None

    if filt and filt not in TOOLS and filt != "dynamics":
        print(f"Unknown tool: {filt}")
        print(f"Available: {', '.join(TOOLS.keys())}, dynamics")
        return 1

    last_activity_id = None
    for key, (name, fn, args) in TOOLS.items():
        if filt and filt != key:
            continue
        result = _run(name, fn, *args)
        # Capture an activity_id from get_activities so we can also test
        # get_running_dynamics, which requires one.
        if key == "activities" and isinstance(result, dict):
            for a in result.get("activities", []):
                if a.get("sport") == "run":
                    last_activity_id = a.get("activity_id")
                    break

    # get_running_dynamics needs an activity_id; use the latest run we saw.
    if (not filt or filt == "dynamics") and last_activity_id:
        _run("get_running_dynamics", garmin_mcp.get_running_dynamics, last_activity_id)
    elif (not filt or filt == "dynamics") and not last_activity_id:
        print("\n  [skipping get_running_dynamics: no run found in the last 14 sessions]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
