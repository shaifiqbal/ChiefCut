"""Visual 'looks' applied with FFmpeg filters (no AI models, fast on any PC).

Each look is a filtergraph fragment with one input and one output. Internal
labels all start with "lk" so they never clash with other labels.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Look:
    key: str
    label: str
    description: str
    filter: str          # empty = no change
    speed_note: str = ""


def _posterize(levels_expr: str) -> str:
    return f"lutrgb=r='{levels_expr}':g='{levels_expr}':b='{levels_expr}'"


LOOKS: dict[str, Look] = {
    "none": Look("none", "Original", "No style — the video as it is.", ""),
    "comic": Look(
        "comic", "Comic book",
        "Flat, punchy colours with black ink outlines.",
        # Each branch starts with its own converter (scale) so FFmpeg doesn't force
        # both branches of the split into the same (grey) pixel format.
        "split[lka][lkb];"
        "[lka]scale=iw:ih,gblur=sigma=1.2,eq=saturation=1.45:contrast=1.1,format=gbrp,"
        + _posterize("trunc(val/56)*56+28") + "[lkc];"
        "[lkb]scale=iw:ih,format=gray,gblur=sigma=0.8,edgedetect=low=0.08:high=0.22,negate,"
        "scale=iw:ih,format=gbrp[lke];"
        "[lkc][lke]blend=all_mode=multiply",
        "a little slower",
    ),
    "sketch": Look(
        "sketch", "Pencil sketch",
        "Black-and-white pencil drawing.",
        "format=gray,split[lka][lkb];[lkb]negate,gblur=sigma=10[lkc];"
        "[lka][lkc]blend=all_mode=dodge,curves=all='0/0 0.55/0.4 1/1'",
    ),
    "popart": Look(
        "popart", "Pop-art",
        "Super-saturated poster colours.",
        "eq=saturation=2.2:contrast=1.25,format=gbrp,"
        + _posterize("if(gt(val,140),255,if(gt(val,70),128,0))"),
    ),
    "vintage": Look(
        "vintage", "Vintage film",
        "Faded colours, film grain and a soft vignette.",
        "curves=preset=vintage,eq=saturation=0.75:contrast=0.95:gamma=1.05,"
        "noise=alls=10:allf=t,vignette=angle=PI/4.5",
    ),
    "noir": Look(
        "noir", "Noir (black & white)",
        "High-contrast black and white with light grain.",
        "hue=s=0,eq=contrast=1.35:brightness=-0.03,noise=alls=6:allf=t,vignette=angle=PI/4",
    ),
    "vivid": Look(
        "vivid", "Vivid",
        "Richer colours and crisper detail — subtle.",
        "eq=saturation=1.35:contrast=1.08,unsharp=5:5:0.6",
    ),
}


def get_look(key: str | None) -> Look:
    return LOOKS.get(key or "none", LOOKS["none"])
