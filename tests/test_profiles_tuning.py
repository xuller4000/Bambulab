import json

import pytest

from bambu_mcp.mesh import ModelAnalysis
from bambu_mcp.profiles import ProfileError, ProfileLibrary, pick_filament, pick_process
from bambu_mcp.tuning import recommend
from conftest import A1M


def test_flatten_resolves_inherits(profile_dir):
    lib = ProfileLibrary([profile_dir])
    m = lib.flatten("machine", A1M)
    assert m["printable_height"] == "180" and m["nozzle_diameter"] == ["0.4"] and "inherits" not in m
    p = lib.flatten("process", "0.20mm Strength @BBL A1M")
    assert p["wall_loops"] == "6" and p["sparse_infill_density"] == "15%"


def test_user_preset_overrides_and_inherits_system(profile_dir, tmp_path):
    user = tmp_path / "user" / "123"
    (user / "filament").mkdir(parents=True)
    (user / "filament" / "Mine.json").write_text(json.dumps(
        {"name": "My PLA", "inherits": "Generic PLA @BBL A1M", "nozzle_temperature": ["210"], "from": "User"}))
    lib = ProfileLibrary([profile_dir, user])
    f = pick_filament(lib, A1M, preset_name="My PLA")
    assert f["nozzle_temperature"] == ["210"] and f["filament_id"] == "GFL99"


def test_pick_process_by_height_and_purpose(profile_dir):
    lib = ProfileLibrary([profile_dir])
    assert pick_process(lib, A1M, 0.12, "quality")["name"] == "0.12mm High Quality @BBL A1M"
    assert pick_process(lib, A1M, 0.12, "balanced")["name"] == "0.12mm Fine @BBL A1M"
    assert pick_process(lib, A1M, 0.2, "strength")["name"] == "0.20mm Strength @BBL A1M"
    assert pick_process(lib, A1M, 0.24, "speed")["name"] == "0.20mm Standard @BBL A1M"
    names = [p["name"] for p in lib.selectable("process", A1M)]
    assert "0.20mm Standard @BBL X1C" not in names and "fdm_process_common" not in names


def test_pick_filament(profile_dir):
    lib = ProfileLibrary([profile_dir])
    assert pick_filament(lib, A1M, filament_id="GFA00")["name"] == "Bambu PLA Basic @BBL A1M"
    assert pick_filament(lib, A1M, filament_type="pla")["name"] == "Generic PLA @BBL A1M"
    assert pick_filament(lib, A1M, filament_id="GFX00", filament_type="PETG")["name"] == "Generic PETG @BBL A1M"
    with pytest.raises(ProfileError, match="PETG"):
        pick_filament(lib, A1M, filament_type="ABS")


def analysis(**kw):
    base = dict(file="m.stl", parts=1, size_mm=(50.0, 50.0, 30.0), volume_cm3=40.0, surface_cm2=100.0,
                bed_contact_mm2=2500.0, support_area_mm2=0.0, overhang_area_mm2=0.0, top_flat_area_mm2=0.0,
                slenderness=0.6, fits_build_volume=True)
    return ModelAnalysis(**(base | kw))


def test_recommend_balanced_default():
    plan = recommend(analysis())
    assert plan.purpose == "balanced" and plan.layer_height == 0.2
    assert plan.process_overrides == {"wall_loops": 3, "enable_support": 0}


def test_recommend_small_part_goes_quality():
    plan = recommend(analysis(size_mm=(20.0, 20.0, 15.0), volume_cm3=2.0, top_flat_area_mm2=400.0))
    assert plan.purpose == "quality" and plan.layer_height == 0.12
    assert plan.process_overrides["sparse_infill_density"] == "25%"
    assert plan.process_overrides["ironing_type"] == "top"


def test_recommend_supports_brim_and_warnings():
    plan = recommend(analysis(support_area_mm2=800.0, slenderness=4.0, bed_contact_mm2=60.0),
                     filament_type="PETG-CF", purpose="strength")
    o = plan.process_overrides
    assert o["enable_support"] == 1 and o["support_type"] == "tree(auto)" and o["brim_type"] == "outer_only"
    assert o["wall_loops"] == 6
    assert any("hardened steel" in w for w in plan.warnings)
    assert any("support_top_z_distance" in w for w in plan.warnings)


def test_recommend_scales_with_nozzle_and_warns_enclosure():
    plan = recommend(analysis(), purpose="speed", nozzle=0.8, filament_type="ABS")
    assert plan.layer_height == 0.48
    assert any("enclosed" in w for w in plan.warnings)
