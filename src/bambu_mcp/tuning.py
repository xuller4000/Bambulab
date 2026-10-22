"""Rule-based choice of print settings from model geometry, material and purpose.

The Bambu presets are the base; these rules only pick the right preset and
change what the geometry or the purpose clearly calls for. Every change comes
with a reason so the user can see why.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .mesh import ModelAnalysis

PURPOSES = ("auto", "balanced", "quality", "strength", "speed")

# Materials Bambu does not offer for the A1 mini (open frame, no enclosure).
ENCLOSURE_MATERIALS = ("ABS", "ASA", "PC", "PA", "PAHT", "PPS", "PPA", "PET-CF", "PA6")
ABRASIVE_HINTS = ("CF", "GF", "GLOW")


@dataclass
class PrintPlan:
    purpose: str
    nozzle: float
    filament_type: str
    layer_height: float
    process_overrides: dict[str, Any] = field(default_factory=dict)
    filament_overrides: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _snap(value: float) -> float:
    return round(round(value / 0.02) * 0.02, 2)


def recommend(
    analysis: ModelAnalysis,
    *,
    filament_type: str = "PLA",
    purpose: str = "auto",
    nozzle: float = 0.4,
) -> PrintPlan:
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}")
    ftype = filament_type.upper()
    a = analysis
    reasons: list[str] = []
    warnings: list[str] = []

    if purpose == "auto":
        if a.max_dim < 40:
            purpose = "quality"
            reasons.append(f"Small part ({a.max_dim:.0f} mm): quality settings, the extra time is minor")
        else:
            purpose = "balanced"
            reasons.append("No purpose given: balanced settings")

    # -- layer height (values for 0.4 mm, scaled for other nozzles) -------------
    if purpose == "quality":
        lh = 0.12 if a.height <= 50 else 0.16
        why = "fine layers for smooth curved surfaces" + ("" if lh == 0.12 else "; 0.16 mm because the part is tall")
    elif purpose == "strength":
        lh = 0.20
        why = "0.20 mm gives good layer bonding at reasonable speed"
    elif purpose == "speed":
        lh = 0.28 if a.height > 60 and a.max_dim >= 30 else 0.24
        why = "thick layers for a fast print"
    else:
        lh = 0.16 if a.max_dim < 30 else 0.20
        why = "standard layer height" if lh == 0.20 else "slightly finer layers for a small part"
    lh = _snap(lh * nozzle / 0.4)
    reasons.append(f"Layer height {lh} mm: {why}")

    proc: dict[str, Any] = {}
    fil: dict[str, Any] = {}

    # -- walls and infill ---------------------------------------------------------
    if purpose == "strength":
        proc["wall_loops"] = 6
        proc["sparse_infill_density"] = "35%"
        proc["sparse_infill_pattern"] = "gyroid"
        proc["top_shell_layers"] = 6
        proc["bottom_shell_layers"] = 5
        reasons.append("Strength: 6 walls, 35 % gyroid infill, thicker top and bottom; walls carry most of the load")
    elif purpose == "quality":
        proc["wall_loops"] = 3
        reasons.append("3 walls so the infill does not show through the surface")
    elif purpose == "balanced" and a.max_dim >= 30:
        proc["wall_loops"] = 3
        reasons.append("3 instead of 2 walls: noticeably stiffer for a few percent more time")
    if purpose != "strength" and a.volume_cm3 < 3:
        proc["sparse_infill_density"] = "25%"
        reasons.append("Tiny part: 25 % infill costs seconds and avoids pillowing on top surfaces")

    if purpose == "quality" and a.top_flat_area_mm2 >= 300 and not ftype.startswith("TPU"):
        if a.top_flat_area_mm2 <= 6000:
            proc["ironing_type"] = "top"
            reasons.append(f"Ironing on the flat top surface ({a.top_flat_area_mm2:.0f} mm²) for a smooth finish")
        else:
            warnings.append(
                f"Flat top of {a.top_flat_area_mm2:.0f} mm²: ironing would add a lot of time, so it is off. "
                "Enable it with extra_process_settings {'ironing_type': 'top'} if the top must be glossy."
            )

    # -- supports ----------------------------------------------------------------
    if a.needs_support:
        proc["enable_support"] = 1
        proc["support_type"] = "tree(auto)"
        reasons.append(
            f"Tree supports: {a.support_area_mm2:.0f} mm² of surfaces flatter than 30° would sag; "
            "tree supports touch the part less and come off cleanly"
        )
    else:
        proc["enable_support"] = 0
        if a.overhang_area_mm2 > 0:
            reasons.append("No supports: remaining overhangs are steep enough to print freely")
        else:
            reasons.append("No supports needed")

    # -- bed adhesion ------------------------------------------------------------
    risky_contact = a.bed_contact_mm2 < 100 and a.height > 10
    if a.slenderness >= 3 or risky_contact:
        proc["brim_type"] = "outer_only"
        reasons.append(
            f"Brim: {'tall and narrow' if a.slenderness >= 3 else 'small contact area'} "
            f"(contact {a.bed_contact_mm2:.0f} mm², height/width {a.slenderness}); "
            "the A1 mini moves the bed, so tall parts get pushed around"
        )
    elif ftype.startswith(("PETG", "PCTG")) and max(a.size_mm[0], a.size_mm[1]) > 100:
        proc["brim_type"] = "outer_only"
        reasons.append("Brim: large PETG footprint tends to lift at the corners")
    if a.slenderness >= 6:
        warnings.append("Very slender part: consider printing several at once or using speed level 'silent'")

    # -- material ------------------------------------------------------------------
    if any(ftype.startswith(m) for m in ENCLOSURE_MATERIALS):
        warnings.append(
            f"{ftype} needs an enclosed, heated chamber. Bambu has no A1 mini profile for it; "
            "expect warping and cracking. PETG or PLA Tough are the practical alternatives."
        )
    if any(h in ftype for h in ABRASIVE_HINTS):
        warnings.append(f"{ftype} is abrasive: use a hardened steel nozzle, the stock A1 mini nozzle wears quickly")
    if ftype.startswith("TPU"):
        warnings.append("TPU: feed from the external spool holder (not the AMS lite) unless it is 'TPU for AMS'")
        if proc.get("enable_support"):
            warnings.append("Supports in TPU are hard to remove; reorienting the part is usually better")
    if ftype.startswith("PETG") and proc.get("enable_support"):
        warnings.append("PETG bonds strongly to its own supports; if removal is hard, raise support_top_z_distance to 0.25")

    reasons.append("Temperatures, cooling and flow limits come from the matching Bambu filament profile")

    if not a.fits_build_volume:
        warnings.extend(a.notes)
    if a.orientation_gain:
        reasons.append(f"Rotated to '{a.orientation}': {a.orientation_gain}")

    return PrintPlan(
        purpose=purpose,
        nozzle=nozzle,
        filament_type=ftype,
        layer_height=lh,
        process_overrides=proc,
        filament_overrides=fil,
        reasons=reasons,
        warnings=warnings,
    )
