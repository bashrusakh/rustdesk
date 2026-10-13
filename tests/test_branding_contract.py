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
  F4  branding text is escaped for each concrete insertion syntax (Windows .rc vs
      TOML basic strings vs interpolated Dart single-quoted literals), so ordinary
      safe punctuation is accepted instead of blanket-rejected.

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
        self.server.recorder.connected.clear()
        self.server.recorder.connections = 0
        try:
            if self.path == "/big":
                self._send_chunked(self.BIG)
            elif self.path == "/huge":
                self._send_chunked(self.HUGE)
            elif self.path == "/redirect-ftp":
                self.send_response(302)
                self.send_header(
                    "Location", f"ftp://127.0.0.1:{self.server.recorder.server_address[1]}/x"
                )
                self.end_headers()
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


class _ConnectionRecorder(socketserver.BaseRequestHandler):
    """Raw TCP listener recording connections to a redirect target.

    Following a redirect to a non-HTTP scheme is only observable as a TCP connect to
    the redirected host. Recording that connect distinguishes "the client refused to
    follow the redirect" from "the client followed it and failed later", independently
    of the curl version's error wording.
    """

    def handle(self):
        self.server.connections += 1
        self.server.connected.set()
        try:
            # Answer with an FTP-style error banner so a client that does follow the
            # redirect fails fast instead of waiting for a client-side timeout.
            self.request.sendall(b"421 Service not available\r\n")
        except OSError:
            pass


class FetchCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        cls.recorder = _Server(("127.0.0.1", 0), _ConnectionRecorder)
        cls.recorder.connections = 0
        cls.recorder.connected = threading.Event()
        cls.server = _Server(("127.0.0.1", 0), _OversizeHandler)
        cls.server.request_done = threading.Event()
        cls.server.bytes_written = 0
        cls.server.completed = False
        cls.server.recorder = cls.recorder
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.recorder_thread = threading.Thread(target=cls.recorder.serve_forever, daemon=True)
        cls.recorder_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.recorder.shutdown()
        cls.recorder.server_close()

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

    def test_redirect_to_non_http_scheme_is_not_followed(self):
        # fetch_branding_image pins both the request and its redirects to http(s). A
        # provider URL that redirects to ftp://... must not make the runner open the ftp
        # connection: with the redirect protocol restricted, curl refuses before any
        # connection, so the recorder sees none. Without the restriction curl follows the
        # redirect (its default redirect protocol set includes ftp) and connects.
        result, out = self._fetch("/redirect-ftp", 1024 * 1024)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::", result.stdout)
        self.assertFalse(out.exists())
        self.assertFalse(
            self.server.recorder.connected.wait(1.0),
            "the fetch followed the provider redirect to a non-HTTP scheme (ftp connection attempted)",
        )


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
