"""Sensible display defaults (colormap, title) guessed from a variable's metadata.

The guesses use the CF ``standard_name``, the variable name and the units.
Anything set in the config overrides them.
"""

from __future__ import annotations

import re
from typing import Any

from terrascope.config import Style

# (pattern on name/standard_name, colormap, resampling)
_RULES: list[tuple[str, str, str | None]] = [
    (
        r"(^|_)(u|v|ua|va|uas|vas|u10|v10|u100|v100)($|_)|eastward_wind|northward_wind",
        "RdBu_r",
        None,
    ),
    (r"anom|bias|diff|delta|error|change", "RdBu_r", None),
    (
        r"temp|(^|_)(tas|tasmax|tasmin|t2m|t2|ta|tmean|tmax|tmin|sst|tos|skt|ts)($|_)",
        "RdYlBu_r",
        None,
    ),
    (r"precip|rain|(^|_)(pr|prec|tp|prcp|rr|cp|lsp)($|_)", "YlGnBu", None),
    (r"wind_speed|(^|_)(sfcwind|ws|wspd|si10|wind)($|_)", "speed", None),
    (r"humid|(^|_)(hurs|huss|rh|hus|q|r)($|_)", "YlGnBu", None),
    (r"pressure|(^|_)(ps|sp|psl|msl|slp|ps0)($|_)", "viridis", None),
    (r"radiation|flux|(^|_)(rsds|rlds|ssrd|strd|rsdt|ssr)($|_)", "inferno", None),
    (r"cloud|(^|_)(clt|tcc|cc)($|_)", "gray", None),
    (r"elev|altitude|orog|topo|height|(^|_)(dem|z|hgt)($|_)", "terrain", None),
    (r"snow|ice|(^|_)(snd|snw|sd|sic)($|_)", "ice", None),
    (r"land_cover|landcover|class|category|(^|_)(lc|lulc|mask)($|_)", "turbo", "nearest"),
]


def default_style(name: str, attrs: dict[str, Any]) -> Style:
    """Guess a colormap (and resampling for categorical data) for a variable."""
    keys = " ".join(
        str(s).lower()
        for s in (name, attrs.get("standard_name", ""), attrs.get("long_name", ""))
        if s
    )
    cmap, resampling = "viridis", None
    for pattern, cm, rs in _RULES:
        if re.search(pattern, keys):
            cmap, resampling = cm, rs
            break
    if attrs.get("flag_values") is not None or attrs.get("flag_meanings"):
        cmap, resampling = "turbo", "nearest"
    title = attrs.get("long_name") or attrs.get("standard_name")
    return Style(title=str(title) if title else None, colormap=cmap, resampling=resampling)  # type: ignore[arg-type]


def classes_from_attrs(attrs: dict[str, Any]) -> dict[int, dict[str, str | None]] | None:
    """Classes of categorical data from CF flag_values / flag_meanings / flag_colors."""
    values = attrs.get("flag_values")
    if values is None:
        return None
    vals = [int(v) for v in (values.tolist() if hasattr(values, "tolist") else list(values))]
    meanings = str(attrs.get("flag_meanings", "")).split()
    colors = str(attrs.get("flag_colors", "")).split()
    out: dict[int, dict[str, str | None]] = {}
    for i, v in enumerate(vals):
        name = meanings[i].replace("_", " ") if i < len(meanings) else str(v)
        out[v] = {"name": name, "color": colors[i] if i < len(colors) else None}
    return out
