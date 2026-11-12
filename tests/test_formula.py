"""The formula language and the error definitions (shared cases with the web client)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from geoloupe import errors
from geoloupe import formula as fx

CASES = json.loads((Path(__file__).parent / "formula_cases.json").read_text())


def arr(values: list) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in values], dtype=np.float64)


def same(got: np.ndarray, expect: list) -> None:
    np.testing.assert_allclose(got, arr(expect), rtol=1e-6, equal_nan=True)


@pytest.mark.parametrize("case", CASES["values"], ids=lambda c: c["formula"])
def test_values(case: dict) -> None:
    node = fx.parse(case["formula"])
    fx.check(node, {"P", "T"})
    p, t = arr(case["P"]), arr(case["T"])
    got = np.broadcast_to(fx.evaluate(node, {"P": p, "T": t}), p.shape)
    same(got, case["expect"])


@pytest.mark.parametrize("case", CASES["errors"], ids=lambda c: repr(c["formula"]))
def test_errors(case: dict) -> None:
    with pytest.raises(fx.FormulaError) as info:
        fx.check(fx.parse(case["formula"]), {"P", "T"})
    assert info.value.message == case["message"]
    assert info.value.pos == case["pos"]


@pytest.mark.parametrize("case", CASES["metrics"], ids=lambda c: c["metric"])
def test_metrics(case: dict) -> None:
    spec = errors.resolve(case["metric"], case.get("formula"), case.get("params"))
    same(spec.compute(arr(case["P"]), arr(case["T"])), case["expect"])


def test_every_metric_is_tested_and_valid() -> None:
    tested = {c["metric"] for c in CASES["metrics"]}
    assert set(errors.metrics()) | {errors.CUSTOM} == tested
    for mid, m in errors.metrics().items():
        spec = errors.resolve(mid)
        assert spec.params == {k: p["default"] for k, p in m.get("params", {}).items()}
        if m["units"] == "class":
            assert [c["value"] for c in m["classes"]] == sorted(c["value"] for c in m["classes"])


def test_compute_keeps_shape_and_dtype() -> None:
    p = np.ones((4, 5), dtype=np.float32)
    t = np.zeros((4, 5), dtype=np.float32)
    out = errors.resolve("events").compute(p, t)
    assert out.shape == (4, 5) and out.dtype == np.float32
    assert (out == 3).all()  # P ≥ 1 everywhere, T never: false alarms
    const = errors.resolve(errors.CUSTOM, "2").compute(p, t)  # a constant still fills the map
    assert const.shape == (4, 5) and (const == 2).all()


def test_units() -> None:
    assert errors.resolve("bias").units_for("K") == "K"
    assert errors.resolve("sq").units_for("K") == "K²"
    assert errors.resolve("sq").units_for("mm/day") == "(mm/day)²"
    assert errors.resolve("rel").units_for("K") == "%"
    assert errors.resolve("ratio").units_for("K") == ""
    assert errors.resolve("events").units_for("mm/day") == ""
    assert errors.resolve(errors.CUSTOM, "P - T", units="K").units_for("mm") == "K"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("nope",), "unknown error 'nope'"),
        (("rel", None, {"h": 1}), "has no parameter 'h'"),
        (("rel", None, {"eps": float("nan")}), "finite number"),
        (("rel", None, {"eps": True}), "finite number"),
        (("custom",), "needs a formula"),
        (("custom", "P - T", {"eps": 1}), "takes no parameters"),
        (("custom", "P - eps"), "unknown name 'eps'"),
    ],
)
def test_resolve_errors(args: tuple, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        errors.resolve(*args)


def test_limits() -> None:
    with pytest.raises(fx.FormulaError, match="too long"):
        fx.parse("P+" * 300 + "T")
    with pytest.raises(fx.FormulaError, match="nested too deeply"):
        fx.parse("(" * 60 + "P" + ")" * 60)
    with pytest.raises(fx.FormulaError, match="nested too deeply"):
        fx.parse("-" * 60 + "P")
    fx.parse("(" * 30 + "P" + ")" * 30)  # reasonable nesting is fine


def test_no_python_evaluation() -> None:
    # Attribute access, strings, indexing and assignments are not part of the language.
    for text in ("P.__class__", "'a'", "P[0]", "P = 1", "__import__('os')", "lambda: 1"):
        with pytest.raises(fx.FormulaError):
            fx.check(fx.parse(text), {"P", "T"})
