"""Pure timing, accounting, and JSON export logic (no Windows dependencies)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import math
import os
from typing import Any

from tracker_config import TrackerConfig


UTC = timezone.utc


@dataclass(frozen=True)
class ActivitySample:
    """One point-in-time observation from the platform layer."""

    monotonic: float
    wall_time: datetime
    executable: str | None
    last_input_monotonic: float | None
    desktop_locked: bool | None


class ActivityTracker:
    """In-memory activity totals keyed by local date and executable identity."""

    def __init__(self, config: TrackerConfig, *, started_at: datetime | None = None) -> None:
        self.config = config
        self.tracking_started_at = _aware(started_at or datetime.now().astimezone())
        self._totals: dict[str, dict[str, float]] = {}
        self._executables: dict[str, str] = {}
        self._previous: ActivitySample | None = None
        self._maximum_gap_seconds = config.poll_interval_seconds * 2.0

    def observe(self, sample: ActivitySample) -> None:
        """Accept a sample and credit only conservatively attributable time."""
        sample = _validate_sample(sample)
        previous = self._previous
        self._previous = sample
        if sample.executable:
            self._executables.setdefault(sample.executable.casefold(), sample.executable)
        if previous is None:
            return

        elapsed = sample.monotonic - previous.monotonic
        if elapsed <= 0 or elapsed > self._maximum_gap_seconds:
            return
        wall_elapsed = (
            sample.wall_time.astimezone(UTC) - previous.wall_time.astimezone(UTC)
        ).total_seconds()
        clock_tolerance = max(2.0, self.config.poll_interval_seconds * 0.1)
        if wall_elapsed < 0 or abs(wall_elapsed - elapsed) > clock_tolerance:
            # A wall-clock jump or suspend/resume discrepancy means this
            # interval cannot safely be treated as continuous foreground use.
            return

        # Unknown lock state is treated as unavailable; this prevents an API
        # failure from accidentally attributing locked-screen time.
        if previous.desktop_locked is not False or sample.desktop_locked is not False:
            return
        if not previous.executable or not sample.executable:
            return

        executable = os.path.basename(sample.executable.strip())
        previous_executable = os.path.basename(previous.executable.strip())
        if not executable or executable.casefold() != previous_executable.casefold():
            # The transition time was not observed. Leave the whole interval
            # untracked rather than assigning it to either endpoint app.
            return

        if previous.last_input_monotonic is None or sample.last_input_monotonic is None:
            return

        start = previous.monotonic
        end = sample.monotonic
        threshold = float(self.config.idle_threshold_seconds)

        # With no new input observed, the prior last-input timestamp gives an
        # exact cutoff. A new input observed within the interval starts a new
        # eligible window at its Windows timestamp; no earlier AFK time is
        # backfilled.
        windows: list[tuple[float, float]] = []
        prior_cutoff = previous.last_input_monotonic + threshold
        first_end = min(end, prior_cutoff)
        if first_end > start:
            windows.append((start, first_end))

        latest_input = sample.last_input_monotonic
        if start < latest_input <= end:
            resumed_end = min(end, latest_input + threshold)
            if resumed_end > latest_input:
                windows.append((latest_input, resumed_end))

        for segment_start, segment_end in _merge_windows(windows):
            self._credit_monotonic_interval(
                executable,
                segment_start,
                segment_end,
                sample.monotonic,
                sample.wall_time,
            )

    def reset(self, *, reset_at: datetime | None = None) -> None:
        """Clear totals and establish a new, accurately dated tracking baseline."""
        self.tracking_started_at = _aware(reset_at or datetime.now().astimezone())
        self._totals.clear()
        self._executables.clear()
        self._previous = None

    def export_data(self, *, exported_at: datetime | None = None) -> dict[str, Any]:
        """Build an export snapshot without changing tracked memory."""
        exported = _aware(exported_at or datetime.now().astimezone())
        days: list[dict[str, Any]] = []
        for day in sorted(self._totals):
            programs: list[dict[str, Any]] = []
            values = self._totals[day]
            for key in sorted(values, key=lambda k: (self._executables[k].casefold(), self._executables[k])):
                seconds = values[key]
                executable = self._executables[key]
                if seconds <= 0 or key in self.config.excluded_programs:
                    continue
                rounded_seconds = round(seconds, 3)
                if rounded_seconds <= 0:
                    continue
                programs.append(
                    {
                        "executable": executable,
                        "name": self.config.program_aliases.get(key, executable),
                        "seconds": rounded_seconds,
                        "minutes": round(rounded_seconds / 60.0, 3),
                    }
                )
            if programs:
                days.append({"date": day, "programs": programs})

        return {
            "schema_version": 1,
            "source": "windows_foreground_tracker",
            "tracking_started_at": self.tracking_started_at.isoformat(timespec="seconds"),
            "exported_at": exported.isoformat(timespec="seconds"),
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "idle_threshold_seconds": self.config.idle_threshold_seconds,
            "days": days,
        }

    def _credit_monotonic_interval(
        self,
        executable: str,
        start: float,
        end: float,
        sample_monotonic: float,
        sample_wall: datetime,
    ) -> None:
        # Anchor wall time at the current observation, while using monotonic
        # time for duration. This avoids callback drift and supports midnight
        # splits without assuming a callback was exactly on schedule.
        wall_end_utc = sample_wall.astimezone(UTC)
        utc_start = wall_end_utc - timedelta(seconds=sample_monotonic - start)
        utc_end = wall_end_utc - timedelta(seconds=sample_monotonic - end)
        zone = sample_wall.tzinfo
        key = executable.casefold()
        self._executables.setdefault(key, executable)

        cursor = utc_start
        while cursor < utc_end:
            local_cursor = cursor.astimezone(zone)
            next_date = local_cursor.date() + timedelta(days=1)
            next_midnight = datetime.combine(next_date, time.min, tzinfo=zone).astimezone(UTC)
            piece_end = min(utc_end, next_midnight)
            if piece_end <= cursor:  # Defensive for unusual timezone transitions.
                piece_end = utc_end
            seconds = (piece_end - cursor).total_seconds()
            day_key = local_cursor.date().isoformat()
            per_day = self._totals.setdefault(day_key, {})
            per_day[key] = per_day.get(key, 0.0) + seconds
            cursor = piece_end


def _merge_windows(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not windows:
        return []
    ordered = sorted(windows)
    merged: list[list[float]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _validate_sample(sample: ActivitySample) -> ActivitySample:
    if not math.isfinite(sample.monotonic):
        raise ValueError("sample monotonic time must be finite")
    wall = _aware(sample.wall_time)
    last_input = sample.last_input_monotonic
    if last_input is not None and (
        not math.isfinite(last_input) or last_input > sample.monotonic + 0.05
    ):
        last_input = None
    executable = sample.executable
    if executable is not None:
        executable = os.path.basename(executable.strip()) or None
    return ActivitySample(sample.monotonic, wall, executable, last_input, sample.desktop_locked)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value
