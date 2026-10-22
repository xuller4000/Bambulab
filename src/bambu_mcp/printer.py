"""MQTT control of a Bambu Lab printer in LAN mode.

The printer runs an MQTT broker on port 8883 (TLS, self-signed certificate).
Login is user ``bblp`` with the LAN access code. Commands go to
``device/<serial>/request``, state arrives on ``device/<serial>/report``.
A1/P1 printers only send changed fields, so reports are merged into a cached
state dict.
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
import os
import ssl
import threading
import time
from typing import Any, Callable

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

SPEED_LEVELS = {"silent": 1, "standard": 2, "sport": 3, "ludicrous": 4}
SPEED_NAMES = {v: k for k, v in SPEED_LEVELS.items()}

# Limits for the A1 mini. Bed is rated to 80 °C, hotend to 300 °C.
MAX_NOZZLE_TEMP = 300
MAX_BED_TEMP = 80

EXTERNAL_SPOOL = (255, 254)  # (ams_id, tray_id) of the spool holder


class PrinterError(RuntimeError):
    pass


def deep_merge(target: dict, update: dict) -> dict:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def tls_context() -> ssl.SSLContext:
    # The printer presents a certificate signed by Bambu's private CA with the
    # serial as CN, so hostname and chain verification are switched off.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class BambuPrinter:
    def __init__(
        self,
        host: str,
        serial: str,
        access_code: str,
        *,
        port: int = 8883,
        sdcard_root: str = "/sdcard",
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.port = port
        self.sdcard_root = sdcard_root.rstrip("/")
        self._client_factory = client_factory or self._make_client
        self._client: Any = None
        self._lock = threading.Condition()
        self._state: dict[str, Any] = {}
        self._acks: dict[str, dict] = {}
        self._connected = False
        self._connect_error: str | None = None
        self._connect_done = threading.Event()
        self._last_report = 0.0
        self._seq = itertools.count(int(time.time()) % 1_000_000)

    # -- connection ---------------------------------------------------------

    @property
    def request_topic(self) -> str:
        return f"device/{self.serial}/request"

    @property
    def report_topic(self) -> str:
        return f"device/{self.serial}/report"

    def _make_client(self) -> mqtt.Client:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-mcp-{os.getpid()}",
            protocol=mqtt.MQTTv311,
        )
        client.tls_set_context(tls_context())
        client.username_pw_set("bblp", self.access_code)
        client.reconnect_delay_set(1, 30)
        return client

    def connect(self, timeout: float = 10.0) -> None:
        if self._client is not None and self._connected:
            return
        if self._client is not None:
            self.disconnect()
        self._connect_done.clear()
        self._connect_error = None
        client = self._client_factory()
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        client.connect_async(self.host, self.port, keepalive=60)
        client.loop_start()
        if not self._connect_done.wait(timeout) or not self._connected:
            reason = self._connect_error or "timeout"
            self.disconnect()
            raise PrinterError(
                f"Could not connect to MQTT at {self.host}:{self.port} ({reason}). "
                "Check IP, access code, that LAN mode is on and, on newer firmware, "
                "that Developer Mode is enabled."
            )

    def disconnect(self) -> None:
        client, self._client = self._client, None
        self._connected = False
        if client is not None:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception:  # pragma: no cover - best effort
                log.debug("disconnect failed", exc_info=True)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            self._connected = False
            self._connect_error = str(reason_code)
        else:
            self._connected = True
            client.subscribe(self.report_topic, qos=1)
            self._publish_raw({"pushing": {"command": "pushall", "version": 1, "push_target": 1}}, client)
        self._connect_done.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._connected = False
        log.info("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            data = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            log.debug("ignoring non-JSON message on %s", msg.topic)
            return
        self.handle_report(data)

    def handle_report(self, data: dict) -> None:
        with self._lock:
            deep_merge(self._state, data)
            self._last_report = time.time()
            for section in data.values():
                if not isinstance(section, dict):
                    continue
                seq = section.get("sequence_id")
                command = section.get("command")
                if seq is not None and command and command != "push_status":
                    self._acks[str(seq)] = section
            self._lock.notify_all()

    # -- publishing ---------------------------------------------------------

    def _publish_raw(self, payload: dict, client: Any = None) -> str:
        seq = str(next(self._seq))
        for section in payload.values():
            section["sequence_id"] = seq
        info = (client or self._client).publish(self.request_topic, json.dumps(payload), qos=1)
        if getattr(info, "rc", 0) != 0:
            raise PrinterError(f"MQTT publish failed (rc={info.rc})")
        return seq

    def send(self, payload: dict, wait_ack: float = 3.0) -> dict:
        """Publish a command and wait briefly for the printer's echo."""
        self.connect()
        seq = self._publish_raw(copy.deepcopy(payload))
        ack = None
        if wait_ack > 0:
            deadline = time.time() + wait_ack
            with self._lock:
                while seq not in self._acks and (remaining := deadline - time.time()) > 0:
                    self._lock.wait(remaining)
                ack = self._acks.pop(seq, None)
        result: dict[str, Any] = {"sequence_id": seq, "acknowledged": ack is not None}
        if ack:
            for key in ("result", "reason", "err_code"):
                if key in ack:
                    result[key] = ack[key]
            if str(ack.get("result", "")).lower() in ("fail", "failed", "error"):
                raise PrinterError(f"Printer rejected command: {ack}")
        return result

    # -- state --------------------------------------------------------------

    def refresh(self, timeout: float = 5.0) -> dict:
        self.connect(timeout=max(timeout, 3.0))
        before = self._last_report
        self._publish_raw({"pushing": {"command": "pushall", "version": 1, "push_target": 1}})
        deadline = time.time() + timeout
        with self._lock:
            while self._last_report == before and (remaining := deadline - time.time()) > 0:
                self._lock.wait(remaining)
            if not self._state:
                raise PrinterError("No status report received from the printer.")
            return copy.deepcopy(self._state)

    def raw_state(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    # -- commands -----------------------------------------------------------

    def _print_cmd(self, command: str, **params: Any) -> dict:
        return self.send({"print": {"command": command, **params}})

    def pause(self) -> dict:
        return self._print_cmd("pause")

    def resume(self) -> dict:
        return self._print_cmd("resume")

    def stop(self) -> dict:
        return self._print_cmd("stop")

    def set_speed(self, level: str | int) -> dict:
        if isinstance(level, str):
            if level.lower() not in SPEED_LEVELS:
                raise PrinterError(f"Unknown speed level {level!r}, use one of {list(SPEED_LEVELS)}")
            level = SPEED_LEVELS[level.lower()]
        if level not in SPEED_NAMES:
            raise PrinterError("Speed level must be 1-4")
        return self._print_cmd("print_speed", param=str(level))

    def gcode(self, lines: str) -> dict:
        text = "\n".join(line.strip() for line in lines.strip().splitlines() if line.strip())
        if not text:
            raise PrinterError("Empty G-code")
        return self._print_cmd("gcode_line", param=text + "\n")

    def set_nozzle_temp(self, celsius: int) -> dict:
        if not 0 <= celsius <= MAX_NOZZLE_TEMP:
            raise PrinterError(f"Nozzle temperature must be 0-{MAX_NOZZLE_TEMP} °C")
        return self.gcode(f"M104 S{celsius}")

    def set_bed_temp(self, celsius: int) -> dict:
        if not 0 <= celsius <= MAX_BED_TEMP:
            raise PrinterError(f"Bed temperature must be 0-{MAX_BED_TEMP} °C")
        return self.gcode(f"M140 S{celsius}")

    def set_part_fan(self, percent: int) -> dict:
        if not 0 <= percent <= 100:
            raise PrinterError("Fan speed must be 0-100 %")
        return self.gcode(f"M106 P1 S{round(percent * 255 / 100)}")

    def set_light(self, on: bool) -> dict:
        return self.send({
            "system": {
                "command": "ledctrl",
                "led_node": "chamber_light",
                "led_mode": "on" if on else "off",
                "led_on_time": 500,
                "led_off_time": 500,
                "loop_times": 0,
                "interval_time": 0,
            }
        })

    def set_filament(
        self,
        *,
        ams_id: int,
        tray_id: int,
        filament_type: str,
        color: str,
        nozzle_temp_min: int,
        nozzle_temp_max: int,
        filament_id: str = "",
    ) -> dict:
        color = color.lstrip("#").upper()
        if len(color) == 6:
            color += "FF"
        if len(color) != 8 or any(c not in "0123456789ABCDEF" for c in color):
            raise PrinterError("Color must be hex like FF0000 or FF0000FF")
        return self._print_cmd(
            "ams_filament_setting",
            ams_id=ams_id,
            tray_id=tray_id,
            tray_info_idx=filament_id,
            tray_color=color,
            nozzle_temp_min=nozzle_temp_min,
            nozzle_temp_max=nozzle_temp_max,
            tray_type=filament_type.upper(),
        )

    def start_print(
        self,
        filename: str,
        *,
        plate: int = 1,
        bed_leveling: bool = True,
        flow_calibration: bool = True,
        vibration_calibration: bool = True,
        timelapse: bool = False,
        layer_inspect: bool = False,
        use_ams: bool = False,
        ams_mapping: list[int] | None = None,
        bed_type: str = "auto",
    ) -> dict:
        name = filename.lstrip("/")
        if name.lower().endswith(".gcode"):
            return self._print_cmd("gcode_file", param=f"{self.sdcard_root}/{name}")
        if not name.lower().endswith(".3mf"):
            raise PrinterError("Only .3mf (sliced, e.g. *.gcode.3mf) or .gcode files can be printed")
        if plate < 1:
            raise PrinterError("Plate numbers start at 1")
        task = os.path.basename(name)
        for suffix in (".gcode.3mf", ".3mf"):
            if task.lower().endswith(suffix):
                task = task[: -len(suffix)]
                break
        return self._print_cmd(
            "project_file",
            param=f"Metadata/plate_{plate}.gcode",
            url=f"file://{self.sdcard_root}/{name}",
            subtask_name=task,
            project_id="0",
            profile_id="0",
            task_id="0",
            subtask_id="0",
            file="",
            md5="",
            bed_type=bed_type,
            bed_levelling=bed_leveling,
            flow_cali=flow_calibration,
            vibration_cali=vibration_calibration,
            timelapse=timelapse,
            layer_inspect=layer_inspect,
            use_ams=use_ams,
            ams_mapping=ams_mapping or [],
        )


GCODE_STATES = {
    "IDLE": "idle",
    "PREPARE": "preparing",
    "SLICING": "slicing",
    "RUNNING": "printing",
    "PAUSE": "paused",
    "FINISH": "finished",
    "FAILED": "failed",
}


def summarize(state: dict) -> dict:
    """Condense a raw report into the fields that matter for a person."""
    p = state.get("print", {})

    def num(key: str) -> float | None:
        value = p.get(key)
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return None

    summary: dict[str, Any] = {
        "state": GCODE_STATES.get(p.get("gcode_state"), p.get("gcode_state")),
        "job": p.get("subtask_name") or None,
        "file": p.get("gcode_file") or None,
        "progress_percent": p.get("mc_percent"),
        "remaining_minutes": p.get("mc_remaining_time"),
        "layer": p.get("layer_num"),
        "total_layers": p.get("total_layer_num"),
        "nozzle_temp": num("nozzle_temper"),
        "nozzle_target": num("nozzle_target_temper"),
        "bed_temp": num("bed_temper"),
        "bed_target": num("bed_target_temper"),
        "speed": SPEED_NAMES.get(p.get("spd_lvl"), p.get("spd_lvl")),
        "speed_percent": p.get("spd_mag"),
        "part_fan": p.get("cooling_fan_speed"),
        "wifi_signal": p.get("wifi_signal"),
        "print_error": p.get("print_error") or 0,
        "hms_errors": p.get("hms") or [],
    }
    if lights := p.get("lights_report"):
        summary["lights"] = {l.get("node"): l.get("mode") for l in lights}
    if tray := p.get("vt_tray"):
        summary["external_spool"] = {
            "type": tray.get("tray_type"),
            "color": tray.get("tray_color"),
            "nozzle_temp_range": [tray.get("nozzle_temp_min"), tray.get("nozzle_temp_max")],
        }
    ams = p.get("ams") or {}
    units = ams.get("ams") if isinstance(ams, dict) else None
    if units:
        summary["ams"] = [
            {
                "ams_id": unit.get("id"),
                "trays": [
                    {
                        "tray_id": t.get("id"),
                        "type": t.get("tray_type"),
                        "color": t.get("tray_color"),
                        "remaining_percent": t.get("remain"),
                    }
                    for t in unit.get("tray", [])
                    if t.get("tray_type")
                ],
            }
            for unit in units
        ]
    return {k: v for k, v in summary.items() if v is not None}
