import json

from bambu_mcp import check
from bambu_mcp.mesh import part_stats


def test_reads_bambu_entry_from_store_and_classic_paths(tmp_path, monkeypatch):
    store = tmp_path / "local" / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)
    (store / "claude_desktop_config.json").write_text(json.dumps(
        {"mcpServers": {"bambu": {"command": "x.exe", "env": {"BAMBU_HOST": "192.168.1.9"}}}, "preferences": {}}))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    entry, path = check.load_bambu_entry()
    assert entry["env"]["BAMBU_HOST"] == "192.168.1.9" and path.parent == store


def test_log_tail_masks_access_code(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "mcp-server-bambu.log").write_text("start\nlogin with 87654321 failed\n")
    assert check.log_tail(tmp_path / "claude_desktop_config.json", "87654321") == ["start", "login with XXX failed"]


def test_tiny_test_model(tmp_path):
    from bambu_mcp.mesh import load_parts
    s = part_stats(load_parts(check.tiny_test_model(tmp_path))[0])
    assert s.size_mm == (20, 20, 1) and s.volume_cm3 == 0.4
