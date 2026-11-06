"""Time axes: ISO labels for the API and nearest-time matching with a tolerance.

Times are compared as float days since 1970-01-01 *in the axis' own calendar*,
so cftime calendars (noleap, 360_day, ...) from climate models work as well as
numpy datetimes. Labels are ISO 8601 strings: a date (``1978-07-01``) when all
times are at midnight, otherwise date and time (``1978-07-01T06:00``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

import numpy as np
import pandas as pd

_ISO_RE = re.compile(
    r"^\s*(-?\d{1,4})-(\d{1,2})(?:-(\d{1,2}))?(?:[T ](\d{1,2})(?::(\d{2})(?::(\d{2})(?:\.\d+)?)?)?)?"
    r"\s*(Z|[+-]00:?00)?\s*$"
)


class TimeError(ValueError):
    """A requested time cannot be parsed or does not exist on the axis."""


class TimeFormatError(TimeError):
    """The time string is not ISO 8601 (a client error, unlike a date missing from a calendar)."""


def _parse_fields(label: str) -> tuple[int, int, int, int, int, int]:
    m = _ISO_RE.match(label)
    if not m:
        raise TimeFormatError(
            f"cannot parse time {label!r}; use ISO 8601 such as 1978-07-01 or 1978-07-01T12:00"
        )
    y, mo, d, h, mi, s = (
        int(g) if g else default
        for g, default in zip(m.groups()[:6], (0, 1, 1, 0, 0, 0), strict=True)
    )
    return y, mo, d, h, mi, s


def default_tolerance(days: np.ndarray) -> float:
    """How far (in days) a requested time may be from a stored one and still match.

    Monthly and coarser data are matched by calendar period instead (see `TimeAxis.index`).
    Finer data: half a step but at most half a day, so daily data that are only sampled every few
    days (e.g. model test days) never show a neighbouring day's field for the requested date.
    Set `time_tolerance` in the dataset config for multi-day means.
    """
    if days.size < 2:
        return 0.5
    half = float(np.median(np.abs(np.diff(days)))) / 2
    return half if half >= 12.5 else min(half, 0.5)


@dataclass
class TimeAxis:
    """A time coordinate of one source."""

    values: np.ndarray  # float64 days since 1970-01-01 in `calendar`
    calendar: str  # "standard" for numpy datetimes, else the cftime calendar
    labels: list[str]
    tolerance: float  # days
    numpy: bool = True  # built from numpy datetime64 (pandas arithmetic is safe)
    explicit_tolerance: bool = False

    @classmethod
    def from_values(cls, raw: np.ndarray, tolerance: str | None = None) -> TimeAxis:
        raw = np.asarray(raw)
        if np.issubdtype(raw.dtype, np.datetime64):
            ns = raw.astype("datetime64[ns]")
            days = ns.astype("int64") / 86_400e9
            idx = pd.DatetimeIndex(ns)
            midnight = bool((idx.normalize() == idx).all())
            # vectorised ISO labels ("2023-07-18" or "2023-07-18T14:00"): hourly axes have ~10^6
            labels = np.datetime_as_string(ns, unit="D" if midnight else "m").tolist()
            calendar = "standard"
        elif raw.dtype == object and raw.size and hasattr(raw.flat[0], "calendar"):
            import cftime

            calendar = raw.flat[0].calendar
            days = np.asarray(
                cftime.date2num(list(raw), "days since 1970-01-01", calendar=calendar),
                dtype=np.float64,
            )
            midnight = all(t.hour == 0 and t.minute == 0 and t.second == 0 for t in raw)
            labels = [
                f"{t.year:04d}-{t.month:02d}-{t.day:02d}"
                + ("" if midnight else f"T{t.hour:02d}:{t.minute:02d}")
                for t in raw
            ]
        else:
            raise TimeError(f"unsupported time coordinate dtype {raw.dtype}")
        if tolerance is not None:
            tol = pd.Timedelta(tolerance).total_seconds() / 86_400
        else:
            tol = default_tolerance(days)
        is_numpy = bool(np.issubdtype(raw.dtype, np.datetime64))
        return cls(
            np.asarray(days, dtype=np.float64),
            calendar,
            labels,
            tol,
            is_numpy,
            tolerance is not None,
        )

    @cached_property
    def period(self) -> str | None:
        """'month' or 'year' for monthly / yearly data, else None."""
        if self.values.size < 2:
            return None
        d = float(np.median(np.diff(np.sort(self.values))))
        if 27 <= d <= 31.5:
            return "month"
        if 359 <= d <= 367:
            return "year"
        return None

    def __len__(self) -> int:
        return int(self.values.size)

    @cached_property
    def increasing(self) -> bool:
        return bool(np.all(np.diff(self.values) > 0))

    @cached_property
    def _period_index(self) -> dict[str, int]:
        n = 7 if self.period == "month" else 4
        out: dict[str, int] = {}
        for i, lab in enumerate(self.labels):
            out.setdefault(lab[:n], i)
        return out

    @cached_property
    def _order(self) -> np.ndarray:
        return np.argsort(self.values, kind="stable")

    def to_days(self, label: str) -> float:
        """Parse an ISO label into days since 1970-01-01 in this axis' calendar."""
        y, mo, d, h, mi, s = _parse_fields(label)
        if self.numpy:  # pandas covers 1677–2262; cftime axes (any calendar) use cftime
            try:
                ts = pd.Timestamp(year=y, month=mo, day=d, hour=h, minute=mi, second=s)
            except ValueError as e:
                raise TimeError(f"invalid time {label!r}: {e}") from e
            return float(ts.value / 86_400e9)
        import cftime

        try:
            t = cftime.datetime(y, mo, d, h, mi, s, calendar=self.calendar)
        except ValueError as e:
            raise TimeError(f"invalid time {label!r} for calendar {self.calendar}: {e}") from e
        return float(cftime.date2num(t, "days since 1970-01-01", calendar=self.calendar))

    def index(self, label: str) -> int | None:
        """Index of the stored time for `label`, or None.

        Monthly and yearly data: the value of the month / year that contains the requested time,
        whatever day the values are stamped with (1st, 16th, end of month). Otherwise: the nearest
        time within the tolerance.
        """
        if self.period is not None and not self.explicit_tolerance:
            y, mo, *_ = _parse_fields(label)
            self.to_days(label)  # a date that does not exist in this calendar is an error
            key = f"{y:04d}-{mo:02d}" if self.period == "month" else f"{y:04d}"
            hit = self._period_index.get(key)
            return hit
        t = self.to_days(label)
        v = self.values[self._order]
        k = int(np.searchsorted(v, t))
        best: int | None = None
        best_d = np.inf
        for j in (k - 1, k):
            if 0 <= j < v.size and abs(v[j] - t) < best_d:
                best, best_d = j, abs(v[j] - t)
        if best is None or best_d > self.tolerance + 1e-9:
            return None
        return int(self._order[best])

    def range(self, start: str | None, end: str | None) -> np.ndarray:
        """Indices (ascending in time) of all times within [start, end]."""
        lo = self.to_days(start) if start else -np.inf
        hi = self.to_days(end) if end else np.inf
        sel = (self.values >= lo - 1e-9) & (self.values <= hi + 1e-9)
        idx = np.nonzero(sel)[0]
        return idx[np.argsort(self.values[idx], kind="stable")]

    def step_label(self) -> str | None:
        """Typical step, e.g. '1 day', '6 hours', '1 month' (None for a single time)."""
        if self.values.size < 2:
            return None
        d = float(np.median(np.diff(np.sort(self.values))))
        if 27 <= d <= 31.5:
            return "1 month"
        if 359 <= d <= 367:
            return "1 year"
        if d >= 1:
            d = round(d, 6)
            return f"{d:g} day" + ("s" if d != 1 else "")
        hours = round(d * 24, 6)
        return f"{hours:g} hour" + ("s" if hours != 1 else "")


def merge_labels(axes: list[TimeAxis]) -> list[str]:
    """Union of the labels of several axes, in time order (ISO labels sort correctly)."""
    distinct: list[TimeAxis] = []
    for a in axes:
        if not any(a.labels is d.labels for d in distinct):  # variables of one store share an axis
            distinct.append(a)
    if len(distinct) == 1 and distinct[0].increasing:
        return list(distinct[0].labels)  # already sorted and unique: skip sorting 10^6 strings
    out: set[str] = set()
    for a in distinct:
        out.update(a.labels)
    return sorted(out)


LIST_LIMIT = 20_000  # longer time axes are sent as start/step/count when they are regular


def compact_axis(labels: list[str], limit: int = LIST_LIMIT) -> dict[str, object] | None:
    """{start, end, count, step_seconds, format} for a long, regular axis of ISO labels, else None.

    Hourly data over decades has hundreds of thousands of steps: listing them would be tens of
    megabytes of JSON. A regular axis is fully described by its start, step and count. Only
    labels on the standard calendar are compacted (clients compute them with plain dates).
    """
    n = len(labels)
    if n <= limit:
        return None
    try:
        t = np.array(labels, dtype="datetime64[s]")  # vectorised ISO parsing
    except ValueError:
        return None
    d = np.diff(t).astype(np.int64)
    step = int(d[0])
    if step <= 0 or not np.all(d == step):  # every step, not a sample: one odd time breaks it
        return None
    return {
        "start": labels[0],
        "end": labels[-1],
        "count": n,
        "step_seconds": step,
        "format": "datetime" if "T" in labels[0] else "date",
    }
