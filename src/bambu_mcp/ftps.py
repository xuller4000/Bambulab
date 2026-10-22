"""File access to the printer's SD card over implicit FTPS (port 990)."""

from __future__ import annotations

import ftplib
import os
import socket
import ssl
from pathlib import Path

from .printer import tls_context


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP_TLS variant for implicit TLS with TLS session reuse on data channels.

    Bambu printers refuse data connections that do not resume the control
    channel's TLS session. Some firmware never answers the TLS shutdown after
    a transfer, so ``unwrap()`` gets a short timeout instead of blocking.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._sock: socket.socket | None = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=32768, callback=None, rest=None):
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while buf := fp.read(blocksize):
                conn.sendall(buf)
                if callback:
                    callback(buf)
            if isinstance(conn, ssl.SSLSocket):
                conn.settimeout(5)
                try:
                    conn.unwrap()
                except (OSError, ssl.SSLError):
                    pass
        return self.voidresp()


class PrinterFiles:
    def __init__(self, host: str, access_code: str, port: int = 990, timeout: float = 30) -> None:
        self.host = host
        self.access_code = access_code
        self.port = port
        self.timeout = timeout

    def _open(self) -> ImplicitFTPS:
        ftp = ImplicitFTPS(context=tls_context(), timeout=self.timeout)
        ftp.connect(self.host, self.port)
        ftp.login("bblp", self.access_code)
        ftp.prot_p()
        return ftp

    def upload(self, local_path: str | os.PathLike, remote_name: str | None = None) -> dict:
        path = Path(local_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"No such file: {path}")
        remote = (remote_name or path.name).lstrip("/")
        ftp = self._open()
        try:
            with path.open("rb") as fh:
                ftp.storbinary(f"STOR {remote}", fh)
        finally:
            _close(ftp)
        return {"remote_name": remote, "bytes": path.stat().st_size}

    def list(self, directory: str = "/") -> list[dict]:
        ftp = self._open()
        lines: list[str] = []
        try:
            ftp.cwd(directory)
            ftp.retrlines("LIST", lines.append)
        finally:
            _close(ftp)
        return [entry for line in lines if (entry := parse_list_line(line))]

    def delete(self, remote_name: str) -> None:
        ftp = self._open()
        try:
            ftp.delete(remote_name)
        finally:
            _close(ftp)


def parse_list_line(line: str) -> dict | None:
    """Parse a unix-style LIST line (``-rw-r--r-- 1 u g 1234 Oct 22 14:31 name``)."""
    parts = line.split(None, 8)
    if len(parts) < 9:
        return None
    perms, size, name = parts[0], parts[4], parts[8]
    if name in (".", ".."):
        return None
    return {
        "name": name,
        "type": "dir" if perms.startswith("d") else "file",
        "size": int(size) if size.isdigit() else None,
        "modified": " ".join(parts[5:8]),
    }


def _close(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except Exception:
        ftp.close()
