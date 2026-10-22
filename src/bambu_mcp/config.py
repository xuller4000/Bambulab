"""Settings read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


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
    slice_output_dir: Path = Path.home() / ".cache" / "bambu-mcp"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        missing = [k for k in ("BAMBU_HOST", "BAMBU_SERIAL", "BAMBU_ACCESS_CODE") if not env.get(k)]
        if missing:
            raise ConfigError(
                "Missing environment variables: " + ", ".join(missing)
                + ". Set them in the MCP client config (see README)."
            )

        def path(key: str) -> Path | None:
            value = env.get(key)
            return Path(value).expanduser() if value else None

        return cls(
            host=env["BAMBU_HOST"].strip(),
            serial=env["BAMBU_SERIAL"].strip(),
            access_code=env["BAMBU_ACCESS_CODE"].strip(),
            mqtt_port=int(env.get("BAMBU_MQTT_PORT", 8883)),
            ftp_port=int(env.get("BAMBU_FTP_PORT", 990)),
            camera_port=int(env.get("BAMBU_CAMERA_PORT", 6000)),
            sdcard_root=env.get("BAMBU_SDCARD_ROOT", "/sdcard").rstrip("/"),
            slicer_path=env.get("BAMBU_SLICER_PATH") or None,
            profile_machine=path("BAMBU_PROFILE_MACHINE"),
            profile_process=path("BAMBU_PROFILE_PROCESS"),
            profile_filament=path("BAMBU_PROFILE_FILAMENT"),
            slice_output_dir=path("BAMBU_SLICE_OUTPUT_DIR") or cls.slice_output_dir,
        )
