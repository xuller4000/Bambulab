import json
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest


def box(x0, y0, z0, x1, y1, z1):
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], float)
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return v[np.array(f)]


def tee():
    """A T standing upright: 60 mm crossbar on a 10 x 10 x 30 mm stem."""
    return np.concatenate([box(25, 0, 0, 35, 10, 30), box(0, 0, 30, 25, 10, 36),
                           box(25, 0, 30, 35, 10, 36), box(35, 0, 30, 60, 10, 36)])


A1M = "Bambu Lab A1 mini 0.4 nozzle"


def write_profiles(root: Path) -> Path:
    """Minimal profile tree shaped like Bambu Studio's resources/profiles/BBL."""
    presets = {
        "machine": [
            {"name": "fdm_machine_common", "nozzle_diameter": ["0.4"], "printable_height": "250"},
            {"name": A1M, "inherits": "fdm_machine_common", "instantiation": "true", "printable_height": "180"},
        ],
        "process": [
            {"name": "fdm_process_common", "layer_height": "0.2", "wall_loops": "2", "sparse_infill_density": "15%",
             "enable_support": "0", "outer_wall_speed": ["200"], "instantiation": "false"},
            {"name": "0.20mm Standard @BBL A1M", "inherits": "fdm_process_common", "instantiation": "true",
             "compatible_printers": [A1M]},
            {"name": "0.20mm Strength @BBL A1M", "inherits": "fdm_process_common", "instantiation": "true",
             "wall_loops": "6", "compatible_printers": [A1M]},
            {"name": "0.12mm Fine @BBL A1M", "inherits": "fdm_process_common", "instantiation": "true",
             "layer_height": "0.12", "compatible_printers": [A1M]},
            {"name": "0.12mm High Quality @BBL A1M", "inherits": "fdm_process_common", "instantiation": "true",
             "layer_height": "0.12", "outer_wall_speed": ["60"], "compatible_printers": [A1M]},
            {"name": "0.20mm Standard @BBL X1C", "inherits": "fdm_process_common", "instantiation": "true",
             "compatible_printers": ["Bambu Lab X1 Carbon 0.4 nozzle"]},
        ],
        "filament": [
            {"name": "fdm_filament_pla", "filament_type": ["PLA"], "nozzle_temperature": ["220"],
             "textured_plate_temp": ["65"], "instantiation": "false"},
            {"name": "Generic PLA @BBL A1M", "inherits": "fdm_filament_pla", "filament_id": "GFL99",
             "instantiation": "true", "compatible_printers": [A1M]},
            {"name": "Bambu PLA Basic @BBL A1M", "inherits": "fdm_filament_pla", "filament_id": "GFA00",
             "instantiation": "true", "compatible_printers": [A1M]},
            {"name": "Generic PETG @BBL A1M", "filament_type": ["PETG"], "nozzle_temperature": ["255"],
             "filament_id": "GFG99", "instantiation": "true", "compatible_printers": [A1M]},
        ],
    }
    base = root / "BBL"
    for kind, items in presets.items():
        (base / kind).mkdir(parents=True)
        for p in items:
            (base / kind / f"{p['name']}.json").write_text(json.dumps(p))
    return base


@pytest.fixture
def profile_dir(tmp_path):
    return write_profiles(tmp_path / "profiles")


@pytest.fixture
def fake_slicer(tmp_path):
    """Records its arguments and the profiles it got, then writes a sliced 3MF with slice_info."""
    log = tmp_path / "calls.json"
    script = tmp_path / "fake_slicer.py"
    script.write_text(textwrap.dedent(f"""
        import json, sys, pathlib, zipfile
        args = sys.argv[1:]
        outdir = pathlib.Path(args[args.index('--outputdir') + 1])
        name = args[args.index('--export-3mf') + 1]
        machine, process = args[args.index('--load-settings') + 1].split(';')
        fil = args[args.index('--load-filaments') + 1]
        model = pathlib.Path(args[-1])
        rec = {{'args': args, 'machine': json.load(open(machine)), 'process': json.load(open(process)),
               'filament': json.load(open(fil)), 'model_name': model.name, 'model_size': model.stat().st_size}}
        pathlib.Path({str(log)!r}).write_text(json.dumps(rec))
        info = ('<config><plate><metadata key="index" value="1"/><metadata key="prediction" value="4530"/>'
                '<metadata key="weight" value="12.5"/><metadata key="support_used" value="false"/>'
                '<filament id="1" type="PLA" color="#FFFFFF" used_m="4.19" used_g="12.5"/></plate></config>')
        with zipfile.ZipFile(outdir / name, 'w') as z:
            z.writestr('Metadata/slice_info.config', info)
    """))
    wrapper = tmp_path / "slicer"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    wrapper.chmod(0o755)
    return wrapper, log
