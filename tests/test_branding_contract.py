#!/usr/bin/env python3
"""Focused regression tests for the DeskForge #80 branding PNG consumers.

Covers the review findings on the Windows branding workflow:

  F1  bounded + exact PNG decompression (no decompression bomb; no trailing
      decompressed payload accepted);
  F2  one shared structural PNG validator, used by prepare_branding_assets and
      by every consumer (build icon/logo and third-party privacy screen), so any
      branding PNG declared acceptable is structurally whole and decodable;
  F3  the per-field response cap is bound during transfer, independent of HTTP
      Content-Length and of the runner's curl version, so an oversized response
      never reaches the manifest or a downstream consumer;
  F4  branding text is escaped for each concrete insertion syntax (Windows .rc /
      TOML double-quoted strings vs interpolated Dart single-quoted literals), so
      ordinary safe punctuation is accepted instead of blanket-rejected.

Run with:  python3 tests/test_branding_contract.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / ".github" / "workflows" / "rustqs-windows.yml"
THIRD_PARTY = ROOT / ".github" / "workflows" / "third-party-RustDeskTempTopMostWindow.yml"

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# workflow extraction
# --------------------------------------------------------------------------- #

def _steps(path: Path):
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job in (document.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                yield step


def _run_named(path: Path, name: str) -> str:
    for step in _steps(path):
        if step.get("name") == name and isinstance(step.get("run"), str):
            return step["run"]
    raise AssertionError(f"step {name!r} not found in {path.name}")


def _prepare_run() -> str:
    for step in _steps(WINDOWS):
        run = step.get("run")
        if isinstance(run, str) and "branding assets staged" in run and 'cat > "$validator"' in run:
            return run
    raise AssertionError("prepare_branding_assets sanity step not found")


def _heredocs(run: str):
    return re.findall(r"<<'PY'\n(.*?)\nPY\n", run, re.DOTALL)


def validator_source() -> str:
    for body in _heredocs(_prepare_run()):
        if "class PngError" in body:
            return body
    raise AssertionError("shared validator heredoc not found in the prepare step")


class _ValidatorModule:
    _module = None

    @classmethod
    def load(cls):
        if cls._module is None:
            directory = Path(tempfile.mkdtemp(prefix="branding-validator-"))
            path = directory / "validate_branding_png.py"
            path.write_text(validator_source(), encoding="utf-8")
            spec = importlib.util.spec_from_file_location("deskforge_validate_branding_png", path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cls._module = module
        return cls._module


def run_validator_module(args, *, cwd=None, run_input=None, env=None):
    directory = Path(tempfile.mkdtemp(prefix="branding-validator-run-"))
    path = directory / "validate_branding_png.py"
    path.write_text(validator_source(), encoding="utf-8")
    command = [sys.executable, str(path), *args]
    return subprocess.run(
        command, cwd=cwd, input=run_input, text=True, capture_output=True, env=env
    )


# --------------------------------------------------------------------------- #
# PNG fixture builders
# --------------------------------------------------------------------------- #

def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def filtered_rows(width, height, channels, filter_type, modulo=None):
    rows = bytearray()
    prev = [0] * (width * channels)
    for y in range(height):
        row = [(x * 13 + y * 7 + 5) & 0xFF for x in range(width * channels)]
        if modulo is not None:
            row = [value % modulo for value in row]
        encoded = bytearray(row)
        for i, value in enumerate(row):
            a = row[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = a
            elif filter_type == 2:
                predictor = b
            elif filter_type == 3:
                predictor = (a + b) // 2
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predictor = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            encoded[i] = (value - predictor) & 0xFF
        rows.append(filter_type)
        rows.extend(encoded)
        prev = row
    return bytes(rows)


def make_png(
    width=8,
    height=8,
    color_type=6,
    filter_type=0,
    *,
    depth=8,
    interlace=0,
    compression=0,
    filter_method=0,
    plte=None,
    raw=None,
    idat_zlib=None,
    corrupt_idat_crc=False,
    truncate_idat=False,
    omit_iend=False,
    trailing_after_iend=b"",
    ihdr_override=None,
):
    channels = _CHANNELS.get(color_type, 1)
    if idat_zlib is None:
        if raw is None:
            raw = filtered_rows(width, height, channels, filter_type)
        idat_zlib = zlib.compress(raw)
    ihdr = ihdr_override if ihdr_override is not None else struct.pack(
        ">IIBBBBB", width, height, depth, color_type, compression, filter_method, interlace
    )
    out = bytearray(PNG_SIGNATURE)
    out += _chunk(b"IHDR", ihdr)
    if plte is not None:
        out += _chunk(b"PLTE", plte)
    idat = _chunk(b"IDAT", idat_zlib)
    if corrupt_idat_crc:
        idat = idat[:-1] + bytes([idat[-1] ^ 0xFF])
    if truncate_idat:
        idat = idat[: len(idat) // 2]
    out += idat
    if not omit_iend:
        out += _chunk(b"IEND", b"")
    out += trailing_after_iend
    return bytes(out)


def zeros_stream(total, block_size=1 << 20):
    compressor = zlib.compressobj(9)
    parts = []
    block = b"\x00" * block_size
    remaining = total
    while remaining > 0:
        size = min(block_size, remaining)
        parts.append(compressor.compress(block[:size]))
        remaining -= size
    parts.append(compressor.flush())
    return b"".join(parts)


def reference_bgra(module, data):
    """Independent re-implementation of the reviewed icon raster contract."""
    pos = 8
    ihdr = plte = None
    trns = b""
    idat = b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", payload)
        elif ctype == b"PLTE":
            plte = payload
        elif ctype == b"tRNS":
            trns = payload
        elif ctype == b"IDAT":
            idat += payload
        elif ctype == b"IEND":
            break
    width, height, _depth, color_type, _c, _f, _i = ihdr
    channels = _CHANNELS[color_type]
    raw = zlib.decompress(idat)
    stride = width * channels
    screen = bytearray(width * height * 4)
    prev = bytearray(stride)
    off = 0
    for y in range(height):
        ftype = raw[off]
        off += 1
        row = bytearray(raw[off:off + stride])
        off += stride
        for i in range(stride):
            a = row[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if ftype == 1:
                row[i] = (row[i] + a) & 0xFF
            elif ftype == 2:
                row[i] = (row[i] + b) & 0xFF
            elif ftype == 3:
                row[i] = (row[i] + (a + b) // 2) & 0xFF
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                row[i] = (row[i] + pr) & 0xFF
        prev = row
        base = y * width * 4
        for x in range(width):
            sx = x * channels
            dx = base + x * 4
            if color_type == 6:
                screen[dx:dx + 4] = bytes([row[sx + 2], row[sx + 1], row[sx], row[sx + 3]])
            elif color_type == 2:
                screen[dx:dx + 4] = bytes([row[sx + 2], row[sx + 1], row[sx], 255])
            elif color_type == 4:
                screen[dx:dx + 4] = bytes([row[sx], row[sx], row[sx], row[sx + 1]])
            elif color_type == 0:
                screen[dx:dx + 4] = bytes([row[sx], row[sx], row[sx], 255])
            else:
                index = row[sx]
                alpha = trns[index] if index < len(trns) else 255
                screen[dx:dx + 4] = bytes([plte[index * 3 + 2], plte[index * 3 + 1], plte[index * 3], alpha])
    return bytes(screen), width, height


# --------------------------------------------------------------------------- #
# F1/F2: shared validator
# --------------------------------------------------------------------------- #

class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.module = _ValidatorModule.load()

    def assert_ok(self, data, **kwargs):
        image = self.module.load(data, "test.png", **kwargs)
        return image

    def assert_reject(self, data, fragment=None, **kwargs):
        with self.assertRaises(self.module.PngError) as ctx:
            self.module.load(data, "test.png", **kwargs)
        if fragment is not None:
            self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    def test_accepts_every_color_type_and_filter_type(self):
        for color_type, channels in _CHANNELS.items():
            entries = 4 if color_type == 3 else None
            plte = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0]) if color_type == 3 else None
            for filter_type in range(5):
                raw = filtered_rows(9, 5, channels, filter_type, modulo=entries)
                data = make_png(9, 5, color_type, raw=raw, plte=plte)
                image = self.assert_ok(data)
                self.assertEqual((image.width, image.height), (9, 5), (color_type, filter_type))

    def test_accepts_valid_indexed_with_plte(self):
        plte = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
        raw = filtered_rows(4, 4, 1, 0, modulo=3)
        image = self.assert_ok(make_png(4, 4, 3, raw=raw, plte=plte))
        self.assertEqual(image.color_type, 3)

    def test_rejects_header_only(self):
        ihdr = struct.pack(">IIBBBBB", 16, 16, 8, 6, 0, 0, 0)
        self.assert_reject(PNG_SIGNATURE + _chunk(b"IHDR", ihdr), "truncated")

    def test_rejects_truncated_and_no_iend(self):
        valid = make_png()
        self.assert_reject(valid[:-1], "past its declared length")
        self.assert_reject(make_png(omit_iend=True), "truncated (no IEND")

    def test_rejects_bad_chunk_crc(self):
        self.assert_reject(make_png(corrupt_idat_crc=True), "CRC mismatch")

    def test_rejects_data_after_iend(self):
        self.assert_reject(make_png(trailing_after_iend=b"JUNKJUNK"), "data after IEND")

    def test_rejects_indexed_without_plte(self):
        self.assert_reject(make_png(4, 4, 3), "missing its PLTE")

    def test_rejects_unsupported_color_type(self):
        ihdr = struct.pack(">IIBBBBB", 8, 8, 8, 1, 0, 0, 0)
        self.assert_reject(make_png(8, 8, 6, ihdr_override=ihdr), "color type")

    def test_rejects_interlace_and_bit_depth_and_methods(self):
        self.assert_reject(make_png(interlace=1), "interlaced")
        self.assert_reject(make_png(depth=16), "bit depth")
        self.assert_reject(make_png(compression=1), "compression/filter method")

    def test_rejects_trailing_decompressed_payload(self):
        raw = filtered_rows(8, 8, 4, 0) + b"\xAA" * (256 * 1024)
        self.assert_reject(make_png(8, 8, 6, raw=raw), "more scanline bytes")

    @unittest.skipIf(sys.platform.startswith("win"), "POSIX rlimit only")
    def test_decompression_bomb_is_bounded(self):
        bomb = make_png(16, 16, 6, idat_zlib=zeros_stream(256 * 1024 * 1024))
        self.assertLess(len(bomb), 4 * 1024 * 1024)
        directory = Path(tempfile.mkdtemp(prefix="branding-bomb-"))
        module_path = directory / "validate_branding_png.py"
        module_path.write_text(validator_source(), encoding="utf-8")
        bomb_path = directory / "bomb.png"
        bomb_path.write_bytes(bomb)
        code = (
            "import resource\n"
            "resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))\n"
            "import importlib.util, pathlib, sys\n"
            "spec = importlib.util.spec_from_file_location('v', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "try:\n"
            "    module.load(pathlib.Path(sys.argv[2]).read_bytes(), 'bomb')\n"
            "    print('ACCEPTED')\n"
            "except module.PngError as exc:\n"
            "    print('REJECTED:' + str(exc))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(module_path), str(bomb_path)],
            text=True, capture_output=True,
        )
        self.assertNotIn("MemoryError", result.stderr)
        self.assertIn("REJECTED", result.stdout)

    def test_bgra_matches_reviewed_raster_contract(self):
        for color_type in (0, 2, 4, 6):
            for filter_type in range(5):
                data = make_png(12, 7, color_type, filter_type)
                image = self.module.load(data, "x.png")
                self.assertEqual(image.bgra(), reference_bgra(self.module, data), (color_type, filter_type))
        big = make_png(300, 300, 6, 0)
        got = self.module.load(big, "x.png").bgra()
        self.assertEqual(got[1:], (256, 256))
        self.assertEqual(len(got[0]), 256 * 256 * 4)

    def test_cli_verify_and_prepare_modes(self):
        valid = make_png(16, 16, 6, 0)
        directory = Path(tempfile.mkdtemp(prefix="branding-cli-"))
        (directory / "app-logo.png").write_bytes(valid)
        prepared = run_validator_module([str(directory)])
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        manifest = json.loads((directory / "branding_manifest.json").read_text())
        self.assertIn("app-logo.png", manifest["images"])

        verified = run_validator_module([str(directory / "app-logo.png"), "--label", "app-logo.png"])
        self.assertEqual(verified.returncode, 0, verified.stderr)

        corrupt = directory / "app-icon.png"
        corrupt.write_bytes(make_png(corrupt_idat_crc=True))
        rejected = run_validator_module([str(corrupt), "--label", "app-icon.png"])
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("::error::", rejected.stdout)


# --------------------------------------------------------------------------- #
# F2: prepare staging and consumers
# --------------------------------------------------------------------------- #

class PrepareStagingTests(unittest.TestCase):
    def _prepare(self, files):
        staging = Path(tempfile.mkdtemp(prefix="branding-prepare-"))
        for name, data in files.items():
            (staging / name).write_bytes(data)
        result = run_validator_module([str(staging)])
        manifest = staging / "branding_manifest.json"
        return result, (json.loads(manifest.read_text()) if manifest.exists() else None)

    def test_valid_images_write_manifest(self):
        result, manifest = self._prepare({
            "app-icon.png": make_png(32, 32, 6, 0),
            "app-logo.png": make_png(16, 16, 6, 0),
            "privacy-screen.png": make_png(16, 16, 6, 0),
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(manifest["images"]), {"app-icon.png", "app-logo.png", "privacy-screen.png"})

    def test_no_images_is_noop(self):
        result, manifest = self._prepare({})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(manifest)
        self.assertIn("branding assets staged: none", result.stdout)

    def test_rejects_every_corrupt_fixture_without_writing_manifest(self):
        fixtures = {
            "header-only": PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 6, 0, 0, 0)),
            "truncated": make_png(omit_iend=True),
            "bad-crc": make_png(corrupt_idat_crc=True),
            "after-iend": make_png(trailing_after_iend=b"JUNKJUNK"),
            "indexed-no-plte": make_png(4, 4, 3),
            "trailing": make_png(8, 8, 6, raw=filtered_rows(8, 8, 4, 0) + b"\xAA" * 4096),
        }
        for name, data in fixtures.items():
            with self.subTest(name=name):
                result, manifest = self._prepare({"app-logo.png": data})
                self.assertEqual(result.returncode, 1, name)
                self.assertIsNone(manifest, name)
                self.assertIn("::error::", result.stdout, name)

    def test_icon_must_be_square(self):
        result, manifest = self._prepare({"app-icon.png": make_png(16, 24, 6, 0)})
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(manifest)


# --------------------------------------------------------------------------- #
# F3: fetch-time size cap
# --------------------------------------------------------------------------- #

class _OversizeHandler(BaseHTTPRequestHandler):
    BIG = 4 * 1024 * 1024
    SMALL = 1024
    # A body far larger than any per-field cap, sent chunked (no Content-Length) so
    # the transfer can only be stopped by a client-side bound, never by a declared
    # length. On the ubuntu-22.04 runner's curl 7.81.0 this endpoint is what proves
    # the cap is enforced during transfer rather than after a full download.
    HUGE = 64 * 1024 * 1024
    BLOCK = 65536

    def _send_chunked(self, total):
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        block = b"A" * self.BLOCK
        written = 0
        try:
            while written < total:
                self.wfile.write(("%x\r\n" % len(block)).encode() + block + b"\r\n")
                written += len(block)
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            # The bounded client closed the stream once the cap was passed.
            pass
        self.server.bytes_written = written
        self.server.completed = written >= total

    def do_GET(self):
        self.server.request_done.clear()
        self.server.bytes_written = 0
        self.server.completed = False
        try:
            if self.path == "/big":
                self._send_chunked(self.BIG)
            elif self.path == "/huge":
                self._send_chunked(self.HUGE)
            elif self.path == "/chunked-small":
                body = b"C" * self.SMALL
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(("%x\r\n" % len(body)).encode() + body + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            elif self.path == "/small":
                body = b"C" * self.SMALL
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
        finally:
            self.server.request_done.set()

    def log_message(self, *args):
        pass


class FetchCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        cls.server = _Server(("127.0.0.1", 0), _OversizeHandler)
        cls.server.request_done = threading.Event()
        cls.server.bytes_written = 0
        cls.server.completed = False
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    @classmethod
    def fetch_function(cls):
        run = _prepare_run()
        start = run.index("fetch_branding_image() {")
        end = run.index("fetch_branding_image app_icon_url", start)
        return run[start:end]

    def _fetch(self, path, cap):
        directory = Path(tempfile.mkdtemp(prefix="branding-fetch-"))
        out = directory / "image.png"
        script = "set -euo pipefail\n" + self.fetch_function() + (
            'fetch_branding_image app_logo_url "$URL" "$OUT" "$CAP"\n'
        )
        env = {
            **os.environ,
            "URL": f"http://127.0.0.1:{self.port}{path}",
            "OUT": str(out),
            "CAP": str(cap),
        }
        result = subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True, env=env)
        # Wait for the server-side handler to finish so the recorded stream stats
        # reflect this fetch rather than a race with the client shutting down.
        self.assertTrue(self.server.request_done.wait(10), "server never completed the request")
        return result, out

    def test_oversized_response_is_rejected_without_leaving_a_file(self):
        result, out = self._fetch("/big", 1024 * 1024)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::", result.stdout)
        self.assertFalse(out.exists())
        self.assertFalse((out.parent / (out.name + ".part")).exists())

    def test_chunked_oversize_is_bounded_during_transfer(self):
        # No Content-Length, and a body far above the cap. The fetch must stop the
        # transfer once the cap is passed instead of downloading the whole stream and
        # rejecting it afterwards. The server records whether it ever streamed the
        # complete body: a bounded client makes that impossible.
        result, out = self._fetch("/huge", 1024 * 1024)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::", result.stdout)
        self.assertIn("exceeds the per-field byte cap", result.stdout)
        self.assertFalse(out.exists())
        self.assertFalse((out.parent / (out.name + ".part")).exists())
        self.assertFalse(
            self.server.completed,
            "the client read the entire oversized chunked body instead of bounding it at fetch time",
        )
        self.assertLess(
            self.server.bytes_written,
            _OversizeHandler.HUGE // 2,
            "the transfer was not cut off near the cap",
        )

    def test_under_cap_content_length_response_is_accepted(self):
        result, out = self._fetch("/small", 1024 * 1024)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(out.stat().st_size, _OversizeHandler.SMALL)

    def test_under_cap_chunked_response_is_accepted(self):
        # The bound must not reject a legitimate body whose length is unknown before
        # download.
        result, out = self._fetch("/chunked-small", 1024 * 1024)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(out.stat().st_size, _OversizeHandler.SMALL)


# --------------------------------------------------------------------------- #
# F4: per-target escaping
# --------------------------------------------------------------------------- #

class BrandingEscapeTests(unittest.TestCase):
    ANCHOR_FILES = {
        "flutter/windows/runner/Runner.rc": [
            'VALUE "CompanyName", "Purslane Tech Pte. Ltd." "\\0"',
            'VALUE "LegalCopyright", "Copyright \u00a9 2026 Purslane Tech Pte. Ltd. All rights reserved." "\\0"',
        ],
        "Cargo.toml": ['LegalCopyright = "Copyright \u00a9 2026 Purslane Tech Pte. Ltd. All rights reserved."'],
        "libs/portable/Cargo.toml": ['LegalCopyright = "Copyright \u00a9 2026 Purslane Tech Pte. Ltd. All rights reserved."'],
        "flutter/lib/desktop/pages/desktop_setting_page.dart": [
            "        'Copyright \u00a9 ${year} Purslane Tech Pte. Ltd.\\n$license',",
            "                    launchUrlString('https://rustdesk.com/privacy.html');",
            "                    launchUrlString('https://rustdesk.com');",
        ],
        "flutter/lib/desktop/pages/desktop_home_page.dart": ["'https://rustdesk.com/download'"],
        "flutter/lib/mobile/pages/connection_page.dart": ["'https://rustdesk.com/download'"],
        "flutter/lib/common.dart": ["launchUrl(Uri.parse('https://rustdesk.com'));"],
        "flutter/lib/mobile/pages/settings_page.dart": [
            "const url = 'https://rustdesk.com/';",
            "const url = 'https://rustdesk.com/';",
            "launchUrlString('https://rustdesk.com/privacy.html');",
        ],
        "flutter/lib/desktop/pages/install_page.dart": [
            "'https://rustdesk.com/privacy.html'),",
            "message: 'https://rustdesk.com/privacy.html',",
        ],
    }

    @classmethod
    def branding_python(cls):
        run = _run_named(WINDOWS, "L3 brand: company name + branded About links")
        return _heredocs(run)[0]

    def _make_repo(self, mutate=None):
        repo = Path(tempfile.mkdtemp(prefix="branding-repo-"))
        for rel, lines in self.ANCHOR_FILES.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            content = (mutate or {}).get(rel, "\n".join(lines) + "\n")
            path.write_text(content, encoding="utf-8")
        return repo

    def _run(self, company="", download="", website="", privacy="", mutate=None):
        repo = self._make_repo(mutate=mutate)
        env = {
            **os.environ,
            "RQS_COMPANY_NAME": company,
            "RQS_DOWNLOAD_URL": download,
            "RQS_WEBSITE_URL": website,
            "RQS_PRIVACY_POLICY_URL": privacy,
        }
        result = subprocess.run(
            [sys.executable, "-"], input=self.branding_python(), text=True,
            capture_output=True, cwd=str(repo), env=env,
        )
        outputs = {}
        if result.returncode == 0:
            for rel in self.ANCHOR_FILES:
                text = (repo / rel).read_text(encoding="utf-8")
                outputs[rel] = text
        shutil.rmtree(repo)
        return result, outputs

    def test_ordinary_apostrophe_is_escaped_for_dart_only(self):
        result, out = self._run(company="O'Reilly")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        dart = out["flutter/lib/desktop/pages/desktop_setting_page.dart"]
        self.assertIn("O\\'Reilly", dart)
        cargo = out["Cargo.toml"]
        self.assertIn("O'Reilly", cargo)  # TOML double-quoted: apostrophe is literal
        self.assertNotIn("O\\'Reilly", cargo)

    def test_quote_backslash_dollar_escaped_per_target(self):
        value = 'He said "hi" \\ $cash'
        result, out = self._run(company=value)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rc = out["flutter/windows/runner/Runner.rc"]
        self.assertIn('"He said \\"hi\\" \\\\ $cash"', rc)
        toml = out["Cargo.toml"]
        self.assertIn('He said \\"hi\\" \\\\ $cash. All rights reserved.', toml)
        dart = out["flutter/lib/desktop/pages/desktop_setting_page.dart"]
        self.assertIn('He said "hi" \\\\ \\$cash', dart)

    def test_url_fields_escape_dart_literals(self):
        result, out = self._run(
            download="https://x.example/a'b$c\\d",
            website="https://x.example/w's",
            privacy="https://x.example/p$v",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        home = out["flutter/lib/desktop/pages/desktop_home_page.dart"]
        self.assertIn("'https://x.example/a\\'b\\$c\\\\d'", home)
        settings = out["flutter/lib/mobile/pages/settings_page.dart"]
        self.assertIn("'https://x.example/w\\'s'", settings)
        self.assertIn("'https://x.example/p\\$v'", settings)

    def test_control_characters_still_rejected(self):
        result, _ = self._run(company="bad\nname")
        self.assertEqual(result.returncode, 1)
        self.assertIn("control characters", result.stdout)

    def test_anchor_mismatch_fails_closed(self):
        result, _ = self._run(company="X", mutate={"Cargo.toml": "LegalCopyright = \"changed\"\n"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("anchor mismatch", result.stdout)

    def test_no_branding_value_fails_closed(self):
        result, _ = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("without any branding value", result.stdout)


# --------------------------------------------------------------------------- #
# consumers: build icon/logo and third-party privacy
# --------------------------------------------------------------------------- #

ICON_ANCHORS = {
    "flutter/lib/common.dart": [
        "        return Image.asset('assets/icon.png',",
        "const _kDefaultLogoAsset = 'assets/logo.png';",
    ],
    "flutter/windows/runner/Runner.rc": ['IDI_APP_ICON ICON "app_icon.ico"'],
    "build.rs": ['res.set_icon("res/icon.ico")'],
    "src/tray.rs": ['icon = include_bytes!("../res/tray-icon.ico");'],
}
ICO_TARGETS = (
    "flutter/windows/runner/resources/app_icon.ico",
    "res/icon.ico",
    "res/tray-icon.ico",
)
PLACEHOLDER_ICO = b"\x00\x00\x01\x00\x01\x00" + b"\x00" * 16


def make_icon_repo():
    repo = Path(tempfile.mkdtemp(prefix="branding-iconrepo-"))
    for rel, lines in ICON_ANCHORS.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (repo / "flutter" / "assets").mkdir(parents=True, exist_ok=True)
    for target in ICO_TARGETS:
        path = repo / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PLACEHOLDER_ICO)
    return repo


def make_branding_artifact(icon=None, logo=None, manifest_override=None):
    artifact = Path(tempfile.mkdtemp(prefix="branding-artifact-"))
    (artifact / "validate_branding_png.py").write_text(validator_source(), encoding="utf-8")
    images = {}
    for name, data in (("app-icon.png", icon), ("app-logo.png", logo)):
        if data is None:
            continue
        (artifact / name).write_bytes(data)
        images[name] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "width": 0,
            "height": 0,
        }
    manifest = {"images": images}
    if manifest_override is not None:
        manifest = manifest_override(manifest)
    (artifact / "branding_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return artifact


class BuildConsumerTests(unittest.TestCase):
    @classmethod
    def icon_python(cls):
        return _heredocs(_run_named(WINDOWS, "L3 brand: apply branded icon + logo assets"))[0]

    def _run(self, artifact, repo):
        return subprocess.run(
            [sys.executable, "-", str(artifact)], input=self.icon_python(),
            text=True, capture_output=True, cwd=str(repo),
        )

    def test_valid_icon_and_logo_are_consumed(self):
        icon = make_png(32, 32, 6, 0)
        logo = make_png(16, 16, 6, 0)
        artifact = make_branding_artifact(icon=icon, logo=logo)
        repo = make_icon_repo()
        result = self._run(artifact, repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((repo / "flutter/assets/icon.png").read_bytes(), icon)
        self.assertEqual((repo / "flutter/assets/logo.png").read_bytes(), logo)
        branded = (repo / "res/icon.ico").read_bytes()
        self.assertNotEqual(branded, PLACEHOLDER_ICO)
        self.assertEqual(branded[:4], b"\x00\x00\x01\x00")

    def test_digest_mismatch_fails_closed(self):
        icon = make_png(32, 32, 6, 0)
        logo = make_png(16, 16, 6, 0)
        artifact = make_branding_artifact(icon=icon, logo=logo)
        # Tamper the logo bytes after the manifest was written.
        (artifact / "app-logo.png").write_bytes(make_png(20, 20, 6, 0))
        repo = make_icon_repo()
        result = self._run(artifact, repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("digest does not match", result.stdout)

    def test_corrupt_logo_with_matching_digest_fails_closed(self):
        icon = make_png(32, 32, 6, 0)
        corrupt = make_png(omit_iend=True)
        artifact = make_branding_artifact(icon=icon, logo=corrupt)
        repo = make_icon_repo()
        result = self._run(artifact, repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::", result.stdout)

    def test_missing_validator_fails_closed(self):
        icon = make_png(32, 32, 6, 0)
        artifact = make_branding_artifact(icon=icon)
        (artifact / "validate_branding_png.py").unlink()
        repo = make_icon_repo()
        result = self._run(artifact, repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("shared PNG validator", result.stdout)

    def test_missing_manifest_fails_closed(self):
        artifact = make_branding_artifact(icon=make_png(32, 32, 6, 0))
        (artifact / "branding_manifest.json").unlink()
        repo = make_icon_repo()
        result = self._run(artifact, repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("branding_manifest.json", result.stdout)


class PrivacyConsumerTests(unittest.TestCase):
    IMG_CPP = 'const unsigned char g_img[] = {\n0x00\n};\n'
    IMG_H = "extern const unsigned char g_img[];\n"

    @classmethod
    def privacy_shell(cls):
        return _run_named(THIRD_PARTY, "Brand privacy screen: replace WindowInjection img.cpp")

    def _make_repo(self):
        repo = Path(tempfile.mkdtemp(prefix="branding-privacyrepo-"))
        img_root = repo / "third_party/windows/RustDeskTempTopMostWindow/WindowInjection"
        img_root.mkdir(parents=True)
        (img_root / "img.cpp").write_text('#include "pch.h"\n#include "./img.h"\n\n' + self.IMG_CPP, encoding="utf-8")
        (img_root / "img.h").write_text(self.IMG_H, encoding="utf-8")
        patch = repo / ".github/patches"
        patch.mkdir(parents=True)
        shutil.copy(ROOT / ".github/patches/rdgen-privacyScreen.py", patch / "rdgen-privacyScreen.py")
        return repo

    def _run(self, png_bytes, entry_overrides=None, include_validator=True, include_manifest=True):
        runner_temp = Path(tempfile.mkdtemp(prefix="branding-runner-"))
        artifact = runner_temp / "deskforge-privacy-screen"
        artifact.mkdir(parents=True)
        (artifact / "privacy-screen.png").write_bytes(png_bytes)
        entry = {
            "sha256": hashlib.sha256(png_bytes).hexdigest(),
            "bytes": len(png_bytes),
            "width": 0,
            "height": 0,
        }
        if entry_overrides is not None:
            entry = entry_overrides(entry)
        if include_manifest:
            (artifact / "branding_manifest.json").write_text(
                json.dumps({"images": {"privacy-screen.png": entry}}), encoding="utf-8"
            )
        if include_validator:
            (artifact / "validate_branding_png.py").write_text(validator_source(), encoding="utf-8")
        repo = self._make_repo()
        result = subprocess.run(
            ["bash", "-s"], input=self.privacy_shell(), text=True,
            capture_output=True, cwd=str(repo),
            env={**os.environ, "RUNNER_TEMP": str(runner_temp)},
        )
        img_cpp = repo / "third_party/windows/RustDeskTempTopMostWindow/WindowInjection/img.cpp"
        return result, img_cpp.read_bytes()

    def test_valid_privacy_screen_is_applied(self):
        png = make_png(16, 16, 6, 0)
        result, img_cpp = self._run(png)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b"g_img", img_cpp)
        self.assertNotEqual(img_cpp, ('#include "pch.h"\n#include "./img.h"\n\n' + self.IMG_CPP).encode())

    def test_corrupt_png_with_matching_digest_fails_closed(self):
        for name, png in {
            "truncated": make_png(omit_iend=True),
            "bad-crc": make_png(corrupt_idat_crc=True),
            "after-iend": make_png(trailing_after_iend=b"JUNKJUNK"),
            "trailing": make_png(8, 8, 6, raw=filtered_rows(8, 8, 4, 0) + b"\xAA" * 4096),
        }.items():
            with self.subTest(name=name):
                result, _ = self._run(png)
                self.assertEqual(result.returncode, 1, name)

    def test_digest_mismatch_fails_closed(self):
        result, _ = self._run(make_png(16, 16, 6, 0), entry_overrides=lambda e: {**e, "sha256": "0" * 64})
        self.assertEqual(result.returncode, 1)
        self.assertIn("digest does not match", result.stdout)

    def test_missing_manifest_entry_fails_closed(self):
        result, _ = self._run(make_png(16, 16, 6, 0), entry_overrides=lambda e: {"bytes": e["bytes"]})
        self.assertEqual(result.returncode, 1)
        self.assertIn("no sha256 entry", result.stdout)

    def test_missing_validator_fails_closed(self):
        result, _ = self._run(make_png(16, 16, 6, 0), include_validator=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("shared branding PNG validator is missing", result.stdout)


# --------------------------------------------------------------------------- #
# workflow-level contract
# --------------------------------------------------------------------------- #

class WorkflowContractTests(unittest.TestCase):
    def test_single_branding_artifact_and_shared_validator_wiring(self):
        windows = WINDOWS.read_text(encoding="utf-8")
        third = THIRD_PARTY.read_text(encoding="utf-8")
        uploads = [
            step for step in _steps(WINDOWS)
            if str(step.get("uses", "")).startswith("actions/upload-artifact")
            and step.get("with", {}).get("name") == "deskforge-branding-assets"
        ]
        self.assertEqual(len(uploads), 1)
        # prepare ships the validator; consumers load it from the artifact.
        self.assertIn('cp -- "$validator" "$staging/validate_branding_png.py"', windows)
        self.assertIn("validate_branding_png.py", windows)
        self.assertIn("validate_branding_png.py", third)

    def test_no_unbounded_decompression_remains(self):
        for path in (WINDOWS, THIRD_PARTY):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("zlib.decompress(idat)", text, path.name)
            self.assertNotIn("zlib.decompress(data)", text, path.name)

    def test_prepare_job_keeps_no_permissions_and_no_checkout(self):
        document = yaml.safe_load(WINDOWS.read_text(encoding="utf-8"))
        prepare = document["jobs"]["prepare_branding_assets"]
        self.assertEqual(prepare.get("permissions"), {})
        for step in prepare.get("steps", []):
            self.assertFalse(str(step.get("uses", "")).startswith("actions/checkout"))

    def test_branding_step_has_a_non_empty_field_guard(self):
        for step in _steps(WINDOWS):
            if step.get("name") == "L3 brand: company name + branded About links":
                guard = step.get("if", "")
                self.assertIn("env.RQS_COMPANY_NAME != ''", guard)
                return
        self.fail("branding step not found")

    def test_no_duplicate_rustdesk_name_replacement_mechanism(self):
        text = WINDOWS.read_text(encoding="utf-8")
        self.assertEqual(text.count("RustDesk Remote Desktop"), len(re.findall(r"RustDesk Remote Desktop", text)))
        self.assertNotIn("RQS_COMPANY_NAME:-RustDesk", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
