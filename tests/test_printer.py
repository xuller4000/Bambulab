import json

import pytest

from bambu_mcp.printer import BambuPrinter, PrinterError, deep_merge, summarize


class FakeInfo:
    rc = 0


class FakeClient:
    """Stands in for paho's client and echoes commands like the printer does."""

    def __init__(self, printer_ref, result="success"):
        self.printer_ref = printer_ref
        self.result = result
        self.published = []

    def connect_async(self, host, port, keepalive):
        pass

    def loop_start(self):
        self.on_connect(self, None, None, 0)

    def subscribe(self, topic, qos):
        self.topic = topic

    def publish(self, topic, payload, qos):
        data = json.loads(payload)
        self.published.append((topic, data))
        for key, section in data.items():
            if key == "pushing":
                self.printer_ref[0].handle_report({"print": {"command": "push_status", "gcode_state": "IDLE"}})
            elif self.result is not None:
                echo = dict(section, result=self.result)
                self.printer_ref[0].handle_report({key: echo})
        return FakeInfo()

    def disconnect(self):
        pass

    def loop_stop(self):
        pass


def make_printer(result="success"):
    ref = []
    clients = []

    def factory():
        client = FakeClient(ref, result)
        clients.append(client)
        return client

    p = BambuPrinter("192.0.2.1", "SERIAL1", "12345678", client_factory=factory)
    ref.append(p)
    p.connect(timeout=1)
    return p, clients[0]


def last_command(client):
    topic, data = client.published[-1]
    assert topic == "device/SERIAL1/request"
    return data


def test_connect_requests_full_status():
    p, client = make_printer()
    assert client.topic == "device/SERIAL1/report"
    assert "pushing" in client.published[0][1]
    assert p.raw_state()["print"]["gcode_state"] == "IDLE"


def test_start_print_3mf_payload():
    p, client = make_printer()
    res = p.start_print("cube.gcode.3mf", plate=2, timelapse=True, use_ams=True, ams_mapping=[1])
    cmd = last_command(client)["print"]
    assert res["acknowledged"] is True
    assert cmd["command"] == "project_file"
    assert cmd["param"] == "Metadata/plate_2.gcode"
    assert cmd["url"] == "file:///sdcard/cube.gcode.3mf"
    assert cmd["subtask_name"] == "cube"
    assert cmd["timelapse"] is True and cmd["use_ams"] is True and cmd["ams_mapping"] == [1]
    assert cmd["bed_levelling"] is True


def test_start_print_plain_gcode():
    p, client = make_printer()
    p.start_print("test.gcode")
    cmd = last_command(client)["print"]
    assert cmd == {"command": "gcode_file", "param": "/sdcard/test.gcode", "sequence_id": cmd["sequence_id"]}


def test_start_print_rejects_stl():
    p, _ = make_printer()
    with pytest.raises(PrinterError):
        p.start_print("model.stl")


def test_rejected_command_raises():
    p, _ = make_printer(result="failed")
    with pytest.raises(PrinterError, match="rejected"):
        p.pause()


def test_missing_ack_is_reported_not_raised():
    p, _ = make_printer(result=None)
    res = p.send({"print": {"command": "resume"}}, wait_ack=0.05)
    assert res["acknowledged"] is False


def test_speed_gcode_and_limits():
    p, client = make_printer()
    p.set_speed("sport")
    assert last_command(client)["print"]["param"] == "3"
    p.set_nozzle_temp(215)
    assert last_command(client)["print"]["param"] == "M104 S215\n"
    p.set_part_fan(50)
    assert last_command(client)["print"]["param"] == "M106 P1 S128\n"
    with pytest.raises(PrinterError):
        p.set_bed_temp(110)
    with pytest.raises(PrinterError):
        p.set_speed("warp")


def test_light_and_filament():
    p, client = make_printer()
    p.set_light(False)
    assert last_command(client)["system"]["led_mode"] == "off"
    p.set_filament(ams_id=255, tray_id=254, filament_type="petg", color="#00ff00",
                   nozzle_temp_min=220, nozzle_temp_max=260)
    cmd = last_command(client)["print"]
    assert cmd["tray_color"] == "00FF00FF" and cmd["tray_type"] == "PETG"
    with pytest.raises(PrinterError):
        p.set_filament(ams_id=255, tray_id=254, filament_type="PLA", color="red",
                       nozzle_temp_min=190, nozzle_temp_max=230)


def test_incremental_reports_merge():
    state = {"print": {"nozzle_temper": 25.0, "mc_percent": 0}}
    deep_merge(state, {"print": {"mc_percent": 40}})
    assert state["print"] == {"nozzle_temper": 25.0, "mc_percent": 40}


def test_summarize():
    s = summarize({"print": {
        "gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 17,
        "layer_num": 30, "total_layer_num": 120, "nozzle_temper": 219.84,
        "bed_temper": 60.0, "spd_lvl": 2, "subtask_name": "benchy",
        "lights_report": [{"node": "chamber_light", "mode": "on"}],
        "vt_tray": {"tray_type": "PLA", "tray_color": "FFFFFFFF", "nozzle_temp_min": "190", "nozzle_temp_max": "230"},
    }})
    assert s["state"] == "printing" and s["speed"] == "standard"
    assert s["nozzle_temp"] == 219.8
    assert s["lights"] == {"chamber_light": "on"}
    assert s["external_spool"]["type"] == "PLA"
