"""Error definitions for error panes: P (compared data) against T (reference).

The definitions live in ``errors.json`` and are also sent to the web client, so the browser
preview and the exact server maps use the same formulas (see geoloupe/formula.py).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

import numpy as np

from . import formula as fx

CUSTOM = "custom"
DATA_NAMES = frozenset({"P", "T"})


@lru_cache(maxsize=1)
def metric_table() -> dict[str, Any]:
    """The parsed errors.json: {"metrics": [{id, group, label, symbol, formula, units, scale,
    params?, classes?}]}."""
    text = resources.files("geoloupe").joinpath("errors.json").read_text()
    data: dict[str, Any] = json.loads(text)
    return data


def metrics() -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in metric_table()["metrics"]}


@dataclass(frozen=True)
class ErrorSpec:
    """One error definition with its parameters, ready to evaluate."""

    metric: str
    formula: str
    params: dict[str, float] = field(default_factory=dict)
    units: str = "same"  # same | squared | % | "" | class | free text (custom)
    scale: str = "diverging"
    classes: tuple[dict[str, Any], ...] = ()

    @property
    def node(self) -> fx.Node:
        return _parsed(self.formula)

    def units_for(self, t_units: str | None) -> str:
        """Units of the error map, given the reference's units."""
        u = t_units or ""
        if self.units == "same":
            return u
        if self.units == "squared":
            return f"({u})²" if " " in u or "/" in u else (f"{u}²" if u else "")
        if self.units == "class":
            return ""
        return self.units

    def compute(self, p: np.ndarray, t: np.ndarray) -> np.ndarray:
        """Error per pixel (float32); NaN wherever P or T has no value."""
        p = np.asarray(p, dtype=np.float64)
        t = np.asarray(t, dtype=np.float64)
        out = fx.evaluate(self.node, {"P": p, "T": t, **self.params})
        out = np.broadcast_to(out, np.broadcast_shapes(p.shape, t.shape))
        return np.where(np.isnan(p) | np.isnan(t), np.nan, out).astype(np.float32)


@lru_cache(maxsize=256)
def _parsed(text: str) -> fx.Node:
    return fx.parse(text)


def resolve(
    metric: str,
    formula: str | None = None,
    params: Mapping[str, float] | None = None,
    units: str | None = None,
) -> ErrorSpec:
    """Build an ErrorSpec from a metric id (or "custom" with a formula) and parameters.

    Missing parameters take their defaults; unknown parameters, unknown metrics and formulas that
    cannot be read raise ValueError (FormulaError for formulas, with the position).
    """
    given = dict(params or {})
    for k, v in given.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            raise ValueError(f"parameter {k!r} must be a finite number")
    if metric == CUSTOM:
        if not formula:
            raise ValueError("a custom error needs a formula")
        if given:
            raise ValueError("a custom formula takes no parameters; write the numbers in it")
        node = _parsed(formula)
        fx.check(node, DATA_NAMES)
        return ErrorSpec(CUSTOM, formula, {}, units if units is not None else "", "diverging")
    table = metrics()
    if metric not in table:
        raise ValueError(f"unknown error {metric!r}; available: {', '.join(table)}, {CUSTOM}")
    m = table[metric]
    declared = m.get("params", {})
    unknown = sorted(set(given) - set(declared))
    if unknown:
        raise ValueError(f"error {metric!r} has no parameter {unknown[0]!r}")
    values = {k: float(given.get(k, p["default"])) for k, p in declared.items()}
    fx.check(_parsed(m["formula"]), DATA_NAMES | set(values))
    return ErrorSpec(
        metric,
        m["formula"],
        values,
        m["units"],
        m["scale"],
        tuple(m.get("classes", ())),
    )
