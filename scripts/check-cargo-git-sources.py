#!/usr/bin/env python3
"""Fail if active Cargo manifests or lockfiles retain git sources."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = sorted(ROOT.glob("**/Cargo.toml"))
LOCKS = sorted(ROOT.glob("**/Cargo.lock"))


def cargo_configs() -> list[Path]:
    """Discover Cargo config files instead of treating a glob as a filename."""
    config_root = ROOT / ".cargo"
    if not config_root.is_dir():
        return []
    return sorted(
        path
        for path in config_root.rglob("*")
        if path.is_file()
        and (
            path.name in {"config", "config.toml"}
            or (path.name.startswith("config.") and path.name.endswith(".toml"))
        )
    )


CONFIGS = cargo_configs()
DEPENDENCY_TABLES = {"dependencies", "dev-dependencies", "build-dependencies"}
SOURCE_TABLES = DEPENDENCY_TABLES | {"workspace.dependencies", "workspace.dev-dependencies", "workspace.build-dependencies"}
GIT_SOURCE = re.compile(r"^git(?:\+|$)")
URL_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
CARGO_SOURCE_CONFIG_KEYS = {"replace-with", "registry", "local-registry", "directory", "git"}
SAFE_SOURCE_KEYS = {
    "patch": {"crates-io"},
    "replace": {"crates-io"},
    "source": {"crates-io", "vendored-sources"},
}
SOURCE_BEARING_KEYS = {
    "branch",
    "default-features",
    "directory",
    "features",
    "git",
    "local-registry",
    "optional",
    "path",
    "package",
    "registry",
    "replace-with",
    "rev",
    "source",
    "tag",
    "version",
}


def toml_parser() -> Any:
    try:
        import tomllib
    except ModuleNotFoundError as error:
        raise RuntimeError("Python 3.11+ tomllib is required to parse Cargo metadata") from error
    return tomllib


def read_toml(path: Path) -> dict[str, Any]:
    try:
        display_path = path.relative_to(ROOT)
    except ValueError:
        display_path = path
    try:
        text = path.read_bytes().decode("utf-8")
        parsed = toml_parser().loads(text)
    except (OSError, UnicodeDecodeError, ValueError, RuntimeError) as error:
        raise RuntimeError(f"cannot parse UTF-8 TOML {display_path}: {error}") from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"TOML document is not a table: {display_path}")
    return parsed


def manifest_git_sources(data: dict[str, Any]) -> list[str]:
    failures: list[str] = []

    def source_spec(value: Any, location: str) -> None:
        if isinstance(value, str):
            if GIT_SOURCE.match(value):
                failures.append(f"{location}: contains git source {value!r}")
            return
        if not isinstance(value, dict):
            failures.append(f"{location}: unsupported Cargo source specification")
            return
        if "git" in value:
            failures.append(f"{location}: contains git = {value['git']!r}")
        if "source" in value and isinstance(value["source"], str) and GIT_SOURCE.match(value["source"]):
            failures.append(f"{location}: contains git source = {value['source']!r}")
        source_keys = {"git", "branch", "rev", "tag", "path", "version", "registry", "source", "optional", "default-features", "features", "package"}
        unknown = sorted(key for key in value if key not in source_keys)
        if unknown:
            failures.append(f"{location}: unsupported source-bearing keys: {unknown}")

    def dependency_table(value: Any, location: str) -> None:
        if not isinstance(value, dict):
            failures.append(f"{location}: dependency table is not a TOML table")
            return
        for dependency, specification in value.items():
            source_spec(specification, f"{location}.{dependency}")

    def cargo_source_config(value: Any, location: str) -> None:
        if not isinstance(value, dict):
            failures.append(f"{location}: Cargo source configuration is not a TOML table")
            return
        for key, source in value.items():
            if key not in CARGO_SOURCE_CONFIG_KEYS:
                failures.append(f"{location}.{key}: unsupported source-bearing configuration key")
            elif not isinstance(source, str):
                failures.append(f"{location}.{key}: Cargo source configuration value is not a string")
            elif key == "git":
                failures.append(f"{location}: contains git source = {source!r}")

    def has_source_bearing_value(value: Any) -> bool:
        """Identify source-bearing descendants under an arbitrary TOML table."""
        if isinstance(value, dict):
            if any(key in SOURCE_BEARING_KEYS for key in value):
                return True
            return any(has_source_bearing_value(child) for child in value.values())
        if isinstance(value, list):
            return any(has_source_bearing_value(child) for child in value)
        return False

    def validate_source_key(table_kind: str, source_name: Any, location: str) -> None:
        """Reject URL source keys unless the table has an explicit safe allowlist entry."""
        if not isinstance(source_name, str):
            failures.append(f"{location}: Cargo source key is not a string")
            return
        if URL_KEY.match(source_name) and source_name not in SAFE_SOURCE_KEYS.get(table_kind, set()):
            failures.append(
                f"{location}: URL-keyed Cargo {table_kind} source is not allowlisted: {source_name!r}"
            )

    def source_table(value: Any, location: str, table_kind: str) -> None:
        if not isinstance(value, dict):
            failures.append(f"{location}: source-bearing table is not a TOML table")
            return
        for source_name, source_table_value in value.items():
            source_location = f"{location}.{source_name}"
            validate_source_key(table_kind, source_name, source_location)
            if not isinstance(source_table_value, dict):
                failures.append(f"{source_location}: source-bearing table is not a TOML table")
                continue
            if table_kind == "source":
                cargo_source_config(source_table_value, source_location)
                continue
            if any(key in source_table_value for key in ("git", "branch", "rev", "tag", "path", "source")):
                source_spec(source_table_value, source_location)
                continue
            for package, specification in source_table_value.items():
                source_spec(specification, f"{source_location}.{package}")

    def visit(value: Any, location: str) -> None:
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{location}[{index}]")
            return
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            child_location = f"{location}.{key}" if location else str(key)
            if key in DEPENDENCY_TABLES:
                dependency_table(child, child_location)
            elif location == "workspace" and key in {"dependencies", "dev-dependencies", "build-dependencies"}:
                dependency_table(child, child_location)
            elif key in {"patch", "replace"}:
                source_table(child, child_location, key)
            elif key == "source" and (location == "" or location.startswith(".cargo")):
                source_table(child, child_location, "source")
            elif (
                isinstance(child, (dict, list))
                and URL_KEY.match(key)
                and has_source_bearing_value(child)
            ):
                validate_source_key("nested", key, child_location)
            elif isinstance(child, dict) and any(name in child for name in ("git", "branch", "rev", "tag", "source")):
                failures.append(f"{child_location}: unsupported source-bearing Cargo structure")
            elif isinstance(child, dict):
                visit(child, child_location)

    visit(data, "")
    return failures


def lock_git_sources(data: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    packages = data.get("package", [])
    if not isinstance(packages, list):
        return ["package: Cargo.lock package table is not an array"]
    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            failures.append(f"package[{index}]: package entry is not a TOML table")
            continue
        source = package.get("source")
        if isinstance(source, str) and source.startswith("git+"):
            failures.append(f"package[{index}].source: {source!r}")
        elif source is not None and not isinstance(source, str):
            failures.append(f"package[{index}].source: source is not a string")
    return failures


def main() -> int:
    failures: list[str] = []
    for manifest in [*MANIFESTS, *CONFIGS]:
        try:
            findings = manifest_git_sources(read_toml(manifest))
        except RuntimeError as error:
            failures.append(str(error))
            continue
        failures.extend(f"{manifest.relative_to(ROOT)}: {finding}" for finding in findings)
    for lock in LOCKS:
        try:
            findings = lock_git_sources(read_toml(lock))
        except RuntimeError as error:
            failures.append(str(error))
            continue
        failures.extend(f"{lock.relative_to(ROOT)}: {finding}" for finding in findings)
    if (ROOT / "vendor").exists():
        failures.append("vendor/: full registry vendor tree must remain outside this migration")
    if failures:
        print("active Cargo git-source check failed:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print(f"checked {len(MANIFESTS)} Cargo.toml files, {len(CONFIGS)} Cargo config files, and {len(LOCKS)} Cargo.lock files: no git sources")
    print("confirmed no full vendor/ tree is present")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"active Cargo git-source check failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
