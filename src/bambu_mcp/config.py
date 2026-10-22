"""Settings from environment variables and the optional config file.

The config file (``~/.bambu-mcp/config.env``, written by ``bambu-mcp setup``)
holds ``KEY=VALUE`` lines. Environment variables win over the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def config_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("BAMBU_CONFIG") or Path.home() / ".bambu-mcp" / "config.env").expanduser()


def read_config_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"')
    return values


def write_config_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# bambu-mcp settings, written by 'bambu-mcp setup'"]
    lines += [f"{k}={v}" for k, v in values.items() if v not in (None, "")]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Settings:
    host: str
    serial: str
    access_code: str
    mqtt_port: int = 8883
    ftp_port: int = 990
    camera_port: int = 6000
    # FTP root of the printer maps to this path in print commands.
    sdcard_root: str = "/sdcard"
    slicer_path: str | None = None
    profile_machine: Path | None = None
    profile_process: Path | None = None
    profile_filament: Path | None = None
    profile_dirs: tuple[Path, ...] = ()
    plate_type: str = "textured_pei"
    nozzle: float | None = None
    slice_output_dir: Path = Path.home() / ".cache" / "bambu-mcp"
    # Folder for models handed over from the phone (synced folder or downloads).
    model_dir: Path | None = None
    # Remote access: secret part of the URL, and whether file paths must stay in model_dir.
    http_token: str | None = None
    http_port: int = 8765
    # Public address of a fixed Cloudflare tunnel, e.g. https://drucker.example.de
    public_url: str | None = None
    restrict_paths: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        source = dict(os.environ if env is None else env)
        env = read_config_file(config_path(source)) | source
        # The serial is optional: the server reads it from the printer if it is missing.
        missing = [k for k in ("BAMBU_HOST", "BAMBU_ACCESS_CODE") if not env.get(k)]
        if missing:
            raise ConfigError(
                "Missing settings: " + ", ".join(missing)
                + ". Run 'bambu-mcp setup' or set them in the MCP client config (see README)."
            )

        def path(key: str) -> Path | None:
            value = env.get(key)
            return Path(value).expanduser() if value else None

        return cls(
            host=env["BAMBU_HOST"].strip(),
            serial=env.get("BAMBU_SERIAL", "").strip(),
            access_code=env["BAMBU_ACCESS_CODE"].strip(),
            mqtt_port=int(env.get("BAMBU_MQTT_PORT", 8883)),
            ftp_port=int(env.get("BAMBU_FTP_PORT", 990)),
            camera_port=int(env.get("BAMBU_CAMERA_PORT", 6000)),
            sdcard_root=env.get("BAMBU_SDCARD_ROOT", "/sdcard").rstrip("/"),
            slicer_path=env.get("BAMBU_SLICER_PATH") or None,
            profile_machine=path("BAMBU_PROFILE_MACHINE"),
            profile_process=path("BAMBU_PROFILE_PROCESS"),
            profile_filament=path("BAMBU_PROFILE_FILAMENT"),
            profile_dirs=tuple(
                Path(p).expanduser() for p in env.get("BAMBU_PROFILE_DIR", "").split(os.pathsep) if p
            ),
            plate_type=env.get("BAMBU_PLATE_TYPE", "textured_pei"),
            nozzle=float(env["BAMBU_NOZZLE"]) if env.get("BAMBU_NOZZLE") else None,
            slice_output_dir=path("BAMBU_SLICE_OUTPUT_DIR") or cls.slice_output_dir,
            model_dir=path("BAMBU_MODEL_DIR"),
            http_token=env.get("BAMBU_HTTP_TOKEN") or None,
            http_port=int(env.get("BAMBU_HTTP_PORT", 8765)),
            public_url=(env.get("BAMBU_PUBLIC_URL") or "").rstrip("/") or None,
        )
