"""MCP server exposing the printer as tools."""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
from dataclasses import replace
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import camera
from .config import ConfigError, Settings
from .discovery import find_serial
from .ftps import PrinterFiles
from .printer import BambuPrinter, PrinterError, summarize
from .mesh import MeshError, analyze
from .profiles import ProfileError, machine_name
from .slicer import SliceOptions, SlicerError
from .workflow import NOZZLES, PLATE_TYPES, SliceRequest, library_for, plan, public, slice_request

INSTRUCTIONS = """\
Controls a Bambu Lab A1 mini on the local network and slices models for it.
For a model file (STL/3MF/OBJ): slice_model_file or slice_and_print. They
analyse the geometry and pick the best settings automatically (auto_tune):
preset, layer height, walls, infill, supports, brim, orientation, using the
filament loaded in the printer. Only pass explicit settings the user asked
for; they override the recommendation. Show the user the reasons, warnings
and the time/weight estimate. Ask about the purpose (quality, strength,
speed) when it is unclear and matters. recommend_print_settings previews
the choice without slicing.
For an already sliced .gcode.3mf: upload_and_print. Before starting a print,
check that the printer is idle and the plate is clear; ask the user if unsure.
Live tuning while printing: set_print_speed, set_temperature, set_part_fan.
"""

mcp = MCPServer("bambu-a1-mini", instructions=INSTRUCTIONS)

_settings: Settings | None = None
_printer: BambuPrinter | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def printer() -> BambuPrinter:
    global _printer, _settings
    if _printer is None:
        s = settings()
        if not s.serial:
            _settings = s = replace(s, serial=find_serial(s.host, s.mqtt_port))
        _printer = BambuPrinter(s.host, s.serial, s.access_code, port=s.mqtt_port, sdcard_root=s.sdcard_root)
    return _printer


def files() -> PrinterFiles:
    s = settings()
    return PrinterFiles(s.host, s.access_code, port=s.ftp_port)


async def run(fn, *args, **kwargs) -> Any:
    """Run blocking printer I/O off the event loop and turn errors into tool errors."""
    try:
        return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))
    except (PrinterError, SlicerError, ProfileError, MeshError, ConfigError, FileNotFoundError) as exc:
        raise ToolError(str(exc)) from exc
    except OSError as exc:
        raise ToolError(f"Network error talking to the printer: {exc}") from exc


READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

Plate = Annotated[int, Field(ge=1, le=36, description="Plate number inside the 3MF (1 for single-plate projects)")]
BedLeveling = Annotated[bool, Field(description="Auto bed leveling before the print")]
FlowCali = Annotated[bool, Field(description="Flow dynamics calibration before the print")]
VibrationCali = Annotated[bool, Field(description="Vibration compensation calibration before the print")]
Timelapse = Annotated[bool, Field(description="Record a timelapse to the SD card")]
UseAms = Annotated[bool, Field(description="Feed from the AMS lite. False = external spool holder")]
AmsMapping = Annotated[
    list[int] | None,
    Field(description="AMS slot per filament used in the file, e.g. [0, 2]. Only with use_ams"),
]


# -- status ------------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
async def printer_status(
    raw: Annotated[bool, Field(description="Return the full raw MQTT report instead of a summary")] = False,
) -> dict:
    """Current state: job, progress, layers, temperatures, speed, fans, filament, errors."""
    state = await run(printer().refresh)
    return state if raw else summarize(state)


@mcp.tool(annotations=READ_ONLY)
async def camera_snapshot() -> Image:
    """Take a single JPEG from the built-in camera (e.g. to check the first layer)."""
    s = settings()
    frame = await run(camera.snapshot, s.host, s.access_code, port=s.camera_port)
    return Image(data=frame, format="jpeg")


# -- files -------------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
async def list_sd_files(directory: str = "/") -> list[dict]:
    """List files on the printer's SD card."""
    return await run(files().list, directory)


@mcp.tool(annotations=ACTION)
async def upload_file(
    local_path: Annotated[str, Field(description="Path of a .gcode.3mf/.3mf/.gcode file on this computer")],
    remote_name: Annotated[str | None, Field(description="Name on the SD card, default: same file name")] = None,
) -> dict:
    """Upload a file to the printer's SD card without starting it."""
    return await run(files().upload, local_path, remote_name)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_sd_file(remote_name: str) -> str:
    """Delete a file from the printer's SD card."""
    await run(files().delete, remote_name)
    return f"Deleted {remote_name}"


# -- printing ----------------------------------------------------------------

async def _start(filename: str, **opts) -> dict:
    result = await run(printer().start_print, filename, **opts)
    return {"started": filename, **result}


@mcp.tool(annotations=ACTION)
async def start_print(
    filename: Annotated[str, Field(description="File already on the SD card, e.g. 'benchy.gcode.3mf'")],
    plate: Plate = 1,
    bed_leveling: BedLeveling = True,
    flow_calibration: FlowCali = True,
    vibration_calibration: VibrationCali = True,
    timelapse: Timelapse = False,
    use_ams: UseAms = False,
    ams_mapping: AmsMapping = None,
) -> dict:
    """Start printing a sliced file that is already on the SD card."""
    return await _start(
        filename, plate=plate, bed_leveling=bed_leveling, flow_calibration=flow_calibration,
        vibration_calibration=vibration_calibration, timelapse=timelapse,
        use_ams=use_ams, ams_mapping=ams_mapping,
    )


@mcp.tool(annotations=ACTION)
async def upload_and_print(
    local_path: Annotated[str, Field(description="Sliced .gcode.3mf (or .gcode) on this computer")],
    plate: Plate = 1,
    bed_leveling: BedLeveling = True,
    flow_calibration: FlowCali = True,
    vibration_calibration: VibrationCali = True,
    timelapse: Timelapse = False,
    use_ams: UseAms = False,
    ams_mapping: AmsMapping = None,
) -> dict:
    """Upload a sliced file to the SD card and start it."""
    uploaded = await run(files().upload, local_path)
    started = await _start(
        uploaded["remote_name"], plate=plate, bed_leveling=bed_leveling,
        flow_calibration=flow_calibration, vibration_calibration=vibration_calibration,
        timelapse=timelapse, use_ams=use_ams, ams_mapping=ams_mapping,
    )
    return {"upload": uploaded, **started}


@mcp.tool(annotations=ACTION)
async def pause_print() -> dict:
    """Pause the running print."""
    return await run(printer().pause)


@mcp.tool(annotations=ACTION)
async def resume_print() -> dict:
    """Resume a paused print."""
    return await run(printer().resume)


@mcp.tool(annotations=DESTRUCTIVE)
async def stop_print() -> dict:
    """Cancel the current print. It cannot be resumed afterwards."""
    return await run(printer().stop)


# -- live tuning -------------------------------------------------------------

@mcp.tool(annotations=ACTION)
async def set_print_speed(level: Literal["silent", "standard", "sport", "ludicrous"]) -> dict:
    """Change the speed profile of the running print (50 / 100 / 124 / 166 %)."""
    return await run(printer().set_speed, level)


@mcp.tool(annotations=ACTION)
async def set_temperature(
    nozzle: Annotated[int | None, Field(ge=0, le=300, description="Nozzle target in °C, 0 = off")] = None,
    bed: Annotated[int | None, Field(ge=0, le=80, description="Bed target in °C, 0 = off")] = None,
) -> dict:
    """Set nozzle and/or bed target temperature."""
    if nozzle is None and bed is None:
        raise ToolError("Give nozzle and/or bed")
    out: dict[str, Any] = {}
    if nozzle is not None:
        out["nozzle"] = await run(printer().set_nozzle_temp, nozzle)
    if bed is not None:
        out["bed"] = await run(printer().set_bed_temp, bed)
    return out


@mcp.tool(annotations=ACTION)
async def set_part_fan(percent: Annotated[int, Field(ge=0, le=100)]) -> dict:
    """Set the part cooling fan. The print's G-code may override it at the next layer change."""
    return await run(printer().set_part_fan, percent)


@mcp.tool(annotations=ACTION)
async def set_light(on: bool) -> dict:
    """Switch the printer's LED on or off."""
    return await run(printer().set_light, on)


@mcp.tool(annotations=ACTION)
async def set_filament(
    filament_type: Annotated[str, Field(description="PLA, PETG, TPU, ABS, ASA ...")],
    color: Annotated[str, Field(description="Hex color, e.g. 'FF6A13'")],
    nozzle_temp_min: Annotated[int, Field(ge=150, le=300)],
    nozzle_temp_max: Annotated[int, Field(ge=150, le=300)],
    ams_slot: Annotated[
        int | None, Field(ge=0, le=3, description="AMS lite slot 0-3, empty = external spool holder")
    ] = None,
    filament_id: Annotated[str, Field(description="Bambu filament preset id, e.g. 'GFA00' (optional)")] = "",
) -> dict:
    """Tell the printer which filament is loaded (external spool or an AMS lite slot)."""
    if nozzle_temp_min > nozzle_temp_max:
        raise ToolError("nozzle_temp_min is above nozzle_temp_max")
    ams_id, tray_id = (255, 254) if ams_slot is None else (0, ams_slot)
    return await run(
        printer().set_filament, ams_id=ams_id, tray_id=tray_id, filament_type=filament_type,
        color=color, nozzle_temp_min=nozzle_temp_min, nozzle_temp_max=nozzle_temp_max,
        filament_id=filament_id,
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def send_gcode(gcode: Annotated[str, Field(description="One or more G-code lines")]) -> dict:
    """Send raw G-code (e.g. 'G28' to home). Can move the toolhead; make sure the bed is clear."""
    return await run(printer().gcode, gcode)


# -- slicing -----------------------------------------------------------------

Purpose = Annotated[
    Literal["auto", "balanced", "quality", "strength", "speed"],
    Field(description="auto: decided from the model. quality = fine visible surfaces, "
                      "strength = functional/load-bearing, speed = quick draft"),
]
FilamentType = Annotated[
    str | None, Field(description="PLA, PETG, TPU, PLA-CF ... Empty: use what is loaded in the printer, else PLA")
]
FilamentPreset = Annotated[
    str | None, Field(description="Exact slicer filament preset, e.g. 'Bambu PLA Matte @BBL A1M' (see list_filament_presets)")
]
UsePrinterFilament = Annotated[
    bool, Field(description="Ask the printer which filament is loaded when no filament is given")
]
Nozzle = Annotated[float | None, Field(description="Nozzle diameter 0.2/0.4/0.6/0.8. Empty: read from printer, else 0.4")]
PlateType = Annotated[
    Literal["textured_pei", "smooth_pei", "cool_plate", "engineering"] | None,
    Field(description="Build plate; the A1 mini ships with textured PEI"),
]
AutoTune = Annotated[bool, Field(description="Choose settings from the model geometry (recommended)")]
AutoOrient = Annotated[bool, Field(description="Rotate the model if that clearly saves supports or height")]
LayerHeight = Annotated[float | None, Field(ge=0.04, le=0.64, description="Layer height in mm")]
Infill = Annotated[int | None, Field(ge=0, le=100, description="Sparse infill density in %")]
InfillPattern = Annotated[
    str | None, Field(description="e.g. grid, gyroid, cubic, crosshatch, zig-zag, honeycomb, lightning")
]
Walls = Annotated[int | None, Field(ge=1, le=20, description="Number of perimeters")]
Supports = Annotated[bool | None, Field(description="Force supports on/off")]
SupportType = Annotated[Literal["normal", "tree"] | None, Field(description="Support style")]
Brim = Annotated[Literal["auto", "none", "outer"] | None, Field(description="Brim mode")]
NozzleTemp = Annotated[int | None, Field(ge=150, le=300, description="Nozzle temperature °C")]
BedTemp = Annotated[int | None, Field(ge=0, le=80, description="Bed temperature °C")]
ExtraProcess = Annotated[
    dict[str, Any] | None,
    Field(description="Any other process setting by its slicer key, e.g. {'top_shell_layers': 5}"),
]
ExtraFilament = Annotated[
    dict[str, Any] | None,
    Field(description="Any other filament setting by its slicer key, e.g. {'fan_max_speed': 80}"),
]
ModelPath = Annotated[str, Field(description="STL/3MF/OBJ/STEP file on this computer")]


def _printer_info(ams_slot: int | None = None) -> dict:
    """Loaded filament and nozzle as reported by the printer; empty if it is not reachable."""
    try:
        p = printer()
        state = p.raw_state() or p.refresh(timeout=4)
    except Exception:
        return {}
    report = state.get("print", {})
    tray = report.get("vt_tray") or {}
    if ams_slot is not None:
        for unit in (report.get("ams") or {}).get("ams") or []:
            for t in unit.get("tray", []):
                if str(unit.get("id")) == "0" and str(t.get("id")) == str(ams_slot):
                    tray = t
    info = {"type": tray.get("tray_type") or None, "id": tray.get("tray_info_idx") or None}
    try:
        info["nozzle"] = float(report.get("nozzle_diameter"))
    except (TypeError, ValueError):
        pass
    return info


def _build_request(model_path: str, purpose: str, filament_type: str | None, filament_preset: str | None,
                   use_printer_filament: bool, nozzle: float | None, plate_type: str | None,
                   auto_tune: bool, auto_orient: bool, options: SliceOptions,
                   ams_slot: int | None = None) -> tuple[SliceRequest, dict]:
    s = settings()
    source: dict[str, Any] = {}
    filament_id = None
    if use_printer_filament and not (filament_type or filament_preset) or nozzle is None and s.nozzle is None:
        info = _printer_info(ams_slot)
        if use_printer_filament and not (filament_type or filament_preset) and (info.get("type") or info.get("id")):
            filament_type, filament_id = info.get("type"), info.get("id")
            source["filament"] = f"loaded in printer ({filament_type}, id {filament_id})"
        if nozzle is None and s.nozzle is None and info.get("nozzle") in NOZZLES:
            nozzle = info["nozzle"]
            source["nozzle"] = "reported by printer"
    req = SliceRequest(
        model_path=model_path, purpose=purpose, filament_type=filament_type, filament_id=filament_id,
        filament_preset=filament_preset, nozzle=nozzle or s.nozzle or 0.4,
        plate_type=plate_type or s.plate_type, auto_tune=auto_tune, auto_orient=auto_orient, options=options,
    )
    return req, source


def _options(layer_height, infill_percent, infill_pattern, wall_loops, supports, support_type, brim,
             nozzle_temp, bed_temp, extra_process_settings, extra_filament_settings) -> SliceOptions:
    return SliceOptions(
        layer_height=layer_height, infill_percent=infill_percent, infill_pattern=infill_pattern,
        wall_loops=wall_loops, supports=supports, support_type=support_type, brim=brim,
        nozzle_temp=nozzle_temp, bed_temp=bed_temp,
        extra_process=extra_process_settings or {}, extra_filament=extra_filament_settings or {},
    )


@mcp.tool(annotations=READ_ONLY)
async def analyze_model(model_path: ModelPath, auto_orient: AutoOrient = True) -> dict:
    """Geometry check: size, volume, bed contact, overhangs needing support, best orientation, fit on the A1 mini."""
    analysis, _ = await run(analyze, model_path, auto_orient=auto_orient)
    return analysis.to_dict()


@mcp.tool(annotations=READ_ONLY)
async def recommend_print_settings(
    model_path: ModelPath,
    purpose: Purpose = "auto",
    filament_type: FilamentType = None,
    filament_preset: FilamentPreset = None,
    use_printer_filament: UsePrinterFilament = True,
    nozzle: Nozzle = None,
    plate_type: PlateType = None,
) -> dict:
    """Preview the settings slice_model_file would choose, with reasons and warnings, without slicing."""
    req, source = await run(_build_request, model_path, purpose, filament_type, filament_preset,
                            use_printer_filament, nozzle, plate_type, True, True, SliceOptions())
    result = public(await run(plan, req, settings()))
    return result | ({"detected": source} if source else {})


@mcp.tool(annotations=READ_ONLY)
async def list_filament_presets(
    contains: Annotated[str | None, Field(description="Filter, e.g. 'PETG' or 'Matte'")] = None,
    nozzle: Nozzle = None,
) -> list[str]:
    """Filament presets of the installed slicer that fit the A1 mini."""
    lib, _ = await run(library_for, settings())
    if not lib:
        raise ToolError("No slicer profiles found; install Bambu Studio/OrcaSlicer or set BAMBU_PROFILE_DIR")
    machine = machine_name(nozzle or settings().nozzle or 0.4)
    names = [p["name"] for p in await run(lib.selectable, "filament", machine)]
    if contains:
        names = [n for n in names if contains.lower() in n.lower()]
    return sorted(names)


@mcp.tool(annotations=ACTION)
async def slice_model_file(
    model_path: ModelPath,
    purpose: Purpose = "auto",
    filament_type: FilamentType = None,
    filament_preset: FilamentPreset = None,
    use_printer_filament: UsePrinterFilament = True,
    nozzle: Nozzle = None,
    plate_type: PlateType = None,
    auto_tune: AutoTune = True,
    auto_orient: AutoOrient = True,
    layer_height: LayerHeight = None,
    infill_percent: Infill = None,
    infill_pattern: InfillPattern = None,
    wall_loops: Walls = None,
    supports: Supports = None,
    support_type: SupportType = None,
    brim: Brim = None,
    nozzle_temp: NozzleTemp = None,
    bed_temp: BedTemp = None,
    extra_process_settings: ExtraProcess = None,
    extra_filament_settings: ExtraFilament = None,
) -> dict:
    """Slice a model with automatically chosen best settings. Explicit settings override the choice.

    Returns the .gcode.3mf path, chosen presets, every change with its reason, and the
    slicer's print time and filament estimate.
    """
    opts = _options(layer_height, infill_percent, infill_pattern, wall_loops, supports, support_type, brim,
                    nozzle_temp, bed_temp, extra_process_settings, extra_filament_settings)
    req, source = await run(_build_request, model_path, purpose, filament_type, filament_preset,
                            use_printer_filament, nozzle, plate_type, auto_tune, auto_orient, opts)
    result = await run(slice_request, req, settings())
    return result | ({"detected": source} if source else {})


@mcp.tool(annotations=ACTION)
async def slice_and_print(
    model_path: ModelPath,
    purpose: Purpose = "auto",
    filament_type: FilamentType = None,
    filament_preset: FilamentPreset = None,
    use_printer_filament: UsePrinterFilament = True,
    nozzle: Nozzle = None,
    plate_type: PlateType = None,
    auto_tune: AutoTune = True,
    auto_orient: AutoOrient = True,
    layer_height: LayerHeight = None,
    infill_percent: Infill = None,
    infill_pattern: InfillPattern = None,
    wall_loops: Walls = None,
    supports: Supports = None,
    support_type: SupportType = None,
    brim: Brim = None,
    nozzle_temp: NozzleTemp = None,
    bed_temp: BedTemp = None,
    extra_process_settings: ExtraProcess = None,
    extra_filament_settings: ExtraFilament = None,
    bed_leveling: BedLeveling = True,
    flow_calibration: FlowCali = True,
    vibration_calibration: VibrationCali = True,
    timelapse: Timelapse = False,
    ams_slot: Annotated[int | None, Field(ge=0, le=3, description="AMS lite slot to print from; empty = external spool")] = None,
) -> dict:
    """Slice with automatically chosen best settings, upload and start the print (plate 1)."""
    opts = _options(layer_height, infill_percent, infill_pattern, wall_loops, supports, support_type, brim,
                    nozzle_temp, bed_temp, extra_process_settings, extra_filament_settings)
    req, source = await run(_build_request, model_path, purpose, filament_type, filament_preset,
                            use_printer_filament, nozzle, plate_type, auto_tune, auto_orient, opts, ams_slot)
    result = await run(slice_request, req, settings())
    uploaded = await run(files().upload, result["sliced_file"])
    started = await _start(
        uploaded["remote_name"], plate=1, bed_leveling=bed_leveling, flow_calibration=flow_calibration,
        vibration_calibration=vibration_calibration, timelapse=timelapse,
        use_ams=ams_slot is not None, ams_mapping=[ams_slot] if ams_slot is not None else None,
    )
    return result | ({"detected": source} if source else {}) | {"upload": uploaded, **started}


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP connector for a Bambu Lab A1 mini")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for streamable-http")
    parser.add_argument("--port", type=int, default=8765, help="Port for streamable-http")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
