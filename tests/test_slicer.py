import json

import pytest

from bambu_mcp.config import Settings
from bambu_mcp.mesh import write_stl
from bambu_mcp.slicer import SliceOptions, SlicerError, apply_overrides
from bambu_mcp.workflow import SliceRequest, plan, slice_request
from conftest import box, tee


def settings(profile_dir, tmp_path, slicer=None):
    return Settings(host="h", serial="s", access_code="c", slicer_path=str(slicer) if slicer else None,
                    profile_dirs=(profile_dir,), slice_output_dir=tmp_path / "out")


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
    assert o.filament_overrides()["textured_plate_temp"] == "55"
    with pytest.raises(SlicerError):
        SliceOptions(support_type="organic").process_overrides()


def test_full_pipeline_with_fake_slicer(tmp_path, profile_dir, fake_slicer):
    slicer, log = fake_slicer
    model = tmp_path / "tee.stl"
    write_stl(model, tee())
    req = SliceRequest(model_path=str(model), filament_id="GFA00", filament_type="PLA",
                       options=SliceOptions(infill_percent=40))
    result = slice_request(req, settings(profile_dir, tmp_path, slicer))

    rec = json.loads(log.read_text())
    assert result["presets"] == {"machine": "Bambu Lab A1 mini 0.4 nozzle", "process": "0.20mm Standard @BBL A1M",
                                 "filament": "Bambu PLA Basic @BBL A1M"}
    assert "inherits" not in rec["process"] and rec["machine"]["printable_height"] == "180"
    assert rec["process"]["curr_bed_type"] == "Textured PEI Plate"
    assert rec["process"]["wall_loops"] == "3"                 # recommendation
    assert rec["process"]["sparse_infill_density"] == "40%"    # explicit wins
    assert rec["process"]["enable_support"] == "0"             # turned flat, no supports
    assert rec["process"]["outer_wall_speed"] == ["200"]
    assert rec["model_name"] == "tee.stl" and "--arrange" in rec["args"]
    assert result["estimate"]["print_time"] == "1 h 15 min" and result["estimate"]["weight_g"] == 12.5
    assert result["analysis"]["orientation"] != "as modelled"
    assert result["sliced_file"].endswith("tee.gcode.3mf")


def test_no_auto_tune_keeps_preset(tmp_path, profile_dir, fake_slicer):
    slicer, log = fake_slicer
    model = tmp_path / "cube.stl"
    write_stl(model, box(0, 0, 0, 20, 20, 20))
    slice_request(SliceRequest(model_path=str(model), auto_tune=False), settings(profile_dir, tmp_path, slicer))
    rec = json.loads(log.read_text())
    assert rec["process"]["wall_loops"] == "2" and rec["filament"]["name"] == "Generic PLA @BBL A1M"


def test_plan_without_slicer_still_recommends(tmp_path, profile_dir):
    model = tmp_path / "cube.stl"
    write_stl(model, box(0, 0, 0, 20, 20, 20))
    result = plan(SliceRequest(model_path=str(model), filament_type="PETG"), settings(profile_dir, tmp_path))
    assert result["presets"]["filament"] == "Generic PETG @BBL A1M"
    assert result["_slicer"] is None
    with pytest.raises(SlicerError, match="No slicer found"):
        slice_request(SliceRequest(model_path=str(model)), settings(profile_dir, tmp_path))


def test_layer_height_checked_against_nozzle(tmp_path, profile_dir):
    model = tmp_path / "cube.stl"
    write_stl(model, box(0, 0, 0, 20, 20, 20))
    with pytest.raises(SlicerError, match="nozzle"):
        plan(SliceRequest(model_path=str(model), nozzle=0.2, options=SliceOptions(layer_height=0.28)),
             settings(profile_dir, tmp_path))
