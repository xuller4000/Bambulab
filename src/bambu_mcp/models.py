"""The model folder: listing, path checks and downloads from a link.

From the phone, Claude cannot hand a file to this server directly. Models
reach it through a folder on this computer (e.g. synced with OneDrive) or
through a download link.
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import socket
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from email.message import Message
from pathlib import Path

from .config import ConfigError, Settings

MODEL_EXTS = (".stl", ".3mf", ".obj", ".step", ".stp", ".gcode")
MAX_DOWNLOAD = 200 * 1024 * 1024
MAX_EXTRACT = 500 * 1024 * 1024


class ModelError(RuntimeError):
    pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_local(path_str: str, settings: Settings) -> Path:
    """Resolve a file name or path. Relative names are looked up in the model folder.

    In remote mode (restrict_paths) only the model folder and the slicer output are allowed,
    so whoever holds the URL cannot reach other files on this computer.
    """
    path = Path(path_str).expanduser()
    if not path.is_absolute() and settings.model_dir:
        path = settings.model_dir / path
    path = path.resolve()
    if settings.restrict_paths:
        roots = [r.resolve() for r in (settings.model_dir, settings.slice_output_dir) if r]
        if not any(_inside(path, r) for r in roots):
            raise ModelError("Only files in the model folder can be used remotely. Use list_model_files.")
    if not path.is_file():
        hint = " Use list_model_files to see what is there." if settings.model_dir else ""
        raise ModelError(f"File not found: {path_str}.{hint}")
    return path


def model_dir(settings: Settings) -> Path:
    if not settings.model_dir:
        raise ConfigError("No model folder set. Run 'bambu-mcp setup' or set BAMBU_MODEL_DIR.")
    settings.model_dir.mkdir(parents=True, exist_ok=True)
    return settings.model_dir


def list_models(root: Path, limit: int = 100) -> list[dict]:
    files = [p for p in root.rglob("*") if p.is_file() and p.name.lower().endswith(MODEL_EXTS)]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": p.relative_to(root).as_posix(),
            "size_kb": round(p.stat().st_size / 1024),
            "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
        for p in files[:limit]
    ]


def _check_public(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ModelError("Only http(s) links can be downloaded")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ModelError(f"Unknown host {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ModelError("Links into the local network are not allowed")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).name).strip(" .")
    return name[:120] or "model"


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise ModelError("Too many files with the same name")


def _filename(url: str, headers: Message) -> str:
    disposition = headers.get_filename() if hasattr(headers, "get_filename") else None
    name = disposition or urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    return safe_name(name)


def download(url: str, root: Path, opener=None) -> list[Path]:
    """Download a model (or a zip of models) into the model folder."""
    _check_public(url)
    opener = opener or urllib.request.build_opener(_CheckedRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "bambu-mcp/0.1"})
    with tempfile.TemporaryDirectory(prefix="bambu-dl-") as tmp:
        tmpfile = Path(tmp) / "download"
        try:
            with opener.open(request, timeout=60) as resp, tmpfile.open("wb") as out:
                name = _filename(resp.geturl(), resp.headers)
                total = 0
                while chunk := resp.read(1 << 16):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD:
                        raise ModelError("Download is larger than 200 MB")
                    out.write(chunk)
        except OSError as exc:
            raise ModelError(f"Download failed: {exc}") from exc

        if zipfile.is_zipfile(tmpfile):
            return _extract(tmpfile, root / Path(name).stem)
        if not name.lower().endswith(MODEL_EXTS):
            raise ModelError(
                f"'{name}' is not a model file ({', '.join(MODEL_EXTS)}). "
                "The link must point to the file itself, not to the web page about it."
            )
        target = _unique(root / name)
        shutil.move(str(tmpfile), target)
        return [target]


def _extract(archive: Path, folder: Path) -> list[Path]:
    saved: list[Path] = []
    total = 0
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.lower().endswith(MODEL_EXTS):
                continue
            total += info.file_size
            if total > MAX_EXTRACT:
                raise ModelError("Archive contents are larger than 500 MB")
            folder.mkdir(parents=True, exist_ok=True)
            target = _unique(folder / safe_name(info.filename))
            with z.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            saved.append(target)
    if not saved:
        raise ModelError("The archive contains no model files")
    return saved
