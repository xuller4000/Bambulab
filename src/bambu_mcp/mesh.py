"""Mesh loading and geometry analysis for print planning.

Everything works on triangle arrays of shape (N, 3, 3) in millimetres.
Normals are recomputed from the winding, the STL normal field is ignored.
"""

from __future__ import annotations

import math
import struct
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

# A1 mini build volume in mm
BUILD_VOLUME = (180.0, 180.0, 180.0)

# A face needs support when it is flatter than this angle to the horizontal.
# Matches Bambu's default support_threshold_angle of 30° for 0.20 mm layers.
SUPPORT_ANGLE_DEG = 30.0
BED_TOLERANCE_MM = 0.05

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


class MeshError(ValueError):
    pass


# -- loading -----------------------------------------------------------------

def load_parts(path: str | Path) -> list[np.ndarray]:
    """Load a model as a list of parts (one per 3MF build item, one for STL/OBJ)."""
    path = Path(path).expanduser()
    suffix = path.suffix.lower()
    if suffix == ".stl":
        parts = [load_stl(path)]
    elif suffix == ".obj":
        parts = [load_obj(path)]
    elif suffix == ".3mf":
        parts = load_3mf(path)
    else:
        raise MeshError(f"Geometry analysis supports STL, OBJ and 3MF, not {suffix}")
    parts = [p for p in parts if len(p)]
    if not parts:
        raise MeshError(f"No triangles found in {path.name}")
    return parts


def load_stl(path: Path) -> np.ndarray:
    data = path.read_bytes()
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + count * 50 == len(data):
            rec = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
            return np.frombuffer(data, dtype=rec, count=count, offset=84)["v"].astype(np.float64)
    text = data.decode("utf-8", errors="replace")
    verts = [line.split()[1:4] for line in text.splitlines() if line.strip().startswith("vertex")]
    if not verts or len(verts) % 3:
        raise MeshError(f"{path.name} is neither a valid binary nor ASCII STL")
    return np.array(verts, dtype=np.float64).reshape(-1, 3, 3)


def load_obj(path: Path) -> np.ndarray:
    verts: list[list[float]] = []
    faces: list[tuple[int, int, int]] = []
    for line in path.read_text(errors="replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v":
            verts.append([float(x) for x in parts[1:4]])
        elif parts[0] == "f":
            idx = [int(p.split("/")[0]) for p in parts[1:]]
            idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
            faces += [(idx[0], idx[k], idx[k + 1]) for k in range(1, len(idx) - 1)]
    if not faces:
        return np.zeros((0, 3, 3))
    return np.asarray(verts, dtype=np.float64)[np.asarray(faces)]


def _transform(attr: str | None) -> np.ndarray:
    m = np.eye(4)
    if attr:
        v = [float(x) for x in attr.split()]
        if len(v) != 12:
            raise MeshError(f"Bad 3MF transform: {attr}")
        m[:3, :3] = np.array(v[:9]).reshape(3, 3).T
        m[:3, 3] = v[9:]
    return m


def _apply(tris: np.ndarray, m: np.ndarray) -> np.ndarray:
    return tris @ m[:3, :3].T + m[:3, 3]


def load_3mf(path: Path) -> list[np.ndarray]:
    core = f"{{{CORE_NS}}}"
    with zipfile.ZipFile(path) as z:
        cache: dict[str, dict] = {}

        def objects(name: str) -> dict:
            name = name.lstrip("/")
            if name not in cache:
                root = ET.fromstring(z.read(name))
                objs: dict[str, tuple] = {}
                for obj in root.iter(core + "object"):
                    mesh = obj.find(core + "mesh")
                    if mesh is not None:
                        v = np.array([[float(e.get(a)) for a in "xyz"] for e in mesh.iter(core + "vertex")])
                        t = np.array([[int(e.get(a)) for a in ("v1", "v2", "v3")] for e in mesh.iter(core + "triangle")])
                        objs[obj.get("id")] = ("mesh", v[t] if len(t) else np.zeros((0, 3, 3)))
                    else:
                        comps = [
                            (c.get(f"{{{PROD_NS}}}path") or name, c.get("objectid"), _transform(c.get("transform")))
                            for c in obj.iter(core + "component")
                        ]
                        objs[obj.get("id")] = ("components", comps)
                cache[name] = {"root": root, "objects": objs}
            return cache[name]

        def expand(fname: str, oid: str, m: np.ndarray, depth: int = 0) -> list[np.ndarray]:
            if depth > 16:
                raise MeshError("3MF component nesting too deep")
            kind, data = objects(fname)["objects"][oid]
            if kind == "mesh":
                return [_apply(data, m)]
            return [t for p, cid, cm in data for t in expand(p, cid, m @ cm, depth + 1)]

        main = "3D/3dmodel.model"
        root = objects(main)["root"]
        parts = []
        for item in root.iter(core + "item"):
            if item.get("printable", "1") == "0":
                continue
            pieces = expand(main, item.get("objectid"), _transform(item.get("transform")))
            if pieces:
                parts.append(np.concatenate(pieces))
        return parts


def write_stl(path: Path, tris: np.ndarray) -> None:
    normals = _normals(tris)[0]
    rec = np.zeros(len(tris), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")]))
    rec["n"] = normals
    rec["v"] = tris
    with path.open("wb") as fh:
        fh.write(b"bambu-mcp".ljust(80, b"\0"))
        fh.write(struct.pack("<I", len(tris)))
        fh.write(rec.tobytes())


# -- analysis ----------------------------------------------------------------

def _normals(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    cross = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    norm = np.linalg.norm(cross, axis=1)
    area = norm / 2
    with np.errstate(invalid="ignore", divide="ignore"):
        unit = np.where(norm[:, None] > 0, cross / norm[:, None], 0.0)
    return unit, area


def signed_volume(tris: np.ndarray) -> float:
    return float(np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum() / 6)


@dataclass
class PartStats:
    size_mm: tuple[float, float, float]
    volume_cm3: float
    surface_cm2: float
    bed_contact_mm2: float
    support_area_mm2: float
    overhang_area_mm2: float
    top_flat_area_mm2: float
    triangles: int

    @property
    def height(self) -> float:
        return self.size_mm[2]


def part_stats(tris: np.ndarray) -> PartStats:
    tris = tris - np.array([0, 0, tris[:, :, 2].min()])
    vol = signed_volume(tris)
    unit, area = _normals(tris)
    if vol < 0:  # inverted winding
        unit, vol = -unit, -vol
    nz = unit[:, 2]
    zmax_face = tris[:, :, 2].max(axis=1)
    on_bed = zmax_face <= BED_TOLERANCE_MM
    down = (nz < 0) & ~on_bed
    support_cos = math.cos(math.radians(SUPPORT_ANGLE_DEG))
    overhang_cos = math.cos(math.radians(45))
    lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
    size = tuple(round(float(x), 2) for x in hi - lo)
    top = (nz > 0.995) & (tris[:, :, 2].min(axis=1) >= hi[2] - BED_TOLERANCE_MM)
    return PartStats(
        size_mm=size,
        volume_cm3=round(vol / 1000, 3),
        surface_cm2=round(float(area.sum()) / 100, 2),
        bed_contact_mm2=round(float(area[on_bed & (nz < -0.99)].sum()), 1),
        support_area_mm2=round(float(area[down & (-nz > support_cos)].sum()), 1),
        overhang_area_mm2=round(float(area[down & (-nz > overhang_cos)].sum()), 1),
        top_flat_area_mm2=round(float(area[top].sum()), 1),
        triangles=len(tris),
    )


# Rotations that put each of the six axis directions onto the bed.
ORIENTATIONS: dict[str, np.ndarray] = {
    "as modelled": np.eye(3),
    "upside down": np.diag([1.0, -1.0, -1.0]),
    "front face down": np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=float),
    "back face down": np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=float),
    "left side down": np.array([[0, 0, -1], [0, 1, 0], [1, 0, 0]], dtype=float),
    "right side down": np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], dtype=float),
}


def fits(size: tuple[float, float, float]) -> bool:
    x, y, z = size
    bx, by, bz = BUILD_VOLUME
    return z <= bz and ((x <= bx and y <= by) or (y <= bx and x <= by))


def orientation_score(s: PartStats) -> float:
    """Lower is better: support area dominates, height costs time, tiny contact risks detaching."""
    score = s.support_area_mm2 + 2.0 * s.height
    if s.bed_contact_mm2 < 50:
        score += 200
    return score


@dataclass
class OrientationChoice:
    name: str
    rotation: np.ndarray
    stats: PartStats
    improvement: str | None = None


def best_orientation(tris: np.ndarray) -> OrientationChoice:
    """Keep the modelled orientation unless turning the part clearly saves supports or makes it fit.

    Designers usually export in the intended orientation, and the orientation decides
    where layer lines run, so height alone is no reason to turn a part.
    """
    results = [OrientationChoice(name, rot, part_stats(tris @ rot.T)) for name, rot in ORIENTATIONS.items()]
    current = results[0]
    candidates = [r for r in results if fits(r.stats.size_mm)]
    if not candidates:
        return current
    best = min(candidates, key=lambda r: orientation_score(r.stats))
    if best is current:
        return current
    saved = current.stats.support_area_mm2 - best.stats.support_area_mm2
    clear_gain = saved > 50 and best.stats.support_area_mm2 < 0.7 * current.stats.support_area_mm2
    if fits(current.stats.size_mm) and not clear_gain:
        return current
    best.improvement = (
        f"support area {current.stats.support_area_mm2:.0f} -> {best.stats.support_area_mm2:.0f} mm², "
        f"height {current.stats.height:.0f} -> {best.stats.height:.0f} mm"
        + ("" if fits(current.stats.size_mm) else ", fits the build volume only this way")
    )
    return best


@dataclass
class ModelAnalysis:
    file: str
    parts: int
    size_mm: tuple[float, float, float]
    volume_cm3: float
    surface_cm2: float
    bed_contact_mm2: float
    support_area_mm2: float
    overhang_area_mm2: float
    top_flat_area_mm2: float
    slenderness: float
    fits_build_volume: bool
    orientation: str = "as modelled"
    orientation_gain: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def max_dim(self) -> float:
        return max(self.size_mm)

    @property
    def height(self) -> float:
        return self.size_mm[2]

    @property
    def needs_support(self) -> bool:
        return self.support_area_mm2 > max(25.0, 0.005 * self.surface_cm2 * 100)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["needs_support"] = self.needs_support
        return d


def analyze(path: str | Path, *, auto_orient: bool = True) -> tuple[ModelAnalysis, np.ndarray | None]:
    """Analyse a model. Returns the analysis and, if a better orientation was found, the rotated mesh."""
    path = Path(path).expanduser()
    parts = load_parts(path)
    rotated = None
    notes = []
    orientation, gain = "as modelled", None
    if len(parts) == 1 and auto_orient and path.suffix.lower() != ".3mf":
        choice = best_orientation(parts[0])
        if choice.improvement:
            rotated = parts[0] @ choice.rotation.T
            parts = [rotated]
            orientation, gain = choice.name, choice.improvement
    stats = [part_stats(p) for p in parts]
    if len(parts) > 1:
        size = tuple(max(s.size_mm[i] for s in stats) for i in range(3))
        notes.append(f"{len(parts)} objects; size is the largest extent of any single object")
    else:
        size = stats[0].size_mm
    footprint_min = max(min(size[0], size[1]), 0.1)
    analysis = ModelAnalysis(
        file=path.name,
        parts=len(parts),
        size_mm=size,
        volume_cm3=round(sum(s.volume_cm3 for s in stats), 2),
        surface_cm2=round(sum(s.surface_cm2 for s in stats), 1),
        bed_contact_mm2=round(sum(s.bed_contact_mm2 for s in stats), 1),
        support_area_mm2=round(sum(s.support_area_mm2 for s in stats), 1),
        overhang_area_mm2=round(sum(s.overhang_area_mm2 for s in stats), 1),
        top_flat_area_mm2=round(sum(s.top_flat_area_mm2 for s in stats), 1),
        slenderness=round(size[2] / footprint_min, 2),
        fits_build_volume=all(fits(s.size_mm) for s in stats),
        orientation=orientation,
        orientation_gain=gain,
        notes=notes,
    )
    if not analysis.fits_build_volume:
        largest = max(max(s.size_mm[0], s.size_mm[1]) for s in stats)
        tallest = max(s.height for s in stats)
        scale = min(BUILD_VOLUME[0] / largest, BUILD_VOLUME[2] / tallest)
        analysis.notes.append(
            f"Does not fit the 180 x 180 x 180 mm build volume; scaling to {scale * 100:.0f} % or less would fit"
        )
    return analysis, rotated
