import io
import os
import sys
import zipfile
from dataclasses import replace
from email.message import Message

import pytest

from bambu_mcp import models, remote
from bambu_mcp.config import ConfigError, Settings, read_config_file, write_config_file
from bambu_mcp.models import ModelError, download, list_models, resolve_local


def make_settings(tmp_path, **kw):
    return Settings(host="h", serial="s", access_code="c", model_dir=tmp_path / "models",
                    slice_output_dir=tmp_path / "out", **kw)


def test_config_file_roundtrip_and_env_wins(tmp_path):
    path = tmp_path / "config.env"
    write_config_file(path, {"BAMBU_HOST": "192.168.1.5", "BAMBU_ACCESS_CODE": "abc", "BAMBU_SERIAL": ""})
    assert read_config_file(path) == {"BAMBU_HOST": "192.168.1.5", "BAMBU_ACCESS_CODE": "abc"}
    s = Settings.from_env({"BAMBU_CONFIG": str(path), "BAMBU_ACCESS_CODE": "fromenv"})
    assert s.host == "192.168.1.5" and s.access_code == "fromenv"


def test_resolve_local_restricted(tmp_path):
    s = make_settings(tmp_path, restrict_paths=True)
    s.model_dir.mkdir()
    (s.model_dir / "a.stl").write_text("solid")
    (tmp_path / "secret.txt").write_text("x")
    assert resolve_local("a.stl", s) == (s.model_dir / "a.stl").resolve()
    for bad in ("../secret.txt", str(tmp_path / "secret.txt")):
        with pytest.raises(ModelError, match="model folder"):
            resolve_local(bad, s)
    # Locally (Claude Desktop) any existing path is fine.
    assert resolve_local(str(tmp_path / "secret.txt"), replace(s, restrict_paths=False)).name == "secret.txt"


def test_list_models_newest_first(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "old.stl").write_text("a")
    os.utime(tmp_path / "old.stl", (1, 1))
    (tmp_path / "sub" / "new.3mf").write_text("b")
    (tmp_path / "notes.txt").write_text("c")
    assert [m["name"] for m in list_models(tmp_path)] == ["sub/new.3mf", "old.stl"]


class FakeResponse(io.BytesIO):
    def __init__(self, data, url, filename=None):
        super().__init__(data)
        self._url = url
        self.headers = Message()
        if filename:
            self.headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeOpener:
    def __init__(self, response):
        self.response = response

    def open(self, request, timeout):
        return self.response


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(models.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))])


def test_download_single_file(tmp_path, public_dns):
    url = "https://example.com/files/Halter%20v2.stl"
    saved = download(url, tmp_path, opener=FakeOpener(FakeResponse(b"solid x", url)))
    assert [p.name for p in saved] == ["Halter v2.stl"]
    again = download(url, tmp_path, opener=FakeOpener(FakeResponse(b"solid x", url)))
    assert again[0].name == "Halter v2 (1).stl"


def test_download_zip_extracts_models_only(tmp_path, public_dns):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("files/part.stl", "solid")
        z.writestr("../../evil.stl", "solid")
        z.writestr("README.txt", "hi")
    url = "https://example.com/download/123"
    saved = download(url, tmp_path, opener=FakeOpener(FakeResponse(buf.getvalue(), url, "Bracket.zip")))
    assert sorted(p.relative_to(tmp_path).as_posix() for p in saved) == ["Bracket/evil.stl", "Bracket/part.stl"]


def test_download_rejects_web_pages_and_local_network(tmp_path, public_dns, monkeypatch):
    url = "https://example.com/thing/123"
    with pytest.raises(ModelError, match="not a model file"):
        download(url, tmp_path, opener=FakeOpener(FakeResponse(b"<html>", url)))
    monkeypatch.setattr(models.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("192.168.178.1", 80))])
    with pytest.raises(ModelError, match="local network"):
        download("http://router.local/x.stl", tmp_path)
    with pytest.raises(ModelError, match="http"):
        download("file:///etc/passwd", tmp_path)


def test_build_app_requires_long_token():
    from bambu_mcp.server import mcp
    with pytest.raises(ConfigError):
        remote.build_app(mcp, "short")
    app = remote.build_app(mcp, "x" * 32)
    paths = [getattr(r, "path", "") for r in app.routes]
    assert "/" + "x" * 32 + "/mcp" in paths and "/" in paths


def test_quick_tunnel_reads_address(tmp_path, monkeypatch):
    fake = tmp_path / "cloudflared"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "print('INF Requesting new quick Tunnel on trycloudflare.com...', file=sys.stderr, flush=True)\n"
        "print('INF |  https://brave-lion-test.trycloudflare.com  |', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(remote, "find_cloudflared", lambda: str(fake))
    assert remote.start_quick_tunnel(8765, timeout=10) == "https://brave-lion-test.trycloudflare.com"


def test_connector_url(tmp_path, monkeypatch):
    monkeypatch.setenv("BAMBU_CONFIG", str(tmp_path / "config.env"))
    assert remote.connector_url("tok", 8765, "https://drucker.example.de") == "https://drucker.example.de/tok/mcp"
    assert "127.0.0.1:8765/tok/mcp" in remote.connector_url("tok", 8765, None)
