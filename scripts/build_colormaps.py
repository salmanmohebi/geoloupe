"""Regenerate src/geoloupe/colormaps.json (256-entry RGB lookup tables).

The server (PNG tiles) and the web client (data tiles) use this one file, so
both render identical colours. Run when the colormap list changes:

    uv run --with matplotlib --with cmocean --with cmcrameri python scripts/build_colormaps.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parents[1] / "src" / "geoloupe" / "colormaps.json"

# name -> (category, provider, provider name)
CMAPS: dict[str, tuple[str, str, str]] = {
    # perceptually uniform sequential
    "viridis": ("sequential", "matplotlib", "viridis"),
    "cividis": ("sequential", "matplotlib", "cividis"),
    "plasma": ("sequential", "matplotlib", "plasma"),
    "inferno": ("sequential", "matplotlib", "inferno"),
    "magma": ("sequential", "matplotlib", "magma"),
    "batlow": ("sequential", "crameri", "batlow"),
    "oslo": ("sequential", "crameri", "oslo"),
    "lajolla": ("sequential", "crameri", "lajolla"),
    # domain colormaps (cmocean)
    "thermal": ("sequential", "cmocean", "thermal"),
    "haline": ("sequential", "cmocean", "haline"),
    "rain": ("sequential", "cmocean", "rain"),
    "speed": ("sequential", "cmocean", "speed"),
    "ice": ("sequential", "cmocean", "ice"),
    "deep": ("sequential", "cmocean", "deep"),
    "dense": ("sequential", "cmocean", "dense"),
    "amp": ("sequential", "cmocean", "amp"),
    "tempo": ("sequential", "cmocean", "tempo"),
    "solar": ("sequential", "cmocean", "solar"),
    "topo": ("other", "cmocean", "topo"),
    # ColorBrewer and classic sequential
    "YlGnBu": ("sequential", "matplotlib", "YlGnBu"),
    "YlOrRd": ("sequential", "matplotlib", "YlOrRd"),
    "Blues": ("sequential", "matplotlib", "Blues"),
    "Greens": ("sequential", "matplotlib", "Greens"),
    "BuPu": ("sequential", "matplotlib", "BuPu"),
    "gray": ("sequential", "matplotlib", "gray"),
    "terrain": ("other", "matplotlib", "terrain"),
    "turbo": ("other", "matplotlib", "turbo"),
    # diverging
    "RdBu_r": ("diverging", "matplotlib", "RdBu_r"),
    "RdYlBu_r": ("diverging", "matplotlib", "RdYlBu_r"),
    "coolwarm": ("diverging", "matplotlib", "coolwarm"),
    "BrBG": ("diverging", "matplotlib", "BrBG"),
    "PiYG": ("diverging", "matplotlib", "PiYG"),
    "PuOr_r": ("diverging", "matplotlib", "PuOr_r"),
    "Spectral_r": ("diverging", "matplotlib", "Spectral_r"),
    "balance": ("diverging", "cmocean", "balance"),
    "delta": ("diverging", "cmocean", "delta"),
    "curl": ("diverging", "cmocean", "curl"),
    "vik": ("diverging", "crameri", "vik"),
    "roma": ("diverging", "crameri", "roma"),
    # cyclic (directions, phases)
    "twilight": ("cyclic", "matplotlib", "twilight"),
    "phase": ("cyclic", "cmocean", "phase"),
}


def _get(provider: str, name: str):
    if provider == "matplotlib":
        import matplotlib

        return matplotlib.colormaps[name]
    if provider == "cmocean":
        import cmocean

        return cmocean.cm.cmap_d[name]
    if provider == "crameri":
        import cmcrameri.cm as cmc

        return getattr(cmc, name)
    raise ValueError(provider)


def main() -> None:
    out = {}
    for key, (category, provider, name) in CMAPS.items():
        rgb = (
            (np.asarray(_get(provider, name)(np.linspace(0, 1, 256)))[:, :3] * 255)
            .round()
            .astype(np.uint8)
        )
        out[key] = {"category": category, "provider": provider, "lut": rgb.tobytes().hex()}
    OUT.write_text(json.dumps(out, indent=0, separators=(",", ":")) + "\n")
    print(f"wrote {len(out)} colormaps to {OUT} ({OUT.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
