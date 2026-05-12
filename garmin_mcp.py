"""
Garmin Connect MCP server for Claude Desktop.

Exposes Garmin Connect data as MCP tools so Claude Desktop can fetch real
fitness metrics on demand: recovery, fitness markers, training load,
recent activities, running dynamics, stress data, and personal records.

Two run modes:

    python garmin_mcp.py
        Normal mode. Speaks the MCP protocol over stdio. Spawned by
        Claude Desktop as a child process — do not run interactively.

    python garmin_mcp.py login
        One-time interactive login that authenticates against Garmin
        Connect and caches OAuth tokens in ~/.garminconnect/.
        Handles MFA via input prompt. Run once before adding the server
        to Claude Desktop.
"""
from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Callable

# Light startup imports only: mcp + dotenv (~10 MB).
# Heavy libraries (garminconnect, curl_cffi, garth) are imported lazily
# inside _get_client(), so when Claude Desktop spawns the server but the
# user is not actively asking for Garmin data, the process stays small.
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

# Defensive: if the .env file was saved with a UTF-8 BOM (PowerShell 5.1's
# Set-Content -Encoding utf8 does this), python-dotenv reads the first key
# as "﻿GARMIN_EMAIL" instead of "GARMIN_EMAIL", making it invisible to
# os.environ.get("GARMIN_EMAIL"). Alias any BOM-prefixed keys to their
# clean counterparts so the rest of the code can stay BOM-agnostic.
for _key in list(os.environ.keys()):
    if _key.startswith("﻿"):
        os.environ.setdefault(_key.lstrip("﻿"), os.environ[_key])

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("garmin-mcp")

# OAuth tokens live here. garth/garminconnect manage their lifecycle:
# access tokens auto-refresh, full re-login is only needed if the refresh
# token expires or is revoked (typically every few months).
# normpath cleans up the mixed-slash output of expanduser on Windows
# (turns "C:\Users\me/.garminconnect" into "C:\Users\me\.garminconnect").
TOKEN_STORE = os.path.normpath(os.path.expanduser("~/.garminconnect"))

mcp = FastMCP("garmin")

# Cached Garmin client. Built on first tool invocation, then reused.
_client: Any = None


def _build_client(allow_interactive_mfa: bool) -> Any:
    """Construct a Garmin client from env credentials.

    `allow_interactive_mfa=True` is only safe when running from a real
    terminal (the `login` subcommand). Under the MCP runtime, stdin is
    owned by the protocol — prompting would corrupt the JSON-RPC stream.
    """
    from garminconnect import Garmin  # lazy import

    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "GARMIN_EMAIL and GARMIN_PASSWORD are not set. "
            "Define them in the .env file or as environment variables "
            "(under the 'env' key in claude_desktop_config.json)."
        )
    return Garmin(
        email=email,
        password=password,
        prompt_mfa=(lambda: input("MFA code: ")) if allow_interactive_mfa else None,
    )


def _get_client() -> Any:
    """Return an authenticated Garmin client, reusing cached tokens.

    Lazy: the first call imports `garminconnect` and authenticates;
    subsequent calls reuse the cached client.

    MFA is intentionally disabled here: if the cached tokens are missing
    or expired, the call fails fast with an actionable error pointing
    the user at `python garmin_mcp.py login`.
    """
    global _client
    if _client is not None:
        return _client

    client = _build_client(allow_interactive_mfa=False)
    try:
        client.login(TOKEN_STORE)
    except Exception as e:
        # Distinguish bad credentials from expired/missing tokens by
        # inspecting the exception class name (avoids eager-importing
        # garminconnect just for the isinstance check).
        cls_name = type(e).__name__
        if cls_name == "GarminConnectAuthenticationError":
            raise RuntimeError(
                "Garmin authentication failed - your email or password "
                "in .env is wrong. Edit .env and run: "
                "`python garmin_mcp.py login` "
                f"(or use install.bat -Reconfigure). Original error: {e}"
            ) from e
        raise RuntimeError(
            "Garmin login failed - cached tokens are likely missing or "
            "expired. Run this once in a terminal: "
            "`python garmin_mcp.py login` "
            f"(original error: {e})"
        ) from e
    _client = client
    log.info("Garmin client authenticated (token cache: %s)", TOKEN_STORE)
    return client


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    """Run `fn` swallowing benign errors (missing data, partial endpoints).

    Garmin Connect commonly returns 404/500 for metrics that don't exist
    for a given day (e.g. HRV before a device has collected enough data).
    We treat these as missing values and return `default`.

    Auth/connection/rate-limit errors ARE fatal and bubble up — we detect
    them by class name to avoid an eager import of garminconnect.
    """
    try:
        return fn()
    except Exception as e:
        cls_name = type(e).__name__
        if cls_name in {
            "GarminConnectAuthenticationError",
            "GarminConnectConnectionError",
            "GarminConnectTooManyRequestsError",
        }:
            raise
        log.debug("safe-call swallowed (%s): %s", cls_name, e)
        return default


def _today_iso() -> str:
    return date.today().isoformat()


def _date_range_iso(days: int) -> list[str]:
    """ISO dates for the last `days` days, most recent first."""
    return [(date.today() - timedelta(days=i)).isoformat() for i in range(days)]


def _round(value: Any, ndigits: int = 2) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


def _parse_local(ts: str | None) -> str | None:
    """Normalize Garmin's local timestamps (e.g. '2026-05-10 18:32:11') to ISO."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace(" ", "T")).isoformat()
    except Exception:
        return ts


# ---------------------------------------------------------------------------
# Sport classification
# ---------------------------------------------------------------------------

SWIM_TYPES = {"lap_swimming", "open_water_swimming", "swimming"}
BIKE_TYPES = {
    "cycling",
    "indoor_cycling",
    "road_biking",
    "mountain_biking",
    "gravel_cycling",
    "virtual_ride",
    "e_bike_mountain",
    "e_bike_fitness",
}
RUN_TYPES = {"running", "treadmill_running", "trail_running", "track_running", "virtual_run"}


def _bucket(type_key: str | None) -> str | None:
    """Map a Garmin activity typeKey to a coarse sport bucket (swim/bike/run)."""
    if not type_key:
        return None
    if type_key in SWIM_TYPES:
        return "swim"
    if type_key in BIKE_TYPES:
        return "bike"
    if type_key in RUN_TYPES:
        return "run"
    return None


# ---------------------------------------------------------------------------
# Tool 1: get_recovery
# ---------------------------------------------------------------------------

@mcp.tool()
def get_recovery() -> dict[str, Any]:
    """Current recovery state and how ready you are to train hard.

    Returns:
      - training_readiness: score 0-100, level, estimated recovery time
      - hrv: 7-day average, status, daily values
      - sleep: 7-day average hours and score
      - body_battery_today: charged/drained, end-of-day level
      - resting_heart_rate_bpm
      - training_status: productive / maintaining / recovery / detraining
                         / overreaching / unproductive

    Call this when deciding intensity for today, when the user asks how
    they're recovering, or before suggesting a hard workout.
    """
    client = _get_client()
    today = _today_iso()
    week_dates = _date_range_iso(7)

    with ThreadPoolExecutor(max_workers=8) as ex:
        f_readiness = ex.submit(_safe, lambda: client.get_training_readiness(today))
        f_status = ex.submit(_safe, lambda: client.get_training_status(today))
        f_hrv = {d: ex.submit(_safe, lambda dd=d: client.get_hrv_data(dd)) for d in week_dates}
        f_sleep = {d: ex.submit(_safe, lambda dd=d: client.get_sleep_data(dd)) for d in week_dates}
        f_bb = ex.submit(_safe, lambda: client.get_body_battery(week_dates[-1], today))
        f_rhr = ex.submit(_safe, lambda: client.get_resting_heart_rate(today))

        readiness_raw = f_readiness.result()
        status_raw = f_status.result()
        hrv_raw = {d: f.result() for d, f in f_hrv.items()}
        sleep_raw = {d: f.result() for d, f in f_sleep.items()}
        body_battery_raw = f_bb.result()
        rhr_raw = f_rhr.result()

    # --- Training readiness ---
    readiness: dict[str, Any] | None = None
    item: dict[str, Any] | None = None
    if isinstance(readiness_raw, list) and readiness_raw:
        item = readiness_raw[0] if isinstance(readiness_raw[0], dict) else None
    elif isinstance(readiness_raw, dict):
        item = readiness_raw
    if item:
        readiness = {
            "score": item.get("score"),
            "level": item.get("level"),
            "feedback_short": item.get("feedbackShort"),
            "feedback_long": item.get("feedbackLong"),
            "recovery_time_hours": item.get("recoveryTime"),
            "sleep_score": item.get("sleepScore"),
            "hrv_weekly_avg": item.get("hrvWeeklyAverage"),
        }

    # --- HRV: daily values and 7-day average ---
    hrv_daily: list[dict[str, Any]] = []
    for d in week_dates:
        raw = hrv_raw.get(d)
        if not isinstance(raw, dict):
            continue
        summary = raw.get("hrvSummary") or {}
        last_night = summary.get("lastNightAvg")
        if last_night is None and summary.get("weeklyAvg") is None:
            continue
        hrv_daily.append({
            "date": d,
            "last_night_avg_ms": last_night,
            "weekly_avg_ms": summary.get("weeklyAvg"),
            "status": summary.get("status"),
        })
    hrv_values = [h["last_night_avg_ms"] for h in hrv_daily if h["last_night_avg_ms"] is not None]
    hrv_7d_avg = _round(sum(hrv_values) / len(hrv_values), 1) if hrv_values else None
    hrv_status_latest = hrv_daily[0]["status"] if hrv_daily else None

    # --- Sleep: 7-day averages ---
    sleep_hour_values: list[float] = []
    sleep_score_values: list[float] = []
    for d in week_dates:
        raw = sleep_raw.get(d)
        if not isinstance(raw, dict):
            continue
        dto = raw.get("dailySleepDTO") or {}
        seconds = dto.get("sleepTimeSeconds")
        if isinstance(seconds, (int, float)) and seconds > 0:
            sleep_hour_values.append(seconds / 3600)
        scores = dto.get("sleepScores") or {}
        overall = scores.get("overall") if isinstance(scores, dict) else None
        score = overall.get("value") if isinstance(overall, dict) else None
        if isinstance(score, (int, float)):
            sleep_score_values.append(float(score))
    sleep_7d_avg_hours = (
        _round(sum(sleep_hour_values) / len(sleep_hour_values), 2) if sleep_hour_values else None
    )
    sleep_7d_avg_score = (
        _round(sum(sleep_score_values) / len(sleep_score_values), 1) if sleep_score_values else None
    )

    # --- Body battery (today) ---
    body_battery: dict[str, Any] | None = None
    if isinstance(body_battery_raw, list) and body_battery_raw:
        latest = body_battery_raw[-1] if isinstance(body_battery_raw[-1], dict) else None
        if latest:
            body_battery = {
                "charged": latest.get("charged"),
                "drained": latest.get("drained"),
                "highest": latest.get("highestBatteryLevel") or latest.get("highest"),
                "lowest": latest.get("lowestBatteryLevel") or latest.get("lowest"),
                "end_of_day": latest.get("endOfDayBatteryLevel"),
            }

    # --- Resting heart rate ---
    rhr_bpm: int | None = None
    if isinstance(rhr_raw, dict):
        try:
            metrics_list = rhr_raw["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"]
            if isinstance(metrics_list, list) and metrics_list:
                value = metrics_list[0].get("value")
                if isinstance(value, (int, float)):
                    rhr_bpm = int(value)
        except (KeyError, TypeError, IndexError):
            pass

    # --- Training status ---
    training_status: dict[str, Any] | None = None
    if isinstance(status_raw, dict):
        try:
            most_recent = status_raw.get("mostRecentTrainingStatus") or {}
            latest_map = most_recent.get("latestTrainingStatusData") or {}
            if latest_map:
                first = next(iter(latest_map.values()))
                if isinstance(first, dict):
                    training_status = {
                        "status": first.get("trainingStatus"),
                        "feedback": first.get("trainingStatusFeedbackPhrase"),
                        "fitness_trend": first.get("fitnessTrend"),
                        "load_tunnel_min": first.get("loadTunnelMin"),
                        "load_tunnel_max": first.get("loadTunnelMax"),
                    }
        except Exception:
            pass

    return {
        "training_readiness": readiness,
        "hrv": {
            "weekly_avg_ms": hrv_7d_avg,
            "status_latest": hrv_status_latest,
            "daily": hrv_daily,
        },
        "sleep": {
            "weekly_avg_hours": sleep_7d_avg_hours,
            "weekly_avg_score": sleep_7d_avg_score,
        },
        "body_battery_today": body_battery,
        "resting_heart_rate_bpm": rhr_bpm,
        "training_status": training_status,
    }


# ---------------------------------------------------------------------------
# Tool 2: get_fitness
# ---------------------------------------------------------------------------

@mcp.tool()
def get_fitness() -> dict[str, Any]:
    """Current fitness markers: VO2max, cycling FTP, and race time predictions.

    Returns:
      - vo2_max_running, vo2_max_cycling (mL/kg/min)
      - cycling_ftp_w (functional threshold power in watts)
      - race_predictions for 5K / 10K / half marathon / marathon
        (each with both raw seconds and a formatted hh:mm:ss string)

    Call this when discussing performance level, race goals, or comparing
    progress over time.
    """
    client = _get_client()
    today = _today_iso()

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_max = ex.submit(_safe, lambda: client.get_max_metrics(today))
        f_race = ex.submit(_safe, lambda: client.get_race_predictions())
        f_ftp = ex.submit(_safe, lambda: client.get_cycling_ftp())
        max_raw = f_max.result()
        race_raw = f_race.result()
        ftp_raw = f_ftp.result()

    vo2_run = None
    vo2_bike = None
    if isinstance(max_raw, list) and max_raw:
        first = max_raw[0] if isinstance(max_raw[0], dict) else {}
        generic = first.get("generic") or {}
        cycling = first.get("cycling") or {}
        if isinstance(generic, dict):
            vo2_run = _round(generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue"), 1)
        if isinstance(cycling, dict):
            vo2_bike = _round(cycling.get("vo2MaxPreciseValue") or cycling.get("vo2MaxValue"), 1)

    def _format_time(seconds: Any) -> str | None:
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return None
        total = int(seconds)
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

    race: dict[str, Any] = {}
    race_item: dict[str, Any] | None = None
    if isinstance(race_raw, list) and race_raw:
        race_item = race_raw[-1] if isinstance(race_raw[-1], dict) else None
    elif isinstance(race_raw, dict):
        race_item = race_raw
    if race_item:
        for key_in, key_out in [
            ("time5K", "5k"),
            ("time10K", "10k"),
            ("timeHalfMarathon", "half_marathon"),
            ("timeMarathon", "marathon"),
        ]:
            seconds = race_item.get(key_in)
            if isinstance(seconds, (int, float)) and seconds > 0:
                race[key_out] = {"seconds": int(seconds), "time": _format_time(seconds)}

    ftp_watts: int | None = None
    if isinstance(ftp_raw, dict):
        ftp_watts = ftp_raw.get("functionalThresholdPower") or ftp_raw.get("ftp")
    elif isinstance(ftp_raw, (int, float)):
        ftp_watts = int(ftp_raw)

    return {
        "vo2_max_running": vo2_run,
        "vo2_max_cycling": vo2_bike,
        "cycling_ftp_w": ftp_watts,
        "race_predictions": race or None,
    }


# ---------------------------------------------------------------------------
# Internal: fetch recent activities (used by multiple tools)
# ---------------------------------------------------------------------------

def _normalize_activity(activity: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a raw Garmin activity into a clean, sport-aware dict.

    Returns None for sports outside swim/bike/run (we focus on multisport
    endurance: a yoga session or a strength workout is filtered out).
    """
    type_key = (activity.get("activityType") or {}).get("typeKey")
    sport = _bucket(type_key)
    if not sport:
        return None

    duration_s = activity.get("duration") or 0
    distance_m = activity.get("distance") or 0
    hr_avg = activity.get("averageHR")
    hr_max = activity.get("maxHR")
    out: dict[str, Any] = {
        "activity_id": activity.get("activityId"),
        "name": activity.get("activityName"),
        "sport": sport,
        "type_key": type_key,
        "date": _parse_local(activity.get("startTimeLocal")),
        "duration_min": _round(duration_s / 60, 1),
        "distance_km": _round(distance_m / 1000, 2) if distance_m else None,
        "hr_avg": int(hr_avg) if isinstance(hr_avg, (int, float)) else None,
        "hr_max": int(hr_max) if isinstance(hr_max, (int, float)) else None,
    }

    if sport == "run":
        avg_speed_mps = activity.get("averageSpeed")
        if isinstance(avg_speed_mps, (int, float)) and avg_speed_mps > 0:
            pace_seconds_per_km = 1000.0 / avg_speed_mps
            minutes = int(pace_seconds_per_km // 60)
            seconds = int(pace_seconds_per_km % 60)
            out["pace_min_km"] = f"{minutes}:{seconds:02d}"
        cadence = (
            activity.get("averageRunningCadenceInStepsPerMinute")
            or activity.get("avgRunCadence")
        )
        if isinstance(cadence, (int, float)):
            out["cadence_spm"] = _round(cadence, 0)

    if sport == "bike":
        avg_power = activity.get("avgPower") or activity.get("averagePower")
        if isinstance(avg_power, (int, float)):
            out["avg_power_w"] = int(avg_power)
        normalized_power = activity.get("normPower") or activity.get("normalizedPower")
        if isinstance(normalized_power, (int, float)):
            out["normalized_power_w"] = int(normalized_power)

    if sport == "swim":
        strokes = activity.get("totalNumberOfStrokes") or activity.get("strokes")
        if isinstance(strokes, (int, float)):
            out["total_strokes"] = int(strokes)
        stroke_distance = activity.get("avgStrokeDistance")
        if isinstance(stroke_distance, (int, float)):
            out["avg_stroke_distance_m"] = _round(stroke_distance, 2)

    return out


def _fetch_recent_activities(client: Any, days: int) -> list[dict[str, Any]]:
    """Fetch activities and filter to those within the last `days` days.

    Garmin's API has no server-side date filter, so we pull a wide page
    (limit 200) and filter client-side. 200 covers even ~6 sessions/day
    for an entire month.
    """
    raw = _safe(lambda: client.get_activities(0, 200), default=[]) or []
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    activities: list[dict[str, Any]] = []
    for activity in raw:
        if not isinstance(activity, dict):
            continue
        start = activity.get("startTimeLocal") or ""
        # ISO strings sort lexicographically, so a string compare on YYYY-MM-DD works
        if start[:10] < cutoff:
            continue
        normalized = _normalize_activity(activity)
        if normalized:
            activities.append(normalized)
    activities.sort(key=lambda a: a.get("date") or "", reverse=True)
    return activities


# ---------------------------------------------------------------------------
# Tool 3: get_recent_load
# ---------------------------------------------------------------------------

@mcp.tool()
def get_recent_load(days: int = 28) -> dict[str, Any]:
    """Training load aggregated by sport over the last `days` days.

    For each of swim / bike / run returns:
      - session count
      - total km
      - total minutes and hours
      - average heart rate across sessions that have HR data

    Default window is 28 days (roughly a training block). Use this for
    weekly/monthly volume questions, sport balance discussions, or to see
    if the user is over- or under-doing one discipline.
    """
    client = _get_client()
    activities = _fetch_recent_activities(client, days)

    buckets: dict[str, dict[str, Any]] = {
        "swim": {"sessions": 0, "total_km": 0.0, "total_minutes": 0.0, "hr_sum": 0.0, "hr_count": 0},
        "bike": {"sessions": 0, "total_km": 0.0, "total_minutes": 0.0, "hr_sum": 0.0, "hr_count": 0},
        "run":  {"sessions": 0, "total_km": 0.0, "total_minutes": 0.0, "hr_sum": 0.0, "hr_count": 0},
    }
    for activity in activities:
        bucket = buckets.get(activity["sport"])
        if not bucket:
            continue
        bucket["sessions"] += 1
        if activity.get("distance_km"):
            bucket["total_km"] += activity["distance_km"]
        if activity.get("duration_min"):
            bucket["total_minutes"] += activity["duration_min"]
        if activity.get("hr_avg"):
            bucket["hr_sum"] += activity["hr_avg"]
            bucket["hr_count"] += 1

    by_sport: dict[str, Any] = {}
    for sport, bucket in buckets.items():
        by_sport[sport] = {
            "sessions": bucket["sessions"],
            "total_km": _round(bucket["total_km"], 2),
            "total_minutes": _round(bucket["total_minutes"], 1),
            "total_hours": _round(bucket["total_minutes"] / 60, 2) if bucket["total_minutes"] else 0,
            "avg_hr": _round(bucket["hr_sum"] / bucket["hr_count"], 0) if bucket["hr_count"] else None,
        }

    return {"period_days": days, "by_sport": by_sport}


# ---------------------------------------------------------------------------
# Tool 4: get_activities
# ---------------------------------------------------------------------------

@mcp.tool()
def get_activities(days: int = 14) -> dict[str, Any]:
    """List recent multisport activities with normalized details.

    Filtered to swim/bike/run only. Each activity includes:
      - sport, date, duration_min, distance_km
      - hr_avg, hr_max
      - pace_min_km (run), avg_power_w / normalized_power_w (bike),
        total_strokes / avg_stroke_distance_m (swim)
      - activity_id (use with get_running_dynamics for deep run analysis)

    Default window is 14 days. Use this to discuss specific recent
    sessions or to find an activity_id for further analysis.
    """
    client = _get_client()
    activities = _fetch_recent_activities(client, days)
    return {"period_days": days, "count": len(activities), "activities": activities}


# ---------------------------------------------------------------------------
# Tool 5: get_running_dynamics
# ---------------------------------------------------------------------------

@mcp.tool()
def get_running_dynamics(activity_id: int) -> dict[str, Any]:
    """Running form metrics for a specific run (requires compatible sensor).

    Returns:
      - cadence_spm (steps per minute)
      - ground_contact_time_ms
      - vertical_oscillation_cm
      - stride_length_m

    These metrics need an HRM-Pro/HRM-Run strap, a foot pod, or a watch
    with onboard running dynamics. If the activity wasn't a run or no
    compatible sensor was paired, fields will be null.

    Pass an activity_id from get_activities(). If you pass a cycling or
    swimming ID, you'll get an error rather than misleading numbers.

    Use this to give technique feedback on a specific run.
    """
    client = _get_client()
    raw = _safe(lambda: client.get_activity(activity_id))
    if not isinstance(raw, dict):
        return {"activity_id": activity_id, "error": "activity not found or not accessible"}

    type_key = (raw.get("activityTypeDTO") or raw.get("activityType") or {}).get("typeKey")
    sport = _bucket(type_key)

    # Reject non-run activities: dynamics fields exist for some non-run sports
    # but have different semantics; returning them under "running dynamics"
    # would mislead the consumer.
    if sport != "run":
        return {
            "activity_id": activity_id,
            "sport": sport,
            "type_key": type_key,
            "error": (
                f"activity_id {activity_id} is not a run "
                f"(sport={sport}, type_key={type_key}). "
                "Running dynamics are only meaningful for run activities. "
                "Use get_activities() to find a run and pass its activity_id."
            ),
        }

    summary = raw.get("summaryDTO") or raw
    if isinstance(summary, dict):
        cadence = (
            summary.get("averageRunCadence")
            or summary.get("averageRunningCadenceInStepsPerMinute")
            or summary.get("avgRunCadence")
        )
        gct = summary.get("groundContactTime") or summary.get("avgGroundContactTime")
        vertical = summary.get("verticalOscillation") or summary.get("avgVerticalOscillation")
        stride = summary.get("avgStrideLength") or summary.get("averageStrideLength")
    else:
        cadence = gct = vertical = stride = None

    # Garmin reports stride length in cm in most endpoints — convert to meters.
    # If the raw value is suspiciously small (<5) we assume it's already meters.
    stride_m: float | None = None
    if isinstance(stride, (int, float)):
        stride_m = _round(stride / 100.0 if stride > 5 else stride, 2)

    vertical_oscillation_cm: float | None = None
    if isinstance(vertical, (int, float)):
        vertical_oscillation_cm = _round(vertical, 1)

    return {
        "activity_id": activity_id,
        "sport": sport,
        "type_key": type_key,
        "cadence_spm": _round(cadence, 0) if isinstance(cadence, (int, float)) else None,
        "ground_contact_time_ms": _round(gct, 0) if isinstance(gct, (int, float)) else None,
        "vertical_oscillation_cm": vertical_oscillation_cm,
        "stride_length_m": stride_m,
    }


# ---------------------------------------------------------------------------
# Tool 6: get_training_load
# ---------------------------------------------------------------------------

@mcp.tool()
def get_training_load() -> dict[str, Any]:
    """Training load metrics: acute load, chronic load, ratio, and focus breakdown.

    Returns:
      - acute_load: ~7-day training load (ATL)
      - chronic_load: ~28-day training load (CTL)
      - load_ratio: acute / chronic (a.k.a. ACWR)
          * 0.8 - 1.3: sweet spot
          * > 1.5:     overtraining risk
          * < 0.8:     detraining
      - acwr_status: Garmin's qualitative label for the ratio
      - load_focus: distribution across base / tempo / threshold / vo2 /
        anaerobic with target ranges from Garmin

    Use this when discussing periodization, intensity distribution
    (e.g. polarized vs. pyramidal), or to spot imbalances.
    """
    client = _get_client()
    today = _today_iso()
    status_raw = _safe(lambda: client.get_training_status(today))

    if not isinstance(status_raw, dict):
        return {
            "error": "training status not available "
                     "(needs ~7 days of activities on a compatible device)"
        }

    acute = chronic = ratio = None
    acwr_status = None
    load_focus: dict[str, Any] | None = None

    try:
        most_recent = status_raw.get("mostRecentTrainingStatus") or {}
        latest_map = most_recent.get("latestTrainingStatusData") or {}
        if latest_map:
            first = next(iter(latest_map.values()))
            if isinstance(first, dict):
                atl_dto = first.get("acuteTrainingLoadDTO") or {}
                if isinstance(atl_dto, dict):
                    acute = _round(atl_dto.get("dailyTrainingLoadAcute"), 1)
                    chronic = _round(atl_dto.get("dailyTrainingLoadChronic"), 1)
                    ratio = _round(atl_dto.get("dailyAcuteChronicWorkloadRatio"), 2)
                    acwr_status = atl_dto.get("acwrStatus")
    except Exception:
        pass

    try:
        balance = status_raw.get("mostRecentTrainingLoadBalance") or {}
        balance_map = balance.get("metricsTrainingLoadBalanceDTOMap") or {}
        if balance_map:
            first_balance = next(iter(balance_map.values()))
            if isinstance(first_balance, dict):
                load_focus = {
                    "monthly_load_aerobic_low": _round(first_balance.get("monthlyLoadAerobicLow"), 1),
                    "monthly_load_aerobic_high": _round(first_balance.get("monthlyLoadAerobicHigh"), 1),
                    "monthly_load_anaerobic": _round(first_balance.get("monthlyLoadAnaerobic"), 1),
                    "aerobic_low_target_min": _round(first_balance.get("monthlyLoadAerobicLowTargetMin"), 1),
                    "aerobic_low_target_max": _round(first_balance.get("monthlyLoadAerobicLowTargetMax"), 1),
                    "aerobic_high_target_min": _round(first_balance.get("monthlyLoadAerobicHighTargetMin"), 1),
                    "aerobic_high_target_max": _round(first_balance.get("monthlyLoadAerobicHighTargetMax"), 1),
                    "anaerobic_target_min": _round(first_balance.get("monthlyLoadAnaerobicTargetMin"), 1),
                    "anaerobic_target_max": _round(first_balance.get("monthlyLoadAnaerobicTargetMax"), 1),
                    "training_balance_feedback_phrase": first_balance.get("trainingBalanceFeedbackPhrase"),
                }
    except Exception:
        pass

    return {
        "acute_load": acute,
        "chronic_load": chronic,
        "load_ratio": ratio,
        "acwr_status": acwr_status,
        "load_focus": load_focus,
    }


# ---------------------------------------------------------------------------
# Tool 7: get_stress_data
# ---------------------------------------------------------------------------

@mcp.tool()
def get_stress_data(days: int = 7) -> dict[str, Any]:
    """Daily stress levels over the last `days` days.

    Garmin's all-day stress score is derived from HRV. Higher numbers
    mean more physiological stress (NOT necessarily psychological).

    Per-day fields:
      - stress_avg (0-100), max_stress
      - rest_minutes (0-25), low_minutes (26-50),
        medium_minutes (51-75), high_minutes (76-100)
      - activity_minutes (time spent exercising — excluded from stress)

    Plus `period_avg_stress` aggregated across the window.

    Use this to discuss life-load (work, illness, poor sleep) interacting
    with training, or to flag patterns of chronic high stress.
    """
    client = _get_client()
    dates = _date_range_iso(days)
    daily: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {d: ex.submit(_safe, lambda dd=d: client.get_all_day_stress(dd)) for d in dates}
        for d in dates:
            raw = futures[d].result()
            if not isinstance(raw, dict):
                daily.append({"date": d, "stress_avg": None})
                continue
            daily.append({
                "date": d,
                "stress_avg": raw.get("avgStressLevel") or raw.get("overallStressLevel"),
                "max_stress": raw.get("maxStressLevel"),
                "rest_minutes": _round((raw.get("restStressDuration") or 0) / 60, 1),
                "low_minutes": _round((raw.get("lowStressDuration") or 0) / 60, 1),
                "medium_minutes": _round((raw.get("mediumStressDuration") or 0) / 60, 1),
                "high_minutes": _round((raw.get("highStressDuration") or 0) / 60, 1),
                "activity_minutes": _round((raw.get("activityStressDuration") or 0) / 60, 1),
            })

    valid_days = [d for d in daily if isinstance(d.get("stress_avg"), (int, float))]
    period_avg = (
        _round(sum(d["stress_avg"] for d in valid_days) / len(valid_days), 1) if valid_days else None
    )

    return {
        "period_days": days,
        "period_avg_stress": period_avg,
        "daily": daily,
    }


# ---------------------------------------------------------------------------
# Tool 8: get_personal_records
# ---------------------------------------------------------------------------

# Garmin uses numeric typeIds for personal records. The map below covers
# the most common multisport records; unknown typeIds are still returned
# with their raw typeLabelKey so nothing is hidden.
PR_TYPE_LABELS: dict[int, dict[str, str]] = {
    1:  {"sport": "run",     "label": "1K best time",            "unit": "seconds"},
    2:  {"sport": "run",     "label": "1 mile best time",        "unit": "seconds"},
    3:  {"sport": "run",     "label": "5K best time",            "unit": "seconds"},
    4:  {"sport": "run",     "label": "10K best time",           "unit": "seconds"},
    5:  {"sport": "run",     "label": "Half marathon best time", "unit": "seconds"},
    6:  {"sport": "run",     "label": "Marathon best time",      "unit": "seconds"},
    7:  {"sport": "run",     "label": "Longest run",             "unit": "meters"},
    8:  {"sport": "bike",    "label": "Longest ride",            "unit": "meters"},
    9:  {"sport": "bike",    "label": "Best 20-min power",       "unit": "watts"},
    10: {"sport": "bike",    "label": "Best 1-hour power",       "unit": "watts"},
    12: {"sport": "general", "label": "Most steps in a day",     "unit": "steps"},
    13: {"sport": "general", "label": "Most steps in a week",    "unit": "steps"},
}


def _format_pr_value(value: Any, unit: str) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    if unit == "seconds":
        total = int(value)
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
    if unit == "meters":
        return f"{value / 1000:.2f} km"
    if unit == "watts":
        return f"{int(value)} W"
    if unit == "steps":
        return f"{int(value)} steps"
    return str(value)


@mcp.tool()
def get_personal_records() -> dict[str, Any]:
    """Personal records grouped by sport.

    Includes best times on standard running distances (1K/5K/10K/HM/marathon),
    longest distances, cycling power records, and general PRs tracked by
    Garmin (e.g. most steps in a day).

    Returns a structure like:
        {
          "count": 12,
          "by_sport": {
            "run":  [{"label": "5K best time", "value_formatted": "21:33", "date": ...}, ...],
            "bike": [...],
            "general": [...]
          }
        }

    Use this when discussing the user's all-time bests or contextualizing
    a recent performance against their personal history.
    """
    client = _get_client()
    raw = _safe(lambda: client.get_personal_records(), default=[])
    if not isinstance(raw, list):
        return {"by_sport": {}, "count": 0}

    grouped: dict[str, list[dict[str, Any]]] = {"run": [], "bike": [], "swim": [], "general": []}
    for record in raw:
        if not isinstance(record, dict):
            continue
        type_id = record.get("typeId")
        meta = PR_TYPE_LABELS.get(type_id) if isinstance(type_id, int) else None
        value = record.get("value")
        date_value = (
            record.get("prStartTimeGmtFormatted")
            or record.get("prStartTimeGmt")
            or record.get("prTypeLabelKey")
        )
        entry: dict[str, Any] = {
            "type_id": type_id,
            "label": meta["label"] if meta else (record.get("prTypeLabelKey") or f"type_{type_id}"),
            "value_raw": value,
            "value_formatted": (
                _format_pr_value(value, meta["unit"]) if meta else
                (str(value) if value is not None else None)
            ),
            "unit": meta["unit"] if meta else None,
            "date": _parse_local(date_value) if isinstance(date_value, str) else date_value,
            "activity_id": record.get("activityId"),
        }
        sport = meta["sport"] if meta else "general"
        grouped.setdefault(sport, []).append(entry)

    total = sum(len(records) for records in grouped.values())
    # Drop empty sport buckets so the output stays compact
    return {"count": total, "by_sport": {k: v for k, v in grouped.items() if v}}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _interactive_login() -> None:
    """One-time interactive Garmin login.

    Run this from a real terminal BEFORE adding the server to Claude
    Desktop. Authenticates against Garmin Connect (handling MFA if
    enabled on your account) and saves OAuth tokens to ~/.garminconnect/.
    The MCP server will then reuse and auto-refresh those tokens.
    """
    print("Garmin Connect login...", file=sys.stderr)
    try:
        client = _build_client(allow_interactive_mfa=True)
        client.login(TOKEN_STORE)
    except Exception as e:
        # Catch by class name to avoid eager-importing garminconnect at the top
        cls_name = type(e).__name__
        if cls_name == "GarminConnectAuthenticationError":
            print("", file=sys.stderr)
            print("ERROR: Garmin rejected your credentials (401 Unauthorized).", file=sys.stderr)
            print("       Edit the .env file with the correct email/password and re-run:", file=sys.stderr)
            print("       python garmin_mcp.py login", file=sys.stderr)
            sys.exit(1)
        # For anything else, give the original message but skip the stack trace
        print("", file=sys.stderr)
        print(f"ERROR ({cls_name}): {e}", file=sys.stderr)
        sys.exit(1)
    print(f"OK. Tokens saved in {TOKEN_STORE}", file=sys.stderr)
    print("You can now start the MCP server normally: python garmin_mcp.py", file=sys.stderr)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        _interactive_login()
        return
    log.info("Starting Garmin MCP server (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
