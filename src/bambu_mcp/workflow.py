"""Analyse -> recommend -> pick presets -> slice, as one pipeline."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .mesh import MeshError, analyze, write_stl
from .profiles import (
    ProfileError,
    ProfileLibrary,
    _types_of,
    default_profile_dirs,
    find_slicer,
    machine_name,
    pick_filament,
    pick_process,
)
from .slicer import SliceOptions, SlicerError, apply_overrides, output_name_for, read_slice_info, run_slicer
from .tuning import PrintPlan, recommend

PLATE_TYPES = {
    "textured_pei": "Textured PEI Plate",
    "smooth_pei": "High Temp Plate",
    "cool_plate": "Cool Plate",
    "engineering": "Engineering Plate",
}
NOZZLES = (0.2, 0.4, 0.6, 0.8)
MODEL_SUFFIXES = (".stl", ".3mf", ".obj", ".step", ".stp")


@dataclass
class SliceRequest:
    model_path: str
    purpose: str = "auto"
    filament_type: str | None = None
    filament_id: str | None = None
    filament_preset: str | None = None
    nozzle: float = 0.4
    plate_type: str = "textured_pei"
    auto_tune: bool = True
    auto_orient: bool = True
    options: SliceOptions = field(default_factory=SliceOptions)


def _model(path: str) -> Path:
    model = Path(path).expanduser().resolve()
    if not model.is_file():
        raise SlicerError(f"Model not found: {model}")
    if model.suffix.lower() not in MODEL_SUFFIXES:
        raise SlicerError("Supported model formats: .stl, .3mf, .obj, .step")
    return model


def _check_request(req: SliceRequest) -> None:
    if req.nozzle not in NOZZLES:
        raise SlicerError(f"Nozzle must be one of {NOZZLES} mm")
    if req.plate_type not in PLATE_TYPES:
        raise SlicerError(f"plate_type must be one of {list(PLATE_TYPES)}")
    lh = req.options.layer_height
    if lh is not None and not 0.2 * req.nozzle <= lh <= 0.8 * req.nozzle + 1e-9:
        raise SlicerError(
            f"Layer height {lh} mm does not suit a {req.nozzle} mm nozzle "
            f"({0.2 * req.nozzle:.2f}-{0.8 * req.nozzle:.2f} mm)"
        )


_library_cache: dict[tuple, ProfileLibrary] = {}


def library_for(settings: Settings) -> tuple[ProfileLibrary, Any]:
    slicer = None
    try:
        slicer = find_slicer(settings.slicer_path)
    except ProfileError:
        if not settings.profile_dirs:
            raise
    dirs = [Path(d) for d in settings.profile_dirs] or default_profile_dirs(slicer)
    # A real install has a few thousand preset files; rescan only when a folder changed.
    key = tuple(
        (str(d / kind), (d / kind).stat().st_mtime_ns if (d / kind).is_dir() else 0)
        for d in dirs for kind in ("machine", "process", "filament")
    )
    if key not in _library_cache:
        _library_cache.clear()
        _library_cache[key] = ProfileLibrary(dirs)
    return _library_cache[key], slicer


def _choose_presets(req: SliceRequest, settings: Settings, lib: ProfileLibrary, plan_lh: float, purpose: str):
    machine_preset = machine_name(req.nozzle)
    if settings.profile_machine:
        machine = lib.flatten_file("machine", settings.profile_machine)
    else:
        machine = lib.flatten("machine", machine_preset)
    if settings.profile_filament and not (req.filament_preset or req.filament_id or req.filament_type):
        filament = lib.flatten_file("filament", settings.profile_filament)
    else:
        filament = pick_filament(
            lib, machine["name"], preset_name=req.filament_preset,
            filament_id=req.filament_id, filament_type=req.filament_type,
        )
    if settings.profile_process:
        process = lib.flatten_file("process", settings.profile_process)
    else:
        process = pick_process(lib, machine["name"], req.options.layer_height or plan_lh, purpose)
    return machine, process, filament


def plan(req: SliceRequest, settings: Settings) -> dict:
    """Everything up to slicing: geometry, recommended settings, chosen presets."""
    _check_request(req)
    model = _model(req.model_path)
    try:
        analysis, rotated = analyze(model, auto_orient=req.auto_orient)
    except MeshError as exc:
        if req.auto_tune:
            raise SlicerError(f"{exc}. Export the model as STL/3MF or pass auto_tune=false.") from exc
        analysis, rotated = None, None

    lib, slicer = library_for(settings)
    filament_type = req.filament_type
    filament = None
    if lib:
        try:
            filament = pick_filament(
                lib, machine_name(req.nozzle), preset_name=req.filament_preset,
                filament_id=req.filament_id, filament_type=req.filament_type,
            )
            filament_type = filament_type or _types_of(filament)[0]
        except ProfileError:
            if req.filament_preset:
                raise
    filament_type = filament_type or "PLA"

    rec: PrintPlan | None = None
    if req.auto_tune and analysis is not None:
        rec = recommend(analysis, filament_type=filament_type, purpose=req.purpose, nozzle=req.nozzle)
    purpose = rec.purpose if rec else ("balanced" if req.purpose == "auto" else req.purpose)
    layer_height = req.options.layer_height or (rec.layer_height if rec else round(0.2 * req.nozzle / 0.4, 2))

    result: dict[str, Any] = {
        "model": model.name,
        "analysis": analysis.to_dict() if analysis else None,
        "recommendation": rec.to_dict() if rec else None,
        "explicit_settings": req.options.process_overrides() | req.options.filament_overrides(),
        "plate_type": PLATE_TYPES[req.plate_type],
        "_rotated": rotated,
        "_model": model,
        "_slicer": slicer,
        "_lib": lib,
        "_purpose": purpose,
        "_layer_height": layer_height,
    }
    if lib:
        machine, process, filament = _choose_presets(req, settings, lib, layer_height, purpose)
        result["presets"] = {"machine": machine["name"], "process": process["name"], "filament": filament["name"]}
        result["_profiles"] = (machine, process, filament)
    else:
        result["presets"] = None
        result["note"] = "No Bambu Studio/OrcaSlicer profiles found; install the slicer or set BAMBU_PROFILE_DIR"
    return result


def public(result: dict) -> dict:
    return {k: v for k, v in result.items() if not k.startswith("_") and v not in (None, {}, [])}


def slice_request(req: SliceRequest, settings: Settings) -> dict:
    result = plan(req, settings)
    slicer = result["_slicer"]
    if slicer is None:
        raise SlicerError(
            "No slicer found. Install Bambu Studio or OrcaSlicer, or set BAMBU_SLICER_PATH to its executable."
        )
    if "_profiles" not in result:
        raise SlicerError(result["note"])
    machine, process, filament = result["_profiles"]
    rec = result["recommendation"] or {}

    process_changes = {"curr_bed_type": result["plate_type"]}
    process_changes |= rec.get("process_overrides", {})
    process_changes |= req.options.process_overrides()
    filament_changes = rec.get("filament_overrides", {}) | req.options.filament_overrides()
    final_process = apply_overrides(process, process_changes)
    final_filament = apply_overrides(filament, filament_changes)

    model: Path = result["_model"]
    with tempfile.TemporaryDirectory(prefix="bambu-mcp-model-") as tmp:
        source = model
        if result["_rotated"] is not None:
            source = Path(tmp) / f"{model.stem}.stl"
            write_stl(source, result["_rotated"])
        out = run_slicer(
            slicer.executable, source, settings.slice_output_dir,
            output_name=output_name_for(model),
            machine=machine, process=final_process, filament=final_filament,
        )

    result["sliced_file"] = str(out)
    result["applied_changes"] = {"process": process_changes, "filament": filament_changes}
    result["estimate"] = read_slice_info(out)
    return public(result)
