"""Finding the slicer and its Bambu Lab profiles, resolving ``inherits`` chains.

Bambu Studio and OrcaSlicer ship their system presets as JSON files that only
store differences to a parent (``inherits``). The command line wants complete
profiles, so every preset is flattened before it is handed over.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

KINDS = ("machine", "process", "filament")


class ProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class SlicerInstall:
    executable: str
    family: str  # "bambu" or "orca"


def _family(path: str) -> str:
    return "orca" if "orca" in path.lower() else "bambu"


def find_slicer(configured: str | None = None) -> SlicerInstall | None:
    if configured:
        exe = shutil.which(configured) or (configured if Path(configured).exists() else None)
        if not exe:
            raise ProfileError(f"Slicer not found: {configured}")
        return SlicerInstall(exe, _family(exe))
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates += [
            "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
            "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
        ]
    elif os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates += [rf"{pf}\Bambu Studio\bambu-studio.exe", rf"{pf}\OrcaSlicer\orca-slicer.exe"]
    for name in ("bambu-studio", "BambuStudio", "orca-slicer", "OrcaSlicer"):
        if found := shutil.which(name):
            candidates.append(found)
    for exe in candidates:
        if Path(exe).exists():
            return SlicerInstall(exe, _family(exe))
    return None


def default_profile_dirs(slicer: SlicerInstall | None) -> list[Path]:
    """Profile folders, lowest priority first: install dir, updated system presets, user presets."""
    home = Path.home()
    family = slicer.family if slicer else "bambu"
    app = "OrcaSlicer" if family == "orca" else "BambuStudio"
    if sys.platform == "darwin":
        data = home / "Library" / "Application Support" / app
    elif os.name == "nt":
        data = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / app
    else:
        flatpak = "io.github.softfever.OrcaSlicer" if family == "orca" else "com.bambulab.BambuStudio"
        data = home / ".config" / app
        if not data.exists() and (home / ".var" / "app" / flatpak).exists():
            data = home / ".var" / "app" / flatpak / "config" / app

    dirs: list[Path] = []
    if slicer:
        exe = Path(slicer.executable).resolve()
        for base in (exe.parent, exe.parent.parent, exe.parent.parent / "Resources", exe.parent.parent / "share"):
            for sub in ("resources/profiles/BBL", "profiles/BBL", "BambuStudio/profiles/BBL", "OrcaSlicer/profiles/BBL"):
                dirs.append(base / sub)
    dirs.append(data / "system" / "BBL")
    user_root = data / "user"
    if user_root.is_dir():
        dirs += sorted(p for p in user_root.iterdir() if p.is_dir())
    return [d for d in dict.fromkeys(dirs) if d.is_dir()]


class ProfileLibrary:
    def __init__(self, dirs: list[Path]) -> None:
        self.dirs = [Path(d) for d in dirs]
        self._files: dict[str, dict[str, Path]] = {k: {} for k in KINDS}
        self._raw: dict[tuple[str, str], dict] = {}
        self._flat: dict[tuple[str, str], dict] = {}
        for d in self.dirs:
            for kind in KINDS:
                folder = d / kind
                if not folder.is_dir():
                    continue
                for f in folder.glob("*.json"):
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if isinstance(data, dict):
                        name = data.get("name") or f.stem
                        self._files[kind][name] = f
                        self._raw[(kind, name)] = data

    def __bool__(self) -> bool:
        return any(self._files[k] for k in KINDS)

    def names(self, kind: str) -> list[str]:
        return sorted(self._files[kind])

    def flatten(self, kind: str, name: str) -> dict:
        key = (kind, name)
        if key in self._flat:
            return self._flat[key]
        if key not in self._raw:
            raise ProfileError(f"No {kind} preset named {name!r}")
        return self._flatten_data(kind, self._raw[key], key)

    def _flatten_data(self, kind: str, data: dict, cache_key: tuple | None = None) -> dict:
        chain, seen = [data], set()
        parent = data.get("inherits")
        while parent:
            if parent in seen:
                raise ProfileError(f"Inheritance loop at {parent!r}")
            seen.add(parent)
            raw = self._raw.get((kind, parent))
            if raw is None:
                raise ProfileError(f"{kind} preset {data.get('name')!r} inherits unknown preset {parent!r}")
            chain.append(raw)
            parent = raw.get("inherits")
        flat: dict = {}
        for layer in reversed(chain):
            flat.update(layer)
        flat.pop("inherits", None)
        flat["name"] = data.get("name", flat.get("name"))
        if cache_key:
            self._flat[cache_key] = flat
        return flat

    def flatten_file(self, kind: str, path: Path) -> dict:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProfileError(f"Cannot read profile {path}: {exc}") from exc
        return self._flatten_data(kind, data)

    def selectable(self, kind: str, machine: str) -> list[dict]:
        """Instantiable presets of a kind that list the machine as compatible."""
        out = []
        for name in self._files[kind]:
            try:
                flat = self.flatten(kind, name)
            except ProfileError:
                continue
            if flat.get("instantiation", "true") != "true":
                continue
            compatible = flat.get("compatible_printers") or []
            if compatible and machine not in compatible:
                continue
            out.append(flat)
        return out


def machine_name(nozzle: float) -> str:
    return f"Bambu Lab A1 mini {nozzle:g} nozzle"


STYLE_WORDS = {
    "quality": ("High Quality", "Fine", "Optimal", "Extra Fine"),
    "strength": ("Strength", "Standard"),
    "balanced": ("Standard", "Optimal", "Fine"),
    "speed": ("Draft", "Extra Draft", "Standard"),
}


def pick_process(lib: ProfileLibrary, machine: str, layer_height: float, purpose: str) -> dict:
    presets = lib.selectable("process", machine)
    if not presets:
        raise ProfileError(f"No process presets for {machine}")

    def lh(p: dict) -> float:
        try:
            return float(p.get("layer_height", 0))
        except ValueError:
            return 0.0

    def rank(p: dict) -> tuple:
        words = STYLE_WORDS.get(purpose, STYLE_WORDS["balanced"])
        name = p["name"]
        style = next((i for i, w in enumerate(words) if f" {w} @" in name), len(words))
        user = 0 if p.get("from", "system") == "system" else 1
        return (round(abs(lh(p) - layer_height), 3), style, user, name)

    return min(presets, key=rank)


def _types_of(preset: dict) -> list[str]:
    value = preset.get("filament_type") or [""]
    return [str(v).upper() for v in (value if isinstance(value, list) else [value])]


def pick_filament(
    lib: ProfileLibrary,
    machine: str,
    *,
    preset_name: str | None = None,
    filament_id: str | None = None,
    filament_type: str | None = None,
) -> dict:
    presets = lib.selectable("filament", machine)
    if preset_name:
        for p in presets:
            if p["name"] == preset_name:
                return p
        similar = [p["name"] for p in presets if preset_name.lower() in p["name"].lower()]
        raise ProfileError(f"Filament preset {preset_name!r} not found for {machine}. Similar: {similar[:10]}")
    if filament_id:
        matches = [p for p in presets if p.get("filament_id") == filament_id]
        if matches:
            return min(matches, key=lambda p: (p.get("from", "system") != "system", p["name"]))
    ftype = (filament_type or "PLA").upper()
    matches = [p for p in presets if ftype in _types_of(p)]
    if not matches:
        available = sorted({t for p in presets for t in _types_of(p) if t})
        raise ProfileError(
            f"No {ftype} profile for {machine}. Bambu offers these filament types for it: {available}"
        )
    return min(matches, key=lambda p: (not p["name"].startswith("Generic "), p["name"]))
