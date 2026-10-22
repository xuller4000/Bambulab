import zipfile

import numpy as np
import pytest

from bambu_mcp.mesh import MeshError, analyze, load_parts, part_stats, write_stl
from conftest import box, tee


def test_cube_stats(tmp_path):
    path = tmp_path / "cube.stl"
    write_stl(path, box(0, 0, 0, 20, 20, 20))
    a, rotated = analyze(path)
    assert rotated is None
    assert a.size_mm == (20, 20, 20)
    assert a.volume_cm3 == pytest.approx(8.0)
    assert a.bed_contact_mm2 == pytest.approx(400)
    assert a.support_area_mm2 == 0 and not a.needs_support
    assert a.top_flat_area_mm2 == pytest.approx(400)
    assert a.fits_build_volume


def test_inverted_winding_still_positive():
    s = part_stats(box(0, 0, 0, 10, 10, 10)[:, ::-1])
    assert s.volume_cm3 == pytest.approx(1.0)
    assert s.bed_contact_mm2 == pytest.approx(100)


def test_tee_needs_support_and_gets_turned(tmp_path):
    path = tmp_path / "tee.stl"
    write_stl(path, tee())
    a, _ = analyze(path, auto_orient=False)
    assert a.needs_support and a.support_area_mm2 >= 500
    a, rotated = analyze(path)
    assert rotated is not None
    assert a.support_area_mm2 == 0 and a.size_mm[2] == pytest.approx(10)
    assert "support area" in a.orientation_gain


def test_tall_part_is_not_laid_down_without_support_gain(tmp_path):
    path = tmp_path / "pillar.stl"
    write_stl(path, box(0, 0, 0, 8, 8, 70))
    a, rotated = analyze(path)
    assert rotated is None and a.slenderness == pytest.approx(8.75)


def test_too_big_part_reports_scale(tmp_path):
    path = tmp_path / "big.stl"
    write_stl(path, box(0, 0, 0, 200, 20, 20))
    a, _ = analyze(path)
    assert not a.fits_build_volume
    assert "90 %" in a.notes[0]


def test_ascii_stl_and_obj(tmp_path):
    tri = box(0, 0, 0, 10, 10, 10)
    lines = ["solid t"]
    for t in tri:
        lines += ["facet normal 0 0 0", "outer loop"] + [f"vertex {x} {y} {z}" for x, y, z in t] + ["endloop", "endfacet"]
    (tmp_path / "a.stl").write_text("\n".join(lines + ["endsolid t"]))
    assert part_stats(load_parts(tmp_path / "a.stl")[0]).volume_cm3 == pytest.approx(1.0)

    obj = ["v 0 0 0", "v 10 0 0", "v 10 10 0", "v 0 10 0", "v 0 0 10", "v 10 0 10", "v 10 10 10", "v 0 10 10",
           "f 1 3 2", "f 1 4 3", "f 5/1 6/1 7/1 8/1", "f 1 2 6 5", "f 2 3 7 6", "f 3 4 8 7", "f 4 1 5 8"]
    (tmp_path / "a.obj").write_text("\n".join(obj))
    assert part_stats(load_parts(tmp_path / "a.obj")[0]).volume_cm3 == pytest.approx(1.0)


def _mesh_xml(tris):
    verts = tris.reshape(-1, 3)
    v = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
    t = "".join(f'<triangle v1="{3*i}" v2="{3*i+1}" v3="{3*i+2}"/>' for i in range(len(tris)))
    return f"<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh>"


def test_3mf_with_components_and_transforms(tmp_path):
    core = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    prod = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    obj = f'<model xmlns="{core}"><resources><object id="1" type="model">{_mesh_xml(box(0, 0, 0, 10, 10, 10))}</object></resources></model>'
    # Component scaled x2 in z, build item moved up by 5 mm: 10 x 10 x 20 part, one item not printable.
    main = (f'<model xmlns="{core}" xmlns:p="{prod}"><resources>'
            f'<object id="2" type="model"><components><component p:path="/3D/Objects/object_1.model" objectid="1" '
            f'transform="1 0 0 0 1 0 0 0 2 0 0 0"/></components></object></resources>'
            f'<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 90 90 5"/>'
            f'<item objectid="2" printable="0"/></build></model>')
    path = tmp_path / "proj.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", main)
        z.writestr("3D/Objects/object_1.model", obj)
    parts = load_parts(path)
    assert len(parts) == 1
    assert parts[0][:, :, 0].min() == pytest.approx(90)
    s = part_stats(parts[0])
    assert s.size_mm == (10, 10, 20) and s.volume_cm3 == pytest.approx(2.0)


def test_unsupported_format(tmp_path):
    (tmp_path / "x.step").write_text("ISO-10303-21;")
    with pytest.raises(MeshError):
        load_parts(tmp_path / "x.step")
