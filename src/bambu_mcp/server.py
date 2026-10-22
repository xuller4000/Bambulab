"""MCP server exposing the printer as tools."""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import camera
from .config import ConfigError, Settings
from .ftps import PrinterFiles
from .printer import BambuPrinter, PrinterError, summarize
from .slicer import SliceOptions, SlicerError, slice_model

INSTRUCTIONS = """\
Controls a Bambu Lab A1 mini on the local network.
Typical flow: printer_status -> upload_and_print (already sliced .gcode.3mf)
or slice_and_print (STL/3MF plus settings). Before starting a print, check that
the printer is idle and the plate is clear; ask the user if unsure.
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
    global _printer
    if _printer is None:
        s = settings()
        _printer = BambuPrinter(s.host, s.serial, s.access_code, port=s.mqtt_port, sdcard_root=s.sdcard_root)
    return _printer


def files() -> PrinterFiles:
    s = settings()
    return PrinterFiles(s.host, s.access_code, port=s.ftp_port)


async def run(fn, *args, **kwargs) -> Any:
    """Run blocking printer I/O off the event loop and turn errors into tool errors."""
    try:
        return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))
    except (PrinterError, SlicerError, ConfigError, FileNotFoundError) as exc:
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

def _options(**kw) -> SliceOptions:
    extra_process = kw.pop("extra_process_settings") or {}
    extra_filament = kw.pop("extra_filament_settings") or {}
    return SliceOptions(**kw, extra_process=extra_process, extra_filament=extra_filament)


async def _slice(model_path: str, opts: SliceOptions):
    s = settings()
    return await run(
        slice_model, model_path, opts, slicer=s.slicer_path, machine=s.profile_machine,
        process=s.profile_process, filament=s.profile_filament, output_dir=s.slice_output_dir,
    )


LayerHeight = Annotated[float | None, Field(ge=0.04, le=0.32, description="Layer height in mm")]
Infill = Annotated[int | None, Field(ge=0, le=100, description="Sparse infill density in %")]
InfillPattern = Annotated[
    str | None, Field(description="e.g. grid, gyroid, cubic, crosshatch, zig-zag, honeycomb, lightning")
]
Walls = Annotated[int | None, Field(ge=1, le=20, description="Number of perimeters")]
Supports = Annotated[bool | None, Field(description="Generate supports")]
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


@mcp.tool(annotations=ACTION)
async def slice_model_file(
    model_path: Annotated[str, Field(description="STL/3MF/OBJ/STEP file on this computer")],
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
    """Slice a model with the configured profiles plus the given overrides. Returns the .gcode.3mf path."""
    opts = _options(**{k: v for k, v in locals().items() if k != "model_path"})
    out = await _slice(model_path, opts)
    return {"sliced_file": str(out), "overrides": opts.process_overrides() | opts.filament_overrides()}


@mcp.tool(annotations=ACTION)
async def slice_and_print(
    model_path: Annotated[str, Field(description="STL/3MF/OBJ/STEP file on this computer")],
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
    use_ams: UseAms = False,
    ams_mapping: AmsMapping = None,
) -> dict:
    """Slice a model with the given settings, upload it and start the print (plate 1)."""
    start_keys = ("bed_leveling", "flow_calibration", "vibration_calibration", "timelapse", "use_ams", "ams_mapping")
    args = {k: v for k, v in locals().items() if k != "model_path"}
    start_opts = {k: args.pop(k) for k in start_keys}
    opts = _options(**args)
    out = await _slice(model_path, opts)
    uploaded = await run(files().upload, out)
    started = await _start(uploaded["remote_name"], plate=1, **start_opts)
    return {"sliced_file": str(out), "upload": uploaded, **started}


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
