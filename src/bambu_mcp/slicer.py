"""Slicing through the Bambu Studio or OrcaSlicer command line.

Profiles are the JSON files the slicer itself uses (machine, process,
filament). Overrides are written into temporary copies, so the originals stay
untouched. Values keep the slicer's own format: scalars are strings, and keys
that hold a list (most filament keys, one entry per extruder) stay lists.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORT_TYPES = {"normal": "normal(auto)", "tree": "tree(auto)"}
BRIM_TYPES = {"auto": "auto_brim", "none": "no_brim", "outer": "outer_only"}


class SlicerError(RuntimeError):
    pass


@dataclass
class SliceOptions:
    layer_height: float | None = None
    infill_percent: int | None = None
    infill_pattern: str | None = None
    wall_loops: int | None = None
    supports: bool | None = None
    support_type: str | None = None
    brim: str | None = None
    nozzle_temp: int | None = None
    bed_temp: int | None = None
    extra_process: dict[str, Any] = field(default_factory=dict)
    extra_filament: dict[str, Any] = field(default_factory=dict)

    def process_overrides(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.layer_height is not None:
            if not 0.04 <= self.layer_height <= 0.32:
                raise SlicerError("layer_height must be between 0.04 and 0.32 mm for a 0.4 mm nozzle")
            out["layer_height"] = f"{self.layer_height:g}"
        if self.infill_percent is not None:
            if not 0 <= self.infill_percent <= 100:
                raise SlicerError("infill_percent must be 0-100")
            out["sparse_infill_density"] = f"{self.infill_percent}%"
        if self.infill_pattern:
            out["sparse_infill_pattern"] = self.infill_pattern
        if self.wall_loops is not None:
            out["wall_loops"] = str(self.wall_loops)
        if self.supports is not None:
            out["enable_support"] = "1" if self.supports else "0"
        if self.support_type:
            if self.support_type not in SUPPORT_TYPES:
                raise SlicerError(f"support_type must be one of {list(SUPPORT_TYPES)}")
            out["support_type"] = SUPPORT_TYPES[self.support_type]
        if self.brim:
            if self.brim not in BRIM_TYPES:
                raise SlicerError(f"brim must be one of {list(BRIM_TYPES)}")
            out["brim_type"] = BRIM_TYPES[self.brim]
        out.update(self.extra_process)
        return out

    def filament_overrides(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.nozzle_temp is not None:
            out["nozzle_temperature"] = str(self.nozzle_temp)
            out["nozzle_temperature_initial_layer"] = str(self.nozzle_temp)
        if self.bed_temp is not None:
            for plate in ("hot_plate_temp", "textured_plate_temp"):
                out[plate] = str(self.bed_temp)
                out[f"{plate}_initial_layer"] = str(self.bed_temp)
        out.update(self.extra_filament)
        return out

    def is_empty(self) -> bool:
        return not self.process_overrides() and not self.filament_overrides()


def apply_overrides(profile: dict, overrides: dict[str, Any]) -> dict:
    result = dict(profile)
    for key, value in overrides.items():
        value = value if isinstance(value, (list, str)) else _to_str(value)
        if isinstance(result.get(key), list) and not isinstance(value, list):
            value = [value] * max(1, len(result[key]))
        result[key] = value
    return result


def _to_str(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return f"{value:g}" if isinstance(value, float) else str(value)


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SlicerError(f"Cannot read profile {path}: {exc}") from exc


def build_command(
    slicer: str,
    model: Path,
    output_dir: Path,
    output_name: str,
    *,
    machine: Path | None,
    process: Path | None,
    filament: Path | None,
) -> list[str]:
    cmd = [slicer, "--slice", "0", "--outputdir", str(output_dir), "--export-3mf", output_name]
    if machine and process:
        cmd += ["--load-settings", f"{machine};{process}"]
    if filament:
        cmd += ["--load-filaments", str(filament)]
    if model.suffix.lower() != ".3mf":
        cmd += ["--arrange", "1", "--orient", "1"]
    cmd.append(str(model))
    return cmd


def slice_model(
    model_path: str | Path,
    options: SliceOptions,
    *,
    slicer: str | None,
    machine: Path | None,
    process: Path | None,
    filament: Path | None,
    output_dir: Path,
    timeout: float = 600,
) -> Path:
    if not slicer:
        raise SlicerError("BAMBU_SLICER_PATH is not set (path to the Bambu Studio or OrcaSlicer executable)")
    executable = shutil.which(slicer) or (slicer if Path(slicer).exists() else None)
    if not executable:
        raise SlicerError(f"Slicer not found: {slicer}")
    model = Path(model_path).expanduser().resolve()
    if not model.is_file():
        raise SlicerError(f"Model not found: {model}")
    if model.suffix.lower() not in (".stl", ".3mf", ".obj", ".step", ".stp"):
        raise SlicerError("Supported model formats: .stl, .3mf, .obj, .step")

    has_profiles = bool(machine and process and filament)
    if not has_profiles and (model.suffix.lower() != ".3mf" or not options.is_empty()):
        raise SlicerError(
            "Slicing a bare model or changing settings needs BAMBU_PROFILE_MACHINE, "
            "BAMBU_PROFILE_PROCESS and BAMBU_PROFILE_FILAMENT"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = model.name[: -len(".gcode.3mf")] if model.name.lower().endswith(".gcode.3mf") else model.stem
    output_name = f"{stem}.gcode.3mf"

    with tempfile.TemporaryDirectory(prefix="bambu-mcp-") as tmp:
        tmpdir = Path(tmp)
        if has_profiles:
            proc_file = tmpdir / "process.json"
            fil_file = tmpdir / "filament.json"
            proc_file.write_text(json.dumps(apply_overrides(_load_json(process), options.process_overrides())))
            fil_file.write_text(json.dumps(apply_overrides(_load_json(filament), options.filament_overrides())))
            machine_file: Path | None = machine
        else:
            proc_file = fil_file = machine_file = None

        cmd = build_command(
            executable, model, output_dir, output_name,
            machine=machine_file, process=proc_file, filament=fil_file,
        )
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise SlicerError(f"Slicer timed out after {timeout:.0f} s") from exc

    result = output_dir / output_name
    if run.returncode != 0 or not result.is_file():
        tail = "\n".join((run.stdout + run.stderr).strip().splitlines()[-20:])
        raise SlicerError(f"Slicing failed (exit {run.returncode}):\n{tail}")
    return result
