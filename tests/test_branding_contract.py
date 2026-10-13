#!/usr/bin/env python3
"""Focused regression tests for the DeskForge #80 branding PNG consumers.

Covers the review findings on the Windows branding workflow:

  F1  bounded + exact PNG decompression (no decompression bomb; no trailing
      decompressed payload accepted);
  F2  one shared structural PNG validator, used by prepare_branding_assets and
      by every consumer (build icon/logo and third-party privacy screen), so any
      branding PNG declared acceptable is structurally whole and decodable;
  F3  the per-field response cap is bound during transfer by the shipped fetch
      helper, independent of HTTP Content-Length and of the runner's curl
      version, so an oversized response never reaches the manifest or a
      downstream consumer;
  F4  branding text is escaped for each concrete insertion syntax (Windows .rc vs
      TOML basic strings vs interpolated Dart single-quoted literals), so ordinary
      safe punctuation is accepted instead of blanket-rejected;
  F5  the per-field byte cap bounds only the fetched response. It is never re-applied
      as a validity gate on the canonical artifact at any consumer: a valid in-domain
      source within the fetch cap may re-encode to bytes larger than that cap, and the
      canonical bytes are bounded by their geometry and pinned by the manifest digest
      instead.

  F6  the branding fetch helper connects only to validated public destinations:
      each hop is resolved once, a hop whose resolution contains a loopback,
      private, link-local or other non-public address is refused before any
      connection is opened, the connection goes to the validated address rather
      than to the hostname (so nothing can re-resolve between the check and the
      connect), and every redirect hop repeats the scheme check and the address
      policy before connecting. A local HTTP fixture plus a policy seam prove
      the refusals at the connection boundary, not by string validation.

  F7  each fetch attempt is bounded by a wall-clock deadline, not by the named
      checks between socket operations: a peer that dribbles status-line, header,
      interim-response or body bytes inside every read timeout is cut off at the
      deadline by a watchdog that shuts the attempt's live socket down, and the
      workflow shell wrapper adds a process-level timeout backstop for the one
      call that cannot be interrupted in-process (a hung resolver call). Framing
      is verified before the final rename (a body shorter than its declared
      Content-Length or a chunked body without its terminating chunk fails
      closed), and a resolution that mixes public and non-public addresses is
      refused in full before any connection.

Run with:  python3 tests/test_branding_contract.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
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


def fetcher_source() -> str:
    for body in _heredocs(_prepare_run()):
        if "class FetchError" in body and "def fetch_branding_image(" in body:
            return body
    raise AssertionError("destination-pinned fetch helper heredoc not found in the prepare step")


class _FetcherModule:
    _module = None

    @classmethod
    def load(cls):
        if cls._module is None:
            directory = Path(tempfile.mkdtemp(prefix="branding-fetcher-"))
            path = directory / "fetch_branding_image.py"
            path.write_text(fetcher_source(), encoding="utf-8")
            spec = importlib.util.spec_from_file_location("deskforge_fetch_branding_image", path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cls._module = module
        return cls._module


def fixture_address_policy(module):
    """Permit exactly the fixture address; defer every other address to production."""
    def policy(address):
        return str(address) == FIXTURE_ADDRESS or module.is_public_address(address)
    return policy


def fetch_shell_function() -> str:
    run = _prepare_run()
    start = run.index("fetch_branding_image() {")
    end = run.index("fetch_branding_image app_icon_url", start)
    return run[start:end]


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
    trns=None,
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
    if trns is not None:
        out += _chunk(b"tRNS", trns)
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


def png_chunks(data):
    """Walk a PNG chunk stream (test-side, structural only) -> [(type, payload), ...]."""
    pos = 8
    chunks = []
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunks.append((ctype, data[pos + 8:pos + 8 + length]))
        pos += 12 + length
    return chunks


def png_header(data):
    """Return the IHDR tuple (width, height, depth, color_type, compression, filter, interlace)."""
    for ctype, payload in png_chunks(data):
        if ctype == b"IHDR":
            return struct.unpack(">IIBBBBB", payload)
    raise AssertionError("PNG has no IHDR chunk")


# Adam7 interlace passes as (xstart, ystart, xstep, ystep) (PNG 1.2 8.2).
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
          (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def pass_extent(size, start, step):
    """Number of pixels along one axis of an Adam7 pass; zero means no data."""
    if size <= start:
        return 0
    return (size - start + step - 1) // step


def pack_samples(samples, depth):
    """Pack native sample values MSB-first into PNG row bytes (PNG 1.2 4.1.2)."""
    if depth == 8:
        return bytes(value & 0xFF for value in samples)
    if depth == 16:
        out = bytearray()
        for value in samples:
            out += struct.pack(">H", value & 0xFFFF)
        return bytes(out)
    mask = (1 << depth) - 1
    out = bytearray()
    accumulator = 0
    bits = 0
    for value in samples:
        accumulator = (accumulator << depth) | (value & mask)
        bits += depth
        while bits >= 8:
            bits -= 8
            out.append((accumulator >> bits) & 0xFF)
    if bits:
        out.append((accumulator << (8 - bits)) & 0xFF)
    return bytes(out)


def encode_filter(raw_row, prev_row, bpp, filter_type):
    """Apply one PNG scanline filter to ``raw_row`` with byte stride ``bpp``."""
    encoded = bytearray(len(raw_row))
    for i, value in enumerate(raw_row):
        a = raw_row[i - bpp] if i >= bpp else 0
        b = prev_row[i]
        c = prev_row[i - bpp] if i >= bpp else 0
        if filter_type == 1:
            predictor = a
        elif filter_type == 2:
            predictor = b
        elif filter_type == 3:
            predictor = (a + b) // 2
        elif filter_type == 4:
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            predictor = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
        else:
            predictor = 0
        encoded[i] = (value - predictor) & 0xFF
    return bytes(encoded)


def png_bytes(width, height, color_type, depth, pixel_fn, *, interlace=0, filter_type=0,
              plte=None, trns=None):
    """Build a real PNG from a per-pixel native-sample function at any legal depth/interlace."""
    channels = _CHANNELS[color_type]
    bpp = max(1, (channels * depth + 7) // 8)
    passes = ((0, 0, 1, 1),) if interlace == 0 else _ADAM7
    raw = bytearray()
    for xstart, ystart, xstep, ystep in passes:
        pass_width = pass_extent(width, xstart, xstep)
        if pass_width == 0:
            continue
        pass_height = pass_extent(height, ystart, ystep)
        row_bytes = (pass_width * channels * depth + 7) // 8
        prev = bytearray(row_bytes)
        for row_index in range(pass_height):
            y = ystart + row_index * ystep
            samples = []
            for column in range(pass_width):
                x = xstart + column * xstep
                samples.extend(pixel_fn(x, y))
            raw_row = pack_samples(samples, depth)
            raw.append(filter_type)
            raw.extend(encode_filter(raw_row, prev, bpp, filter_type))
            prev = raw_row
    return make_png(
        width, height, color_type, depth=depth, interlace=interlace,
        plte=plte, trns=trns, raw=bytes(raw),
    )


def incompressible_canonical_png(width, height, color_type=6):
    """Build a valid prepare-style canonical PNG whose encoded size is ~its raster size.

    Random samples defeat zlib, so the file length can be pushed just past any byte
    threshold by growing the raster. A canonical artifact of this size cannot be produced
    through ``prepare()`` inside the suite (its pure-Python encoder needs minutes at
    multi-megapixel sizes), so the consumer-side byte-cap tests build the canonical
    artifact directly with this helper.
    """
    channels = _CHANNELS[color_type]
    stride = width * channels
    raw = bytearray()
    random_bytes = os.urandom
    for _ in range(height):
        raw.append(0)  # filter type 0 (None): the decoded row is the raster row
        raw.extend(random_bytes(stride))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def native_bgra(width, height, color_type, depth, pixel_fn, *, plte=None, trns=None):
    """Independent reference decode straight from the native sample domain."""
    channels = _CHANNELS[color_type]
    max_value = (1 << depth) - 1
    transparent_gray = None
    transparent_rgb = None
    if trns is not None and color_type == 0:
        (transparent_gray,) = struct.unpack(">H", trns)
    elif trns is not None and color_type == 2:
        transparent_rgb = struct.unpack(">HHH", trns)
    screen = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            native = pixel_fn(x, y)
            destination = (y * width + x) * 4
            if color_type == 0:
                gray = native[0]
                scaled = gray if depth == 8 else (gray >> 8 if depth == 16 else gray * (255 // max_value))
                alpha = 0 if transparent_gray is not None and gray == transparent_gray else 255
                screen[destination:destination + 4] = bytes([scaled, scaled, scaled, alpha])
            elif color_type == 2:
                red, green, blue = native
                red8 = red if depth == 8 else red >> 8
                green8 = green if depth == 8 else green >> 8
                blue8 = blue if depth == 8 else blue >> 8
                alpha = 0 if transparent_rgb is not None and (red, green, blue) == transparent_rgb else 255
                screen[destination:destination + 4] = bytes([blue8, green8, red8, alpha])
            elif color_type == 4:
                gray, alpha = native
                gray8 = gray if depth == 8 else gray >> 8
                alpha8 = alpha if depth == 8 else alpha >> 8
                screen[destination:destination + 4] = bytes([gray8, gray8, gray8, alpha8])
            elif color_type == 6:
                red, green, blue, alpha = native
                if depth == 16:
                    red, green, blue, alpha = red >> 8, green >> 8, blue >> 8, alpha >> 8
                screen[destination:destination + 4] = bytes([blue, green, red, alpha])
            else:
                index = native[0]
                alpha = trns[index] if trns is not None and index < len(trns) else 255
                screen[destination:destination + 4] = bytes([
                    plte[index * 3 + 2], plte[index * 3 + 1], plte[index * 3], alpha,
                ])
    return bytes(screen), width, height


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
    transparent_gray = None
    transparent_rgb = None
    if trns and color_type == 0:
        (transparent_gray,) = struct.unpack(">H", trns)
    elif trns and color_type == 2:
        transparent_rgb = struct.unpack(">HHH", trns)
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
                alpha = 0 if transparent_rgb is not None and (row[sx], row[sx + 1], row[sx + 2]) == transparent_rgb else 255
                screen[dx:dx + 4] = bytes([row[sx + 2], row[sx + 1], row[sx], alpha])
            elif color_type == 4:
                screen[dx:dx + 4] = bytes([row[sx], row[sx], row[sx], row[sx + 1]])
            elif color_type == 0:
                alpha = 0 if transparent_gray is not None and row[sx] == transparent_gray else 255
                screen[dx:dx + 4] = bytes([row[sx], row[sx], row[sx], alpha])
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
        data = make_png(4, 4, 3, raw=raw, plte=plte)
        image = self.assert_ok(data)
        self.assertEqual(image.color_type, 3)
        # A valid indexed PNG must still rasterize to the reviewed BGRA contract.
        self.assertEqual(image.bgra(), reference_bgra(self.module, data))

    def test_rejects_indexed_palette_index_out_of_range(self):
        # A colour-type-3 PNG whose pixel index exceeds its palette is not decodable.
        # load() must reject it exactly like the raster consumer already does, because
        # the logo and privacy consumers trust load()/the CLI only and would otherwise
        # declare an undecodable PNG acceptable and copy it downstream.
        plte = bytes([255, 0, 0])  # one entry -> only palette index 0 is valid
        raw = filtered_rows(2, 2, 1, 0)  # first unfiltered sample is index 5
        self.assert_reject(make_png(2, 2, 3, raw=raw, plte=plte), "palette index")

    def test_rejects_trns_longer_than_palette(self):
        plte = bytes([255, 0, 0])  # one entry
        raw = filtered_rows(2, 2, 1, 0, modulo=1)  # all indices 0 (in range)
        self.assert_reject(
            make_png(2, 2, 3, raw=raw, plte=plte, trns=b"\x00\x00"), "tRNS"
        )
        # A tRNS of exactly the palette length is valid and must stay accepted.
        self.assert_ok(make_png(2, 2, 3, raw=raw, plte=plte, trns=b"\x80"))

    def test_truecolor_trns_makes_matching_pixel_transparent(self):
        # PNG tRNS is legal for truecolor (color type 2): the named RGB sample becomes
        # fully transparent and every other pixel stays opaque. A validator that accepts
        # the file but hard-codes alpha 255 renders the raw PNG transparent in Flutter
        # while the generated exe/tray ICOs come out opaque.
        raw = filtered_rows(4, 4, 3, 0)
        data = make_png(4, 4, 2, raw=raw, trns=struct.pack(">HHH", 5, 18, 31))
        image = self.assert_ok(data)
        bgra = image.bgra()[0]
        # Pixel (0,0) unfilters to RGB(5,18,31) == the tRNS sample -> transparent.
        self.assertEqual(bgra[3], 0)
        # Pixel (1,0) is a different colour -> opaque.
        self.assertEqual(bgra[4 + 3], 255)
        self.assertEqual((bgra, image.width, image.height), reference_bgra(self.module, data))

    def test_grayscale_trns_makes_matching_pixel_transparent(self):
        raw = filtered_rows(4, 4, 1, 0)
        data = make_png(4, 4, 0, raw=raw, trns=struct.pack(">H", 5))
        image = self.assert_ok(data)
        bgra = image.bgra()[0]
        self.assertEqual(bgra[3], 0)   # pixel (0,0) gray == tRNS sample
        self.assertEqual(bgra[4 + 3], 255)
        self.assertEqual((bgra, image.width, image.height), reference_bgra(self.module, data))

    def test_rejects_trns_with_wrong_length_per_color_type(self):
        # Valid tRNS lengths: 2 bytes for grayscale, 6 for truecolor. Anything else is a
        # malformed chunk and must be rejected instead of silently mis-decoded.
        self.assert_reject(make_png(4, 4, 0, trns=b"\x00\x00\x00"), "tRNS")
        self.assert_reject(make_png(4, 4, 2, trns=b"\x00\x00\x00\x00"), "tRNS")
        self.assert_ok(make_png(4, 4, 0, trns=b"\x00\x00"))
        self.assert_ok(make_png(4, 4, 2, trns=b"\x00" * 6))

    def test_rejects_trns_on_alpha_color_types(self):
        # Color types 4 and 6 already carry per-pixel alpha; the spec forbids tRNS there.
        self.assert_reject(make_png(4, 4, 4, trns=b"\x00\x00"), "tRNS")
        self.assert_reject(make_png(4, 4, 6, trns=b"\x00" * 6), "tRNS")

    def test_rejects_plte_after_idat(self):
        # A 1x1 indexed PNG with correct CRCs but PLTE placed after IDAT is accepted by a
        # validator that never enforces chunk order; ImageMagick rejects it.
        plte = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
        idat = _chunk(b"IDAT", zlib.compress(filtered_rows(1, 1, 1, 0, modulo=3)))
        data = (
            PNG_SIGNATURE
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0))
            + idat
            + _chunk(b"PLTE", plte)
            + _chunk(b"IEND", b"")
        )
        self.assert_reject(data, "PLTE")

    def test_rejects_non_consecutive_idat(self):
        # IDAT chunks must be consecutive; an ancillary chunk between two IDATs makes the
        # stream undecodable.
        plte = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
        compressed = zlib.compress(filtered_rows(2, 2, 1, 0, modulo=3))
        half = len(compressed) // 2
        data = (
            PNG_SIGNATURE
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 3, 0, 0, 0))
            + _chunk(b"PLTE", plte)
            + _chunk(b"IDAT", compressed[:half])
            + _chunk(b"tEXt", b"k\x00v")
            + _chunk(b"IDAT", compressed[half:])
            + _chunk(b"IEND", b"")
        )
        self.assert_reject(data, "consecutive")

    def test_rejects_plte_only_where_the_spec_forbids_it(self):
        # PNG 1.2 4.1.2: PLTE must not appear for grayscale (0) or grayscale+alpha (4).
        # Only those two are rejected; 2/6 may carry a suggested palette (see the
        # acceptance test) and 3 requires one.
        plte = bytes([255, 0, 0])
        for color_type in (0, 4):
            with self.subTest(color_type=color_type):
                self.assert_reject(make_png(4, 4, color_type, plte=plte), "PLTE")

    def test_accepts_suggested_plte_on_truecolor_types(self):
        # PNG 1.2 4.1.2 permits PLTE on truecolour (2) and truecolour+alpha (6) as a
        # *suggested* palette. It must be accepted there, and it must not change the
        # decoded raster: the same image with and without the suggested palette must
        # produce byte-identical BGRA (the suggested palette is ignored for 2/6).
        plte = bytes([10, 20, 30, 40, 50, 60, 70, 80, 90])
        for color_type in (2, 6):
            with self.subTest(color_type=color_type):
                plain = self.assert_ok(make_png(6, 5, color_type, 0))
                with_plte = self.assert_ok(make_png(6, 5, color_type, 0, plte=plte))
                self.assertEqual(with_plte.bgra(), plain.bgra())
                self.assertEqual(
                    with_plte.bgra(),
                    reference_bgra(self.module, make_png(6, 5, color_type, 0, plte=plte)),
                )
        # A second PLTE, or a PLTE placed after the first IDAT, is still rejected.
        dup = (
            PNG_SIGNATURE
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + _chunk(b"PLTE", plte)
            + _chunk(b"PLTE", plte)
            + _chunk(b"IDAT", zlib.compress(filtered_rows(2, 2, 3, 0)))
            + _chunk(b"IEND", b"")
        )
        self.assert_reject(dup, "PLTE")
        late = (
            PNG_SIGNATURE
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(filtered_rows(2, 2, 3, 0)))
            + _chunk(b"PLTE", plte)
            + _chunk(b"IEND", b"")
        )
        self.assert_reject(late, "PLTE")

    def test_rejects_duplicate_plte_and_trns(self):
        plte = bytes([255, 0, 0])
        ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0))
        idat = _chunk(b"IDAT", zlib.compress(filtered_rows(1, 1, 1, 0, modulo=1)))
        dup_plte = (
            PNG_SIGNATURE + ihdr + _chunk(b"PLTE", plte) + _chunk(b"PLTE", plte)
            + idat + _chunk(b"IEND", b"")
        )
        self.assert_reject(dup_plte, "PLTE")
        dup_trns = (
            PNG_SIGNATURE + ihdr + _chunk(b"PLTE", plte) + _chunk(b"tRNS", b"\x80")
            + _chunk(b"tRNS", b"\x80") + idat + _chunk(b"IEND", b"")
        )
        self.assert_reject(dup_trns, "tRNS")

    def test_rejects_trns_after_idat(self):
        plte = bytes([255, 0, 0])
        ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0))
        idat = _chunk(b"IDAT", zlib.compress(filtered_rows(1, 1, 1, 0, modulo=1)))
        data = (
            PNG_SIGNATURE + ihdr + _chunk(b"PLTE", plte) + idat
            + _chunk(b"tRNS", b"\x80") + _chunk(b"IEND", b"")
        )
        self.assert_reject(data, "tRNS")

    def test_rejects_indexed_trns_before_plte(self):
        # PNG 1.2 4.1.2 / 4.3.2: for indexed colour (3) tRNS decorates the palette, so it
        # must appear *after* PLTE and before the first IDAT. A stream that emits tRNS
        # before PLTE cannot reference a palette entry and is undecodable.
        plte = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
        ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 3, 0, 0, 0))
        idat = _chunk(b"IDAT", zlib.compress(filtered_rows(2, 2, 1, 0, modulo=3)))
        before = (
            PNG_SIGNATURE + ihdr + _chunk(b"tRNS", b"\x80") + _chunk(b"PLTE", plte)
            + idat + _chunk(b"IEND", b"")
        )
        self.assert_reject(before, "tRNS")
        # The spec ordering (PLTE, then tRNS, then IDAT) stays accepted.
        ordered = (
            PNG_SIGNATURE + ihdr + _chunk(b"PLTE", plte) + _chunk(b"tRNS", b"\x80")
            + idat + _chunk(b"IEND", b"")
        )
        self.assert_ok(ordered)

    def test_rejects_non_letter_chunk_type(self):
        # A chunk type is exactly four ASCII letters A-Z / a-z (PNG 1.2 5.3). A type byte
        # outside that set is a malformed chunk, not an ignorable ancillary chunk: the
        # old `chunk_type[0:1].isupper()` test treated e.g. b"\x00ZYX" as ancillary and
        # silently skipped it, so an unknown or mangled chunk could pass unnoticed.
        ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0))
        idat = _chunk(b"IDAT", zlib.compress(filtered_rows(8, 8, 4, 0)))
        for bad_type in (b"\x00ZYX", b"ab c", b"a1ct", b"1234"):
            with self.subTest(chunk_type=bad_type):
                data = PNG_SIGNATURE + ihdr + _chunk(bad_type, b"") + idat + _chunk(b"IEND", b"")
                self.assert_reject(data, "malformed chunk type")

    def test_rejects_unknown_critical_chunk(self):
        # An unknown chunk whose type starts uppercase is a critical chunk: a decoder
        # cannot safely ignore it and must reject the image.
        ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0))
        idat = _chunk(b"IDAT", zlib.compress(filtered_rows(8, 8, 4, 0)))
        data = PNG_SIGNATURE + ihdr + _chunk(b"XXYZ", b"") + idat + _chunk(b"IEND", b"")
        self.assert_reject(data, "critical")

    def test_accepts_known_ancillary_chunk(self):
        # Unknown *ancillary* chunks (lowercase first byte) are ignorable per spec and
        # must stay accepted, so the critical-chunk rule does not over-reject.
        ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0))
        idat = _chunk(b"IDAT", zlib.compress(filtered_rows(8, 8, 4, 0)))
        data = PNG_SIGNATURE + ihdr + _chunk(b"tEXt", b"Comment\x00hi") + idat + _chunk(b"IEND", b"")
        image = self.assert_ok(data)
        self.assertEqual(image.bgra(), reference_bgra(self.module, data))

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

    def test_rejects_unsupported_method_flags_and_interlace_method(self):
        # compression and filter method flags are pinned to 0; the only interlace methods are
        # 0 (none) and 1 (Adam7).
        self.assert_reject(make_png(compression=1), "compression/filter method")
        self.assert_reject(make_png(filter_method=1), "compression/filter method")
        self.assert_reject(make_png(interlace=2), "interlace method")

    def test_rejects_bit_depth_illegal_for_color_type(self):
        # Every legal depth is accepted, but a depth the spec forbids for the colour type
        # stays rejected: 1/2/4-bit truecolour and alpha types, 16-bit indexed colour, and
        # 3/5-bit greyscale are all outside the PNG spec.
        for color_type, depth in (
            (2, 1), (2, 4), (6, 4), (4, 4), (3, 16), (0, 3), (0, 5),
        ):
            with self.subTest(color_type=color_type, depth=depth):
                self.assert_reject(make_png(4, 4, color_type, depth=depth), "bit depth")

    def test_rejects_out_of_range_dimensions_and_non_square_icon(self):
        # The per-field dimension window and the square-icon rule are enforced by load()
        # itself, so a value outside them still fails closed.
        self.assert_reject(
            make_png(33, 16, 6, 0), "out-of-range dimensions", max_width=32, max_height=32
        )
        self.assert_ok(make_png(32, 32, 6, 0), max_width=32, max_height=32)
        self.assert_reject(
            make_png(16, 24, 6, 0), "must be square", require_square=True,
        )

    def test_rejects_payload_after_the_zlib_end_marker(self):
        # The decompressed scanline count can be exactly right while the compressed stream
        # carries bytes after the zlib end marker; those must be rejected, not ignored.
        raw = filtered_rows(4, 4, 4, 0)
        trailing = make_png(4, 4, 6, 0, idat_zlib=zlib.compress(raw) + b"TRAILING")
        self.assert_reject(trailing, "trailing data")

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

    def test_accepts_every_legal_bit_depth_and_matches_native_reference(self):
        # Every bit depth the PNG spec permits for a colour type must decode, and the
        # canonical 8-bit BGRA must equal an independent native-domain decode.
        cases = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}

        def make_fn(color_type, depth):
            max_value = (1 << depth) - 1
            if color_type == 0:
                return lambda x, y: ((x * 7 + y * 5) % (max_value + 1),)
            if color_type == 3:
                return lambda x, y: ((x * 3 + y * 5) % (1 << depth),)
            if color_type == 2:
                return lambda x, y: (
                    (x * 13 + 1) % (max_value + 1),
                    (x * 7 + y * 3) % (max_value + 1),
                    (x * 5 + y * 11) % (max_value + 1),
                )
            if color_type == 4:
                return lambda x, y: (
                    (x * 7 + y * 5) % (max_value + 1),
                    (x * 3 + y * 2) % (max_value + 1),
                )
            return lambda x, y: (
                (x * 13 + 1) % (max_value + 1),
                (x * 7 + y * 3) % (max_value + 1),
                (x * 5 + y * 11) % (max_value + 1),
                (x * 3 + y * 2) % (max_value + 1),
            )

        for color_type, depths in cases.items():
            for depth in depths:
                with self.subTest(color_type=color_type, depth=depth):
                    pixel_fn = make_fn(color_type, depth)
                    plte = None
                    if color_type == 3:
                        plte = bytes((i * 9 + 3) % 256 for i in range((1 << depth) * 3))
                    data = png_bytes(7, 5, color_type, depth, pixel_fn, plte=plte)
                    image = self.assert_ok(data)
                    self.assertEqual(
                        image.bgra(),
                        native_bgra(7, 5, color_type, depth, pixel_fn, plte=plte),
                    )

    def test_16bit_samples_keep_their_most_significant_byte(self):
        # 16-bit samples decode to the 8-bit output by their most significant byte, the rule
        # libpng's strip_16 uses for the ICO raster (not a full-range rescale).
        def rgb_fn(x, y):
            return [(0x1234, 0x5678, 0x9ABC), (0xFF00, 0x00FF, 0x8000)][x]

        bgra, width, height = self.assert_ok(png_bytes(2, 1, 2, 16, rgb_fn)).bgra()
        self.assertEqual((width, height), (2, 1))
        self.assertEqual(bgra, bytes([0x9A, 0x56, 0x12, 0xFF, 0x80, 0x00, 0xFF, 0xFF]))
        self.assertEqual((bgra, width, height), native_bgra(2, 1, 2, 16, rgb_fn))

        def rgba_fn(x, y):
            return [(0x0102, 0x0304, 0x0506, 0x0708), (0xFFFE, 0xFDEC, 0xFBD8, 0xFA00)][x]

        bgra, width, height = self.assert_ok(png_bytes(2, 1, 6, 16, rgba_fn)).bgra()
        self.assertEqual(bgra, bytes([0x05, 0x03, 0x01, 0x07, 0xFB, 0xFD, 0xFF, 0xFA]))
        self.assertEqual((bgra, width, height), native_bgra(2, 1, 6, 16, rgba_fn))

    def test_sub_byte_grayscale_scales_to_the_full_8bit_range(self):
        cases = {
            1: ([0, 1], [0, 255]),
            2: ([0, 1, 2, 3], [0, 85, 170, 255]),
            4: ([0, 1, 5, 15], [0, 17, 85, 255]),
            16: ([0x0000, 0x1234, 0xFFFF], [0x00, 0x12, 0xFF]),
        }
        for depth, (samples, expected) in cases.items():
            with self.subTest(depth=depth):
                def pixel_fn(x, y, samples=samples):
                    return (samples[x],)
                bgra, width, height = self.assert_ok(
                    png_bytes(len(samples), 1, 0, depth, pixel_fn)
                ).bgra()
                self.assertEqual([bgra[i * 4] for i in range(len(samples))], expected, depth)
                self.assertEqual([bgra[i * 4 + 3] for i in range(len(samples))], [255] * len(samples))
                self.assertEqual(
                    (bgra, width, height), native_bgra(len(samples), 1, 0, depth, pixel_fn)
                )

    def test_sub_byte_indexed_uses_unscaled_palette_indices(self):
        # Indexed samples are palette indices, not intensities: 1/2/4-bit values must NOT be
        # scaled by 255/(2^depth-1) before the palette lookup.
        for depth in (1, 2, 4):
            with self.subTest(depth=depth):
                entries = 1 << depth
                plte = bytes((i * 11 + 1) % 256 for i in range(entries * 3))

                def pixel_fn(x, y, entries=entries):
                    return ((x + y) % entries,)

                bgra, width, height = self.assert_ok(
                    png_bytes(entries, 1, 3, depth, pixel_fn, plte=plte)
                ).bgra()
                self.assertEqual(
                    (bgra, width, height),
                    native_bgra(entries, 1, 3, depth, pixel_fn, plte=plte),
                )
                # Pixel (1, 0) is palette index 1; its BGR bytes come straight from the palette.
                self.assertEqual(bgra[4:7], bytes([plte[5], plte[4], plte[3]]))

    def test_adam7_interlaced_decodes_identically_to_noninterlaced(self):
        # The same logical image encoded interlaced and non-interlaced must decode to the same
        # BGRA, for a non-indexed (incl. 16-bit) and an indexed image, across sizes that
        # exercise empty and partial Adam7 passes.
        def rgba_fn(x, y):
            return (
                (x * 13 + y * 7 + 1) % 256,
                (x * 3 + y * 11 + 2) % 256,
                (x * 5 + y * 2 + 3) % 256,
                (x * 9 + y * 4 + 5) % 256,
            )

        def rgba16_fn(x, y):
            return tuple(value * 257 % 65536 for value in rgba_fn(x, y))

        for color_type, depth, pixel_fn in (
            (6, 8, rgba_fn),
            (6, 16, rgba16_fn),
        ):
            for width, height in ((1, 7), (5, 1), (13, 9), (8, 8)):
                with self.subTest(color_type=color_type, size=(width, height)):
                    plain = png_bytes(width, height, color_type, depth, pixel_fn, interlace=0)
                    interlaced = png_bytes(width, height, color_type, depth, pixel_fn, interlace=1)
                    self.assertEqual(
                        self.assert_ok(interlaced).bgra(),
                        self.assert_ok(plain).bgra(),
                        (width, height),
                    )
                    self.assertEqual(
                        self.assert_ok(interlaced).bgra(),
                        native_bgra(width, height, color_type, depth, pixel_fn),
                    )

        entries = 16
        plte = bytes((i * 7 + 2) % 256 for i in range(entries * 3))

        def index_fn(x, y):
            return ((x * 3 + y) % entries,)

        for width, height in ((1, 9), (6, 5), (11, 4)):
            with self.subTest(color_type=3, size=(width, height)):
                plain = png_bytes(width, height, 3, 4, index_fn, interlace=0, plte=plte)
                interlaced = png_bytes(width, height, 3, 4, index_fn, interlace=1, plte=plte)
                self.assertEqual(
                    self.assert_ok(interlaced).bgra(),
                    self.assert_ok(plain).bgra(),
                    (width, height),
                )

    def test_16bit_trns_is_compared_at_full_sample_width(self):
        # A 16-bit tRNS sample is compared against the full 16-bit pixel value, not its most
        # significant byte: two pixels sharing every high byte but differing in the low bytes
        # are not both transparent.
        trns = struct.pack(">HHH", 0x1234, 0x5678, 0x9ABC)

        def truecolor_fn(x, y):
            return [(0x1234, 0x5678, 0x9ABC), (0x12FF, 0x5678, 0x9ABC)][x]

        bgra, width, height = self.assert_ok(
            png_bytes(2, 1, 2, 16, truecolor_fn, trns=trns)
        ).bgra()
        self.assertEqual(bgra[3], 0)        # exact 16-bit match -> transparent
        self.assertEqual(bgra[4 + 3], 255)  # low-byte difference -> opaque
        self.assertEqual((bgra, width, height), native_bgra(2, 1, 2, 16, truecolor_fn, trns=trns))

        gray_trns = struct.pack(">H", 0xABCD)

        def gray_fn(x, y):
            return ([0xABCD, 0xAB00][x],)

        bgra, width, height = self.assert_ok(
            png_bytes(2, 1, 0, 16, gray_fn, trns=gray_trns)
        ).bgra()
        self.assertEqual(bgra[3], 0)
        self.assertEqual(bgra[4 + 3], 255)
        self.assertEqual((bgra, width, height), native_bgra(2, 1, 0, 16, gray_fn, trns=gray_trns))

    def test_legacy_8bit_noninterlaced_output_is_byte_identical(self):
        # BGRA digests captured from the pre-change decoder. Widening the accepted input
        # domain must not alter a single output byte for an input the old validator accepted.
        plte = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
        fixtures = {
            "rgba-f0": make_png(12, 7, 6, 0),
            "rgba-f4": make_png(12, 7, 6, 4),
            "grayscale-f2": make_png(9, 5, 0, 2),
            "grayalpha-f3": make_png(9, 5, 4, 3),
            "truecolor-trns": make_png(4, 4, 2, 0, trns=struct.pack(">HHH", 5, 18, 31)),
            "grayscale-trns": make_png(4, 4, 0, 0, trns=struct.pack(">H", 5)),
            "indexed8": make_png(4, 4, 3, 0, raw=filtered_rows(4, 4, 1, 0, modulo=3), plte=plte),
            "indexed8-trns": make_png(4, 4, 3, 0, raw=filtered_rows(4, 4, 1, 0, modulo=3), plte=plte, trns=b"\x80\x40\x20"),
            "suggested-plte": make_png(6, 5, 2, 0, plte=bytes([10, 20, 30, 40, 50, 60, 70, 80, 90])),
        }
        expected = {
            "rgba-f0": "0ae98ea9beb39d98681d3c389eb5c2dca414bd63af1ce1017d6e0d9c1d71b316",
            "rgba-f4": "0ae98ea9beb39d98681d3c389eb5c2dca414bd63af1ce1017d6e0d9c1d71b316",
            "grayscale-f2": "90bfe10eefdcbd8f0311dd8009eda1fec98e97ea7faf9115eb2b7cdadbdfac33",
            "grayalpha-f3": "b24673a2677288167caedf0e4618a28453d5d791c2241b3a0209bc61bfdaab6a",
            "truecolor-trns": "64b47ace73fc2c889dd92b554f0d0e5375e80b7d226f53d376b2c92ff05dd12c",
            "grayscale-trns": "93dd0609b1cfbc5ba59418ee706d2b5a1b6ef7a896cf2bde40ff7ffa93dea5c7",
            "indexed8": "68b76511025879c85afadac4d1914bcac0c5b71c8ba2083a192ed0e3cb384d6a",
            "indexed8-trns": "90a5e0558fd3d318d80b10006539230f4e70661b6277c126425f7eb60f886f3f",
            "suggested-plte": "fa25bbcc684534a2e7f92bd3d70c4de444997ea444c55a7c8ace7d190430e2b8",
        }
        for name, data in fixtures.items():
            with self.subTest(name=name):
                bgra, _, _ = self.assert_ok(data).bgra()
                self.assertEqual(hashlib.sha256(bgra).hexdigest(), expected[name], name)

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
# canonical re-encode (download -> decode -> encode -> re-validate -> manifest)
# --------------------------------------------------------------------------- #

class CanonicalEncodeTests(unittest.TestCase):
    """prepare_branding_assets must hand consumers a canonical PNG, not the raw download."""

    def setUp(self):
        self.module = _ValidatorModule.load()

    def _stage(self, name, data):
        staging = Path(tempfile.mkdtemp(prefix="branding-canonical-"))
        (staging / name).write_bytes(data)
        result = run_validator_module([str(staging)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads((staging / "branding_manifest.json").read_text())
        return staging, (staging / name).read_bytes(), manifest["images"][name]

    def test_canonical_output_has_only_ihdr_idat_iend(self):
        _, canonical, _ = self._stage("app-logo.png", make_png(16, 16, 6, 0))
        self.assertEqual([t for t, _ in png_chunks(canonical)], [b"IHDR", b"IDAT", b"IEND"])

    def test_interlaced_input_is_accepted_and_emitted_non_interlaced(self):
        def rgba_fn(x, y):
            return (
                (x * 13 + y * 7 + 1) % 256,
                (x * 3 + y * 11 + 2) % 256,
                (x * 5 + y * 2 + 3) % 256,
                (x * 9 + y * 4 + 5) % 256,
            )

        interlaced = png_bytes(13, 9, 6, 8, rgba_fn, interlace=1)
        self.assertEqual(png_header(interlaced)[6], 1)
        _, canonical, entry = self._stage("app-logo.png", interlaced)
        width, height, depth, color_type, _c, _f, interlace = png_header(canonical)
        self.assertEqual((width, height, depth, color_type, interlace), (13, 9, 8, 6, 0))
        self.assertEqual((entry["width"], entry["height"]), (13, 9))
        self.assertEqual(
            self.module.load(canonical, "app-logo.png").bgra(),
            native_bgra(13, 9, 6, 8, rgba_fn),
        )

    def test_16bit_input_is_accepted_and_emitted_canonical_8bit(self):
        def rgb16_fn(x, y):
            return (
                (x * 4099 + 0x1234) % 65536,
                (x * 2053 + y * 7 + 0x5678) % 65536,
                (x * 1031 + y * 11 + 0x9ABC) % 65536,
            )

        source = png_bytes(5, 3, 2, 16, rgb16_fn)
        _, canonical, _ = self._stage("app-logo.png", source)
        width, height, depth, color_type, _c, _f, interlace = png_header(canonical)
        self.assertEqual((width, height, depth, color_type, interlace), (5, 3, 8, 2, 0))
        self.assertEqual(
            self.module.load(canonical, "app-logo.png").bgra(),
            native_bgra(5, 3, 2, 16, rgb16_fn),
        )

    def test_16bit_alpha_input_is_emitted_canonical_8bit_rgba(self):
        def rgba16_fn(x, y):
            return (
                (x * 4099 + 0x1234) % 65536,
                (x * 2053 + y * 7 + 0x5678) % 65536,
                (x * 1031 + y * 11 + 0x9ABC) % 65536,
                (x * 97 + y * 13) % 65536,
            )

        source = png_bytes(5, 3, 6, 16, rgba16_fn)
        _, canonical, _ = self._stage("app-logo.png", source)
        width, height, depth, color_type, _c, _f, interlace = png_header(canonical)
        self.assertEqual((width, height, depth, color_type, interlace), (5, 3, 8, 6, 0))
        self.assertEqual(
            self.module.load(canonical, "app-logo.png").bgra(),
            native_bgra(5, 3, 6, 16, rgba16_fn),
        )

    def test_alpha_and_dimensions_are_preserved(self):
        def rgba_fn(x, y):
            return (200, 100, 50, 0 if (x, y) == (3, 4) else 255)

        source = png_bytes(8, 7, 6, 8, rgba_fn)
        _, canonical, entry = self._stage("app-logo.png", source)
        width, height, depth, color_type, _c, _f, interlace = png_header(canonical)
        self.assertEqual((width, height, depth, color_type, interlace), (8, 7, 8, 6, 0))
        self.assertEqual((entry["width"], entry["height"]), (8, 7))
        bgra = self.module.load(canonical, "app-logo.png").bgra()[0]
        self.assertEqual(bgra[(4 * 8 + 3) * 4 + 3], 0)   # the transparent pixel stays transparent
        self.assertEqual(bgra[3], 255)                  # an opaque pixel stays opaque
        self.assertEqual(bgra, native_bgra(8, 7, 6, 8, rgba_fn)[0])

    def test_trns_alpha_is_preserved_as_rgba(self):
        source = make_png(4, 4, 2, 0, trns=struct.pack(">HHH", 5, 18, 31))
        _, canonical, _ = self._stage("app-logo.png", source)
        width, height, depth, color_type, _c, _f, interlace = png_header(canonical)
        self.assertEqual((width, height, depth, color_type, interlace), (4, 4, 8, 6, 0))
        self.assertEqual(
            self.module.load(canonical, "app-logo.png").bgra(),
            reference_bgra(self.module, source),
        )

    def test_fully_opaque_truecolor_stays_rgb(self):
        _, canonical, _ = self._stage("app-logo.png", make_png(6, 5, 2, 0))
        self.assertEqual(png_header(canonical)[3], 2)

    def test_already_canonical_png_reencodes_idempotently(self):
        indexed_plte = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
        for label, source in (
            ("rgba", make_png(9, 5, 6, 3)),
            ("rgb", make_png(9, 5, 2, 2)),
            ("indexed", make_png(4, 4, 3, 0, raw=filtered_rows(4, 4, 1, 0, modulo=3), plte=indexed_plte)),
        ):
            with self.subTest(label=label):
                _, once, _ = self._stage("app-logo.png", source)
                _, twice, _ = self._stage("app-logo.png", once)
                self.assertEqual(once, twice)
                self.assertEqual(self.module.encode(self.module.load(once, "app-logo.png")), once)

    def test_corrupt_input_is_rejected_and_staging_left_untouched(self):
        for label, data in (
            ("truncated", make_png(omit_iend=True)),
            ("bad-crc", make_png(corrupt_idat_crc=True)),
            ("after-iend", make_png(trailing_after_iend=b"JUNKJUNK")),
            ("trailing", make_png(8, 8, 6, raw=filtered_rows(8, 8, 4, 0) + b"\xAA" * 4096)),
        ):
            with self.subTest(label=label):
                staging = Path(tempfile.mkdtemp(prefix="branding-canonical-bad-"))
                (staging / "app-logo.png").write_bytes(data)
                result = run_validator_module([str(staging)])
                self.assertEqual(result.returncode, 1, label)
                self.assertIn("::error::", result.stdout, label)
                self.assertFalse((staging / "branding_manifest.json").exists(), label)
                self.assertEqual((staging / "app-logo.png").read_bytes(), data, label)

    def test_field_byte_cap_is_still_enforced(self):
        staging = Path(tempfile.mkdtemp(prefix="branding-canonical-cap-"))
        (staging / "app-icon.png").write_bytes(b"\x00" * (10 * 1024 * 1024 + 1))  # cap is 10 MiB
        result = run_validator_module([str(staging)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("exceeds the byte cap", result.stdout)
        self.assertFalse((staging / "branding_manifest.json").exists())

    def test_download_cap_is_not_reapplied_to_the_canonical_reencode(self):
        # The per-field cap bounds the downloaded response, not the locally generated
        # canonical file.  An indexed (color type 3) source stores one byte per pixel but
        # re-encodes to RGB, so a noisy source whose canonical bytes are larger than the cap
        # is a valid in-domain image that must still be accepted and written.  Exercise the
        # ordering rule with a small cap: the rule is a parameter of the validator, so this
        # does not need a multi-megapixel fixture (the same defect is reached by a real
        # 4096x4096 indexed source against the 10 MiB cap).
        original = self.module._STAGING_RULES["app-logo.png"]
        self.addCleanup(self.module._STAGING_RULES.__setitem__, "app-logo.png", original)

        plte = bytes(range(0, 48))  # 16 colors
        rng = random.Random(0x5EED)
        raw = bytearray()
        for _ in range(128):
            raw.append(0)
            raw.extend(rng.randrange(16) for _ in range(128))
        source = make_png(128, 128, 3, 0, raw=bytes(raw), plte=plte)

        image = self.module.load(source, "app-logo.png")
        canonical = self.module.encode(image)
        cap = (len(source) + len(canonical)) // 2
        self.assertLess(len(source), cap)
        self.assertGreater(len(canonical), cap)
        self.module._STAGING_RULES["app-logo.png"] = (4096, 4096, cap, False, "")

        staging = Path(tempfile.mkdtemp(prefix="branding-canonical-download-cap-"))
        (staging / "app-logo.png").write_bytes(source)
        rc = self.module.prepare(staging)
        self.assertEqual(rc, 0)
        manifest = json.loads((staging / "branding_manifest.json").read_text())
        staged = (staging / "app-logo.png").read_bytes()
        self.assertEqual(staged, canonical)
        entry = manifest["images"]["app-logo.png"]
        self.assertEqual(entry["sha256"], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(entry["bytes"], len(canonical))

    def test_geometry_derived_bound_covers_the_canonical_reencode(self):
        # canonical_byte_bound() must be a true upper bound on encode() for every accepted
        # image, so a valid in-domain input can never be rejected by the re-validation bound.
        indexed_plte = bytes(range(0, 48))
        indexed_raw = bytearray()
        rng = random.Random(0xB0A7)
        for _ in range(64):
            indexed_raw.append(0)
            indexed_raw.extend(rng.randrange(16) for _ in range(64))
        sources = (
            make_png(1, 1, 2, 0),
            make_png(16, 16, 6, 0),
            make_png(64, 64, 2, 0),
            make_png(4, 4, 0, 0, trns=struct.pack(">H", 5)),                  # gray + tRNS -> RGBA
            make_png(64, 64, 3, 0, raw=bytes(indexed_raw), plte=indexed_plte),
            png_bytes(37, 19, 6, 16, lambda x, y: (x * 257, y * 131, x ^ y, 65535)),  # 16-bit RGBA
        )
        for index, source in enumerate(sources):
            with self.subTest(case=index):
                image = self.module.load(source, "app-logo.png")
                canonical = self.module.encode(image)
                self.assertLessEqual(
                    len(canonical), self.module.canonical_byte_bound(image)
                )

    def test_load_byte_cap_boundary(self):
        data = make_png(16, 16, 6, 0)
        with self.assertRaises(self.module.PngError):
            self.module.load(data, "x.png", max_bytes=len(data) - 1)
        self.module.load(data, "x.png", max_bytes=len(data))


# --------------------------------------------------------------------------- #
# F3 + F6: destination-pinned fetch boundary
# --------------------------------------------------------------------------- #

# The local HTTP fixture runs on the machine's loopback interface. The allowed
# hops in these tests connect to the fixture at 127.0.0.1; the production policy
# refuses every loopback address, so tests that need an allowed hop inject a
# policy that additionally permits exactly FIXTURE_ADDRESS and defers every
# other address (including the 127.0.0.2 redirect target) to the production
# policy. The connection, redirect, streaming and cap mechanics are real: real
# sockets, a real HTTP fixture, real chunked and Content-Length bodies, and the
# shipped helper's real fetch entry point. Only the address-policy layer is
# simulated for the allowed hops.
FIXTURE_ADDRESS = "127.0.0.1"
REFERENCE_PNG = make_png(16, 16, 6, 0)


class _FetchServer(socketserver.ThreadingTCPServer):
    """Threaded HTTP fixture that counts accepted connections and requests."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.lock = threading.Lock()
        self.connections = 0
        self.requests = 0
        self.redirect_ok = ""
        self.redirect_forbidden = ""
        self.redirect_ftp = ""
        self.redirect_loop = ""
        self.redirect_port = ""
        self.host_headers = []
        self.stream_done = threading.Event()
        self.stream_completed = False
        self.stream_bytes = 0
        self.trickle_done = threading.Event()
        self.trickle_steps = 0

    def get_request(self):
        conn, addr = super().get_request()
        with self.lock:
            self.connections += 1
        return conn, addr

    def note_request(self):
        with self.lock:
            self.requests += 1

    def note_host_header(self, value):
        with self.lock:
            self.host_headers.append(value)

    def snapshot(self):
        with self.lock:
            return (self.connections, self.requests)

    def note_stream(self, written, completed):
        with self.lock:
            self.stream_bytes = written
            self.stream_completed = completed
        self.stream_done.set()

    def note_trickle(self, steps):
        with self.lock:
            self.trickle_steps = steps
        self.trickle_done.set()


class _BrandingFetchHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    CHUNK = 65536
    SMALL = 1024
    HUGE = 64 * 1024 * 1024
    TRICKLE_DELAY = 0.2
    TRICKLE_LIMIT = 40  # ~8 s of dribbled bytes

    def log_message(self, *args):
        pass

    def _respond(self, body, status=200, headers=()):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _redirect(self, location):
        self._respond(b"", status=302, headers=(("Location", location),))

    def _trickle_bytes(self, preamble):
        # Deliver the preamble and then one byte per TRICKLE_DELAY, each well
        # inside any per-read socket timeout, so only a wall-clock deadline can
        # end the hop.  Bounded, and the delivered step count is recorded, so the
        # tests can prove the client cut the hop off instead of hanging the suite.
        steps = 0
        try:
            self.wfile.write(preamble)
            for _ in range(self.TRICKLE_LIMIT):
                time.sleep(self.TRICKLE_DELAY)
                self.wfile.write(b"x")
                steps += 1
        except OSError:
            pass
        finally:
            self.server.note_trickle(steps)

    def _trickle_interim(self):
        # A stream of interim 1xx responses: on interpreters without an interim
        # limit, only a wall-clock deadline can end this hop.  The delivered
        # response count is recorded for the same cut-off proof as above.
        steps = 0
        try:
            for _ in range(self.TRICKLE_LIMIT):
                self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                steps += 1
                time.sleep(self.TRICKLE_DELAY)
        except OSError:
            pass
        finally:
            self.server.note_trickle(steps)

    def _stream(self, total, chunked):
        # A body far larger than any per-field cap. The chunked variant declares no
        # Content-Length (the transfer can only be stopped by a client-side bound);
        # the Content-Length variant covers the declared-length shape.
        self.send_response(200)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(total))
        self.end_headers()
        block = b"A" * self.CHUNK
        written = 0
        try:
            while written < total:
                payload = block[: min(len(block), total - written)]
                if chunked:
                    self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
                else:
                    self.wfile.write(payload)
                written += len(payload)
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass
        finally:
            self.server.note_stream(written, written >= total)

    def do_GET(self):
        self.server.note_request()
        self.server.note_host_header(self.headers.get("Host") or "")
        path = self.path.split("?", 1)[0]
        if path == "/image.png":
            self._respond(REFERENCE_PNG)
        elif path.startswith("/encoded/"):
            # Percent-encoded ASCII request targets must stay fetchable: the URL
            # carries no raw non-ASCII byte, only the escape sequences.
            self._respond(REFERENCE_PNG)
        elif path == "/chunked-small":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            body = b"C" * self.SMALL
            self.wfile.write(b"%x\r\n" % len(body) + body + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        elif path == "/big-cl":
            self._stream(self.HUGE, chunked=False)
        elif path == "/huge":
            self._stream(self.HUGE, chunked=True)
        elif path == "/redirect-ok":
            self._redirect(self.server.redirect_ok)
        elif path == "/redirect-forbidden":
            self._redirect(self.server.redirect_forbidden)
        elif path == "/redirect-ftp":
            self._redirect(self.server.redirect_ftp)
        elif path == "/redirect-port":
            self._redirect(self.server.redirect_port)
        elif path == "/loop":
            self._redirect(self.server.redirect_loop)
        elif path == "/trickle-status":
            self._trickle_bytes(b"")
        elif path == "/trickle-headers":
            self._trickle_bytes(b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\nX-Trickle: ")
        elif path == "/trickle-interim":
            self._trickle_interim()
        elif path == "/short-body":
            # Declares 4096 bytes, sends 10 and closes: the rest never arrives.
            self.send_response(200)
            self.send_header("Content-Length", "4096")
            self.end_headers()
            self.wfile.write(b"P" * 10)
            self.close_connection = True
        elif path == "/truncated-chunked":
            # Declares a 64-byte chunk, sends 16 bytes and closes without the
            # terminating zero-length chunk.
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"40\r\n" + b"Q" * 16)
            self.close_connection = True
        else:
            self._respond(b"", status=404)


class _TlsHandshakeFixture(socketserver.ThreadingTCPServer):
    """Plain-TCP fixture that accepts a connection and never completes TLS.

    The TLS handshake happens before certificate verification, so a fixture
    with no certificate is enough: ``stall`` reads and never answers, ``drip``
    sends a TLS record header declaring a 16 KiB body and then one body byte
    per ``DRIP_STEP`` (well inside any per-read timeout, so only a wall-clock
    deadline ends the hop).
    """

    allow_reuse_address = True
    daemon_threads = True
    DRIP_STEP = 0.2
    DRIP_LIMIT = 30  # ~6 s of dribbled TLS record bytes

    def __init__(self, address, mode):
        super().__init__(address, _TlsHandshakeHandler)
        self.mode = mode
        self.lock = threading.Lock()
        self.connections = 0
        self.client_closed = threading.Event()

    def get_request(self):
        conn, addr = super().get_request()
        with self.lock:
            self.connections += 1
        return conn, addr

    def snapshot(self):
        with self.lock:
            return self.connections

    def note_client_closed(self):
        self.client_closed.set()


class _TlsHandshakeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            if self.server.mode == "drip":
                self.request.sendall(b"\x16\x03\x03\x40\x00")
                try:
                    for _ in range(self.server.DRIP_LIMIT):
                        time.sleep(self.server.DRIP_STEP)
                        self.request.sendall(b"z")
                except OSError:
                    pass
            else:
                while self.request.recv(4096):
                    pass
        except OSError:
            pass
        finally:
            self.server.note_client_closed()


HAS_OPENSSL = shutil.which("openssl") is not None
_TLS_CERTIFICATES = {}
_TLS_CERTIFICATES_LOCK = threading.Lock()


def tls_fixture_certificate(ip):
    """Self-signed cert/key for one IP SAN, minted once per process.

    Needs an OpenSSL CLI new enough for ``-addext`` (1.1.1+).  The certificate
    is only a fixture: the fetch under test injects the trust anchor, so this
    changes trust, never verification or SNI.
    """
    with _TLS_CERTIFICATES_LOCK:
        if ip not in _TLS_CERTIFICATES:
            directory = Path(tempfile.mkdtemp(prefix="branding-tls-cert-"))
            cert = directory / "cert.pem"
            key = directory / "key.pem"
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048",
                    "-nodes", "-days", "1", "-keyout", str(key),
                    "-out", str(cert), "-subj", "/CN=" + ip,
                    "-addext", "subjectAltName=IP:" + ip,
                ],
                check=True, capture_output=True,
            )
            _TLS_CERTIFICATES[ip] = (cert, key)
        return _TLS_CERTIFICATES[ip]


class _TlsFetchServer(_FetchServer):
    """HTTP fixture behind a real TLS handshake with a fixture certificate."""

    def __init__(self, address, context):
        super().__init__(address, _BrandingFetchHandler)
        self.context = context

    def get_request(self):
        conn, addr = super().get_request()
        return self.context.wrap_socket(conn, server_side=True), addr

    def handle_error(self, request, client_address):
        # The wrong-certificate test aborts the handshake; that is the expected
        # outcome, not a fixture error to print.
        pass


class DestinationPinnedFetchTests(unittest.TestCase):
    """F6: every connection the fetch boundary makes targets a validated address.

    The production policy is the default. Tests that need an allowed hop inject a
    policy that permits exactly the fixture address and defers every other
    address to the production policy, so every refusal below is the production
    rule. Request and connection counters prove that a refused hop never reached
    its target.
    """

    @classmethod
    def setUpClass(cls):
        cls.module = _FetcherModule.load()
        # staticmethod keeps the closure unbound when read back as self.policy.
        cls.policy = staticmethod(fixture_address_policy(cls.module))
        cls.main = _FetchServer(("127.0.0.1", 0), _BrandingFetchHandler)
        cls.ok = _FetchServer(("127.0.0.1", 0), _BrandingFetchHandler)
        cls.forbidden = _FetchServer(("127.0.0.2", 0), _BrandingFetchHandler)
        cls.tls_stall = _TlsHandshakeFixture(("127.0.0.1", 0), "stall")
        cls.tls_drip = _TlsHandshakeFixture(("127.0.0.1", 0), "drip")
        main_port = cls.main.server_address[1]
        ok_port = cls.ok.server_address[1]
        forbidden_port = cls.forbidden.server_address[1]
        cls.main_url = "http://127.0.0.1:%d" % main_port
        cls.main.redirect_ok = "http://127.0.0.1:%d/image.png" % ok_port
        cls.main.redirect_forbidden = "http://127.0.0.2:%d/image.png" % forbidden_port
        cls.main.redirect_ftp = "ftp://127.0.0.1:%d/image.png" % ok_port
        cls.main.redirect_loop = "%s/loop" % cls.main_url
        for server in (
            cls.main, cls.ok, cls.forbidden, cls.tls_stall, cls.tls_drip
        ):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (
            cls.main, cls.ok, cls.forbidden, cls.tls_stall, cls.tls_drip
        ):
            server.shutdown()
            server.server_close()

    def _run(self, url, *, cap=1024 * 1024, policy=None, max_redirects=None, **options):
        """Run the shipped fetch entry point; return (out_path, FetchError or None)."""
        directory = Path(tempfile.mkdtemp(prefix="branding-fetch-"))
        out = directory / "image.png"
        extra = dict(options)
        if policy is not None:
            extra["allow_address"] = policy
        if max_redirects is not None:
            extra["max_redirects"] = max_redirects
        error = None
        try:
            self.module.fetch_branding_image("app_logo_url", url, str(out), cap, **extra)
        except self.module.FetchError as exc:
            error = exc
        return out, error

    def _assert_no_output(self, out):
        self.assertFalse(out.exists(), "a refused fetch left an output file")
        self.assertFalse(
            out.with_name(out.name + ".part").exists(),
            "a refused fetch left a .part file",
        )

    def _run_shell(self, url, *, cap=1024 * 1024):
        directory = Path(tempfile.mkdtemp(prefix="branding-fetch-shell-"))
        out = directory / "image.png"
        helper = directory / "fetch_branding_image.py"
        helper.write_text(fetcher_source(), encoding="utf-8")
        script = (
            "set -euo pipefail\n"
            + 'fetcher="' + str(helper) + '"\n'
            + fetch_shell_function()
            + 'fetch_branding_image app_logo_url "$URL" "$OUT" "$CAP"\n'
        )
        env = {**os.environ, "URL": url, "OUT": str(out), "CAP": str(cap)}
        result = subprocess.run(
            ["bash", "-s"], input=script, text=True, capture_output=True, env=env
        )
        return result, out

    # 1. direct allowed endpoint -> accepted
    def test_allowed_endpoint_is_fetched(self):
        before = self.main.snapshot()
        out, error = self._run(self.main_url + "/image.png", policy=self.policy)
        self.assertIsNone(error)
        self.assertEqual(self.main.snapshot(), (before[0] + 1, before[1] + 1))
        self.assertEqual(out.read_bytes(), REFERENCE_PNG)

    # 2. direct loopback target -> rejected with no connection
    def test_direct_loopback_target_is_refused_without_connecting(self):
        before = self.main.snapshot()
        out, error = self._run(self.main_url + "/image.png")  # production policy
        self.assertIsNotNone(error)
        self.assertIn("non-public address", str(error))
        self.assertEqual(
            self.main.snapshot(), before, "a refused loopback hop was still connected to"
        )
        self._assert_no_output(out)

    # 3. hostname resolving to a forbidden address -> rejected with no connection
    def test_hostname_resolving_to_loopback_is_refused_without_connecting(self):
        before = self.main.snapshot()
        out, error = self._run("http://localhost:%d/image.png" % self.main.server_address[1])
        self.assertIsNotNone(error)
        self.assertIn("localhost", str(error))
        self.assertIn("non-public address", str(error))
        self.assertEqual(
            self.main.snapshot(), before, "the loopback hostname was still connected to"
        )
        self._assert_no_output(out)

    # 4. allowed initial endpoint -> redirect to loopback on a different address
    def test_redirect_to_a_different_loopback_address_is_refused_without_connecting(self):
        before = self.main.snapshot()
        forbidden_before = self.forbidden.snapshot()
        out, error = self._run(self.main_url + "/redirect-forbidden", policy=self.policy)
        self.assertIsNotNone(error)
        self.assertIn("127.0.0.2", str(error))
        self.assertEqual(
            self.main.snapshot(),
            (before[0] + 1, before[1] + 1),
            "the allowed initial hop should be the only connection opened",
        )
        self.assertEqual(
            self.forbidden.snapshot(),
            forbidden_before,
            "the refused redirect target (127.0.0.2) received a connection",
        )
        self._assert_no_output(out)

    # 5. allowed -> allowed redirect -> accepted
    def test_allowed_redirect_chain_is_followed(self):
        before = self.ok.snapshot()
        out, error = self._run(self.main_url + "/redirect-ok", policy=self.policy)
        self.assertIsNone(error)
        self.assertEqual(self.ok.snapshot(), (before[0] + 1, before[1] + 1))
        self.assertEqual(out.read_bytes(), REFERENCE_PNG)

    # 6. redirect to a non-http(s) scheme -> still rejected
    def test_redirect_to_a_non_http_scheme_is_refused(self):
        before = self.ok.snapshot()
        out, error = self._run(self.main_url + "/redirect-ftp", policy=self.policy)
        self.assertIsNotNone(error)
        self.assertIn("http(s)", str(error))
        self.assertEqual(self.ok.snapshot(), before, "the ftp redirect target was connected to")
        self._assert_no_output(out)

    # 7. oversized chunked response with no Content-Length -> bounded, refused
    def test_oversized_chunked_response_is_bounded_during_transfer(self):
        self.main.stream_done.clear()
        out, error = self._run(self.main_url + "/huge", cap=1024 * 1024, policy=self.policy)
        self.assertIsNotNone(error)
        self.assertIn("exceeds the per-field byte cap", str(error))
        self._assert_no_output(out)
        self.assertTrue(self.main.stream_done.wait(10), "the fixture never finished the transfer")
        self.assertFalse(
            self.main.stream_completed,
            "the client read the entire oversized chunked body instead of bounding it at fetch time",
        )
        self.assertLess(
            self.main.stream_bytes,
            _BrandingFetchHandler.HUGE // 2,
            "the transfer was not cut off near the cap",
        )

    # 8. normal under-cap PNG -> accepted, bytes intact
    def test_under_cap_png_is_accepted_with_bytes_intact(self):
        out, error = self._run(self.main_url + "/image.png", cap=1024 * 1024, policy=self.policy)
        self.assertIsNone(error)
        data = out.read_bytes()
        self.assertTrue(data.startswith(PNG_SIGNATURE))
        self.assertEqual(data, REFERENCE_PNG)

    def test_under_cap_chunked_response_is_accepted(self):
        # An unknown-length body within the cap must still be accepted: the bound
        # rejects what is too large, not what is merely undeclared.
        out, error = self._run(
            self.main_url + "/chunked-small", cap=1024 * 1024, policy=self.policy
        )
        self.assertIsNone(error)
        self.assertEqual(out.read_bytes(), b"C" * _BrandingFetchHandler.SMALL)

    def test_oversized_content_length_response_is_bounded_during_transfer(self):
        self.main.stream_done.clear()
        out, error = self._run(self.main_url + "/big-cl", cap=1024 * 1024, policy=self.policy)
        self.assertIsNotNone(error)
        self.assertIn("exceeds the per-field byte cap", str(error))
        self._assert_no_output(out)
        self.assertTrue(self.main.stream_done.wait(10), "the fixture never finished the transfer")
        # 64 MiB cannot be absorbed by socket buffers against a 1 MiB cap, so the
        # fixture must have been cut off far below the declared Content-Length:
        # the transfer-time cap bounded the client, not the declared length.
        self.assertLess(
            self.main.stream_bytes,
            _BrandingFetchHandler.HUGE // 2,
            "the transfer was not cut off near the cap",
        )
        self.assertFalse(
            self.main.stream_completed,
            "a declared Content-Length, not the transfer-time cap, bounded the client",
        )

    def test_redirect_chain_is_capped_and_fails_closed(self):
        before = self.main.snapshot()
        out, error = self._run(self.main_url + "/loop", policy=self.policy, max_redirects=2)
        self.assertIsNotNone(error)
        self.assertIn("redirects", str(error))
        self.assertEqual(
            self.main.snapshot()[1],
            before[1] + 3,
            "expected the initial request plus two followed redirects, then a refusal",
        )
        self._assert_no_output(out)

    def test_address_policy_refuses_the_documented_non_public_ranges(self):
        refused = (
            "0.0.0.1", "127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.255",
            "192.168.1.1", "169.254.169.254", "100.64.0.1", "192.0.0.1",
            "192.0.2.1", "198.18.0.1", "198.51.100.1", "203.0.113.1",
            "192.88.99.1", "224.0.0.1", "240.0.0.1", "255.255.255.255",
            "::", "::1", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1",
            "100::1", "2001:db8::1", "2001::1", "2001:2::1", "2001:10::1",
            "2001:20::1", "64:ff9b::1", "64:ff9b:1::1", "2002::1",
            "::ffff:127.0.0.1", "::ffff:10.0.0.1",
            # IETF-Reserved IPv6 blocks outside the 2000::/3 global-unicast
            # allocation (IANA IPv6 Address Space registry).  The deny list's
            # complement is not public there, so a literal outside 2000::/3 is
            # refused even though it is not in the non-public table.
            "400::1", "800::1", "1000::1", "6000::1", "fe00::1", "fec0::1",
            # IANA-RESERVED blocks inside 2000::/3 (IANA IPv6 Global Unicast
            # Address Assignments registry): 3ffe::/16 is the returned 6bone
            # allocation (RFC 5156) and the tail rows 2d00::/8 .. 3ffc::/15 are
            # reserved by IANA and not yet allocated to any registry.
            "3ffe::1",
            "2d00::1", "2e00::1", "3000::1", "3800::1", "3c00::1", "3e00::1",
            "3f00::1", "3f80::1", "3fc0::1", "3fe0::1", "3ff0::1", "3ff8::1",
            "3ffc::1",
            # IANA non-globally-reachable entries added for policy parity:
            "3fff::1",  # Documentation (RFC 9637)
            "5f00::1",  # Segment Routing (SRv6) SIDs (RFC 9602)
            "100:0:0:1::1",  # Dummy IPv6 Prefix (RFC 9780)
            "::1.2.3.4",  # deprecated IPv4-compatible form (RFC 4291)
        )
        allowed = (
            "8.8.8.8", "1.1.1.1", "93.184.216.34", "172.32.0.1", "192.169.0.1",
            "2606:4700:4700::1111", "2001:4860:4860::8888", "::ffff:8.8.8.8",
            # IANA entries marked globally reachable must stay accepted:
            "2001:1::1", "2001:1::2", "2001:1::3", "2001:3::1", "2001:4:112::1",
            "2001:30::1", "2620:4f:8000::1",
        )
        for address in refused:
            self.assertFalse(self.module.is_public_address(address), address)
        for address in allowed:
            self.assertTrue(self.module.is_public_address(address), address)

    def test_ipv6_allow_list_refuses_every_unallocated_registry_gap(self):
        # Blocking review finding: inside 2000::/3 the complement of a deny list is
        # not public space. The IANA IPv6 Global Unicast Address Assignments registry
        # (retrieved 2026-10-14) states that all address space in the block with no
        # ALLOCATED row is reserved for future allocation, so the gaps between the
        # ALLOCATED rows are reserved, not public. These nine literals sit in such
        # gaps and were accepted by the deny-list-complement policy this allow list
        # replaces (falsified against the shipped 3247d11d helper: all nine returned
        # True).
        gap_literals = (
            "2000::1",        # no ALLOCATED row covers 2000::/16
            "2001:1000::1",   # gap between 2001:e00::/23 and 2001:1200::/23
            "2001:4e00::1",   # gap between 2001:4c00::/23 and 2001:5000::/20
            "2001:6000::1",   # gap between 2001:5000::/20 and 2001:8000::/19
            "2001:c000::1",   # after 2001:b000::/20 (2001:c00::/23 covers 0c00, not c000)
            "2611::1",        # after 2610::/23 (2610:0000 .. 2610:01ff)
            "2622::1",        # gap between 2620::/23 and 2630::/12
            "2650::1",        # after 2630::/12 (2630 .. 263f)
            "3fff:1000::1",   # outside 3fff::/20, in the IANA-RESERVED tail
        )
        refused = gap_literals + (
            # Inside the partially allocated 2001::/23 parent but outside its six
            # Globally Reachable more-specifics: the parent must not be re-admitted
            # as a whole, and the /128 anycasts must not widen their /32.
            "2001::1", "2001:1::4", "2001:5::1",
            # The seven literals the previous corrective already refused: the six
            # IETF-Reserved blocks outside 2000::/3 and the returned 6bone 3ffe::/16.
            "400::1", "800::1", "1000::1", "6000::1", "fe00::1", "fec0::1",
            "3ffe::1",
        )
        allowed = (
            "2606:4700:4700::1111",      # 2600::/12
            "2001:4860:4860::8888",      # 2001:4800::/23
            "2001:1::3",                 # 2001::/23 reachable DNS-SD SDP anycast
            # Other allocated, globally reachable literals from different registry
            # rows.
            "2001:200::1",               # 2001:200::/23 (APNIC)
            "2400:cb00::1",              # 2400::/12 (APNIC)
            "2a00:1450:4009:80f::200e",  # 2a00::/12 (RIPE NCC)
        )
        for address in refused:
            self.assertFalse(self.module.is_public_address(address), address)
        for address in allowed:
            self.assertTrue(self.module.is_public_address(address), address)

    def test_absent_url_is_a_noop_through_the_workflow_shell_wrapper(self):
        result, out = self._run_shell("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_no_output(out)

    def test_workflow_shell_wrapper_refuses_a_loopback_url_end_to_end(self):
        # The shipped path -- shell wrapper, helper CLI, production policy -- refuses
        # a loopback URL without connecting to it, and without echoing the URL (it
        # may be a signed provider URL): only the field and the host are reported.
        before = self.main.snapshot()
        result, out = self._run_shell(self.main_url + "/image.png?token=payload-secret")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count("::error::"), 1, result.stdout)
        self.assertIn("::error::app_logo_url", result.stdout)
        self.assertIn("non-public address", result.stdout)
        self.assertNotIn("http://", result.stdout)
        self.assertNotIn("payload-secret", result.stdout)
        self.assertEqual(self.main.snapshot(), before, "the refused loopback URL was connected to")
        self._assert_no_output(out)

    # 9. regression for the blocking review finding: the per-attempt deadline must
    #    bound the whole hop, not only the named checks between socket operations.
    TRICKLE_ENDPOINTS = ("/trickle-status", "/trickle-headers", "/trickle-interim")

    def test_trickling_peer_cannot_extend_an_attempt_past_the_deadline(self):
        # The pre-fix helper only re-checked its deadline between socket operations
        # and relied on the per-read socket timeout, so each of these dribbling
        # peers kept its hop alive well past the 1 s attempt bound (8 s with this
        # bounded fixture, 40 s in the open-ended probe).
        for path in self.TRICKLE_ENDPOINTS:
            with self.subTest(path=path):
                self.main.trickle_done.clear()
                start = time.monotonic()
                out, error = self._run(
                    self.main_url + path, policy=self.policy, max_time=1.0, retries=0
                )
                elapsed = time.monotonic() - start
                self.assertIsNotNone(error, "the trickling peer was accepted")
                self.assertEqual(
                    str(error),
                    self.module._failure_message("app_logo_url", 1024 * 1024),
                    "expected the deterministic per-field timeout failure",
                )
                self.assertLess(
                    elapsed,
                    5.0,
                    "a peer dribbling one byte inside every read timeout extended the "
                    "attempt past its 1 s deadline",
                )
                self._assert_no_output(out)
                self.assertTrue(
                    self.main.trickle_done.wait(10),
                    "the trickle fixture never finished; the client never cut the hop off",
                )
                self.assertLess(
                    self.main.trickle_steps,
                    _BrandingFetchHandler.TRICKLE_LIMIT,
                    "the peer delivered its whole dribble budget; the transfer was not "
                    "cut off at the attempt deadline",
                )

    def test_trickling_peer_cli_reports_one_deterministic_field_error_within_the_bound(self):
        # The same regression through the shipped CLI: exactly one deterministic
        # per-field ::error:: annotation, no traceback, no echoed URL and no output
        # file.  The test copy shortens the two bound constants and permits exactly
        # the fixture address through the helper's own allow_address seam (the same
        # seam the in-process tests use); everything else is the shipped helper.
        directory = Path(tempfile.mkdtemp(prefix="branding-fetch-cli-"))
        helper = directory / "fetch_branding_image.py"
        source = fetcher_source()
        self.assertIn("FETCH_MAX_TIME = 120.0", source)
        self.assertIn("FETCH_RETRIES = 2", source)
        self.assertIn("allow_address=is_public_address,", source)
        helper.write_text(
            source.replace("FETCH_MAX_TIME = 120.0", "FETCH_MAX_TIME = 1.0")
            .replace("FETCH_RETRIES = 2", "FETCH_RETRIES = 0")
            .replace(
                "allow_address=is_public_address,",
                "allow_address=lambda address: str(address) == %r," % FIXTURE_ADDRESS,
            ),
            encoding="utf-8",
        )
        self.main.trickle_done.clear()
        out = directory / "image.png"
        start = time.monotonic()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "app_logo_url",
                    self.main_url + "/trickle-status?token=payload-secret",
                    str(out),
                    str(1024 * 1024),
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            self.fail("the trickling peer kept the helper alive past the attempt deadline")
        elapsed = time.monotonic() - start
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count("::error::"), 1, result.stdout)
        self.assertTrue(result.stdout.startswith("::error::app_logo_url"), result.stdout)
        self.assertNotIn("http://", result.stdout)
        self.assertNotIn("payload-secret", result.stdout)
        self.assertLess(elapsed, 5.0)
        self.assertTrue(
            self.main.trickle_done.wait(10),
            "the trickle fixture never finished; the client never cut the hop off",
        )
        self.assertLess(
            self.main.trickle_steps,
            _BrandingFetchHandler.TRICKLE_LIMIT,
            "the peer delivered its whole dribble budget; the transfer was not "
            "cut off at the attempt deadline",
        )
        self.assertFalse(out.exists())
        self.assertFalse(Path(str(out) + ".part").exists())

    def test_watchdog_timeout_stays_retryable(self):
        # A watchdog aborts the attempt with a timeout, which must stay in the
        # curl-transient taxonomy: with one retry and no delay the dribbling peer
        # is contacted twice through two full attempts, then the fetch fails.
        self.main.trickle_done.clear()
        before = self.main.snapshot()
        out, error = self._run(
            self.main_url + "/trickle-status",
            policy=self.policy,
            max_time=1.0,
            retries=1,
            retry_delay=0.0,
        )
        self.assertIsNotNone(error, "the trickling peer was accepted")
        self.assertEqual(
            str(error), self.module._failure_message("app_logo_url", 1024 * 1024)
        )
        self.assertEqual(
            self.main.snapshot(),
            (before[0] + 2, before[1] + 2),
            "a deadline timeout must be retried exactly once",
        )
        self._assert_no_output(out)

    def test_shell_wrapper_process_bound_is_a_deterministic_backstop(self):
        # The helper cannot interrupt a resolver call that blocks below Python; the
        # wrapper's process-level timeout bounds that case and prints the
        # deterministic per-field error itself.  Only the timeout exit status gets
        # that message, so a normal helper failure is not reported twice (the
        # loopback wrapper test above counts exactly one ::error:: line).
        directory = Path(tempfile.mkdtemp(prefix="branding-fetch-process-"))
        helper = directory / "hanging_fetcher.py"
        helper.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        out = directory / "image.png"
        function = fetch_shell_function()
        self.assertIn("local process_timeout=420", function)
        script = (
            "set -euo pipefail\n"
            + 'fetcher="' + str(helper) + '"\n'
            + function.replace("local process_timeout=420", "local process_timeout=2")
            + 'fetch_branding_image app_logo_url "$URL" "$OUT" "$CAP"\n'
        )
        start = time.monotonic()
        result = subprocess.run(
            ["bash", "-s"],
            input=script,
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "URL": "http://example.invalid/image.png?token=payload-secret",
                "OUT": str(out),
                "CAP": str(1024 * 1024),
            },
            timeout=30,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.count("::error::"), 1, result.stdout)
        self.assertIn("::error::app_logo_url", result.stdout)
        self.assertIn("timed out", result.stdout)
        self.assertIn("2 s process bound", result.stdout)
        self.assertLess(elapsed, 20.0)
        self.assertFalse(out.exists())
        self.assertFalse(Path(str(out) + ".part").exists())

    def test_short_content_length_body_fails_closed(self):
        out, error = self._run(self.main_url + "/short-body", policy=self.policy, max_time=5.0)
        self.assertIsNotNone(error, "a body shorter than its Content-Length was accepted")
        self.assertEqual(
            str(error), self.module._failure_message("app_logo_url", 1024 * 1024)
        )
        self._assert_no_output(out)

    def test_truncated_chunked_body_fails_closed(self):
        out, error = self._run(
            self.main_url + "/truncated-chunked", policy=self.policy, max_time=5.0
        )
        self.assertIsNotNone(error, "a truncated chunked body was accepted")
        self.assertEqual(
            str(error), self.module._failure_message("app_logo_url", 1024 * 1024)
        )
        self._assert_no_output(out)

    def test_mixed_public_and_non_public_resolution_is_refused_before_connecting(self):
        # The public address comes first, so an implementation that connected to
        # each address as it validated it would reach the public address before
        # discovering the non-public one; the hop must instead be refused in full
        # with zero connections attempted.
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80)),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (FIXTURE_ADDRESS, self.main.server_address[1]),
            ),
        ]
        attempted = []
        real_getaddrinfo = self.module.socket.getaddrinfo
        real_socket = self.module.socket.socket

        def fake_getaddrinfo(host, port, **kwargs):
            return infos

        def forbidden_socket(*args, **kwargs):
            attempted.append((args, kwargs))
            raise AssertionError("a connection was attempted for a refused resolution")

        self.module.socket.getaddrinfo = fake_getaddrinfo
        self.module.socket.socket = forbidden_socket
        try:
            out, error = self._run("http://mixed.example/image.png")
        finally:
            self.module.socket.getaddrinfo = real_getaddrinfo
            self.module.socket.socket = real_socket
        self.assertIsNotNone(error, "the mixed resolution was accepted")
        self.assertIn("mixed.example", str(error))
        self.assertIn("non-public address", str(error))
        self.assertEqual(attempted, [], "the refused hop attempted a connection")
        self._assert_no_output(out)

    # 10. deterministic fail-fast refusals for inputs the wire cannot carry

    def _run_with_resolver_guard(self, url, description):
        """Run the fetch with the resolver replaced by a hard failure.

        The resolver is only reached when the hop was not refused up front, so
        any implementation that resolves (and then fails deep in http.client or
        the resolver) an input it cannot put on the wire fails this test.
        """
        real_getaddrinfo = self.module.socket.getaddrinfo

        def forbidden_getaddrinfo(host, port, **kwargs):
            raise AssertionError(
                description
                + ": the helper resolved "
                + repr(host)
                + " instead of refusing the input up front"
            )

        self.module.socket.getaddrinfo = forbidden_getaddrinfo
        try:
            return self._run(url, policy=self.policy)
        finally:
            self.module.socket.getaddrinfo = real_getaddrinfo

    def test_non_ascii_host_is_refused_before_resolution(self):
        # http.client encodes the Host header as ASCII (or IDNA), so a raw
        # Unicode host reaches it and fails there with UnicodeEncodeError; the
        # fetcher must refuse it deterministically instead.
        out, error = self._run_with_resolver_guard(
            "http://\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444/icon.png",
            "non-ASCII host",
        )
        self.assertIsNotNone(error, "a raw Unicode host was accepted")
        text = str(error)
        self.assertIn("not an ASCII host name", text)
        self.assertNotIn("http://", text)
        self.assertNotIn("icon.png", text)
        self._assert_no_output(out)

    def test_non_ascii_request_target_is_refused_before_connecting(self):
        # http.client encodes the request line as ASCII, so a raw Unicode path
        # or query reaches it and fails there with UnicodeEncodeError; the host
        # may be named in the refusal, the URL, path and query never may.
        before = self.main.snapshot()
        out, error = self._run_with_resolver_guard(
            self.main_url
            + "/\u0438\u043a\u043e\u043d\u043a\u0430.png?token=payload-secret",
            "non-ASCII request target",
        )
        self.assertIsNotNone(error, "a raw Unicode request target was accepted")
        text = str(error)
        self.assertIn("request target is not ASCII", text)
        self.assertNotIn("http://", text)
        self.assertNotIn("payload-secret", text)
        self.assertNotIn("\u0438\u043a\u043e\u043d\u043a\u0430", text)
        self.assertEqual(
            self.main.snapshot(), before, "the refused hop was connected to"
        )
        self._assert_no_output(out)

    def test_host_with_an_empty_dns_label_is_refused_before_resolution(self):
        # A host with an empty DNS label only fails later in getaddrinfo; the
        # fetcher must refuse it deterministically, including a second trailing
        # dot (the single trailing root dot stays usable).
        for url in (
            "http://127.0.0.1..:80/image.png",
            "http://.example.com/image.png",
            "http://example..com/image.png",
        ):
            with self.subTest(url=url):
                out, error = self._run_with_resolver_guard(url, "empty DNS label")
                self.assertIsNotNone(error, "a host with an empty label was accepted")
                text = str(error)
                self.assertIn("empty label", text)
                self.assertNotIn("http://", text)
                self._assert_no_output(out)

    def test_fail_fast_guard_keeps_wire_safe_hosts_and_targets(self):
        # Bracketed IPv6 literals, IDNA/punycode hosts, a single trailing root
        # dot and percent-encoded ASCII targets must stay usable.
        for url, expected_host in (
            ("http://[::1]:8080/image.png", "::1"),
            ("https://xn--e1afmkfd.xn--p1ai/icon.png", "xn--e1afmkfd.xn--p1ai"),
            ("https://example.com./icon.png", "example.com."),
            (
                "http://example.com/%D0%B8%D0%BA%D0%BE%D0%BD%D0%BA%D0%B0.png",
                "example.com",
            ),
        ):
            with self.subTest(url=url):
                _parsed, host, _target = self.module._parse_hop(url, "app_logo_url")
                self.assertEqual(host, expected_host)

    def test_percent_encoded_ascii_target_is_fetched_end_to_end(self):
        out, error = self._run(
            self.main_url + "/encoded/%D0%B8%D0%BA%D0%BE%D0%BD%D0%BA%D0%B0.png",
            policy=self.policy,
        )
        self.assertIsNone(error)
        self.assertEqual(out.read_bytes(), REFERENCE_PNG)

    # 11. the per-attempt wall-clock bound covers the TLS handshake

    STALL_MAX_TIME = 2.0
    STALL_CONNECT_SECONDS = 1.9

    def test_stalled_tls_handshake_ends_at_the_attempt_bound(self):
        # White-box seam, and what it does and does not prove: _connect_pinned
        # is wrapped so that part of the attempt budget is consumed *after* the
        # socket timeout was armed, exactly as a slow TCP connect does.  That
        # makes the stale pre-connect timeout observable: a pre-fix helper
        # reuses it for the blocking handshake and the attempt runs past its
        # deadline.  The seam only consumes budget; it does not change the
        # production path under test.  The fixture is plain TCP and never sends
        # valid TLS data, so this proves the handshake is covered by the attempt
        # bound; it proves nothing about certificate verification (the hang
        # precedes verification, no certificate is involved).
        server = self.tls_stall
        server.client_closed.clear()
        before = server.snapshot()
        real_connect = self.module._connect_pinned

        def slow_connect(addresses, watchdog):
            sock = real_connect(addresses, watchdog)
            time.sleep(self.STALL_CONNECT_SECONDS)
            return sock

        self.module._connect_pinned = slow_connect
        try:
            start = time.monotonic()
            out, error = self._run(
                "https://127.0.0.1:%d/image.png" % server.server_address[1],
                policy=self.policy,
                max_time=self.STALL_MAX_TIME,
                retries=0,
            )
            elapsed = time.monotonic() - start
        finally:
            self.module._connect_pinned = real_connect
        self.assertIsNotNone(error, "the stalled TLS handshake was accepted")
        self.assertEqual(
            str(error), self.module._failure_message("app_logo_url", 1024 * 1024)
        )
        self.assertLess(
            elapsed,
            self.STALL_MAX_TIME + 1.4,
            "the stalled TLS handshake was not ended at the attempt bound "
            "(%.1f s elapsed against a %.1f s deadline)"
            % (elapsed, self.STALL_MAX_TIME),
        )
        self.assertEqual(server.snapshot(), before + 1)
        self._assert_no_output(out)
        self.assertTrue(
            server.client_closed.wait(5),
            "the client never cut the stalled handshake off",
        )

    def test_trickling_tls_handshake_is_cut_off_and_stays_retryable(self):
        # Preserved-behaviour guard, without any seam: the fixture dribbles TLS
        # record bytes inside every per-read socket timeout, so the hop can only
        # end at a wall-clock bound, and the deadline abort must stay in the
        # curl-transient taxonomy (one retry, two bounded attempts).  This held
        # before the fix too: CPython's SSL layer applies the socket timeout as
        # a total handshake deadline, so the pre-fix stale timeout already bound
        # this shape of handshake (it is the *value* of that timeout that was
        # stale -- see the stalled-handshake test, which is the discriminating
        # case).  This test keeps the end-to-end handshake path bounded and
        # retryable after the handshake is deferred and the timeout re-armed.
        server = self.tls_drip
        before = server.snapshot()
        start = time.monotonic()
        out, error = self._run(
            "https://127.0.0.1:%d/image.png" % server.server_address[1],
            policy=self.policy,
            max_time=1.0,
            retries=1,
            retry_delay=0.0,
        )
        elapsed = time.monotonic() - start
        self.assertIsNotNone(error, "the trickling TLS handshake was accepted")
        self.assertEqual(
            str(error), self.module._failure_message("app_logo_url", 1024 * 1024)
        )
        self.assertEqual(
            server.snapshot(),
            before + 2,
            "a watchdog deadline timeout must be retried exactly once",
        )
        self.assertLess(
            elapsed,
            4.5,
            "the trickling TLS handshake extended an attempt past its deadline "
            "(%.1f s elapsed for two 1 s attempts)" % elapsed,
        )
        self._assert_no_output(out)

    # 12. preserved behaviour of the working HTTPS path (the handshake sequence
    #     that finding 3 changed): a real handshake completes with the URL
    #     hostname verified, and a certificate for another host is refused.
    #     Both held before the fix too; they are the only local coverage of a
    #     successful TLS handshake through the shipped helper.

    @unittest.skipUnless(HAS_OPENSSL, "openssl CLI required to mint the TLS fixture certificate")
    def test_https_fetch_completes_with_the_url_hostname_verified(self):
        cert, key = tls_fixture_certificate("127.0.0.1")
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(cert), str(key))
        server = _TlsFetchServer(("127.0.0.1", 0), server_context)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        real_create = self.module.ssl.create_default_context
        # Only the trust anchor is injected: check_hostname and certificate
        # verification stay on, against the URL hostname (server_hostname).
        self.module.ssl.create_default_context = (
            lambda *args, **kwargs: real_create(cafile=str(cert))
        )
        try:
            before = server.snapshot()
            out, error = self._run(
                "https://127.0.0.1:%d/image.png" % server.server_address[1],
                policy=self.policy,
            )
        finally:
            self.module.ssl.create_default_context = real_create
            server.shutdown()
            server.server_close()
        self.assertIsNone(error)
        self.assertEqual(server.snapshot(), (before[0] + 1, before[1] + 1))
        self.assertEqual(out.read_bytes(), REFERENCE_PNG)
        self.assertFalse(out.with_name(out.name + ".part").exists())

    @unittest.skipUnless(HAS_OPENSSL, "openssl CLI required to mint the TLS fixture certificate")
    def test_https_fetch_still_refuses_a_certificate_for_another_host(self):
        cert, key = tls_fixture_certificate("127.0.0.2")
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(cert), str(key))
        server = _TlsFetchServer(("127.0.0.1", 0), server_context)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        real_create = self.module.ssl.create_default_context
        self.module.ssl.create_default_context = (
            lambda *args, **kwargs: real_create(cafile=str(cert))
        )
        try:
            out, error = self._run(
                "https://127.0.0.1:%d/image.png" % server.server_address[1],
                policy=self.policy,
            )
        finally:
            self.module.ssl.create_default_context = real_create
            server.shutdown()
            server.server_close()
        self.assertIsNotNone(error, "a certificate for another host was accepted")
        self.assertEqual(
            str(error), self.module._failure_message("app_logo_url", 1024 * 1024)
        )
        self._assert_no_output(out)

    # 13. a present port is the port actually fetched: the consumer mirrors the
    #     producer's 1..65535 rule instead of substituting the scheme default.

    PORT_SPELLINGS = ("0", "00000", "65536", "99999", "065536")

    def test_present_port_outside_1_65535_is_refused_in_the_initial_url(self):
        # A present port of 0 used to fall back to the scheme default: the URL still
        # said ":0" and the Host header still carried ":0", but the resolver was given
        # port 80.  The producer refuses a port outside 1..65535, so every spelling of
        # such a port -- including the leading-zero forms, however they parse -- must be
        # unusable here too, refused with the same deterministic per-field message a
        # malformed port gets, and before anything is fetched.
        expected = self.module._refused_message(
            "app_logo_url", "provider URL is not a usable absolute http(s) URL"
        )
        before = self.main.snapshot()
        for spelling in self.PORT_SPELLINGS:
            with self.subTest(port=spelling):
                out, error = self._run(
                    "http://127.0.0.1:%s/image.png" % spelling, policy=self.policy
                )
                self.assertIsNotNone(error, "the port %s URL was accepted" % spelling)
                self.assertEqual(
                    str(error),
                    expected,
                    "the port %s URL was not refused by the helper's own port rule"
                    % spelling,
                )
                self._assert_no_output(out)
        self.assertEqual(
            self.main.snapshot(), before, "a refused port URL was connected to"
        )

    def test_present_port_outside_1_65535_in_a_redirect_is_refused(self):
        # The same rule on a provider-controlled redirect Location.  The resolver is
        # recorded, so the refused redirect hop provably never reaches resolution: the
        # initial hop is the only lookup of the whole fetch.
        expected = self.module._refused_message(
            "app_logo_url", "provider URL is not a usable absolute http(s) URL"
        )
        calls = []
        real_getaddrinfo = self.module.socket.getaddrinfo

        def recording_getaddrinfo(host, port, **kwargs):
            calls.append((host, port))
            return real_getaddrinfo(host, port, **kwargs)

        self.module.socket.getaddrinfo = recording_getaddrinfo
        try:
            for spelling in self.PORT_SPELLINGS:
                with self.subTest(port=spelling):
                    calls.clear()
                    self.main.redirect_port = (
                        "http://127.0.0.1:%s/image.png" % spelling
                    )
                    out, error = self._run(
                        self.main_url + "/redirect-port", policy=self.policy
                    )
                    self.assertIsNotNone(
                        error, "the redirect port %s URL was accepted" % spelling
                    )
                    self.assertEqual(
                        str(error),
                        expected,
                        "the redirect port %s URL was not refused by the helper's own "
                        "port rule" % spelling,
                    )
                    self.assertEqual(
                        calls,
                        [("127.0.0.1", self.main.server_address[1])],
                        "the refused redirect hop was resolved",
                    )
                    self._assert_no_output(out)
        finally:
            self.main.redirect_port = ""
            self.module.socket.getaddrinfo = real_getaddrinfo

    def test_present_ports_inside_1_65535_fetch_that_port_with_a_matching_host_header(self):
        # Port 1 and port 65535 stay usable, and the fetched port is exactly the port
        # the URL names: the resolver is given it, the connection is built for it and
        # the request's Host header carries it.  What this proves: _parse_hop keeps both
        # ports, the resolution and the connection use the URL's port, and the Host
        # header matches it.  What it does not prove: that a real TCP connect to the
        # privileged port 1 succeeds -- binding it needs privileges this suite does not
        # have -- so the seam redirects only the TCP destination to the fixture while
        # the port under test stays in the URL and in the Host header.
        for port in (1, 65535):
            with self.subTest(port=port):
                recorded = []
                real_connect = self.module._connect_pinned

                def connect_to_fixture(addresses, watchdog, port=port):
                    recorded.append(addresses)
                    return real_connect(
                        [(socket.AF_INET, (FIXTURE_ADDRESS, self.main.server_address[1]))],
                        watchdog,
                    )

                self.module._connect_pinned = connect_to_fixture
                try:
                    self.main.host_headers[:] = []
                    out, error = self._run(
                        "http://127.0.0.1:%d/image.png" % port, policy=self.policy
                    )
                finally:
                    self.module._connect_pinned = real_connect
                self.assertIsNone(error, "the port %d URL was refused" % port)
                self.assertEqual(out.read_bytes(), REFERENCE_PNG)
                self.assertEqual(
                    recorded,
                    [[(socket.AF_INET, (FIXTURE_ADDRESS, port))]],
                    "the fetch did not resolve/connect to the port the URL names",
                )
                self.assertEqual(
                    self.main.host_headers,
                    ["127.0.0.1:%d" % port],
                    "the Host header does not match the fetched port",
                )

    def test_absent_port_uses_the_scheme_default(self):
        # No port in the URL means no port in the Host header while the connection is
        # built for the scheme default.  The https half of the same rule is covered by
        # the TLS test below.
        recorded = []
        real_connect = self.module._connect_pinned

        def connect_to_fixture(addresses, watchdog):
            recorded.append(addresses)
            return real_connect(
                [(socket.AF_INET, (FIXTURE_ADDRESS, self.main.server_address[1]))],
                watchdog,
            )

        self.module._connect_pinned = connect_to_fixture
        try:
            self.main.host_headers[:] = []
            out, error = self._run("http://127.0.0.1/image.png", policy=self.policy)
        finally:
            self.module._connect_pinned = real_connect
        self.assertIsNone(error)
        self.assertEqual(out.read_bytes(), REFERENCE_PNG)
        self.assertEqual(
            recorded,
            [[(socket.AF_INET, (FIXTURE_ADDRESS, 80))]],
            "an absent port did not use the http scheme default",
        )
        self.assertEqual(self.main.host_headers, ["127.0.0.1"])

    @unittest.skipUnless(HAS_OPENSSL, "openssl CLI required to mint the TLS fixture certificate")
    def test_absent_port_uses_the_443_default_over_tls(self):
        # The other half of the scheme default: an https URL without a port is fetched
        # on 443.  The seam redirects only the TCP destination to the TLS fixture, so
        # the 443 in the URL and in the Host header is what is asserted.
        cert, key = tls_fixture_certificate("127.0.0.1")
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(cert), str(key))
        server = _TlsFetchServer(("127.0.0.1", 0), server_context)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        recorded = []
        real_connect = self.module._connect_pinned
        real_create = self.module.ssl.create_default_context

        def connect_to_fixture(addresses, watchdog):
            recorded.append(addresses)
            return real_connect(
                [(socket.AF_INET, (FIXTURE_ADDRESS, server.server_address[1]))], watchdog
            )

        self.module._connect_pinned = connect_to_fixture
        # Only the trust anchor is injected: check_hostname and certificate
        # verification stay on, against the URL hostname (server_hostname).
        self.module.ssl.create_default_context = (
            lambda *args, **kwargs: real_create(cafile=str(cert))
        )
        try:
            server.host_headers[:] = []
            out, error = self._run("https://127.0.0.1/image.png", policy=self.policy)
        finally:
            self.module._connect_pinned = real_connect
            self.module.ssl.create_default_context = real_create
            server.shutdown()
            server.server_close()
        self.assertIsNone(error)
        self.assertEqual(out.read_bytes(), REFERENCE_PNG)
        self.assertEqual(
            recorded,
            [[(socket.AF_INET, (FIXTURE_ADDRESS, 443))]],
            "an absent https port did not use the 443 scheme default",
        )
        self.assertEqual(server.host_headers, ["127.0.0.1"])

    # 14. the per-attempt wall-clock bound covers the resolution phase

    def test_blocked_resolver_is_bounded_by_the_current_attempt_budget(self):
        # socket.getaddrinfo cannot be interrupted in-process, so the helper runs the
        # lookup on a daemon thread and waits only for the attempt's *current* remaining
        # budget, failing the hop as the same retryable timeout a stalled socket gets.
        # The pre-fix helper calls the resolver inline, so this case cannot be run
        # in-process: a lookup that blocks below Python keeps the whole process alive
        # and the failure is a hang, not an error.  The child therefore runs the shipped
        # helper copy through its own main() (a driver imports the copy and replaces
        # socket.getaddrinfo with a call that waits on an event that is never set) under
        # a 30 s outer deadline, with the attempt bound shortened to 1 s; the retry
        # constants stay as shipped (2 retries), so the failure must be classified as the
        # existing transient timeout and the lookup must be attempted exactly 3 times;
        # the child reports its lookup count on stderr for the test to assert.
        # What it proves: a resolver that never returns makes an attempt end within the
        # budget and the shipped helper exits after its bounded attempts with exactly the
        # deterministic per-field timeout message and no output file.  What it does not
        # prove: that the abandoned resolver thread itself stops -- it cannot be killed
        # and lives until the process exits (bounded by the unchanged retry count), which
        # is why the shell wrapper keeps its process-level timeout as the outermost
        # backstop.
        directory = Path(tempfile.mkdtemp(prefix="branding-fetch-dns-"))
        helper = directory / "fetch_branding_image.py"
        source = fetcher_source()
        helper.write_text(
            source.replace("FETCH_MAX_TIME = 120.0", "FETCH_MAX_TIME = 1.0").replace(
                "FETCH_RETRY_DELAY = 2.0", "FETCH_RETRY_DELAY = 0.0"
            ),
            encoding="utf-8",
        )
        driver = directory / "blocked_resolver_driver.py"
        driver.write_text(
            "import importlib.util\n"
            "import sys\n"
            "import threading\n"
            "spec = importlib.util.spec_from_file_location('branding_fetch', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "blocked = threading.Event()\n"
            "lookups = []\n"
            "def hanging_getaddrinfo(*args, **kwargs):\n"
            "    lookups.append(1)\n"
            "    blocked.wait()\n"
            "module.socket.getaddrinfo = hanging_getaddrinfo\n"
            "code = module.main([\n"
            "    'app_logo_url',\n"
            "    'http://blocked.example/icon.png',\n"
            "    sys.argv[2],\n"
            "    str(1024 * 1024),\n"
            "])\n"
            "print('resolver_lookups=%d' % len(lookups), file=sys.stderr)\n"
            "sys.exit(code)\n",
            encoding="utf-8",
        )
        out = directory / "image.png"
        start = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, str(driver), str(helper), str(out)],
                text=True,
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            self.fail(
                "a blocked resolver kept the helper alive past the attempt budget "
                "(the pre-fix behaviour is a hang, not an error)"
            )
        elapsed = time.monotonic() - start
        expected = self.module._failure_message("app_logo_url", 1024 * 1024)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.count("::error::"), 1, result.stdout)
        self.assertEqual(result.stdout.strip(), "::error::" + expected)
        self.assertNotIn("blocked.example", result.stdout)
        self.assertNotIn("http://", result.stdout)
        self.assertEqual(
            result.stderr.strip(),
            "resolver_lookups=3",
            "the blocked resolver was not retried as the existing transient timeout: the "
            "shipped retry count must stay at 3 bounded attempts",
        )
        self.assertLess(
            elapsed,
            10.0,
            "the blocked resolver extended the fetch past its bounded attempts "
            "(%.1f s elapsed for 3 x 1 s attempts)" % elapsed,
        )
        self.assertFalse(out.exists(), "a blocked resolver left an output file")
        self.assertFalse(Path(str(out) + ".part").exists())


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
        # Windows .rc: embedded double quote is a doubled quote, backslash doubled.
        self.assertIn('"He said ""hi"" \\\\ $cash"', rc)
        self.assertNotIn('He said \\"hi\\"', rc)
        toml = out["Cargo.toml"]
        # TOML basic string: embedded double quote keeps the backslash escape.
        self.assertIn('He said \\"hi\\" \\\\ $cash. All rights reserved.', toml)
        self.assertNotIn("He said \"\"hi\"\"", toml)
        dart = out["flutter/lib/desktop/pages/desktop_setting_page.dart"]
        self.assertIn('He said "hi" \\\\ \\$cash', dart)

    def test_embedded_quote_uses_rc_doubled_quotes_and_toml_backslash_escape(self):
        # Discriminating case for the separated escaping rules: the same company_name
        # containing a double quote must become `""` in the Windows .rc CompanyName and
        # LegalCopyright values (RC has no \" escape) but `\"` in the TOML basic-string
        # LegalCopyright values. Dart single-quoted literals need no quote escape.
        company = 'Northwind "The Best" Traders'
        result, out = self._run(company=company)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rc = out["flutter/windows/runner/Runner.rc"]
        self.assertIn('VALUE "CompanyName", "Northwind ""The Best"" Traders" "\\0"', rc)
        self.assertIn(
            'VALUE "LegalCopyright", "Copyright \u00a9 2026 Northwind ""The Best"" Traders. All rights reserved." "\\0"',
            rc,
        )
        self.assertNotIn('Northwind \\"The Best\\" Traders', rc)
        for rel in ("Cargo.toml", "libs/portable/Cargo.toml"):
            toml = out[rel]
            self.assertIn(
                'LegalCopyright = "Copyright \u00a9 2026 Northwind \\"The Best\\" Traders. All rights reserved."',
                toml,
            )
            self.assertNotIn('Northwind ""The Best"" Traders', toml)
        dart = out["flutter/lib/desktop/pages/desktop_setting_page.dart"]
        self.assertIn('Northwind "The Best" Traders', dart)
        self.assertNotIn('Northwind \\"The Best\\" Traders', dart)

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

    def test_default_equal_value_is_a_successful_noop(self):
        # A branding value that equals the current 1.4.9 default literal makes the
        # escaped replacement byte-identical to the anchor. That is a legitimate
        # no-op and must not be reported as a failed post-apply verification.
        cases = {
            "company_name": {"company": "Purslane Tech Pte. Ltd."},
            "download_url": {"download": "https://rustdesk.com/download"},
            "website_url": {"website": "https://rustdesk.com"},
            "website_url_trailing_slash": {"website": "https://rustdesk.com/"},
            "privacy_policy_url": {"privacy": "https://rustdesk.com/privacy.html"},
        }
        for field, kwargs in cases.items():
            with self.subTest(field=field):
                result, out = self._run(**kwargs)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("post-apply verification failed", result.stdout)
                self.assertIn("applied to", result.stdout)

    def test_control_characters_still_rejected(self):
        result, _ = self._run(company="bad\nname")
        self.assertEqual(result.returncode, 1)
        self.assertIn("control characters", result.stdout)

    def test_anchor_mismatch_fails_closed(self):
        result, _ = self._run(company="X", mutate={"Cargo.toml": "LegalCopyright = \"changed\"\n"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("anchor mismatch", result.stdout)

    def test_replacement_containing_default_literal_applies(self):
        # DeskForge accepts any company name within a length/control-character bound, so a
        # value may legitimately extend the 1.4.9 default literal. The escaped replacement
        # then still contains the anchor bytes as a substring; the old post-apply rule
        # (`anchor in data`) aborted such an accepted value. The corrected rule compares
        # the on-disk bytes to the exact expected post-replacement content instead.
        value = "Purslane Tech Pte. Ltd. (Singapore)"
        result, out = self._run(company=value)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("post-apply verification failed", result.stdout)
        self.assertIn("applied to", result.stdout)
        for rel in (
            "flutter/windows/runner/Runner.rc",
            "Cargo.toml",
            "libs/portable/Cargo.toml",
            "flutter/lib/desktop/pages/desktop_setting_page.dart",
        ):
            self.assertIn(value, out[rel], rel)
        # The bare 1.4.9 copyright literal is replaced, not merely embedded.
        self.assertNotIn(
            'LegalCopyright = "Copyright \u00a9 2026 Purslane Tech Pte. Ltd. All rights reserved."',
            out["Cargo.toml"],
        )

    def test_cross_field_anchor_composition_applies_once(self):
        # privacy_policy_url = https://rustdesk.com rewrites the about-page privacy
        # line into exactly the website_about anchor text, which the per-key count
        # guard used to read as a second website_about occurrence and reject. That is
        # a DeskForge-accepted configuration and must apply: the privacy line takes
        # the privacy value, the website line takes the website value, and each is
        # replaced exactly once (the introduced sibling anchor text is not
        # double-replaced).
        result, out = self._run(privacy="https://rustdesk.com", website="https://example.com")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        dart = out["flutter/lib/desktop/pages/desktop_setting_page.dart"]
        self.assertEqual(dart.count("launchUrlString('https://rustdesk.com');"), 1)
        self.assertEqual(dart.count("launchUrlString('https://example.com');"), 1)
        lines = dart.splitlines()
        self.assertEqual(lines[-2], "                    launchUrlString('https://rustdesk.com');")
        self.assertEqual(lines[-1], "                    launchUrlString('https://example.com');")
        self.assertNotIn("privacy.html", dart)

    def test_cross_field_composition_keeps_anchor_mismatch_fail_closed(self):
        # The composition fix must not weaken fail-closed source-drift detection: a
        # missing anchor still aborts, even while another field introduces a sibling
        # anchor's text.
        result, _ = self._run(
            privacy="https://rustdesk.com",
            website="https://example.com",
            mutate={
                "flutter/lib/desktop/pages/install_page.dart": "'https://rustdesk.com/other.html'),\n",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("anchor mismatch", result.stdout)

    def test_genuine_anchor_mismatch_still_fails_closed(self):
        # Guard the drift direction of the post-apply change: a source file that no longer
        # carries the anchored literal must still abort, so the corrected verification did
        # not weaken fail-closed behavior for a substring-containing branding value.
        result, _ = self._run(
            company="Purslane Tech Pte. Ltd. (Singapore)",
            mutate={"Cargo.toml": "LegalCopyright = \"changed\"\n"},
        )
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

    def test_generated_ico_is_byte_identical_to_the_pre_change_baseline(self):
        # A digest of the ICO the unchanged build consumer generates from a fixed 32x32
        # 8-bit non-interlaced icon, captured before the decoder was widened. The generated
        # ICO is a deterministic function of the decoded BGRA, so this pins the whole
        # validator->ICO path to byte-identical output for an already-accepted input.
        artifact = make_branding_artifact(icon=make_png(32, 32, 6, 0))
        repo = make_icon_repo()
        result = self._run(artifact, repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ico = (repo / "res/icon.ico").read_bytes()
        self.assertEqual(
            hashlib.sha256(ico).hexdigest(),
            "179486104c98c321d91cc7dd807e514531a3d0c3c2756f92e1fe7c1dc8cacb01",
        )

    def test_prepare_then_consumer_icon_ico_is_unchanged(self):
        # End-to-end: the byte the consumer receives is the canonical re-encode written by
        # prepare, the manifest digest covers those bytes, and the generated ICO is still the
        # pinned baseline for the same already-accepted 8-bit non-interlaced fixture.
        source = make_png(32, 32, 6, 0)
        staging = Path(tempfile.mkdtemp(prefix="branding-e2e-"))
        (staging / "app-icon.png").write_bytes(source)
        prepared = run_validator_module([str(staging)])
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        canonical = (staging / "app-icon.png").read_bytes()
        manifest = json.loads((staging / "branding_manifest.json").read_text())
        self.assertEqual(
            manifest["images"]["app-icon.png"]["sha256"], hashlib.sha256(canonical).hexdigest()
        )
        # Ship the validator alongside, exactly as the prepare job does.
        (staging / "validate_branding_png.py").write_text(validator_source(), encoding="utf-8")
        repo = make_icon_repo()
        result = self._run(staging, repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((repo / "flutter/assets/icon.png").read_bytes(), canonical)
        self.assertEqual(
            hashlib.sha256((repo / "res/icon.ico").read_bytes()).hexdigest(),
            "179486104c98c321d91cc7dd807e514531a3d0c3c2756f92e1fe7c1dc8cacb01",
        )

    def test_canonical_larger_than_the_download_cap_is_consumed_for_icon_and_logo(self):
        # Regression guard for the consumer-side misuse of the per-field DOWNLOAD cap.
        # That cap bounds only the fetched response: an indexed 1 B/px source that passes
        # the fetch cap re-encodes to 3-4 B/px, so the canonical artifact handed to the
        # consumer can legitimately be larger than the cap. The consumer must still accept
        # it and produce the ICO / app asset. A pre-fix consumer passed the download cap to
        # validator.load() and aborted here with "exceeds the byte cap".
        cap = 10 * 1024 * 1024
        canonical = incompressible_canonical_png(1630, 1630, 6)
        self.assertGreater(len(canonical), cap)
        artifact = make_branding_artifact(icon=canonical, logo=canonical)
        repo = make_icon_repo()
        result = self._run(artifact, repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("exceeds the byte cap", result.stdout)
        self.assertEqual((repo / "flutter/assets/icon.png").read_bytes(), canonical)
        self.assertEqual((repo / "flutter/assets/logo.png").read_bytes(), canonical)
        self.assertEqual((repo / "res/icon.ico").read_bytes()[:4], b"\x00\x00\x01\x00")

    def test_consumer_dimension_and_square_gates_still_fail_closed(self):
        # Removing the download cap must not disable the consumer-side gates that remain:
        # max_width/max_height are still enforced by the shared validator, and png_to_ico
        # still rejects a non-square icon after decoding.
        for label, icon, expected in (
            ("out-of-range dimension", make_png(4097, 8, 6, 0), "out-of-range dimensions"),
            ("non-square icon", make_png(100, 50, 6, 0), "must be square"),
        ):
            with self.subTest(label=label):
                artifact = make_branding_artifact(icon=icon)
                repo = make_icon_repo()
                result = self._run(artifact, repo)
                self.assertEqual(result.returncode, 1, label)
                self.assertIn(expected, result.stdout, label)

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

    def test_prepare_embeds_canonical_privacy_screen(self):
        # End-to-end: an interlaced provider image is canonicalised by prepare, and the
        # third-party overlay embeds exactly those canonical bytes (8-bit, non-interlaced).
        def rgba_fn(x, y):
            return (
                (x * 13 + y * 7 + 1) % 256,
                (x * 3 + y * 11 + 2) % 256,
                (x * 5 + y * 2 + 3) % 256,
                (x * 9 + y * 4 + 5) % 256,
            )

        interlaced = png_bytes(13, 9, 6, 8, rgba_fn, interlace=1)
        runner_temp = Path(tempfile.mkdtemp(prefix="branding-privacy-e2e-"))
        artifact = runner_temp / "deskforge-privacy-screen"
        artifact.mkdir()
        (artifact / "privacy-screen.png").write_bytes(interlaced)
        prepared = run_validator_module([str(artifact)])
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        (artifact / "validate_branding_png.py").write_text(validator_source(), encoding="utf-8")
        canonical = (artifact / "privacy-screen.png").read_bytes()
        self.assertEqual(png_header(canonical)[6], 0)
        repo = self._make_repo()
        result = subprocess.run(
            ["bash", "-s"], input=self.privacy_shell(), text=True,
            capture_output=True, cwd=str(repo),
            env={**os.environ, "RUNNER_TEMP": str(runner_temp)},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        img_cpp = (repo / "third_party/windows/RustDeskTempTopMostWindow/WindowInjection/img.cpp").read_text(encoding="ascii")
        match = re.search(r"const unsigned char g_img\[\] = \{(.*?)\};", img_cpp, re.DOTALL)
        self.assertIsNotNone(match)
        embedded = bytes(int(h, 16) for h in re.findall(r"0x[0-9a-fA-F]{2}", match.group(1)))
        self.assertEqual(embedded, canonical)
        self.assertEqual(png_header(embedded)[2], 8)
        self.assertEqual(png_header(embedded)[3], 6)
        self.assertEqual(png_header(embedded)[6], 0)

    def test_validator_accepts_a_canonical_over_the_download_cap(self):
        # The privacy overlay is a canonical artifact whose size can legitimately exceed
        # the 20 MiB per-field DOWNLOAD cap (that cap bounds only the fetched response: an
        # indexed 1 B/px source that passes the fetch cap re-encodes to 3-4 B/px). Run the
        # exact validator argv the consumer shell uses, read from the workflow, so a future
        # edit that re-adds --max-bytes fails here as well as in the shell run.
        shell = self.privacy_shell()
        match = re.search(r'python3 "\$validator" "\$privacy_png"([^\n;]*)', shell)
        self.assertIsNotNone(match, "privacy validator invocation not found in the workflow")
        argv = match.group(1).split()
        self.assertNotIn("--max-bytes", argv)
        canonical = incompressible_canonical_png(2660, 2660, 2)
        self.assertGreater(len(canonical), 20 * 1024 * 1024)
        directory = Path(tempfile.mkdtemp(prefix="branding-privacy-cap-"))
        validator = directory / "validate_branding_png.py"
        validator.write_text(validator_source(), encoding="utf-8")
        png = directory / "privacy-screen.png"
        png.write_bytes(canonical)
        result = subprocess.run(
            [sys.executable, str(validator), str(png), *argv],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("exceeds the byte cap", result.stdout)

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
    def test_consumers_do_not_reapply_the_download_cap_to_the_canonical_artifact(self):
        # The per-field byte cap is a fetch-time bound on the downloaded response, never a
        # validity gate on the locally generated canonical artifact. No consumer site may
        # pass it to the shared validator again; the canonical bytes are bounded by their
        # geometry and pinned by the sha256 manifest entry instead.
        icon_python = BuildConsumerTests.icon_python()
        loads = re.findall(r"validator\.load\(([^)]*)\)", icon_python)
        self.assertTrue(loads, "no validator.load() call found in the icon/logo consumer")
        for call in loads:
            self.assertNotIn("max_bytes", call)
        invocations = re.findall(
            r'python3 "\$validator" "\$privacy_png"([^\n;]*)',
            PrivacyConsumerTests.privacy_shell(),
        )
        self.assertEqual(len(invocations), 1, "expected one privacy validator invocation")
        self.assertNotIn("--max-bytes", invocations[0])

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

    def test_branding_fetch_is_destination_pinned_through_the_shipped_helper(self):
        # The fetch boundary is the shipped helper, not a URL string check: one
        # authoritative non-public address list, a policy seam defaulting to the
        # production policy, per-hop pinned connections and TLS on the URL
        # hostname. Every fetch call site and its per-field cap is unchanged.
        run = _prepare_run()
        function = fetch_shell_function()
        self.assertIn('python3 "$fetcher"', function)
        self.assertNotIn("curl", function)
        self.assertIn('fetcher="${RUNNER_TEMP}/fetch_branding_image.py"', run)
        self.assertIn('chmod 700 "$fetcher"', run)
        source = fetcher_source()
        self.assertIn("_NON_PUBLIC_NETWORKS", source)
        self.assertIn("allow_address=is_public_address", source)
        self.assertIn("server_hostname", source)
        for call in (
            'fetch_branding_image app_icon_url "$RQS_APP_ICON_URL" "$staging/app-icon.png" $((10 * 1024 * 1024))',
            'fetch_branding_image app_logo_url "$RQS_APP_LOGO_URL" "$staging/app-logo.png" $((10 * 1024 * 1024))',
            'fetch_branding_image privacy_screen_url "$RQS_PRIVACY_SCREEN_URL" "$staging/privacy-screen.png" $((20 * 1024 * 1024))',
        ):
            self.assertIn(call, run)

    def test_fetch_policy_is_a_registry_derived_allow_list_with_a_recorded_source(self):
        # Blocking review finding: the IPv6 bound must be an allow list derived from
        # the IANA Global Unicast Address Assignments registry, not a deny list whose
        # complement is not public. The registry source, its retrieval date and the
        # fail-closed snapshot property are recorded next to the policy; the partially
        # allocated 2001::/23 parent is represented by its six reachable more-specifics
        # only, and the special-purpose overlay keeps its refusals on top.
        source = fetcher_source()
        self.assertIn("ipv6-unicast-address-assignments.csv", source)
        self.assertIn("retrieved 2026-10-14", source)
        self.assertIn("fail-closed", source)
        self.assertNotIn("_IPV6_REACHABLE_EXCEPTIONS", source)
        start = source.index("_IPV6_ALLOCATED_PREFIXES = (")
        end = source.index("\n)", start)
        allow = source[start:end]
        # The parent row is not re-admitted; its six reachable more-specifics are.
        self.assertNotIn('"2001::/23"', allow)
        for specific in (
            "2001:1::1/128", "2001:1::2/128", "2001:1::3/128", "2001:3::/32",
            "2001:4:112::/48", "2001:30::/28",
        ):
            self.assertIn(specific, allow)
        for row in ("2001:200::/23", "2001:2000::/19", "2400::/12", "2600::/12",
                    "2a00::/12", "2c00::/12"):
            self.assertIn('"' + row + '"', allow)
        # RESERVED rows are not allocated and must not be in the allow list.
        for reserved in ("2d00::/8", "3ffe::/16", "3ffc::/15", "3fff::/20"):
            self.assertNotIn('"' + reserved + '"', allow)
        # The special-purpose overlay stays applied on top of the allow list.
        for denied in (
            "2001::/32", "2001:2::/48", "2001:10::/28", "2001:20::/28",
            "2001:db8::/32", "2002::/16", "64:ff9b::/96", "64:ff9b:1::/48",
            "::/96", "3fff::/20",
        ):
            self.assertIn('"' + denied + '"', source)

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
        # The RustDesk display-name rename is a single mechanism owned by one
        # dedicated step; the DFP1 company-name step must not duplicate it, and
        # there must be no env-default fallback that renames the product to
        # "RustDesk".
        text = WINDOWS.read_text(encoding="utf-8")
        rename_runs = [
            step["run"] for step in _steps(WINDOWS)
            if step.get("name") == "L3 brand: rename RustDesk → app_name"
            and isinstance(step.get("run"), str)
        ]
        self.assertEqual(len(rename_runs), 1, "expected exactly one RustDesk rename step")
        needle = "RustDesk Remote Desktop"
        self.assertGreater(text.count(needle), 0, "RustDesk display-name rename disappeared")
        # Every RustDesk display-name replacement literal must live inside that
        # one step's run block; any occurrence elsewhere is a second, conflicting
        # rename mechanism.
        self.assertEqual(text.count(needle), rename_runs[0].count(needle))
        self.assertNotIn("RQS_COMPANY_NAME:-RustDesk", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
