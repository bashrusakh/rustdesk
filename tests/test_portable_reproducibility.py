import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
GENERATE_PATH = ROOT / "libs" / "portable" / "generate.py"
BUILD_RS_PATH = ROOT / "libs" / "portable" / "build.rs"


def load_generate_module():
    fake_brotli = types.SimpleNamespace(compress=lambda content, quality: content)
    with mock.patch.dict(sys.modules, {"brotli": fake_brotli}):
        spec = importlib.util.spec_from_file_location("portable_generate", GENERATE_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


generate = load_generate_module()


class PortableReproducibilityTests(unittest.TestCase):
    def test_source_date_epoch_is_written_as_utc_milliseconds(self):
        with mock.patch.dict(
            os.environ,
            {"SOURCE_DATE_EPOCH": "1704067200"},
            clear=True,
        ), tempfile.TemporaryDirectory() as output:
            generate.write_app_metadata(output)
            metadata = Path(output, "app_metadata.toml").read_text()

        self.assertEqual(metadata, "timestamp = 1704067200000\n")

    def test_repeated_generation_is_byte_identical(self):
        with mock.patch.dict(
            os.environ,
            {"SOURCE_DATE_EPOCH": "0"},
            clear=True,
        ), tempfile.TemporaryDirectory() as output:
            generate.write_app_metadata(output)
            first = Path(output, "app_metadata.toml").read_bytes()
            generate.write_app_metadata(output)
            second = Path(output, "app_metadata.toml").read_bytes()

        self.assertEqual(first, second)

    def test_shuffled_traversal_has_stable_package_data_order(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as first_output, tempfile.TemporaryDirectory() as second_output:
            source_path = Path(source)
            (source_path / "z-last.txt").write_bytes(b"last")
            (source_path / "a-first.txt").write_bytes(b"first")
            (source_path / "nested").mkdir()
            (source_path / "nested" / "m-middle.txt").write_bytes(b"middle")

            real_walk = os.walk

            def shuffled_walk(path):
                for root, directories, files in real_walk(path):
                    yield root, list(reversed(directories)), list(reversed(files))

            with mock.patch.object(generate.os, "walk", side_effect=shuffled_walk):
                shuffled_table = generate.generate_md5_table(source, 5)
            generate.write_package_metadata(shuffled_table, first_output, "./z-last.txt")

            ordered_table = generate.generate_md5_table(source, 5)
            generate.write_package_metadata(ordered_table, second_output, "./z-last.txt")

            self.assertEqual(
                Path(first_output, "data.bin").read_bytes(),
                Path(second_output, "data.bin").read_bytes(),
            )

    def test_missing_epoch_uses_deterministic_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(generate.app_metadata_timestamp_ms(), 0)

    def test_wall_clock_requires_explicit_debug_opt_in(self):
        with mock.patch.dict(
            os.environ,
            {"RUSTDESK_NON_REPRODUCIBLE_DEBUG": "1"},
            clear=True,
        ), mock.patch.object(generate.time, "time_ns", return_value=1234567890123):
            self.assertEqual(generate.app_metadata_timestamp_ms(), 1234567)

    def test_invalid_epoch_does_not_echo_input(self):
        secret_value = "not-a-valid-epoch-secret"
        with mock.patch.dict(
            os.environ,
            {"SOURCE_DATE_EPOCH": secret_value},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "signed Unix timestamp") as raised:
                generate.app_metadata_timestamp_ms()

        self.assertNotIn(secret_value, str(raised.exception))

    def test_pack_metadata_reruns_when_app_metadata_changes(self):
        build_rs = BUILD_RS_PATH.read_text()
        self.assertIn(
            'println!("cargo:rerun-if-changed=app_metadata.toml");',
            build_rs,
        )

    def test_generator_has_no_unconditional_wall_clock_timestamp(self):
        source = GENERATE_PATH.read_text()
        self.assertNotIn("datetime.now", source)
        self.assertIn('os.environ.get("SOURCE_DATE_EPOCH")', source)
        self.assertIn("RUSTDESK_NON_REPRODUCIBLE_DEBUG", source)
        self.assertIn('== "1"', source)


if __name__ == "__main__":
    unittest.main()
