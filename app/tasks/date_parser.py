"""Lightweight, deterministic date and time parser using only standard library modules."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class DateParser:
    """Parse common natural-language and structured date/time expressions into UTC datetimes."""

    def __init__(
        self,
        default_tz: timezone | ZoneInfo | None = None,
    ) -> None:
        if default_tz is not None:
            self.default_tz = default_tz
        else:
            self.default_tz = datetime.now().astimezone().tzinfo or timezone.utc

    def parse(
        self,
        text: str,
        reference_time: datetime | None = None,
        is_deadline: bool = True,
    ) -> datetime | None:
        """Parse natural-language or ISO date/time string into a timezone-aware UTC datetime.

        Args:
            text: Input date or time string.
            reference_time: Base reference datetime (defaults to now in default_tz).
            is_deadline: If True and only a date is specified, sets time to local end of day (23:59:59).
                         If False (e.g. reminder), sets time to local standard morning (09:00:00).

        Returns:
            Timezone-aware datetime converted to UTC, or None if the expression cannot be parsed.
        """
        if not isinstance(text, str) or not text.strip():
            return None

        raw = text.strip().lower()

        # Determine effective reference timezone (prioritize reference_time tzinfo if passed)
        effective_tz = (
            reference_time.tzinfo
            if reference_time is not None and reference_time.tzinfo is not None
            else self.default_tz
        )

        ref = reference_time or datetime.now(effective_tz)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=effective_tz)
        local_ref = ref.astimezone(effective_tz)

        # 1. ISO 8601 full datetime (e.g., "2026-11-04T17:00:00" or "2026-11-04 17:00")
        iso_dt = self._try_parse_iso_datetime(raw, effective_tz)
        if iso_dt is not None:
            return iso_dt.astimezone(timezone.utc)

        # 2. ISO date only (e.g. "2026-11-04")
        iso_d = self._try_parse_iso_date(raw)
        if iso_d is not None:
            t = time(23, 59, 59) if is_deadline else time(9, 0, 0)
            target = datetime.combine(iso_d, t, tzinfo=effective_tz)
            return target.astimezone(timezone.utc)

        # 3. Relative delta (e.g. "in 2 hours", "in 30 minutes", "in 1 day")
        delta_dt = self._try_parse_relative_delta(raw, local_ref)
        if delta_dt is not None:
            return delta_dt.astimezone(timezone.utc)

        # 4. Check for standalone time expressions (e.g. "at 5pm", "5:30 pm", "17:00")
        standalone_time = self._try_parse_time_component(raw)
        if standalone_time is not None:
            # If time is already past today, schedule for tomorrow
            candidate = datetime.combine(local_ref.date(), standalone_time, tzinfo=effective_tz)
            if candidate <= local_ref and not is_deadline:
                candidate += timedelta(days=1)
            return candidate.astimezone(timezone.utc)

        # 5. Composite: date keyword or weekday + optional time (e.g. "tomorrow at 5pm", "friday 17:00")
        composite_dt = self._try_parse_composite(raw, local_ref, is_deadline, effective_tz)
        if composite_dt is not None:
            return composite_dt.astimezone(timezone.utc)

        return None

    def _try_parse_iso_datetime(self, text: str, tz: timezone | ZoneInfo) -> datetime | None:
        """Attempt to parse ISO datetime string containing a time component."""
        if ":" not in text:
            return None
        clean = text.replace(" ", "t")
        try:
            dt = datetime.fromisoformat(clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt
        except ValueError:
            return None
        clean = text.replace(" ", "t")
        try:
            dt = datetime.fromisoformat(clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self.default_tz)
            return dt
        except ValueError:
            return None

    def _try_parse_iso_date(self, text: str) -> date | None:
        """Attempt to parse ISO date string YYYY-MM-DD."""
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            try:
                return date.fromisoformat(text)
            except ValueError:
                pass
        return None

    def _try_parse_relative_delta(self, text: str, ref: datetime) -> datetime | None:
        """Parse expressions like 'in 2 hours', 'in 30 minutes', 'in 1 day'."""
        m = re.match(
            r"^(?:in\s+)?(?P<num>\d+)\s+(?P<unit>hours?|hrs?|minutes?|mins?|days?|weeks?)$",
            text,
        )
        if not m:
            return None

        val = int(m.group("num"))
        unit = m.group("unit")

        if unit.startswith("hour") or unit.startswith("hr"):
            return ref + timedelta(hours=val)
        if unit.startswith("min"):
            return ref + timedelta(minutes=val)
        if unit.startswith("day"):
            return ref + timedelta(days=val)
        if unit.startswith("week"):
            return ref + timedelta(weeks=val)
        return None

    def _try_parse_time_component(self, text: str) -> time | None:
        """Parse time expressions such as '5pm', '5:30 pm', '17:00', 'at 9 am'."""
        clean = re.sub(r"^(?:at\s+|by\s+)", "", text).strip()

        # 12-hour format e.g. "5pm", "5:30 pm", "9:00 am"
        m12 = re.match(r"^(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<meridiem>am|pm)$", clean)
        if m12:
            hr = int(m12.group("hour"))
            mn = int(m12.group("min") or 0)
            if hr < 1 or hr > 12 or mn < 0 or mn > 59:
                return None
            is_pm = m12.group("meridiem") == "pm"
            if is_pm and hr < 12:
                hr += 12
            elif not is_pm and hr == 12:
                hr = 0
            return time(hr, mn, 0)

        # 24-hour format e.g. "17:00", "09:30"
        m24 = re.match(r"^(?P<hour>\d{1,2}):(?P<min>\d{2})$", clean)
        if m24:
            hr = int(m24.group("hour"))
            mn = int(m24.group("min"))
            if 0 <= hr <= 23 and 0 <= mn <= 59:
                return time(hr, mn, 0)

        return None

    def _try_parse_composite(
        self,
        text: str,
        ref: datetime,
        is_deadline: bool,
        tz: timezone | ZoneInfo,
    ) -> datetime | None:
        """Parse composite date expressions like 'tomorrow at 5pm', 'friday', 'next monday 10am'."""
        # Separate date portion from time portion if 'at' or space exists
        time_part: time | None = None
        date_text = text

        # Check for trailing time e.g. "tomorrow at 5pm" or "friday 17:00"
        time_split = re.search(r"\b(?:at\s+|by\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2})$", text)
        if time_split:
            candidate_time = self._try_parse_time_component(time_split.group(0))
            if candidate_time is not None:
                time_part = candidate_time
                date_text = text[: time_split.start()].strip()
                date_text = re.sub(r"\s+(?:at|by)$", "", date_text).strip()

        # Resolve target date
        target_date: date | None = None
        clean_date = date_text.strip()

        if clean_date in ("today", ""):
            target_date = ref.date()
        elif clean_date == "tomorrow":
            target_date = ref.date() + timedelta(days=1)
        elif clean_date == "yesterday":
            target_date = ref.date() - timedelta(days=1)
        else:
            # Check for weekday (e.g. "friday", "this friday", "next monday")
            weekday_match = re.match(
                r"^(?:(?:on|this|next)\s+)?(?P<day>monday|tuesday|wednesday|thursday|friday|saturday|sunday)$",
                clean_date,
            )
            if weekday_match:
                target_dow = WEEKDAYS[weekday_match.group("day")]
                current_dow = ref.weekday()
                days_ahead = target_dow - current_dow
                if clean_date.startswith("next "):
                    days_ahead += 7
                elif days_ahead <= 0:
                    days_ahead += 7
                target_date = ref.date() + timedelta(days=days_ahead)

        if target_date is None:
            return None

        if time_part is None:
            time_part = time(23, 59, 59) if is_deadline else time(9, 0, 0)

        return datetime.combine(target_date, time_part, tzinfo=tz)
