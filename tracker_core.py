"""Pure timing, accounting, and JSON export logic (no Windows dependencies)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import math
import os
from typing import Any

from tracker_browser import DOMAIN_UNKNOWN, BrowserSnapshot, resolve_browser_domain_label
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
    browser: BrowserSnapshot | None = None


class ActivityTracker:
    """In-memory activity totals keyed by local date and executable identity."""

    def __init__(self, config: TrackerConfig, *, started_at: datetime | None = None) -> None:
        self.config = config
        self.tracking_started_at = _aware(started_at or datetime.now().astimezone())
        self._totals: dict[str, dict[str, float]] = {}
        self._hourly_totals: dict[str, dict[str, float]] = {}
        self._browser_domains: dict[str, dict[str, dict[str, float]]] = {}
        self._browser_hourly_domains: dict[str, dict[str, dict[str, float]]] = {}
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
            return

        if previous.desktop_locked is not False or sample.desktop_locked is not False:
            return
        if not previous.executable or not sample.executable:
            return

        executable = os.path.basename(sample.executable.strip())
        previous_executable = os.path.basename(previous.executable.strip())
        if not executable or executable.casefold() != previous_executable.casefold():
            return

        if previous.last_input_monotonic is None or sample.last_input_monotonic is None:
            return

        start = previous.monotonic
        end = sample.monotonic
        threshold = float(self.config.idle_threshold_seconds)

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

        browser_id = None
        domain_label = None
        if self.config.browser_integration.enabled:
            browser_id = self.config.browser_integration.executable_to_id.get(executable.casefold())
            if browser_id is not None:
                domain_label = resolve_browser_domain_label(
                    browser_id=browser_id,
                    previous=previous.browser,
                    current=sample.browser,
                    previous_monotonic=previous.monotonic,
                    current_monotonic=sample.monotonic,
                    stale_observation_seconds=float(
                        self.config.browser_integration.stale_observation_seconds
                    ),
                )

        for segment_start, segment_end in _merge_windows(windows):
            self._credit_monotonic_interval(
                executable,
                segment_start,
                segment_end,
                sample.monotonic,
                sample.wall_time,
                domain_label=domain_label,
            )

    def reset(self, *, reset_at: datetime | None = None) -> None:
        """Clear totals and establish a new, accurately dated tracking baseline."""
        self.tracking_started_at = _aware(reset_at or datetime.now().astimezone())
        self._totals.clear()
        self._hourly_totals.clear()
        self._browser_domains.clear()
        self._browser_hourly_domains.clear()
        self._executables.clear()
        self._previous = None

    def export_data(self, *, exported_at: datetime | None = None) -> dict[str, Any]:
        """Build an export snapshot without changing tracked memory."""
        exported = _aware(exported_at or datetime.now().astimezone())
        days: list[dict[str, Any]] = []
        for day in sorted(self._totals):
            programs = self._build_program_entries(day)
            hours = self._build_hour_entries(day)
            if programs:
                day_entry: dict[str, Any] = {"date": day, "programs": programs}
                if hours:
                    day_entry["hours"] = hours
                days.append(day_entry)

        return {
            "schema_version": 2,
            "source": "windows_foreground_tracker",
            "tracking_started_at": self.tracking_started_at.isoformat(timespec="seconds"),
            "exported_at": exported.isoformat(timespec="seconds"),
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "idle_threshold_seconds": self.config.idle_threshold_seconds,
            "browser_integration": {
                "enabled": self.config.browser_integration.enabled,
                "stale_observation_seconds": self.config.browser_integration.stale_observation_seconds,
                "browsers": dict(self.config.browser_integration.browsers),
            },
            "days": days,
        }

    def _build_program_entries(self, day: str) -> list[dict[str, Any]]:
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
            entry: dict[str, Any] = {
                "executable": executable,
                "name": self.config.program_aliases.get(key, executable),
                "seconds": rounded_seconds,
                "minutes": round(rounded_seconds / 60.0, 3),
            }
            domains = self._browser_domains.get(day, {}).get(key)
            if domains or (
                self.config.browser_integration.enabled
                and key in self.config.browser_integration.executable_to_id
            ):
                entry["browser_domains"] = self._rounded_domain_entries(
                    domains or {DOMAIN_UNKNOWN: seconds}
                )
            programs.append(entry)
        return programs

    def _build_hour_entries(self, day: str) -> list[dict[str, Any]]:
        hours: list[dict[str, Any]] = []
        hour_keys = sorted(key for key in self._hourly_totals if key.startswith(day))
        for hour_key in hour_keys:
            values = self._hourly_totals[hour_key]
            programs: list[dict[str, Any]] = []
            for key in sorted(values, key=lambda k: (self._executables[k].casefold(), self._executables[k])):
                seconds = values[key]
                executable = self._executables[key]
                if seconds <= 0 or key in self.config.excluded_programs:
                    continue
                rounded_seconds = round(seconds, 3)
                if rounded_seconds <= 0:
                    continue
                entry: dict[str, Any] = {
                    "executable": executable,
                    "name": self.config.program_aliases.get(key, executable),
                    "seconds": rounded_seconds,
                    "minutes": round(rounded_seconds / 60.0, 3),
                }
                domains = self._browser_hourly_domains.get(hour_key, {}).get(key)
                if domains or (
                    self.config.browser_integration.enabled
                    and key in self.config.browser_integration.executable_to_id
                ):
                    entry["browser_domains"] = self._rounded_domain_entries(
                        domains or {DOMAIN_UNKNOWN: seconds}
                    )
                programs.append(entry)
            if programs:
                hours.append({"hour_start": hour_key, "programs": programs})
        return hours

    def _rounded_domain_entries(self, domains: dict[str, float]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for hostname in sorted(domains, key=lambda value: (value != DOMAIN_UNKNOWN, value)):
            seconds = round(domains[hostname], 3)
            if seconds <= 0:
                continue
            entries.append(
                {
                    "hostname": hostname,
                    "seconds": seconds,
                    "minutes": round(seconds / 60.0, 3),
                }
            )
        return entries

    def _credit_monotonic_interval(
        self,
        executable: str,
        start: float,
        end: float,
        sample_monotonic: float,
        sample_wall: datetime,
        *,
        domain_label: str | None,
    ) -> None:
        wall_end_utc = sample_wall.astimezone(UTC)
        utc_start = wall_end_utc - timedelta(seconds=sample_monotonic - start)
        utc_end = wall_end_utc - timedelta(seconds=sample_monotonic - end)
        zone = sample_wall.tzinfo
        key = executable.casefold()
        self._executables.setdefault(key, executable)

        for utc_piece_start, utc_piece_end, day_key, hour_key in _iter_local_segments(
            utc_start, utc_end, zone
        ):
            seconds = (utc_piece_end - utc_piece_start).total_seconds()
            per_day = self._totals.setdefault(day_key, {})
            per_day[key] = per_day.get(key, 0.0) + seconds
            per_hour = self._hourly_totals.setdefault(hour_key, {})
            per_hour[key] = per_hour.get(key, 0.0) + seconds
            if domain_label is not None:
                day_domains = self._browser_domains.setdefault(day_key, {}).setdefault(key, {})
                day_domains[domain_label] = day_domains.get(domain_label, 0.0) + seconds
                hour_domains = self._browser_hourly_domains.setdefault(hour_key, {}).setdefault(key, {})
                hour_domains[domain_label] = hour_domains.get(domain_label, 0.0) + seconds


def _hour_start_iso(local_dt: datetime) -> str:
    hour_start = local_dt.replace(
        minute=0,
        second=0,
        microsecond=0,
        fold=local_dt.fold,
    )
    return hour_start.isoformat(timespec="seconds")


def _next_local_hour_boundary_utc(cursor_utc: datetime, zone) -> datetime:
    """First UTC instant after cursor_utc that enters a new local hour bucket."""
    start_key = _hour_start_iso(cursor_utc.astimezone(zone))
    high = cursor_utc + timedelta(hours=4)
    while _hour_start_iso(high.astimezone(zone)) == start_key:
        high += timedelta(hours=1)
        if high - cursor_utc > timedelta(hours=30):
            raise ValueError("Could not find the next local hour boundary.")
    low = cursor_utc
    while low + timedelta(microseconds=1) < high:
        mid = low + (high - low) / 2
        if _hour_start_iso(mid.astimezone(zone)) == start_key:
            low = mid
        else:
            high = mid
    return high


def _next_local_midnight_boundary_utc(cursor_utc: datetime, zone) -> datetime:
    """First UTC instant after cursor_utc on the next local calendar date."""
    start_date = cursor_utc.astimezone(zone).date()
    high = cursor_utc + timedelta(days=1, hours=4)
    while high.astimezone(zone).date() == start_date:
        high += timedelta(hours=1)
        if high - cursor_utc > timedelta(days=2):
            raise ValueError("Could not find the next local midnight boundary.")
    low = cursor_utc
    while low + timedelta(microseconds=1) < high:
        mid = low + (high - low) / 2
        if mid.astimezone(zone).date() == start_date:
            low = mid
        else:
            high = mid
    return high


def _iter_local_segments(
    utc_start: datetime,
    utc_end: datetime,
    zone,
) -> list[tuple[datetime, datetime, str, str]]:
    """Split a UTC interval into local day/hour pieces.

    Hour keys use ISO local timestamps including UTC offset so repeated
    fall-back hours (for example 1 AM EDT and 1 AM EST) never merge.
    """
    segments: list[tuple[datetime, datetime, str, str]] = []
    cursor = utc_start
    while cursor < utc_end:
        local_cursor = cursor.astimezone(zone)
        day_key = local_cursor.date().isoformat()
        hour_key = _hour_start_iso(local_cursor)
        next_hour_utc = _next_local_hour_boundary_utc(cursor, zone)
        next_midnight_utc = _next_local_midnight_boundary_utc(cursor, zone)
        piece_end = min(utc_end, next_hour_utc, next_midnight_utc)
        if piece_end <= cursor:
            piece_end = utc_end
        segments.append((cursor, piece_end, day_key, hour_key))
        cursor = piece_end
    return segments


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
    return ActivitySample(
        sample.monotonic,
        wall,
        executable,
        last_input,
        sample.desktop_locked,
        sample.browser,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value
