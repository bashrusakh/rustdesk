#!/usr/bin/env python3
"""Ensure the source tree's build.py stages RQS_CUSTOM_TXT_FILE into the Linux bundle.

The workflow tag tree ships this staging inside build.py, but the Linux workflow
replaces the tree with the authenticated RQS_SOURCE_SHA ("Checkout source commit"),
whose build.py may predate the staging. Replacing build.py wholesale would regress
the source tree (it carries newer upstream hardening), so this patcher only ADDS the
minimal staging when it is absent.

It is idempotent: on a tree that already defines and calls the helper it is a no-op.
It fails closed: if the source build.py shape is not one it knows how to patch, it
exits non-zero so the package step never runs without custom_.txt.

Scope: only build_flutter_deb() (the Linux .deb path). The Windows, macOS and
Manjaro builders are never touched.
"""

import ast
import sys
from pathlib import Path

BUILD_PY = Path("build.py")
FUNC_NAME = "stage_custom_txt_for_linux_bundle"
DEB_FUNC = "build_flutter_deb"
ANCHOR = "system2('flutter build linux --release')"
UNTOUCHED_FUNCS = ("build_flutter_windows", "build_flutter_dmg", "build_flutter_arch_manjaro")

FUNC_DEF = (
    "def stage_custom_txt_for_linux_bundle(destination=None):\n"
    '    """Stage the private client config before Debian/RPM package creation."""\n'
    '    source = os.environ.get("RQS_CUSTOM_TXT_FILE", "")\n'
    "    if not source:\n"
    "        return\n"
    "    source_path = Path(source)\n"
    "    if not source_path.is_file():\n"
    '        raise RuntimeError("RQS_CUSTOM_TXT_FILE does not point to a regular file")\n'
    '    destination = (Path(flutter_build_dir) if destination is None else Path(destination)) / "custom_.txt"\n'
    "    shutil.copy2(source_path, destination)\n"
    "    destination.chmod(0o600)\n"
)


class PatchError(Exception):
    pass


def _top_level_functions(tree):
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _call_present(func_node):
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == FUNC_NAME:
            return True
    return False


def _accepts_zero_args(func_node):
    args = func_node.args
    positional = list(args.posonlyargs) + list(args.args)
    required_positional = len(positional) - len(args.defaults)
    if required_positional > 0:
        return False
    return all(default is not None for default in args.kw_defaults)


def _helper_references_source(func_node, lines):
    segment = lines[func_node.lineno - 1:func_node.end_lineno]
    return any("RQS_CUSTOM_TXT_FILE" in line for line in segment)


def _require_funcs(tree):
    funcs = _top_level_functions(tree)
    deb = funcs.get(DEB_FUNC)
    if deb is None:
        raise PatchError(f"top-level {DEB_FUNC}() not found in build.py")
    return funcs, deb


def _find_anchor(lines, deb):
    anchor_idx = None
    for index in range(deb.lineno - 1, min(deb.end_lineno, len(lines))):
        if lines[index].strip() == ANCHOR:
            if anchor_idx is not None:
                raise PatchError(f"multiple {ANCHOR!r} anchors inside {DEB_FUNC}")
            anchor_idx = index
    if anchor_idx is None:
        raise PatchError(f"{ANCHOR!r} not found inside {DEB_FUNC}")
    return anchor_idx


def _verify(text):
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PatchError(f"patched build.py does not parse: {exc}") from exc
    funcs, deb = _require_funcs(tree)
    if FUNC_NAME not in funcs:
        return False
    if not _accepts_zero_args(funcs[FUNC_NAME]):
        raise PatchError(f"{FUNC_NAME} cannot be called with zero arguments")
    if not _call_present(deb):
        return False
    for name in UNTOUCHED_FUNCS:
        if name in funcs and _call_present(funcs[name]):
            raise PatchError(f"unexpected {FUNC_NAME} call in {name}")
    return True


def patch():
    if not BUILD_PY.is_file():
        raise PatchError("build.py is missing from the source tree")

    text = BUILD_PY.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PatchError(f"build.py does not parse: {exc}") from exc

    funcs, deb = _require_funcs(tree)

    if _call_present(deb):
        if FUNC_NAME not in funcs:
            raise PatchError(f"{FUNC_NAME} is called but not defined at top level")
        if not _accepts_zero_args(funcs[FUNC_NAME]):
            raise PatchError(f"{FUNC_NAME} cannot be called with zero arguments")
        if not _helper_references_source(funcs[FUNC_NAME], text.splitlines()):
            raise PatchError(
                f"existing {FUNC_NAME}() does not read RQS_CUSTOM_TXT_FILE; refusing to assume it stages custom_.txt"
            )
        print("build.py already stages custom_.txt for the Linux bundle; no change.")
        return

    if FUNC_NAME in funcs and not _accepts_zero_args(funcs[FUNC_NAME]):
        raise PatchError(f"{FUNC_NAME} cannot be called with zero arguments")

    if FUNC_NAME in funcs and not _helper_references_source(funcs[FUNC_NAME], text.splitlines()):
        raise PatchError(
            f"existing {FUNC_NAME}() does not read RQS_CUSTOM_TXT_FILE; refusing to guess"
        )

    if FUNC_NAME not in funcs:
        lines = text.splitlines(keepends=True)
        insert_at = deb.lineno - 1
        prefix = ["\n"] if insert_at > 0 and lines[insert_at - 1].strip() != "" else []
        lines[insert_at:insert_at] = prefix + [FUNC_DEF, "\n"]
        text = "".join(lines)
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            raise PatchError(f"build.py does not parse after helper insertion: {exc}") from exc

    lines = text.splitlines(keepends=True)
    funcs, deb = _require_funcs(tree)
    anchor_idx = _find_anchor(lines, deb)
    anchor_line = lines[anchor_idx]
    indent = anchor_line[: len(anchor_line) - len(anchor_line.lstrip())]
    lines.insert(anchor_idx + 1, f"{indent}{FUNC_NAME}()\n")
    text = "".join(lines)

    if not _verify(text):
        raise PatchError("patched build.py did not gain the expected staging definition and call")
    compile(text, str(BUILD_PY), "exec")

    BUILD_PY.write_text(text, encoding="utf-8")
    print(f"patched build.py: added {FUNC_NAME}() staging to {DEB_FUNC}.")


def main():
    try:
        patch()
    except PatchError as exc:
        print(f"::error::cannot guarantee custom_.txt staging in build.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
