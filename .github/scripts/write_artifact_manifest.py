#!/usr/bin/env python3
"""Write the shared, deterministic DeskForge producer manifest."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath

VERIFICATION_SCOPE = (
    "producer-reported source_sha, workflow_sha, workflow_ref, version, source_tree_sha, "
    "recursive submodule commits, and delivered output file names, sizes, "
    "and SHA-256 values"
)
CONTRACT = "deskforge.client-artifact-handoff-v1"
DIGEST_SCOPE = "sha256 covers public delivered output files; manifest.txt and declared private files are excluded"
MANIFEST_NAME = "manifest.txt"
PRIVATE_FILENAME = "custom_.txt"
BRIDGE_FILES = tuple(
    sorted(
        (
            "flutter/ios/Runner/bridge_generated.h",
            "flutter/lib/generated_bridge.dart",
            "flutter/lib/generated_bridge.freezed.dart",
            "flutter/macos/Runner/bridge_generated.h",
            "src/bridge_generated.io.rs",
            "src/bridge_generated.rs",
        )
    )
)


def git_output(*args: str) -> str:
    result = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def source_tree_sha() -> str:
    value = git_output("rev-parse", "HEAD^{tree}")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        raise SystemExit("source tree identity is unavailable")
    return value.lower()


def submodules() -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in git_output("submodule", "status", "--recursive").splitlines():
        if not line.strip():
            continue
        if line[0] != " ":
            raise SystemExit("recursive submodule checkout is not clean or exact")
        fields = line[1:].strip().split()
        if len(fields) < 2 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", fields[0]):
            raise SystemExit("recursive submodule identity is unavailable")
        records.append({"path": fields[1], "commit_sha": fields[0].lower()})
    records.sort(key=lambda item: item["path"])
    return records


def publication_timestamp() -> int:
    raw = os.environ.get("MANIFEST_PUBLICATION_TIMESTAMP", "")
    if not raw:
        raise SystemExit("publication timestamp is unavailable")
    try:
        value = int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError as exc:
        raise SystemExit("publication timestamp is invalid") from exc
    if value <= 0:
        raise SystemExit("publication timestamp is invalid")
    return value


def expected_names(platform: str, app_name: str, version: str) -> list[str]:
    if (
        not app_name
        or app_name in {".", ".."}
        or app_name != app_name.strip()
        or any(char in app_name for char in ("/", "\\", "\x00", "\r", "\n"))
    ):
        raise SystemExit("app_name must be a safe filename component")
    if platform == "windows":
        return [f"{app_name}.exe"]
    if platform == "linux":
        return sorted([f"{app_name}-{version}.deb", f"{app_name}-{version}-0.x86_64.rpm"])
    if platform == "android":
        return [f"{app_name}.apk"]
    if platform == "bridge":
        if app_name != "rustdesk-bridge":
            raise SystemExit("bridge producer app_name must be rustdesk-bridge")
        return list(BRIDGE_FILES)
    raise SystemExit(f"unsupported manifest platform {platform!r}")


def output_root(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SystemExit(f"manifest output directory is unavailable: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SystemExit("manifest output must be a regular directory")
    return path.resolve()


def safe_output_file(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if not safe_relative_name(name, allow_nested=True):
        raise SystemExit(f"manifest output path escapes artifact output: {name!r}")
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SystemExit(f"manifest output path escapes artifact output: {name!r}") from exc

    current = root
    for part in relative.parts:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise SystemExit(f"manifest output file is unavailable: {name!r}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit(f"manifest output contains a symlink: {name!r}")
        if current != candidate and not stat.S_ISDIR(info.st_mode):
            raise SystemExit(f"manifest output path contains a non-directory: {name!r}")
        if current == candidate and not stat.S_ISREG(info.st_mode):
            raise SystemExit(f"manifest output contains a non-regular file: {name!r}")
    return candidate


def safe_relative_name(name: str, allow_nested: bool) -> bool:
    """Return whether name is a canonical, non-escaping artifact path."""
    if (
        not name
        or name != name.strip()
        or any(char in name for char in ("\\", "\x00", "\r", "\n"))
        or re.match(r"^[A-Za-z]:", name)
        or name.startswith("/")
        or name.startswith("//")
    ):
        return False
    relative = PurePosixPath(name)
    if relative.is_absolute() or relative.as_posix() != name:
        return False
    if any(part in {"", ".", ".."} for part in relative.parts):
        return False
    return allow_nested or len(relative.parts) == 1


def validate_output_tree(root: Path, expected: set[str]) -> None:
    allowed = expected | {MANIFEST_NAME, PRIVATE_FILENAME}
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        current_path = Path(current)
        for directory in directories:
            path = current_path / directory
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SystemExit(f"manifest output contains an unsafe directory: {path}")
        for filename in files:
            path = current_path / filename
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise SystemExit(f"manifest output contains an unsafe file: {path}")
            relative = path.relative_to(root).as_posix()
            if relative not in allowed:
                raise SystemExit(f"unexpected final output file: {relative!r}")


def reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest key {key!r}")
        result[key] = value
    return result


def verify_bridge_artifact(
    root: Path,
    expected_source_sha: str,
    expected_workflow_sha: str,
    expected_workflow_ref: str,
    expected_version: str,
) -> None:
    """Verify a downloaded bridge artifact before copying files into the source tree."""
    root = output_root(root)
    expected = set(BRIDGE_FILES)
    validate_output_tree(root, expected)
    manifest_path = safe_output_file(root, MANIFEST_NAME)
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_json_keys
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"bridge artifact manifest is invalid: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SystemExit("bridge artifact manifest must be a JSON object")
    expected_manifest_keys = {
        "schema",
        "manifest_schema",
        "schema_version",
        "platform",
        "app_name",
        "output_filenames",
        "source_sha",
        "workflow_sha",
        "workflow_ref",
        "version",
        "source_tree_sha",
        "submodules",
        "digest_scope",
        "verification_scope",
        "verification_result",
        "publication_timestamp",
        "handoff_contract",
        "files",
        "private_filenames",
    }
    if set(manifest) != expected_manifest_keys:
        raise SystemExit("bridge artifact manifest schema fields are invalid")
    source_sha = manifest.get("source_sha")
    workflow_sha = manifest.get("workflow_sha")
    if not isinstance(source_sha, str) or not isinstance(workflow_sha, str):
        raise SystemExit("bridge artifact manifest identity fields are invalid")
    if (
        manifest.get("schema") != "deskforge.client-artifact"
        or manifest.get("manifest_schema") != "deskforge.client-artifact"
        or manifest.get("schema_version") != 2
        or manifest.get("platform") != "bridge"
        or manifest.get("app_name") != "rustdesk-bridge"
        or source_sha.lower() != expected_source_sha.lower()
        or workflow_sha.lower() != expected_workflow_sha.lower()
        or manifest.get("workflow_ref") != expected_workflow_ref
        or manifest.get("version") != expected_version
    ):
        raise SystemExit("bridge artifact manifest identity does not match the current workflow")
    if manifest.get("output_filenames") != list(BRIDGE_FILES) or manifest.get("private_filenames") != []:
        raise SystemExit("bridge artifact manifest output file contract is invalid")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != len(BRIDGE_FILES):
        raise SystemExit("bridge artifact manifest file records are invalid")
    for record, name in zip(records, BRIDGE_FILES, strict=True):
        if not isinstance(record, dict) or set(record) != {"name", "size", "sha256"}:
            raise SystemExit("bridge artifact manifest file record schema is invalid")
        if record["name"] != name or not safe_relative_name(name, allow_nested=True):
            raise SystemExit("bridge artifact manifest contains an unsafe nested path")
        size = record["size"]
        digest = record["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise SystemExit(f"bridge artifact file size is invalid: {name}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SystemExit(f"bridge artifact file hash is invalid: {name}")
        path = safe_output_file(root, name)
        data = path.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit(f"bridge artifact file hash mismatch: {name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform")
    parser.add_argument("--app-name")
    parser.add_argument("--version")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--verify-bridge", action="store_true")
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--expected-version")
    args = parser.parse_args()

    if args.verify_bridge:
        if not args.expected_source_sha or not args.expected_version:
            raise SystemExit("bridge verification requires expected source and version identity")
        verify_bridge_artifact(
            args.output,
            args.expected_source_sha,
            args.workflow_sha,
            args.workflow_ref,
            args.expected_version,
        )
        return

    if not args.platform or not args.app_name or not args.version:
        raise SystemExit("manifest production requires platform, app name, and version")
    output = output_root(args.output)
    names = expected_names(args.platform, args.app_name, args.version)
    private_path = output / PRIVATE_FILENAME
    if private_path.exists() or private_path.is_symlink():
        raise SystemExit("public manifest output must not contain custom_.txt")
    validate_output_tree(output, set(names))
    paths = [safe_output_file(output, name) for name in names]
    private_filenames: list[str] = []
    file_records: list[dict[str, str | int]] = []
    for name, path in zip(names, paths, strict=True):
        before = path.lstat()
        data = path.read_bytes()
        after = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or before.st_size != after.st_size
            or after.st_size != len(data)
        ):
            raise SystemExit(f"manifest output file changed during hashing: {name!r}")
        file_records.append(
            {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )

    manifest = {
        "schema": "deskforge.client-artifact",
        "manifest_schema": "deskforge.client-artifact",
        "schema_version": 2,
        "platform": args.platform,
        "app_name": args.app_name,
        "output_filenames": names,
        "source_sha": os.environ["RQS_SOURCE_SHA"],
        "workflow_sha": args.workflow_sha,
        "workflow_ref": args.workflow_ref,
        "version": args.version,
        "source_tree_sha": source_tree_sha(),
        "submodules": submodules(),
        "digest_scope": DIGEST_SCOPE,
        "verification_scope": VERIFICATION_SCOPE,
        "verification_result": "reported",
        "publication_timestamp": publication_timestamp(),
        "handoff_contract": CONTRACT,
        "files": file_records,
        "private_filenames": private_filenames,
    }
    manifest_path = output / MANIFEST_NAME
    try:
        manifest_info = manifest_path.lstat()
    except FileNotFoundError:
        manifest_info = None
    except OSError as exc:
        raise SystemExit(f"manifest output manifest.txt is unavailable: {exc}") from exc
    if manifest_info is not None and (stat.S_ISLNK(manifest_info.st_mode) or not stat.S_ISREG(manifest_info.st_mode)):
        raise SystemExit("manifest output manifest.txt is not a regular file")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    validate_output_tree(output, set(names))


if __name__ == "__main__":
    main()
