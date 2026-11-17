from __future__ import annotations

import base64
import struct
import sys
import zlib
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png_bytes(width: int = 1, height: int = 1) -> bytes:
    """A real, decodable PNG — mask/dimension validation needs true headers."""
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def png_data_url(width: int = 1, height: int = 1) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes(width, height)).decode()


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path, monkeypatch):
    """Every test gets its own data dir — never a real ~/.hermes/imagegen."""
    monkeypatch.setenv("HERMES_IMAGEGEN_DATA", str(tmp_path / "imagegen"))
    from imagegen_backend import paths, runtime

    paths.data_root()
    runtime.reset_for_tests()
    yield
    runtime.reset_for_tests()
