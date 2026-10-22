import json
import sys
import textwrap

import pytest

from bambu_mcp.slicer import SliceOptions, SlicerError, apply_overrides, slice_model


def test_overrides_keep_list_shape():
    profile = {"nozzle_temperature": ["220"], "layer_height": "0.2"}
    out = apply_overrides(profile, {"nozzle_temperature": 230, "layer_height": 0.12, "enable_support": True})
    assert out == {"nozzle_temperature": ["230"], "layer_height": "0.12", "enable_support": "1"}


def test_options_mapping():
    o = SliceOptions(layer_height=0.16, infill_percent=20, supports=True, support_type="tree", brim="none",
                     nozzle_temp=210, bed_temp=55, extra_process={"top_shell_layers": 5})
    assert o.process_overrides() == {
        "layer_height": "0.16", "sparse_infill_density": "20%", "enable_support": "1",
        "support_type": "tree(auto)", "brim_type": "no_brim", "top_shell_layers": 5,
    }
    f = o.filament_overrides()
    assert f["nozzle_temperature"] == "210" and f["textured_plate_temp"] == "55"
    with pytest.raises(SlicerError):
        SliceOptions(support_type="organic").process_overrides()


@pytest.fixture
def fake_slicer(tmp_path):
    """A script that records its arguments and the profiles it got, then writes the output."""
    log = tmp_path / "calls.json"
    script = tmp_path / "fake_slicer.py"
    script.write_text(textwrap.dedent(f"""
        import json, sys, pathlib
        args = sys.argv[1:]
        outdir = pathlib.Path(args[args.index('--outputdir') + 1])
        name = args[args.index('--export-3mf') + 1]
        settings = args[args.index('--load-settings') + 1].split(';')
        fil = args[args.index('--load-filaments') + 1]
        rec = {{'args': args, 'process': json.load(open(settings[1])), 'filament': json.load(open(fil))}}
        pathlib.Path({str(log)!r}).write_text(json.dumps(rec))
        (outdir / name).write_bytes(b'PK')
    """))
    wrapper = tmp_path / "slicer"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    wrapper.chmod(0o755)
    return wrapper, log


def test_slice_model_with_fake_slicer(tmp_path, fake_slicer):
    slicer, log = fake_slicer
    machine = tmp_path / "machine.json"
    process = tmp_path / "process.json"
    filament = tmp_path / "filament.json"
    machine.write_text("{}")
    process.write_text(json.dumps({"layer_height": "0.2", "sparse_infill_density": "15%"}))
    filament.write_text(json.dumps({"nozzle_temperature": ["220"]}))
    model = tmp_path / "cube.stl"
    model.write_text("solid cube\nendsolid cube\n")

    out = slice_model(model, SliceOptions(layer_height=0.28, nozzle_temp=200), slicer=str(slicer),
                      machine=machine, process=process, filament=filament, output_dir=tmp_path / "out")

    assert out == tmp_path / "out" / "cube.gcode.3mf" and out.is_file()
    rec = json.loads(log.read_text())
    assert rec["process"]["layer_height"] == "0.28"
    assert rec["process"]["sparse_infill_density"] == "15%"
    assert rec["filament"]["nozzle_temperature"] == ["200"]
    assert "--arrange" in rec["args"]
    assert json.loads(process.read_text())["layer_height"] == "0.2"  # original untouched


def test_slice_needs_profiles_for_stl(tmp_path, fake_slicer):
    model = tmp_path / "cube.stl"
    model.write_text("solid")
    with pytest.raises(SlicerError, match="BAMBU_PROFILE"):
        slice_model(model, SliceOptions(), slicer=str(fake_slicer[0]), machine=None, process=None,
                    filament=None, output_dir=tmp_path)
