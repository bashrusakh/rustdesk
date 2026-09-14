#!/usr/bin/env python3
"""Verify a Cargo vendor directory or archive without network or file writes.

Directory mode is bound to the recorded ``vendor_tree.tree_sha256``. The digest
is the SHA-256 of sorted records in the form
``vendor/<path>\\0file\\0SHA256(file-bytes)\\n``. This is deliberately the
same file-only digest for a directory and an archive after unpacking, so a
directory cannot be accepted from package counts or mutable Cargo checksums
alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".cargo" / "config.vendor.toml"
LOCK_PATH = ROOT / "Cargo.lock"
VENDORED_SOURCE = "vendored-sources"
THIRD_PARTY = ROOT / "third_party"
PROVENANCE_PATH = ROOT / "plans" / "upstream-independent-build" / "vendor-provenance.json"
COPIED_PACKAGE_ROOT_COUNT = 44
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class VerificationError(Exception):
    """A contract validation failure."""


class VendorFiles:
    """Read-only view over a vendor directory or a tar archive."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.archive: tarfile.TarFile | None = None
        self.archive_contents: dict[str, bytes] = {}
        self.files: set[str] = set()
        self.package_roots: set[str] = set()
        self.member_count = 0
        if not path.exists() and not path.is_symlink():
            raise VerificationError(f"vendor source does not exist: {path}")
        if self._is_link_like(path):
            raise VerificationError(f"vendor source root is a symlink/reparse point: {path}")
        if path.is_dir():
            self._read_directory()
        elif path.is_file():
            try:
                self.archive = tarfile.open(path, "r:*")
            except (tarfile.TarError, OSError, UnicodeError) as error:
                raise VerificationError(f"cannot read vendor archive {path}: {error}") from error
            self._read_archive()
        else:
            raise VerificationError(f"vendor source does not exist: {path}")

    @staticmethod
    def _is_link_like(path: Path) -> bool:
        metadata = path.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & reparse_flag
        )

    @staticmethod
    def _safe_parts(name: str) -> tuple[str, ...]:
        if not isinstance(name, str):
            raise VerificationError(f"archive member name is not text: {name!r}")
        try:
            encoded = name.encode("utf-8")
            if encoded.decode("utf-8") != name:
                raise VerificationError(f"archive member name is not UTF-8 round-tripping: {name!r}")
        except UnicodeError as error:
            raise VerificationError(f"archive member name is not valid UTF-8: {name!r}") from error
        if not name or "\x00" in name or "\\" in name:
            raise VerificationError(f"unsafe path in vendor archive: {name!r}")
        pure = PurePosixPath(name)
        if pure.is_absolute() or any(part in {".", ".."} for part in pure.parts):
            raise VerificationError(f"unsafe path in vendor archive: {name}")
        return pure.parts

    def _add_member(self, name: str, *, is_file: bool) -> str:
        parts = self._safe_parts(name)
        if parts == ("vendor",):
            if is_file:
                raise VerificationError(f"vendor root is not a file: {name}")
            return "/".join(parts)
        if len(parts) < 2 or parts[0] != "vendor" or not parts[1]:
            raise VerificationError(f"archive must contain only vendor/<package> paths: {name}")
        relative = "/".join(parts)
        if relative in self.files:
            raise VerificationError(f"vendor archive contains duplicate member: {name}")
        self.package_roots.add(parts[1])
        if is_file:
            self.files.add(relative)
        return relative

    def _read_directory(self) -> None:
        pending = [self.path]
        root = self.path.resolve()
        while pending:
            current = pending.pop()
            for child in sorted(current.iterdir()):
                if self._is_link_like(child):
                    raise VerificationError(f"vendor source contains unsupported link/reparse point: {child}")
                child_stat = child.lstat()
                if current == self.path and not stat.S_ISDIR(child_stat.st_mode):
                    raise VerificationError(f"vendor source has unexpected top-level entry: {child.name}")
                try:
                    child.resolve().relative_to(root)
                except ValueError as error:
                    raise VerificationError(f"vendor source entry escapes root: {child}") from error
                if current == self.path:
                    self.package_roots.add(child.name)
                if stat.S_ISDIR(child_stat.st_mode):
                    pending.append(child)
                elif stat.S_ISREG(child_stat.st_mode):
                    relative = child.relative_to(self.path.parent).as_posix()
                    self._safe_parts(relative)
                    self.files.add(relative)
                else:
                    raise VerificationError(f"vendor source contains unsupported entry: {child}")
        self.member_count = len(self.files) + sum(
            1
            for path in self.path.rglob("*")
            if path.is_dir() and not self._is_link_like(path)
        )

    def _read_archive(self) -> None:
        assert self.archive is not None
        try:
            for member in self.archive:
                self.member_count += 1
                self._safe_parts(member.name)
                if member.isdir():
                    self._add_member(member.name.rstrip("/"), is_file=False)
                elif member.isfile():
                    relative = self._add_member(member.name, is_file=True)
                    extracted = self.archive.extractfile(member)
                    if extracted is None:
                        raise VerificationError(f"vendor archive member is not readable: {member.name}")
                    self.archive_contents[relative] = extracted.read()
                elif member.issym() or member.islnk():
                    raise VerificationError(f"archive contains unsupported link: {member.name}")
                else:
                    raise VerificationError(f"archive contains unsupported member: {member.name}")
        except VerificationError:
            raise
        except (OSError, UnicodeError, tarfile.TarError) as error:
            raise VerificationError(f"cannot safely read vendor archive member metadata: {error}") from error

    def has(self, relative: str) -> bool:
        if self.archive is not None:
            return relative in self.files
        return (self.path.parent / relative).is_file()

    def read(self, relative: str) -> bytes:
        if self.archive is not None:
            try:
                return self.archive_contents[relative]
            except KeyError as error:
                raise VerificationError(f"vendor archive member is not a file: {relative}") from error
        candidate = (self.path.parent / relative).resolve()
        if self.path.parent.resolve() not in candidate.parents:
            raise VerificationError(f"unsafe vendor path: {relative}")
        return candidate.read_bytes()

    def package_files(self, root: str) -> set[str]:
        prefix = f"vendor/{root}/"
        return {
            relative[len(prefix) :]
            for relative in self.files
            if relative.startswith(prefix) and relative != prefix + ".cargo-checksum.json"
        }

    def tree_sha256(self) -> str:
        """Return the deterministic file tree digest shared by dir/archive mode."""
        records = []
        for relative in sorted(self.files):
            digest = hashlib.sha256(self.read(relative)).hexdigest()
            records.append(f"{relative}\0file\0{digest}\n".encode("utf-8"))
        return hashlib.sha256(b"".join(records)).hexdigest()

    def close(self) -> None:
        if self.archive is not None:
            self.archive.close()


def fail(message: str) -> None:
    raise VerificationError(message)


def package_path(vendor: VendorFiles, name: str, version: str) -> str:
    candidates = [name, f"{name}-{version}"]
    for candidate in candidates:
        if candidate not in vendor.package_roots:
            continue
        manifest = vendor.read(f"vendor/{candidate}/Cargo.toml")
        try:
            import tomllib

            package = tomllib.loads(manifest.decode("utf-8")).get("package", {})
        except (UnicodeDecodeError, ValueError) as error:
            fail(f"invalid Cargo.toml for vendor package {candidate}: {error}")
        if package.get("name") == name and package.get("version") == version:
            return candidate
    fail(f"missing vendor package directory for {name} {version}")


def package_metadata(vendor: VendorFiles, root: str) -> tuple[dict[str, Any], dict[str, Any]]:
    prefix = f"vendor/{root}/"
    cargo_toml = prefix + "Cargo.toml"
    checksum = prefix + ".cargo-checksum.json"
    if not vendor.has(cargo_toml):
        fail(f"vendor package {root} is missing Cargo.toml")
    if not vendor.has(checksum):
        fail(f"vendor package {root} is missing .cargo-checksum.json")
    try:
        import tomllib

        manifest = tomllib.loads(vendor.read(cargo_toml).decode("utf-8"))
        checksum_data = json.loads(vendor.read(checksum).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        fail(f"invalid metadata for vendor package {root}: {error}")
    if not isinstance(checksum_data, dict) or not isinstance(checksum_data.get("files"), dict):
        fail(f"checksum metadata for {root} must contain a files object")
    return manifest, checksum_data


def verify_package_files(vendor: VendorFiles, root: str, checksum_data: dict[str, Any]) -> None:
    files = checksum_data.get("files")
    if not isinstance(files, dict):
        fail(f"checksum metadata for {root} must contain a files object")
    actual = vendor.package_files(root)
    expected: set[str] = set()
    for relative, digest in files.items():
        if not isinstance(relative, str) or not relative or "\\" in relative:
            fail(f"checksum metadata for {root} contains an unsafe file name: {relative!r}")
        parts = PurePosixPath(relative).parts
        if PurePosixPath(relative).is_absolute() or ".." in parts or parts == (".",):
            fail(f"checksum metadata for {root} contains an unsafe file name: {relative!r}")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            fail(f"checksum metadata for {root} contains an invalid SHA-256 for {relative}")
        expected.add(relative)
        if relative not in actual:
            fail(f"vendor package {root} is missing checksum-listed file: {relative}")
        actual_digest = hashlib.sha256(vendor.read(f"vendor/{root}/{relative}")).hexdigest()
        if actual_digest != digest:
            fail(f"vendor package {root} checksum mismatch for {relative}")
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        fail(f"vendor package {root} checksum file inventory mismatch; missing={missing[:10]}, extra={extra[:10]}")


def copied_packages(lock: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Return the tracked package roots copied from the former git sources."""
    path_packages = {
        (package["name"], package["version"]): package
        for package in lock.get("package", [])
        if "source" not in package
    }
    copied: list[tuple[dict[str, Any], str]] = []
    if not THIRD_PARTY.is_dir():
        fail(f"copied package root directory is missing: {THIRD_PARTY}")
    for manifest_path in sorted(THIRD_PARTY.glob("*/Cargo.toml")):
        if VendorFiles._is_link_like(manifest_path.parent):
            fail(f"copied package root is a symlink/reparse point: {manifest_path.parent}")
        try:
            import tomllib

            metadata = tomllib.loads(manifest_path.read_text(encoding="utf-8")).get("package", {})
        except (OSError, ValueError) as error:
            fail(f"cannot parse copied package manifest {manifest_path}: {error}")
        identity = (metadata.get("name"), metadata.get("version"))
        package = path_packages.get(identity)
        if package is None:
            fail(f"copied package {manifest_path.parent.name} is not a path package in Cargo.lock")
        copied.append((package, manifest_path.parent.name))
    return copied


def copied_root_contract(
    provenance: dict[str, Any], copied: list[tuple[dict[str, Any], str]]
) -> tuple[dict[str, list[str]], set[str]]:
    """Load the explicit local 44-root contract and its documented omissions."""
    contract = provenance.get("copied_root_contract")
    if not isinstance(contract, dict):
        fail("vendor provenance has no copied_root_contract")
    roots = contract.get("roots")
    if not isinstance(roots, list) or not all(isinstance(root, str) and root for root in roots):
        fail("copied_root_contract.roots must be a list of root names")
    if len(roots) != COPIED_PACKAGE_ROOT_COUNT or len(set(roots)) != len(roots):
        fail(f"copied_root_contract must contain exactly {COPIED_PACKAGE_ROOT_COUNT} unique roots")
    actual = {root for _, root in copied}
    if set(roots) != actual:
        fail(f"copied-root contract mismatch; missing={sorted(set(roots) - actual)}, extra={sorted(actual - set(roots))}")
    omissions = contract.get("allowed_omissions", {})
    if not isinstance(omissions, dict):
        fail("copied_root_contract.allowed_omissions must be a mapping")
    normalized: dict[str, list[str]] = {}
    for root, paths in omissions.items():
        if root not in actual or not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            fail(f"invalid copied-root omission contract for {root!r}")
        normalized[root] = paths
    rewrites = contract.get("manifest_rewrites", [])
    if not isinstance(rewrites, list) or not all(isinstance(root, str) for root in rewrites):
        fail("copied_root_contract.manifest_rewrites must be a list of root names")
    if not set(rewrites) <= actual:
        fail(f"manifest rewrite contract names unknown roots: {sorted(set(rewrites) - actual)}")
    return normalized, set(rewrites)


def verify_copied_root(
    vendor: VendorFiles,
    copied_root: str,
    external_root: str,
    omissions: list[str],
    manifest_rewritten: bool,
) -> None:
    """Compare a copied path package with its external vendor package.

    Cargo.toml is the one intentionally rewritten file: path-owned family
    members cannot retain their upstream git/workspace relationships. Every
    other file must have identical bytes and inventory, except for the exact
    cleanup paths recorded in vendor provenance.
    """
    local_root = THIRD_PARTY / copied_root
    if VendorFiles._is_link_like(local_root):
        fail(f"copied package root is a symlink/reparse point: {local_root}")
    local_files: set[str] = set()
    for path in local_root.rglob("*"):
        if VendorFiles._is_link_like(path):
            fail(f"copied package {copied_root} contains a symlink/reparse point: {path}")
        if path.is_file():
            local_files.add(path.relative_to(local_root).as_posix())
    local_files.discard(".cargo-checksum.json")
    external_files = vendor.package_files(external_root)
    omission_set = set(omissions)
    if len(omission_set) != len(omissions):
        fail(f"copied-root omission contract repeats a path for {copied_root}")
    unknown_omissions = sorted(omission_set - external_files)
    if unknown_omissions:
        fail(f"copied-root omission contract names absent external files for {copied_root}: {unknown_omissions}")
    expected = external_files - omission_set
    missing = sorted(expected - local_files)
    extra = sorted(local_files - expected)
    if missing or extra:
        fail(f"copied package {copied_root} file inventory mismatch; missing={missing[:10]}, extra={extra[:10]}")
    external_manifest = vendor.read(f"vendor/{external_root}/Cargo.toml")
    local_manifest = (local_root / "Cargo.toml").read_bytes()
    if local_manifest != external_manifest and not manifest_rewritten:
        fail(f"copied package {copied_root} Cargo.toml differs without a contract entry")
    if local_manifest == external_manifest and manifest_rewritten:
        fail(f"copied package {copied_root} is listed as rewritten but Cargo.toml is unchanged")
    external_checksum = f"vendor/{external_root}/.cargo-checksum.json"
    checksum_data: dict[str, Any] | None = None
    if vendor.has(external_checksum):
        try:
            loaded = json.loads(vendor.read(external_checksum).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            fail(f"invalid checksum metadata for external package {external_root}: {error}")
        if not isinstance(loaded, dict) or not isinstance(loaded.get("files"), dict):
            fail(f"checksum metadata for external package {external_root} must contain a files object")
        checksum_data = loaded
    for relative in sorted(expected):
        if relative == "Cargo.toml":
            continue
        local = (local_root / relative).read_bytes()
        external = vendor.read(f"vendor/{external_root}/{relative}")
        if local != external:
            fail(f"copied package {copied_root} content mismatch for {relative}")
        if checksum_data is not None:
            expected_digest = checksum_data["files"].get(relative)
            if not isinstance(expected_digest, str) or SHA256.fullmatch(expected_digest) is None:
                fail(f"external checksum metadata for {external_root} has no valid SHA-256 for {relative}")
            if hashlib.sha256(local).hexdigest() != expected_digest:
                fail(f"copied package {copied_root} checksum mismatch for {relative}")


def read_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib

        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        fail(f"cannot parse {path}: {error}")


def load_lock() -> dict[str, Any]:
    return read_toml(LOCK_PATH)


def load_provenance() -> dict[str, Any]:
    try:
        data = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"cannot parse vendor provenance {PROVENANCE_PATH}: {error}")
    if not isinstance(data, dict):
        fail("vendor provenance must be a JSON object")
    return data


def verify_provenance(lock: dict[str, Any], vendor: VendorFiles) -> None:
    provenance = load_provenance()
    if hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest() != provenance.get("cargo_lock_sha256"):
        fail("Cargo.lock SHA-256 does not match vendor provenance")
    if hashlib.sha256((ROOT / "Cargo.toml").read_bytes()).hexdigest() != provenance.get("cargo_toml_sha256"):
        fail("Cargo.toml SHA-256 does not match vendor provenance")
    copied = copied_packages(lock)
    copied_count = len(copied)
    if copied_count != provenance.get("copied_package_root_count"):
        fail("copied package root count does not match vendor provenance")
    active_git_count = sum(
        1 for package in lock.get("package", []) if package.get("source", "").startswith("git+")
    )
    if active_git_count != provenance.get("active_git_source_count"):
        fail("active git source count does not match vendor provenance")
    omissions, manifest_rewrites = copied_root_contract(provenance, copied)
    for package, copied_root in copied:
        external_root = package_path(vendor, package["name"], package["version"])
        verify_copied_root(
            vendor,
            copied_root,
            external_root,
            omissions.get(copied_root, []),
            copied_root in manifest_rewrites,
        )
    if vendor.archive is not None:
        expected = provenance.get("vendor_archive")
        if not isinstance(expected, dict):
            fail("vendor provenance has no vendor_archive record")
        archive_sha = hashlib.sha256(vendor.path.read_bytes()).hexdigest()
        if archive_sha != expected.get("sha256"):
            fail("vendor archive SHA-256 does not match vendor provenance")
        for key, actual in (("file_count", len(vendor.files)), ("package_count", len(vendor.package_roots)), ("member_count", vendor.member_count)):
            if actual != expected.get(key):
                fail(f"vendor archive {key} does not match provenance: expected {expected.get(key)}, got {actual}")
    else:
        expected = provenance.get("vendor_tree")
        if not isinstance(expected, dict):
            fail("vendor provenance has no vendor_tree record")
        tree_sha = expected.get("tree_sha256")
        if not isinstance(tree_sha, str) or SHA256.fullmatch(tree_sha) is None:
            fail("vendor tree provenance has no valid tree_sha256 record")
        actual_tree_sha = vendor.tree_sha256()
        if actual_tree_sha != tree_sha:
            fail(f"vendor tree tree_sha256 does not match provenance: expected {tree_sha}, got {actual_tree_sha}")
        for key, actual in (("file_count", len(vendor.files)), ("package_count", len(vendor.package_roots))):
            if actual != expected.get(key):
                fail(f"vendor tree {key} does not match provenance: expected {expected.get(key)}, got {actual}")


def verify_config(lock: dict[str, Any]) -> None:
    config = read_toml(CONFIG_PATH)
    source_table = config.get("source")
    if not isinstance(source_table, dict):
        fail(f"{CONFIG_PATH} has no [source] table")

    locked_sources = {
        package["source"]
        for package in lock.get("package", [])
        if package.get("source")
    }
    git_sources = sorted(source for source in locked_sources if source.startswith("git+"))
    if git_sources:
        fail(f"Cargo.lock still contains active git sources: {git_sources[:20]}")
    unsupported_sources = sorted(
        source for source in locked_sources if not source.startswith("registry+")
    )
    if unsupported_sources:
        fail(f"Cargo.lock contains unsupported external sources: {unsupported_sources[:20]}")
    expected_sources = {"crates-io", VENDORED_SOURCE}
    actual_sources = set(source_table)
    if actual_sources != expected_sources:
        missing = sorted(expected_sources - actual_sources)
        extra = sorted(actual_sources - expected_sources)
        fail(f"source mapping mismatch; missing={missing}, extra={extra}")

    for source in sorted(expected_sources - {VENDORED_SOURCE}):
        definition = source_table[source]
        if definition.get("replace-with") != VENDORED_SOURCE:
            fail(f"source {source!r} is not replaced by {VENDORED_SOURCE!r}")

    vendor_definition = source_table[VENDORED_SOURCE]
    directory = vendor_definition.get("directory")
    if not isinstance(directory, str) or not directory or Path(directory).is_absolute():
        fail("vendored-sources.directory must be a non-empty relative path")
    if directory != "vendor":
        fail(f"vendored-sources.directory must be exactly 'vendor', got {directory!r}")
    if "replace-with" in vendor_definition:
        fail("vendored-sources must be the terminal source and cannot replace another source")
    if config.get("net", {}).get("offline") is not True:
        fail("[net].offline must be true")


def verify_packages(lock: dict[str, Any], vendor: VendorFiles) -> tuple[int, int]:
    registry = [
        package
        for package in lock.get("package", [])
        if package.get("source", "").startswith("registry+")
    ]
    expected_roots: set[str] = set()
    for package in registry:
        name = package["name"]
        version = package["version"]
        root = package_path(vendor, name, version)
        expected_roots.add(root)
        manifest, checksum_data = package_metadata(vendor, root)
        metadata = manifest.get("package", {})
        if metadata.get("name") != name or metadata.get("version") != version:
            fail(f"vendor package {root} metadata does not match lock package {name} {version}")
        if checksum_data.get("package") != package.get("checksum"):
            fail(f"registry checksum mismatch for {name} {version}")
        verify_package_files(vendor, root, checksum_data)

    copied = copied_packages(lock)
    if len(copied) != COPIED_PACKAGE_ROOT_COUNT:
        fail(
            f"expected {COPIED_PACKAGE_ROOT_COUNT} copied package roots in {THIRD_PARTY}, "
            f"found {len(copied)}"
        )
    for package, root in copied:
        expected_roots.add(root)
        manifest, checksum_data = package_metadata(vendor, root)
        metadata = manifest.get("package", {})
        if metadata.get("name") != package["name"] or metadata.get("version") != package["version"]:
            fail(f"copied package {root} metadata does not match lock package {package['name']} {package['version']}")
        verify_package_files(vendor, root, checksum_data)

    unexpected = sorted(vendor.package_roots - expected_roots)
    if unexpected:
        fail(f"vendor contains unknown/extra package directories: {unexpected[:20]}")
    return len(registry), len(copied)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vendor", type=Path, help="vendor directory or .tar/.tar.gz archive")
    args = parser.parse_args()
    if not CONFIG_PATH.is_file():
        fail(f"expected vendor config is missing: {CONFIG_PATH}")
    if not LOCK_PATH.is_file():
        fail(f"Cargo.lock is missing: {LOCK_PATH}")

    lock = load_lock()
    verify_config(lock)
    vendor = VendorFiles(args.vendor)
    try:
        verify_provenance(lock, vendor)
        registry_count, copied_count = verify_packages(lock, vendor)
    finally:
        vendor.close()
    print(
        f"verified {registry_count} Cargo.lock registry packages/checksums and "
        f"{copied_count} copied package roots from {args.vendor}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as error:
        print(f"vendor verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
