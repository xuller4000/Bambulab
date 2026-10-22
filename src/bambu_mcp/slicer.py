"""Running the Bambu Studio or OrcaSlicer command line.

Profiles are passed as complete (flattened) JSON files written to a temporary
folder. Values keep the slicer's own format: scalars are strings, and keys that
hold a list (most filament keys, one entry per extruder) stay lists.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORT_TYPES = {"normal": "normal(auto)", "tree": "tree(auto)"}
BRIM_TYPES = {"auto": "auto_brim", "none": "no_brim", "outer": "outer_only"}


class SlicerError(RuntimeError):
    pass


@dataclass
class SliceOptions:
    """Settings the user asked for explicitly. They win over any recommendation."""

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


def build_command(
    slicer: str,
    model: Path,
    output_dir: Path,
    output_name: str,
    *,
    machine: Path | None,
    process: Path | None,
    filament: Path | None,
    arrange: bool,
) -> list[str]:
    cmd = [slicer, "--slice", "0", "--outputdir", str(output_dir), "--export-3mf", output_name]
    if machine and process:
        cmd += ["--load-settings", f"{machine};{process}"]
    if filament:
        cmd += ["--load-filaments", str(filament)]
    if arrange:
        cmd += ["--arrange", "1", "--orient", "0"]
    cmd.append(str(model))
    return cmd


def output_name_for(model: Path) -> str:
    name = model.name
    stem = name[: -len(".gcode.3mf")] if name.lower().endswith(".gcode.3mf") else model.stem
    return f"{stem}.gcode.3mf"


def run_slicer(
    executable: str,
    model: Path,
    output_dir: Path,
    *,
    output_name: str | None = None,
    machine: dict | None = None,
    process: dict | None = None,
    filament: dict | None = None,
    timeout: float = 900,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_name = output_name or output_name_for(model)
    with tempfile.TemporaryDirectory(prefix="bambu-mcp-") as tmp:
        files: dict[str, Path | None] = {}
        for kind, data in (("machine", machine), ("process", process), ("filament", filament)):
            if data is None:
                files[kind] = None
                continue
            files[kind] = Path(tmp) / f"{kind}.json"
            files[kind].write_text(json.dumps(data, indent=1), encoding="utf-8")
        cmd = build_command(
            executable, model, output_dir, output_name,
            machine=files["machine"], process=files["process"], filament=files["filament"],
            arrange=model.suffix.lower() != ".3mf",
        )
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise SlicerError(f"Slicer timed out after {timeout:.0f} s") from exc
        except OSError as exc:
            raise SlicerError(f"Could not start the slicer: {exc}") from exc

    result = output_dir / output_name
    if run.returncode != 0 or not result.is_file():
        tail = "\n".join((run.stdout + run.stderr).strip().splitlines()[-25:])
        raise SlicerError(f"Slicing failed (exit {run.returncode}):\n{tail}")
    return result


def read_slice_info(path: Path) -> dict:
    """Print time, filament use and slicer warnings from a sliced 3MF."""
    try:
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("Metadata/slice_info.config"))
    except (KeyError, zipfile.BadZipFile, ET.ParseError):
        return {}
    plates = []
    for plate in root.iter("plate"):
        meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
        info: dict[str, Any] = {"plate": int(meta.get("index", len(plates) + 1))}
        if (sec := meta.get("prediction")) and sec.isdigit():
            s = int(sec)
            info["print_time"] = f"{s // 3600} h {s % 3600 // 60:02d} min"
            info["print_seconds"] = s
        if weight := meta.get("weight"):
            info["weight_g"] = float(weight)
        if meta.get("support_used") is not None:
            info["support_used"] = meta["support_used"] == "true"
        if meta.get("outside") == "true":
            info["outside_build_area"] = True
        info["filaments"] = [
            {k: f.get(k) for k in ("type", "color", "used_m", "used_g", "tray_info_idx") if f.get(k)}
            for f in plate.findall("filament")
        ]
        warnings = [w.get("msg") for w in plate.findall("warning") if w.get("msg")]
        if warnings:
            info["slicer_warnings"] = warnings
        plates.append(info)
    return {"plates": plates} if len(plates) != 1 else plates[0]
