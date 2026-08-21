#!/usr/bin/env python3
"""Validate owned Flutter/workflow source metadata and immutable tree digests."""

from __future__ import annotations

from pathlib import Path
import argparse
import ast
import fnmatch
import hashlib
import os
import re
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("rustdesk-org", "https://github.com/rustdesk-org/")
PUBSPEC = ROOT / "flutter/pubspec.yaml"
LOCK = ROOT / "flutter/pubspec.lock"
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.y*ml"))
OWNERSHIP = ROOT / "third_party/source-ownership.yaml"
SUBMODULE_SECTION = re.compile(r'^\[submodule "([^"]+)"\]$')
METADATA_NAMES = {".git", ".gitmodules", ".gitignore"}
GENERATED_ARTIFACT_DIRS = {
    ".dart_tool",
    ".flutter-plugins",
    ".gradle",
    ".generated",
    ".git",
    ".run",
    "Pods",
    "build",
    "ephemeral",
    "target",
}
REFERENCE_REASON = "source-preserved documentation only"
REFERENCE_FIELDS = {"path", "line", "content", "url", "reason"}
CHECKOUT_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*actions/checkout@([^\s#]+)")
FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")


def read_utf8(path: Path) -> str:
    """Read policy and source metadata independently of the host locale."""
    return path.read_text(encoding="utf-8")


def strip_yaml_comment(text: str) -> str:
    """Remove YAML comments without treating quoted URL fragments as comments."""
    quote: str | None = None
    escaped = False
    for index, character in enumerate(text):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        elif character == "#" and quote is None:
            return text[:index].rstrip()
    return text.rstrip()


def is_link_like(path: Path) -> bool:
    """Reject symlinks and Windows reparse points without following them."""
    metadata = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def relative_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def parse_scalar(value: str) -> object:
    value = value.strip()
    if not value:
        return None
    if value == "[]":
        return []
    if value in {"true", "false"}:
        return value == "true"
    if re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    if value[:1] in {"'", '"'}:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError) as error:
            raise ValueError(f"invalid quoted YAML scalar: {value}") from error
    return value


def load_policy(path: Path) -> dict:
    """Parse the small, indentation-based YAML subset used by source policy."""
    lines: list[tuple[int, str]] = []
    for raw in read_utf8(path).splitlines():
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        text = strip_yaml_comment(text)
        lines.append((len(raw) - len(raw.lstrip()), text))

    def parse_block(index: int, indent: int) -> tuple[object, int]:
        if index >= len(lines) or lines[index][0] != indent:
            raise ValueError(f"invalid YAML indentation near line {index + 1}")
        sequence = lines[index][1].startswith("-")
        result: object = [] if sequence else {}
        while index < len(lines) and lines[index][0] == indent:
            text = lines[index][1]
            if sequence:
                if not text.startswith("-") or (len(text) > 1 and text[1] != " "):
                    raise ValueError(f"mixed YAML sequence/mapping near line {index + 1}")
                item_text = text[1:].strip()
                if not isinstance(result, list):
                    raise AssertionError
                if ":" not in item_text:
                    result.append(parse_scalar(item_text))
                    index += 1
                    continue
                key, value = item_text.split(":", 1)
                item: dict[str, object] = {key.strip(): parse_scalar(value)}
                index += 1
                while index < len(lines) and lines[index][0] > indent:
                    child_indent, child_text = lines[index]
                    if child_indent != indent + 2 or ":" not in child_text:
                        raise ValueError(f"invalid YAML mapping near line {index + 1}")
                    child_key, child_value = child_text.split(":", 1)
                    if child_value.strip():
                        item[child_key.strip()] = parse_scalar(child_value)
                        index += 1
                    elif index + 1 < len(lines) and lines[index + 1][0] > child_indent:
                        item[child_key.strip()], index = parse_block(index + 1, lines[index + 1][0])
                    else:
                        item[child_key.strip()] = None
                        index += 1
                result.append(item)
            else:
                if not isinstance(result, dict) or ":" not in text:
                    raise ValueError(f"invalid YAML mapping near line {index + 1}")
                key, value = text.split(":", 1)
                if value.strip():
                    result[key.strip()] = parse_scalar(value)
                    index += 1
                elif index + 1 < len(lines) and lines[index + 1][0] > indent:
                    result[key.strip()], index = parse_block(index + 1, lines[index + 1][0])
                else:
                    result[key.strip()] = None
                    index += 1
        return result, index

    parsed, end = parse_block(0, lines[0][0])
    if end != len(lines) or not isinstance(parsed, dict):
        raise ValueError("ownership manifest must be a YAML mapping")
    return parsed


def is_reparse_point(path: Path) -> bool:
    metadata = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def is_symlink(path: Path) -> bool:
    return stat.S_ISLNK(path.lstat().st_mode)


def allowed_symlink_records(data: dict) -> dict[Path, str]:
    records: dict[Path, str] = {}
    entries = data.get("scan_policy", {}).get("allowed_symlinks", [])
    if not isinstance(entries, dict):
        raise ValueError("scan_policy.allowed_symlinks must be a mapping")
    for path_text, target in entries.items():
        if not isinstance(path_text, str) or not isinstance(target, str):
            raise ValueError("allowed_symlinks entries need path and target strings")
        path = ROOT / path_text
        key = path
        if key in records:
            raise ValueError(f"duplicate allowed symlink: {path_text}")
        records[key] = target
    return records


def allowed_policy_lines(records: dict[tuple[str, int], tuple[str, str]]) -> set[str]:
    """Return only the exact YAML scalar lines needed to describe exceptions."""
    return {
        f"content: {content!r}" if "'" not in content else f'content: "{content}"'
        for content, _ in records.values()
    } | {f"url: {url!r}" for _, url in records.values()}


def validate_symlink(path: Path, root: Path, allowed: dict[Path, str]) -> tuple[str, str]:
    """Validate one declared link and return its literal target and content SHA."""
    if is_reparse_point(path) and not is_symlink(path):
        raise ValueError(f"owned root contains a Windows reparse point: {relative_path(path)}")
    if not is_symlink(path):
        raise ValueError(f"integrity link is not a symlink: {relative_path(path)}")
    target_text = os.readlink(path)
    if allowed.get(path) != target_text:
        raise ValueError(f"owned root contains an undeclared or mismatched symlink: {relative_path(path)}")
    target = Path(target_text)
    if target.is_absolute():
        raise ValueError(f"symlink target is absolute: {relative_path(path)}")
    lexical_target = path.parent
    for part in target.parts:
        if part == ".":
            continue
        if part == "..":
            lexical_target = lexical_target.parent
            continue
        lexical_target /= part
        try:
            lexical_metadata = lexical_target.lstat()
        except OSError as error:
            raise ValueError(f"symlink target is broken: {relative_path(path)}") from error
        if is_link_like(lexical_target):
            raise ValueError(f"symlink target chains through a link: {relative_path(path)}")
    resolved_target = lexical_target.resolve(strict=False)
    try:
        resolved_target.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"symlink target escapes owned root: {relative_path(path)}") from error
    target_metadata = resolved_target.lstat()
    if is_link_like(resolved_target) or not stat.S_ISREG(target_metadata.st_mode):
        raise ValueError(f"symlink target is not a regular file: {relative_path(path)}")
    return target_text, hashlib.sha256(resolved_target.read_bytes()).hexdigest()


def owned_entries(
    root: Path,
    exclusions: set[str],
    allowed: dict[Path, str],
    pruned_dirs: set[str] | None = None,
) -> list[tuple[Path, str, str | None]]:
    """Return regular files and policy-approved links, rejecting all other links."""
    if is_link_like(root) or not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError(f"owned root is not a real directory: {relative_path(root)}")
    found: list[tuple[Path, str, str | None]] = []
    pending = [root]
    pruned_dirs = pruned_dirs or set()
    root_real = root.resolve()
    while pending:
        current = pending.pop()
        try:
            children = sorted(current.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise ValueError(f"cannot inspect owned root entry {relative_path(current)}: {error}") from error
        for path in children:
            try:
                metadata = path.lstat()
            except OSError as error:
                raise ValueError(f"cannot inspect owned root entry {relative_path(path)}: {error}") from error
            if is_link_like(path):
                if not is_symlink(path):
                    raise ValueError(f"owned root contains a Windows reparse point: {relative_path(path)}")
                target_text, target_sha = validate_symlink(path, root, allowed)
                found.append((path, "link", f"{target_text}\0{target_sha}"))
                continue
            try:
                path.resolve().relative_to(root_real)
            except ValueError as error:
                raise ValueError(f"owned root entry escapes its root: {relative_path(path)}") from error
            if stat.S_ISDIR(metadata.st_mode):
                if path.name in pruned_dirs:
                    continue
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                relative = path.relative_to(root).as_posix()
                if not any(fnmatch.fnmatch(relative, pattern) for pattern in exclusions):
                    found.append((path, "file", None))
            else:
                raise ValueError(f"owned root contains a non-regular entry: {relative_path(path)}")
    return sorted(found, key=lambda entry: entry[0].relative_to(root).as_posix())


def file_digest(path: Path) -> str:
    metadata = path.lstat()
    if is_link_like(path) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"integrity entry is not a regular file: {relative_path(path)}")
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest()


def tree_integrity(root: Path, exclusions: set[str], allowed: dict[tuple[Path, str], str]) -> tuple[int, int, str]:
    records: list[bytes] = []
    byte_count = 0
    for path, kind, link_record in owned_entries(root, exclusions, allowed):
        rel = path.relative_to(root).as_posix()
        if kind == "file":
            digest = file_digest(path)
            byte_count += path.stat().st_size
            records.append(f"{rel}\0file\0{digest}\n".encode("utf-8"))
        else:
            assert link_record is not None
            records.append(f"{rel}\0link\0{link_record}\n".encode("utf-8"))
    return len(records), byte_count, hashlib.sha256(b"".join(records)).hexdigest()


def check_integrity(data: dict, failures: list[str]) -> list[tuple[str, tuple[int, int, str]]]:
    entries = data["scan_policy"].get("integrity_roots")
    if not isinstance(entries, list) or not entries:
        failures.append("scan_policy.integrity_roots must be a non-empty list")
        return []
    roots: list[Path] = []
    allowed = allowed_symlink_records(data)
    exclusions = integrity_exclusions(data, failures)
    declared_root_paths = {
        ROOT / "third_party" / entry["path"]
        for section in (data.get("flutter", []), data.get("windows", []))
        for entry in section
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    declared_root_paths.add(ROOT / "third_party" / "hwcodec")
    for path in allowed:
        if not any(path.is_relative_to(root) for root in declared_root_paths):
            failures.append(f"allowed symlink is outside a declared owned root: {relative_path(path)}")
    actuals: list[tuple[str, tuple[int, int, str]]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            failures.append("integrity_roots entries need a path")
            continue
        rel = entry["path"]
        root = ROOT / "third_party" / rel
        if not root.is_dir():
            failures.append(f"integrity root does not exist: third_party/{rel}")
            continue
        expected = (entry.get("file_count"), entry.get("byte_count"), entry.get("tree_sha256"))
        if not isinstance(expected[0], int) or not isinstance(expected[1], int) or not isinstance(expected[2], str):
            failures.append(f"integrity root has incomplete metadata: {rel}")
            continue
        root_exclusions = {
            path[len(rel) + 1 :]
            for path in exclusions
            if path.startswith(rel + "/")
        }
        try:
            actual = tree_integrity(root, root_exclusions, allowed)
        except (OSError, ValueError) as error:
            failures.append(f"integrity traversal failed for third_party/{rel}: {error}")
            roots.append(root)
            continue
        actuals.append((rel, actual))
        if actual != expected:
            failures.append(
                f"integrity mismatch for third_party/{rel}: expected files={expected[0]} bytes={expected[1]} "
                f"tree_sha256={expected[2]}, got files={actual[0]} bytes={actual[1]} "
                f"tree_sha256={actual[2]}"
            )
        roots.append(root)

    declared = {
        ROOT / "third_party" / entry["path"]
        for section in (data.get("flutter", []), data.get("windows", []))
        for entry in section
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    declared.add(ROOT / "third_party" / "hwcodec")
    expected_roots = {
        ROOT / "third_party" / entry["path"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    if expected_roots != declared:
        failures.append("integrity_roots must cover exactly all Flutter, TopMost, and hwcodec owned roots")
    return actuals


def parse_gitmodules(path: Path) -> list[dict[str, str]]:
    declarations: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in read_utf8(path).splitlines():
        section = SUBMODULE_SECTION.match(line.strip())
        if section:
            if current is not None:
                declarations.append(current)
            current = {"name": section.group(1)}
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
    if current is not None:
        declarations.append(current)
    return declarations


def discover_nested_gitmodules(owned_roots: list[Path], failures: list[str]) -> list[Path]:
    """Find metadata only inside declared roots, pruning generated artifacts."""
    discovered: set[Path] = set()
    pending = sorted(set(owned_roots), key=lambda path: path.as_posix(), reverse=True)
    while pending:
        current = pending.pop()
        if current.name in GENERATED_ARTIFACT_DIRS:
            continue
        try:
            if is_link_like(current):
                continue
            children = sorted(current.iterdir(), key=lambda path: path.name)
        except OSError as error:
            failures.append(f"cannot inspect owned metadata root {relative_path(current)}: {error}")
            continue
        for path in children:
            if path.name in GENERATED_ARTIFACT_DIRS and path.is_dir():
                continue
            try:
                link_like = is_link_like(path)
            except OSError as error:
                failures.append(f"cannot inspect owned metadata entry {relative_path(path)}: {error}")
                continue
            if link_like:
                continue
            if path.name == ".gitmodules" and path.is_file():
                discovered.add(path)
            elif path.is_dir():
                pending.append(path)
    return sorted(discovered)


def repository_gitmodules(paths: list[Path]) -> list[Path]:
    """Return the root metadata and deterministic owned nested metadata."""
    root_metadata = ROOT / ".gitmodules"
    return ([root_metadata] if root_metadata.is_file() else []) + paths


def check_nested_gitmodules(data: dict, failures: list[str], paths: list[Path]) -> None:
    entries = data["scan_policy"].get("nested_gitmodules")
    if not isinstance(entries, list):
        failures.append("scan_policy.nested_gitmodules must be a list")
        return
    by_path = {
        entry.get("path"): entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    discovered = {relative_path(path) for path in paths}
    if set(by_path) != discovered:
        failures.append(f"nested .gitmodules classification mismatch: {sorted(discovered ^ set(by_path))}")
    for path in paths:
        entry = by_path.get(relative_path(path))
        if entry is None:
            continue
        actual = parse_gitmodules(path)
        expected = entry.get("declarations")
        if not isinstance(expected, list) or len(expected) != len(actual):
            failures.append(f"{relative_path(path)}: declaration classification does not match file")
            continue
        for record, found in zip(expected, actual):
            if not isinstance(record, dict):
                failures.append(f"{relative_path(path)}: declaration must be a mapping")
                continue
            target_path = path.parent / found.get("path", "")
            try:
                target_link_like = is_link_like(target_path)
            except FileNotFoundError:
                target_link_like = False
            except OSError as error:
                failures.append(f"{relative_path(path)}: cannot inspect submodule target {found.get('path')}: {error}")
                target_link_like = True
            target = target_path.resolve()
            if path.parent.resolve() not in target.parents:
                failures.append(f"{relative_path(path)}: submodule target escapes its repository: {found.get('path')}")
                continue
            source_files = []
            if target_link_like:
                failures.append(f"{relative_path(path)}: submodule target is a symlink/reparse point: {found.get('path')}")
            elif target_path.is_dir():
                target_root = target_path.resolve()
                for candidate in target_path.rglob("*"):
                    try:
                        if is_link_like(candidate):
                            failures.append(f"{relative_path(path)}: submodule source contains a symlink/reparse point: {candidate}")
                            continue
                        candidate.resolve().relative_to(target_root)
                    except OSError as error:
                        failures.append(f"{relative_path(path)}: cannot inspect submodule source {candidate}: {error}")
                        continue
                    except ValueError:
                        failures.append(f"{relative_path(path)}: submodule source escapes target: {candidate}")
                        continue
                    if ".git" in candidate.parts:
                        failures.append(f"{relative_path(path)}: nested .git metadata under {found.get('path')}")
                    mode = candidate.stat().st_mode
                    if stat.S_ISREG(mode):
                        if candidate.name not in METADATA_NAMES:
                            source_files.append(candidate)
                    elif not stat.S_ISDIR(mode):
                        failures.append(f"{relative_path(path)}: submodule source is not a regular file or directory: {candidate}")
            present = target_path.is_dir() and bool(source_files) and not target_link_like
            gitlink_lines = subprocess.run(
                ["git", "ls-files", "--stage", "--", relative_path(target)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout.splitlines()
            if any(line.split()[0] == "160000" for line in gitlink_lines if line.split()):
                failures.append(f"{relative_path(path)}: gitlink mode is forbidden for {found.get('path')}")
            for key in ("name", "path", "url"):
                if record.get(key) != found.get(key):
                    failures.append(f"{relative_path(path)}: classified {key} does not match")
            if record.get("source_present") is not present:
                failures.append(f"{relative_path(path)}: source_present is wrong for {found.get('path')}")
            active = record.get("active")
            if not isinstance(active, bool):
                failures.append(f"{relative_path(path)}: active classification must be boolean")
            elif active and not present:
                failures.append(f"{relative_path(path)}: active submodule target is absent: {found.get('path')}")
            elif active and not source_files:
                failures.append(f"{relative_path(path)}: active target has no source files: {found.get('path')}")
            elif not active and present:
                failures.append(f"{relative_path(path)}: inert submodule target must be absent: {found.get('path')}")
        for found in actual:
            if any(token in found.get("url", "") for token in FORBIDDEN):
                failures.append(f"{relative_path(path)}: forbidden URL in nested submodule: {found.get('url')}")


def ownership_policy() -> tuple[dict, set[Path], list[Path]]:
    data = load_policy(OWNERSHIP)
    if not isinstance(data, dict) or not isinstance(data.get("scan_policy"), dict):
        raise ValueError("ownership manifest must define a scan_policy mapping")
    policy = data["scan_policy"]

    excluded: set[Path] = set()
    for entry in policy.get("excluded_manifests", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("scan_policy.excluded_manifests entries need a path")
        path = ROOT / entry["path"]
        if not path.is_file():
            raise ValueError(f"excluded manifest does not exist: {entry['path']}")
        excluded.add(path)

    active: list[Path] = []
    for entry in policy.get("active_owned_inputs", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("scan_policy.active_owned_inputs entries need a path")
        path = ROOT / entry["path"]
        if not path.exists():
            raise ValueError(f"active owned input does not exist: {entry['path']}")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"active owned input needs a reason: {entry['path']}")
        active.append(path)
    return data, excluded, active


def yaml_files(excluded: set[Path]) -> list[Path]:
    files = [PUBSPEC, LOCK, OWNERSHIP, *WORKFLOWS]
    files.extend(sorted((ROOT / "third_party").glob("**/pubspec.y*ml")))
    files.extend(sorted((ROOT / "third_party").glob("**/pubspec.lock")))
    return list(dict.fromkeys(path for path in files if path.is_file() and path not in excluded))


def active_files(active_roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in active_roots:
        files.extend(path for path, _, _ in owned_entries(root, set(), {}))
    return list(dict.fromkeys(files))


def declared_owned_roots(data: dict) -> list[Path]:
    roots: list[Path] = []
    for section in (data.get("flutter", []), data.get("windows", [])):
        if not isinstance(section, list):
            continue
        for entry in section:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                roots.append(ROOT / "third_party" / entry["path"])
    active_inputs = data.get("scan_policy", {}).get("active_owned_inputs", [])
    if isinstance(active_inputs, list):
        for entry in active_inputs:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                roots.append(ROOT / entry["path"])
    return list(dict.fromkeys(roots))


def cargo_owned_roots() -> list[Path]:
    """Return every copied Cargo path root without consulting .gitignore."""
    return sorted(path.parent for path in (ROOT / "third_party").glob("*/Cargo.toml") if path.is_file())


def integrity_exclusions(data: dict, failures: list[str]) -> set[str]:
    exclusions = data.get("scan_policy", {}).get("integrity_exclusions")
    if not isinstance(exclusions, list) or not all(isinstance(pattern, str) for pattern in exclusions):
        failures.append("scan_policy.integrity_exclusions must be a list of exact paths")
        return set()
    configured = set(exclusions)
    if configured:
        failures.append("integrity_exclusions must be empty; copied source is retained in integrity roots")
    for path_text in configured:
        path = ROOT / "third_party" / path_text
        if path_text != path_text.strip() or any(token in path_text for token in "*?[]"):
            failures.append(f"integrity exclusion is broadened: {path_text}")
        try:
            path.relative_to(ROOT / "third_party" / "flutter/window_size")
        except ValueError:
            failures.append(f"integrity exclusion is outside window_size: {path_text}")
        if path.exists():
            try:
                metadata = path.lstat()
                text = read_utf8(path)
            except (OSError, UnicodeDecodeError) as error:
                failures.append(f"integrity exclusion cannot inspect {path_text}: {error}")
                continue
            if is_link_like(path) or not stat.S_ISREG(metadata.st_mode):
                failures.append(f"integrity exclusion is not a regular generated file: {path_text}")
            if metadata.st_mode & 0o111:
                failures.append(f"integrity exclusion is executable: {path_text}")
            if "Generated file" not in text and "Generated file." not in text:
                failures.append(f"integrity exclusion is not marked generated: {path_text}")
        try:
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", "third_party/" + path_text],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).returncode == 0
        except OSError as error:
            failures.append(f"cannot verify clean-checkout status for {path_text}: {error}")
            tracked = True
        if tracked:
            failures.append(f"integrity exclusion is tracked source input, not clean-checkout-generated: {path_text}")
    return configured


def check_workflow_checkout_pins(data: dict, failures: list[str]) -> None:
    """Require the approved immutable checkout ref in active root workflows only."""
    scan_policy = data.get("scan_policy", {})
    approved_ref = scan_policy.get("active_root_workflow_checkout_sha")
    if not isinstance(approved_ref, str) or FULL_SHA_RE.fullmatch(approved_ref) is None:
        failures.append(
            "scan_policy.active_root_workflow_checkout_sha must be an exact 40-character SHA"
        )
        return
    for workflow in WORKFLOWS:
        for line_number, line in enumerate(read_utf8(workflow).splitlines(), 1):
            match = CHECKOUT_RE.match(line)
            if match is None:
                continue
            ref = match.group(1)
            if FULL_SHA_RE.fullmatch(ref) is None:
                failures.append(
                    f"{relative_path(workflow)}:{line_number}: actions/checkout must use an exact 40-character SHA"
                )
            elif ref != approved_ref:
                failures.append(
                    f"{relative_path(workflow)}:{line_number}: actions/checkout must use the "
                    f"approved SHA {approved_ref}, got {ref}"
                )


def allowed_reference_records(data: dict, declared_roots: list[Path], failures: list[str]) -> dict[tuple[str, int], tuple[str, str]]:
    """Validate and index exact comment-only URL exceptions from policy."""
    entries = data.get("scan_policy", {}).get("allowed_reference_urls")
    if not isinstance(entries, list):
        failures.append("scan_policy.allowed_reference_urls must be a list")
        return {}
    records: dict[tuple[str, int], tuple[str, str]] = {}
    for index, entry in enumerate(entries):
        prefix = f"scan_policy.allowed_reference_urls[{index}]"
        if not isinstance(entry, dict) or set(entry) != REFERENCE_FIELDS:
            failures.append(f"{prefix} must contain exactly path, line, content, url, and reason")
            continue
        path_text = entry["path"]
        line_number = entry["line"]
        content = entry["content"]
        url = entry["url"]
        reason = entry["reason"]
        if (
            not isinstance(path_text, str)
            or not path_text
            or "\\" in path_text
            or Path(path_text).is_absolute()
            or not isinstance(line_number, int)
            or isinstance(line_number, bool)
            or line_number < 1
            or not isinstance(content, str)
            or not isinstance(url, str)
            or not isinstance(reason, str)
        ):
            failures.append(f"{prefix} has invalid field types or path/line")
            continue
        path = ROOT / path_text
        if not any(path.is_relative_to(root) for root in declared_roots):
            failures.append(f"{prefix}.path is outside a declared owned root: {path_text}")
            continue
        try:
            actual_content = read_utf8(path).splitlines()
            link_like = is_link_like(path)
        except (OSError, ValueError) as error:
            failures.append(f"{prefix} cannot inspect {path_text}: {error}")
            continue
        if link_like or not path.is_file():
            failures.append(f"{prefix}.path must name a regular owned source file: {path_text}")
            continue
        if line_number > len(actual_content) or actual_content[line_number - 1] != content:
            failures.append(f"{prefix} does not match the exact current line in {path_text}:{line_number}")
            continue
        if not content.lstrip().startswith("//"):
            failures.append(f"{prefix} must identify a comment-only source line")
            continue
        if not any(token in url for token in FORBIDDEN) or content.count(url) != 1:
            failures.append(f"{prefix} must bind exactly one forbidden-organization URL")
            continue
        if any(token in content.replace(url, "") for token in FORBIDDEN):
            failures.append(f"{prefix} contains an additional forbidden-organization URL")
            continue
        if reason != REFERENCE_REASON:
            failures.append(f"{prefix}.reason must be exactly {REFERENCE_REASON!r}")
            continue
        key = (path_text, line_number)
        if key in records:
            failures.append(f"{prefix} duplicates an exact path/line allowlist record")
            continue
        records[key] = (content, url)
    return records


def scan_owned_source(
    path: Path,
    failures: list[str],
    binary_suffixes: set[str],
    allowed_references: dict[tuple[str, int], tuple[str, str]],
) -> None:
    """Scan UTF-8 text, skipping only explicitly policy-listed binary suffixes."""
    content = path.read_bytes()
    is_metadata = (
        path.name in {".gitmodules", "pubspec.yaml", "pubspec.yml", "pubspec.lock"}
        or path.name in {"Cargo.toml", "Cargo.lock"}
        or ".github" in path.parts and "workflows" in path.parts
    )
    if path.suffix.lower() in binary_suffixes and not is_metadata:
        return
    # Sciter's archived.rc is a binary resource archive despite the source-like
    # suffix. It is not a text source file, but still inspect its raw bytes for
    # forbidden URLs before accepting the known binary format.
    if path.suffix.lower() == ".rc" and content.startswith(b"SAr\0"):
        for token in FORBIDDEN:
            if token.encode("ascii") in content:
                failures.append(f"{relative_path(path)}: forbidden URL in binary resource")
        return
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        failures.append(f"{relative_path(path)}: invalid UTF-8 source metadata/text: {error}")
        return
    for line_number, line in enumerate(text.splitlines(), 1):
        if any(token in line for token in FORBIDDEN):
            reference = allowed_references.get((relative_path(path), line_number))
            if reference is not None and reference[0] == line and not any(
                token in line.replace(reference[1], "") for token in FORBIDDEN
            ):
                continue
            failures.append(f"{relative_path(path)}:{line_number}: {line.strip()}")


def require_window_pin(data: dict, name: str, expected_ref: str) -> None:
    windows = data.get("windows")
    if not isinstance(windows, list):
        raise ValueError("ownership manifest must define a windows list")
    matches = [entry for entry in windows if isinstance(entry, dict) and entry.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one windows ownership record named {name!r}")
    entry = matches[0]
    if entry.get("path") != "windows/RustDeskTempTopMostWindow":
        raise ValueError(f"{name} ownership record has unexpected path: {entry.get('path')!r}")
    if entry.get("ref") != expected_ref:
        raise ValueError(f"{name} ownership record ref is {entry.get('ref')!r}, expected {expected_ref!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-window-pin", nargs=2, metavar=("NAME", "SHA"))
    args = parser.parse_args()
    failures: list[str] = []
    try:
        data, excluded, active_roots = ownership_policy()
        if args.require_window_pin:
            require_window_pin(data, *args.require_window_pin)
    except (OSError, ValueError) as error:
        print(f"Flutter/workflow source ownership check failed: {error}", file=sys.stderr)
        return 1

    owned_roots = declared_owned_roots(data)
    owned_roots.extend(path for path in cargo_owned_roots() if path not in owned_roots)
    nested_gitmodules = discover_nested_gitmodules(owned_roots, failures)
    integrity_results = check_integrity(data, failures)
    check_nested_gitmodules(data, failures, nested_gitmodules)
    check_workflow_checkout_pins(data, failures)
    allowed_references = allowed_reference_records(data, owned_roots, failures)
    allowed_policy = allowed_policy_lines(allowed_references)

    manifests = yaml_files(excluded)
    for path in manifests:
        for line_number, line in enumerate(read_utf8(path).splitlines(), 1):
            if any(token in line for token in FORBIDDEN):
                if path == OWNERSHIP and line.strip() in allowed_policy:
                    continue
                failures.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")

    active_exclusions = set(data["scan_policy"].get("integrity_exclusions", []))
    configured_suffixes = data["scan_policy"].get("binary_suffixes")
    if not isinstance(configured_suffixes, list) or not all(isinstance(suffix, str) and suffix.startswith(".") for suffix in configured_suffixes):
        failures.append("scan_policy.binary_suffixes must be a list of dot-prefixed suffixes")
        binary_suffixes: set[str] = set()
    else:
        binary_suffixes = {suffix.lower() for suffix in configured_suffixes}
    allowed = allowed_symlink_records(data)
    try:
        active_source_files = list(dict.fromkeys(
                path
                for root in owned_roots
                for path, _, _ in owned_entries(root, active_exclusions, allowed, GENERATED_ARTIFACT_DIRS)
        ))
    except (OSError, ValueError) as error:
        failures.append(f"owned source traversal failed: {error}")
        active_source_files = []
    for path in active_source_files:
        scan_owned_source(path, failures, binary_suffixes, allowed_references)
    for path in repository_gitmodules(nested_gitmodules):
        for line_number, line in enumerate(read_utf8(path).splitlines(), 1):
            if any(token in line for token in FORBIDDEN):
                failures.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")

    if failures:
        print("Flutter/workflow source ownership check failed:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    excluded_names = ", ".join(sorted(path.relative_to(ROOT).as_posix() for path in excluded)) or "none"
    active_names = ", ".join(path.relative_to(ROOT).as_posix() for path in active_roots) or "none"
    print(
        f"checked {len(manifests)} active Flutter/workflow/third-party manifests and "
        f"{len(nested_gitmodules)} nested .gitmodules plus {len(active_source_files)} files under "
        f"owned source roots ({active_names}): no forbidden source URLs; "
        f"excluded non-build manifests: {excluded_names}"
    )
    for path, (file_count, byte_count, digest) in integrity_results:
        print(f"integrity {path}: files={file_count} bytes={byte_count} tree_sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
