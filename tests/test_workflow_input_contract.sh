#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python3 - "$repo_root" <<'PY'
import subprocess
import sys
import tempfile
import re
import shlex
import base64
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path

import yaml

root = Path(sys.argv[1])
manifest_writer = (root / ".github" / "scripts" / "write_artifact_manifest.py").read_text()
workflow_names = [
    "bridge.yml",
    "rustqs-windows.yml",
    "rustqs-linux.yml",
    "rustqs-android.yml",
]
workflow_sha_input_workflow_names = {"bridge.yml", "rustqs-windows.yml"}
bridge_files = (
    "flutter/ios/Runner/bridge_generated.h",
    "flutter/lib/generated_bridge.dart",
    "flutter/lib/generated_bridge.freezed.dart",
    "flutter/macos/Runner/bridge_generated.h",
    "src/bridge_generated.io.rs",
    "src/bridge_generated.rs",
)


def run_blocks_with_shell(document):
    if isinstance(document, dict):
        if isinstance(document.get("run"), str):
            yield document["run"], document.get("shell", "")
        for value in document.values():
            yield from run_blocks_with_shell(value)
    elif isinstance(document, list):
        for value in document:
            yield from run_blocks_with_shell(value)


def uses_values(document):
    if isinstance(document, dict):
        for key, value in document.items():
            if key == "uses" and isinstance(value, str):
                yield value
            yield from uses_values(value)
    elif isinstance(document, list):
        for value in document:
            yield from uses_values(value)


def local_bridge_callers():
    callers = []
    workflows = root / ".github" / "workflows"
    for workflow in sorted(
        path for path in workflows.rglob("*")
        if path.is_file() and path.suffix in {".yml", ".yaml"}
    ):
        parsed = yaml.safe_load(workflow.read_text())
        for job_name, job in (parsed.get("jobs") or {}).items():
            if isinstance(job, dict) and job.get("uses") == "./.github/workflows/bridge.yml":
                callers.append((workflow, job_name, job))
    return callers


def bash_contract(workflow):
    text = workflow.read_text()
    parsed = yaml.safe_load(text)
    blocks = []
    for index, (block, shell) in enumerate(run_blocks_with_shell(parsed)):
        blocks.append(block)
        if shell and "bash" not in str(shell):
            continue
        if not shell and workflow.name == "rustqs-windows.yml":
            continue
        normalized = re.sub(r"\$\{\{.*?\}\}", "placeholder", block)
        check = subprocess.run(
            ["bash", "-n"], input=normalized, text=True, capture_output=True
        )
        if check.returncode != 0:
            raise AssertionError(
                f"{workflow.name}: bash syntax failed for run block {index}: {check.stderr}"
            )
    for block in blocks:
        if "reject_control_chars() {" in block:
            return block
    raise AssertionError(f"{workflow.name}: no bash input contract found")


for name in workflow_names:
    workflow = root / ".github" / "workflows" / name
    text = workflow.read_text()
    contract_text = text + manifest_writer
    parsed = yaml.safe_load(text)
    trigger = parsed.get("on", parsed.get(True, {}))
    inputs = trigger.get("workflow_dispatch", trigger.get("workflow_call", {})).get("inputs", {})
    expected_inputs = {"enc_payload", "workflow_sha"} if name in workflow_sha_input_workflow_names else {"enc_payload"}
    if set(inputs) != expected_inputs:
        raise AssertionError(f"{name}: unexpected workflow inputs {set(inputs)!r}, want {expected_inputs!r}")
    if name in workflow_sha_input_workflow_names:
        workflow_sha_input = inputs["workflow_sha"]
        if name == "bridge.yml":
            if workflow_sha_input.get("type") != "string" or workflow_sha_input.get("required") is not True or "default" in workflow_sha_input:
                raise AssertionError("bridge.yml: outer workflow_sha must be a required provider-derived string guard input")
        elif workflow_sha_input.get("type") != "string" or workflow_sha_input.get("required") is not False or workflow_sha_input.get("default") != "":
            raise AssertionError(f"{name}: provider-derived outer workflow_sha must be a public string guard input")
    if name == "rustqs-windows.yml":
        for marker in (
            "# deskforge-workflow-identity-guard: v1",
            "verified immutable protected workflow tag",
            "ref=<verified-immutable-protected-workflow-tag>",
            "not atomically SHA-bound",
        ):
            if marker not in text:
                raise AssertionError(f"rustqs-windows.yml: immutable workflow-tag rollout documentation is missing {marker!r}")
        if "rustqs/workflows" in text:
            raise AssertionError("rustqs-windows.yml: dispatch example must not use the mutable rustqs/workflows ref")
    if "Salted__" in text or "RQS_PAYLOAD_MODE=open" in text or "event SHA fallback" in text:
        raise AssertionError(f"{name}: legacy/open/manual fallback remains in active workflow")
    if "manual/direct runs require an authenticated DFP1 payload" not in text:
        raise AssertionError(f"{name}: manual/direct fail-closed guard is missing")
    if "workflow_repo" not in text or "authenticated workflow repository does not match this fork" not in text:
        raise AssertionError(f"{name}: authenticated workflow repository binding is missing")
    if parsed_permissions := parsed.get("permissions"):
        if parsed_permissions != {"contents": "read"}:
            raise AssertionError(f"{name}: permissions must remain contents: read, got {parsed_permissions!r}")
    else:
        raise AssertionError(f"{name}: explicit read-only permissions are required")
    for action in uses_values(parsed):
        if action.startswith("./"):
            continue
        if not re.search(r"@[0-9a-fA-F]{40}$", action):
            raise AssertionError(f"{name}: third-party action is not pinned to a commit: {action}")
    if "ACTIONS_RUNTIME_TOKEN" in text or "core.exportVariable" in text:
        raise AssertionError(f"{name}: runtime cache token must not be exported job-wide")
    if name != "bridge.yml":
        if 'write_github_env RQS_CUSTOM_TXT "$RQS_CT"' in text:
            raise AssertionError(f"{name}: custom_.txt content must not be persisted in GITHUB_ENV")
        if "RQS_CUSTOM_TXT_FILE" not in text:
            raise AssertionError(f"{name}: restrictive custom_.txt file handoff is missing")
        if "output/custom_.txt" in text:
            raise AssertionError(f"{name}: private custom_.txt must not be published beside public output")
        created_at = text.index("custom_txt_file=")
        trap_at = text.index("trap cleanup_custom_txt_on_failure EXIT", created_at)
        written_at = text.index('printf \'%s\' "$RQS_CT" > "$custom_txt_file"', trap_at)
        if not created_at < trap_at < written_at:
            raise AssertionError(f"{name}: failure cleanup trap must be installed before custom_.txt creation")
        if "- name: Cleanup sensitive custom_.txt" not in text or "if: always()" not in text:
            raise AssertionError(f"{name}: always-run sensitive custom_.txt cleanup is missing")
    if "GITHUB_ENV" not in text:
        raise AssertionError(f"{name}: missing environment-file handoff")
    if "reject_control_chars() {" not in text:
        raise AssertionError(f"{name}: missing control-character validator")
    if "write_github_env() {" not in text:
        raise AssertionError(f"{name}: missing safe environment-file writer")
    if "printf '%s=%s\\n'" not in text:
        raise AssertionError(f"{name}: safe printf environment writer is missing")
    if 'echo "RQS_' in text and '>> "$GITHUB_ENV"' in text:
        raise AssertionError(f"{name}: raw echo-to-GITHUB_ENV contract regressed")
    if "persist-credentials: false" not in text:
        raise AssertionError(f"{name}: checkout must not persist credentials")
    submodule_command = re.compile(
        r"GIT_CONFIG_COUNT=1\s+\\\s+"
        r"GIT_CONFIG_KEY_0=http\.extraheader\s+\\\s+"
        r"GIT_CONFIG_VALUE_0=\"Authorization: Bearer \$\{\{ github\.token \}\}\"\s+\\\s+"
        r"git submodule update --init --recursive"
    )
    if not submodule_command.search(text):
        raise AssertionError(f"{name}: submodule update is missing the process-scoped GitHub token header")
    if "git config --global" in text or "git config --local" in text:
        raise AssertionError(f"{name}: submodule authentication must not persist Git credentials")
    if 'echo "${{ github.token }}"' in text or "printf '%s' \"${{ github.token }}\"" in text:
        raise AssertionError(f"{name}: workflow must not print the GitHub token")
    for marker in (
        'DFP1',
        'hmac.compare_digest',
        'SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD)',
        '"submodule", "status", "--recursive"',
        'source_tree_sha',
        '"submodules"',
        '"manifest_schema": "deskforge.client-artifact"',
        '"schema_version": 2',
        '"verification_scope"',
         '"verification_result": "reported"',
         '"publication_timestamp"',
         '"private_filenames"',
         'deskforge.client-artifact-handoff-v1',
        '"size":',
    ):
        if marker not in contract_text:
            raise AssertionError(f"{name}: authenticated/reproducible payload marker {marker!r} is missing")
    manifest_timestamp_producers = re.findall(
        r"MANIFEST_PUBLICATION_TIMESTAMP=\$\(date -u '\+%Y-%m-%dT%H:%M:%SZ'\)", text
    )
    if len(manifest_timestamp_producers) != 1:
        raise AssertionError(
            f"{name}: expected exactly one runtime publication timestamp producer, "
            f"found {len(manifest_timestamp_producers)}"
        )
    if "github.run_started_at" in text:
        raise AssertionError(f"{name}: unsupported github.run_started_at publication timestamp remains")
    if '"verification_result": "verified"' in text:
        raise AssertionError(f"{name}: producer self-report must not be labelled verified")
    if ".github/scripts/write_artifact_manifest.py" not in text:
        raise AssertionError(f"{name}: shared producer manifest writer is missing")
    for marker in (
        "Preserve workflow manifest helper",
        "source_helper=.github/scripts/write_artifact_manifest.py",
        'test -f "${MANIFEST_HELPER_PATH:-}"',
        'python3 "$MANIFEST_HELPER_PATH"',
    ):
        if marker not in text:
            raise AssertionError(f"{name}: workflow-owned manifest helper marker {marker!r} is missing")
    preserve_at = text.index("- name: Preserve workflow manifest helper")
    source_checkout_at = text.index("- name: Checkout source commit")
    invoke_at = text.index('python3 "$MANIFEST_HELPER_PATH"')
    if not preserve_at < source_checkout_at < invoke_at:
        raise AssertionError(f"{name}: manifest helper must be preserved before source checkout and invoked afterward")

for name in workflow_names[1:]:
    text = (root / ".github" / "workflows" / name).read_text()
    for marker in (
        "validate_app_name() {",
        "app_name must be a non-empty filename component",
        "app_name uses a reserved Windows device name",
        "RQS_KEY=$RQS_KEY",
        "RQS_APP_NAME \"$RQS_APP\"",
    ):
        if marker not in text:
            raise AssertionError(f"{name}: missing {marker!r}")
    if 'RQS_APP_NAME:-rustdesk' in text or 'RQS_APP_NAME:-rustqs' in text:
        raise AssertionError(f"{name}: app_name is silently normalized at an output primitive")

    restore_start = text.index("- name: Restore bridge files")
    verify_start = text.index("- name: Verify and restore bridge files", restore_start)
    assertion_start = text.index("bridge_files=", verify_start)
    restore_end = text.index("- name:", verify_start + 1)
    restore_contract = text[restore_start:restore_end]
    for path in bridge_files:
        if path not in restore_contract:
            raise AssertionError(f"{name}: bridge restore assertion does not cover {path}")
    if "actions/download-artifact@" not in restore_contract or "BRIDGE_ARTIFACT_DIR" not in restore_contract:
        raise AssertionError(f"{name}: bridge artifact is not verified from a temporary directory")
    if "--verify-bridge" not in restore_contract or "--expected-version" not in restore_contract or 'cp -- "$BRIDGE_ARTIFACT_DIR/$file" "$file"' not in restore_contract:
        raise AssertionError(f"{name}: bridge manifest verification must precede source restoration")

bridge_callers = local_bridge_callers()
expected_bridge_caller_names = {
    "rustqs-windows.yml",
    "rustqs-linux.yml",
    "rustqs-android.yml",
}
discovered_bridge_caller_names = {workflow.name for workflow, _, _ in bridge_callers}
if discovered_bridge_caller_names != expected_bridge_caller_names:
    raise AssertionError(
        "bridge.yml callers changed without an explicit SHA contract: "
        f"found {discovered_bridge_caller_names!r}, want {expected_bridge_caller_names!r}"
    )
for workflow, job_name, job in bridge_callers:
    call_inputs = job.get("with")
    if not isinstance(call_inputs, dict) or call_inputs.get("enc_payload") != "${{ inputs.enc_payload }}":
        raise AssertionError(f"{workflow.name}:{job_name}: bridge must receive the authenticated payload")
    expected_workflow_sha = "${{ inputs.workflow_sha }}" if workflow.name == "rustqs-windows.yml" else "${{ github.sha }}"
    if call_inputs.get("workflow_sha") != expected_workflow_sha:
        raise AssertionError(f"{workflow.name}:{job_name}: bridge must receive a nonempty outer workflow_sha guard")

if sum(workflow.name == "rustqs-windows.yml" for workflow, _, _ in bridge_callers) != 1:
    raise AssertionError("rustqs-windows.yml: expected exactly one local bridge caller")


windows_jobs = yaml.safe_load((root / ".github" / "workflows" / "rustqs-windows.yml").read_text())["jobs"]
if next(iter(windows_jobs)) != "verify_workflow_identity":
    raise AssertionError("rustqs-windows.yml: no-secret workflow identity guard must be the first job")
verify_workflow_identity = windows_jobs.get("verify_workflow_identity")
if not isinstance(verify_workflow_identity, dict):
    raise AssertionError("rustqs-windows.yml: no-secret workflow identity guard is missing")
if verify_workflow_identity.get("permissions") != {}:
    raise AssertionError("rustqs-windows.yml: no-secret workflow identity guard must use permissions: {}")
verify_workflow_identity_text = json.dumps(verify_workflow_identity)
for forbidden in ("enc_payload", "secrets", "decrypt_payload", "actions/checkout", "checkout"):
    if forbidden in verify_workflow_identity_text:
        raise AssertionError(f"rustqs-windows.yml: outer guard must not access {forbidden!r}")
verify_steps = verify_workflow_identity.get("steps")
if not isinstance(verify_steps, list) or len(verify_steps) != 1 or not isinstance(verify_steps[0].get("run"), str):
    raise AssertionError("rustqs-windows.yml: outer guard must be a single shell-only step")
outer_guard_block = verify_steps[0]["run"]
if verify_steps[0].get("env") != {
    "OUTER_WORKFLOW_SHA": "${{ inputs.workflow_sha }}",
    "EXECUTION_WORKFLOW_SHA": "${{ github.sha }}",
}:
    raise AssertionError("rustqs-windows.yml: outer guard must compare inputs.workflow_sha with github.sha")
for marker in (
    "^[0-9a-fA-F]{40,64}$",
    '[ "$OUTER_WORKFLOW_SHA" != "$EXECUTION_WORKFLOW_SHA" ]',
):
    if marker not in outer_guard_block:
        raise AssertionError(f"rustqs-windows.yml: outer guard is missing {marker!r}")


def needs_job(job, required):
    needs = job.get("needs")
    return needs == required or (isinstance(needs, list) and required in needs)


for platform in ("linux", "android"):
    workflow_name = f"rustqs-{platform}.yml"
    platform_jobs = yaml.safe_load((root / ".github" / "workflows" / workflow_name).read_text())["jobs"]
    if next(iter(platform_jobs)) != "unsupported":
        raise AssertionError(f"{workflow_name}: unsupported must be the first job")
    unsupported = platform_jobs.get("unsupported")
    if not isinstance(unsupported, dict) or unsupported.get("permissions") != {}:
        raise AssertionError(f"{workflow_name}: unsupported must be a no-permissions gate")
    unsupported_steps = unsupported.get("steps")
    if not isinstance(unsupported_steps, list) or len(unsupported_steps) != 1:
        raise AssertionError(f"{workflow_name}: unsupported must contain one explanatory no-secret step")
    unsupported_step = unsupported_steps[0]
    unsupported_text = json.dumps(unsupported_step)
    if unsupported_step.get("shell") != "bash" or "unavailable" not in unsupported_text.lower() or "Windows" not in unsupported_text:
        raise AssertionError(f"{workflow_name}: unsupported must explain that only Windows is available")
    for forbidden in ("enc_payload", "secrets", "decrypt", "checkout", "github.token"):
        if forbidden in unsupported_text:
            raise AssertionError(f"{workflow_name}: unsupported must not access {forbidden!r}")
    if not needs_job(platform_jobs["bridge"], "unsupported"):
        raise AssertionError(f"{workflow_name}: secret-bearing bridge must depend on unsupported")
    if not needs_job(platform_jobs["build"], "unsupported") or not needs_job(platform_jobs["build"], "bridge"):
        raise AssertionError(f"{workflow_name}: secret-bearing build must be skipped behind unsupported and bridge")


if not needs_job(windows_jobs["bridge"], "verify_workflow_identity"):
    raise AssertionError("rustqs-windows.yml: secret-bearing bridge must depend on the outer guard")
if not needs_job(windows_jobs["build"], "verify_workflow_identity"):
    raise AssertionError("rustqs-windows.yml: secret-bearing build must depend on the outer guard")
if windows_jobs["topmost"].get("needs") != "bridge":
    raise AssertionError("rustqs-windows.yml: topmost must depend on bridge")


def execute_outer_guard(outer_sha, execution_sha):
    return subprocess.run(
        ["bash", "-s"],
        input=outer_guard_block,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "OUTER_WORKFLOW_SHA": outer_sha,
            "EXECUTION_WORKFLOW_SHA": execution_sha,
        },
    )


valid_workflow_sha = "a" * 40
if execute_outer_guard(valid_workflow_sha, valid_workflow_sha).returncode != 0:
    raise AssertionError("rustqs-windows.yml: matching outer workflow SHA was rejected")
if execute_outer_guard("b" * 40, valid_workflow_sha).returncode == 0:
    raise AssertionError("rustqs-windows.yml: outer SHA mismatch reached secret-bearing jobs")
if execute_outer_guard("not-a-sha", valid_workflow_sha).returncode == 0:
    raise AssertionError("rustqs-windows.yml: malformed outer SHA was accepted")


def resolve_identity_block(workflow):
    parsed = yaml.safe_load(workflow.read_text())
    for block, _ in run_blocks_with_shell(parsed):
        if "validate_authenticated_workflow_identity() {" in block:
            return block
    raise AssertionError(f"{workflow.name}: authenticated inner workflow SHA guard is missing")


identity_blocks = {}
for name in workflow_sha_input_workflow_names:
    workflow = root / ".github" / "workflows" / name
    text = workflow.read_text()
    block = resolve_identity_block(workflow)
    identity_blocks[name] = block
    decrypt_at = text.index("decrypted=$(decrypt_payload)")
    validate_at = text.index('validate_authenticated_workflow_identity "$decrypted"', decrypt_at)
    payload_value_at = min(
        text.index(marker, validate_at)
        for marker in ('RQS_SERVER=$(printf', 'RQS_VERSION=$(printf')
        if marker in text[validate_at:]
    )
    for marker in ('>> "$GITHUB_ENV"', "actions/checkout", "git fetch"):
        if validate_at >= text.index(marker, validate_at):
            raise AssertionError(f"{name}: inner SHA guard must precede {marker}")
    if not decrypt_at < validate_at < payload_value_at:
        raise AssertionError(f"{name}: inner SHA guard must run immediately after decrypt before payload reads")
    for marker in (
        "OUTER_WORKFLOW_SHA: ${{ inputs.workflow_sha }}",
        "EXECUTION_WORKFLOW_SHA: ${{ github.sha }}",
        'type == "object"',
        "^[0-9a-fA-F]{40,64}$",
        '[ "$inner_workflow_sha" != "$OUTER_WORKFLOW_SHA" ]',
        '[ "$inner_workflow_sha" != "$EXECUTION_WORKFLOW_SHA" ]',
    ):
        if marker not in text:
            raise AssertionError(f"{name}: authenticated inner SHA guard is missing {marker!r}")
    if name == "bridge.yml":
        for marker in (
            "validate_outer_workflow_identity() {",
            'if [ -z "${OUTER_WORKFLOW_SHA:-}" ]; then',
            "outer workflow_sha is required",
            "validate_outer_workflow_identity\n",
            '[ "$OUTER_WORKFLOW_SHA" != "$EXECUTION_WORKFLOW_SHA" ]',
        ):
            if marker not in text:
                raise AssertionError(f"bridge.yml: required outer SHA guard is missing {marker!r}")
        if "# Legacy Linux/Android callers are production-disabled; Windows must supply this guard." in text:
            raise AssertionError("bridge.yml: legacy no-SHA caller acceptance remains")
        outer_function_at = text.index("validate_outer_workflow_identity() {")
        outer_call_at = text.index("validate_outer_workflow_identity\n", outer_function_at)
        if not outer_function_at < outer_call_at < decrypt_at:
            raise AssertionError("bridge.yml: outer SHA guard must run before DFP1 decryption")

bridge_text = (root / ".github" / "workflows" / "bridge.yml").read_text()
stage_start = bridge_text.index("- name: Stage generated bridge files")
stage_end = bridge_text.index("- name:", stage_start + 1)
stage_contract = bridge_text[stage_start:stage_end]
for path in bridge_files:
    if path not in stage_contract or f'"bridge-output/$file"' not in stage_contract:
        raise AssertionError(f"bridge.yml: bridge-output staging does not cover {path}")
if "test -f \"$file\"" not in stage_contract or "test -f \"bridge-output/$file\"" not in stage_contract:
    raise AssertionError("bridge.yml: bridge-output population assertions are missing")


def execute_contract(block, app, key, version="1.2.3", android_app_id="com.example.rustqs"):
    with tempfile.TemporaryDirectory() as runner_temp, tempfile.NamedTemporaryFile() as env_file:
        setup = f"""\
set -euo pipefail
GITHUB_ENV={shlex.quote(env_file.name)}
RUNNER_TEMP={shlex.quote(runner_temp)}
GITHUB_RUN_ID=workflow-contract
RQS_SERVER='id.example:21116'
RQS_KEY={shlex.quote(key)}
RQS_APP={shlex.quote(app)}
RQS_CT='YWJj'
RQS_VERSION={shlex.quote(version)}
RQS_SOURCE_SHA='{'a' * 40}'
RQS_WORKFLOW_REPO='owner/repo'
RQS_RELEASE_REPO='owner/repo'
RQS_RELEASE_ASSETS='[]'
RQS_PAYLOAD_MODE='open'
RQS_ANDROID_APP_ID={shlex.quote(android_app_id)}
"""
        start = block.index("reject_control_chars() {")
        if "if ! printf '%s' \"$decrypted\" | jq -e '.source_sha" in block[start:]:
            end = block.index("if ! printf '%s' \"$decrypted\" | jq -e '.source_sha", start)
        else:
            writer = block.index("write_github_env() {", start)
            end = block.index("\n}\n", writer) + len("\n}\n")
        contract = block[start:end]
        result = subprocess.run(
            ["bash", "-s"],
            input=setup + contract + "\nwrite_github_env TEST_VALUE \"$RQS_KEY\"\n",
            text=True,
            capture_output=True,
        )
        return result, Path(env_file.name).read_text()


def make_authenticated_payload(key, plaintext):
    salt = b"0123456789abcdef"
    derived = hashlib.pbkdf2_hmac("sha256", key.encode(), salt, 100000, 80)
    padding = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([padding]) * padding
    encrypted = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-K", derived[:32].hex(), "-iv", derived[32:48].hex(), "-nopad"],
        input=padded,
        capture_output=True,
        check=True,
    ).stdout
    signed = b"DFP1" + salt + encrypted
    return signed + hmac.new(derived[48:], signed, hashlib.sha256).digest()


def execute_payload_contract(block, encoded, key):
    start = block.index("decrypt_payload() {")
    end = block.find("validate_authenticated_workflow_identity() {", start)
    if end == -1:
        end = block.index("decrypted=$(decrypt_payload)", start)
    function = block[start:end]
    script = "set -euo pipefail\n" + function + 'decrypted=$(decrypt_payload)\nprintf "%s" "$decrypted"\n'
    return subprocess.run(
        ["bash", "-s"],
        input=script,
        text=True,
        capture_output=True,
        env={**os.environ, "ENC": encoded, "PAYLOAD_KEY": key},
    )


def execute_authenticated_inner_sha_guard(block, encoded, key, outer_sha, execution_sha, require_outer_preflight=False):
    decrypt_start = block.index("decrypt_payload() {")
    identity_start = block.index("validate_authenticated_workflow_identity() {", decrypt_start)
    identity_end = block.index("decrypted=$(decrypt_payload)", identity_start)
    if require_outer_preflight:
        decrypt_start = block.index("validate_outer_workflow_identity() {")
    script = (
        "set -euo pipefail\n"
        + block[decrypt_start:identity_start]
        + block[identity_start:identity_end]
        + 'decrypted=$(decrypt_payload)\nvalidate_authenticated_workflow_identity "$decrypted"\n'
    )
    return subprocess.run(
        ["bash", "-s"],
        input=script,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "ENC": encoded,
            "PAYLOAD_KEY": key,
            "OUTER_WORKFLOW_SHA": outer_sha,
            "EXECUTION_WORKFLOW_SHA": execution_sha,
        },
    )


for name in workflow_names:
    block = bash_contract(root / ".github" / "workflows" / name)
    valid_key = "5Qbwsde3unUcJBtrx9ZkvUmwFNoExHzpryHuPUdqlWM="
    safe_key = valid_key if name == "bridge.yml" else valid_key + "\r\n"
    safe, env_output = execute_contract(block, "My RustDesk 客户端", safe_key)
    if safe.returncode != 0:
        raise AssertionError(f"{name}: safe payload rejected: {safe.stderr}")
    if env_output != f"TEST_VALUE={valid_key}\n":
        raise AssertionError(f"{name}: safe payload was changed or split: {env_output!r}")

    cases = (("version", "1.2.3\r"),)
    if name != "bridge.yml":
        cases = (("key", "public\nkey"), ("key", "public-key"), ("app_name", "../rustqs"), ("app_name", "@rustqs"), ("app_name", "?rustqs"), ("version", "../../etc"), ("version", "1.2"), ("version", "1.2.3-01"), ("version", "1.2.3+build"), ("version", "1.2.3\r"))
    for field, value in cases:
        app = value if field == "app_name" else "rustqs"
        key = value if field == "key" else "public/key+=="
        result, _ = execute_contract(block, app, key, value if field == "version" else "1.2.3")
        if result.returncode == 0:
            raise AssertionError(f"{name}: unsafe {field} was accepted")

    if name != "bridge.yml":
        for reserved in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT9", "con.txt", "LPT1 .exe"):
            result, _ = execute_contract(block, reserved, valid_key)
            if result.returncode == 0:
                raise AssertionError(f"{name}: reserved app_name {reserved!r} was accepted")
        for safe_app in ("CONSOLE", "COM10", "My RustDesk 客户端"):
            result, _ = execute_contract(block, safe_app, valid_key)
            if result.returncode != 0:
                raise AssertionError(f"{name}: safe app_name {safe_app!r} was rejected: {result.stderr}")
        if name == "rustqs-android.yml":
            for invalid_id in ("", "com", "../escape", "Com.Example.App", "com.example..app", "com.example/app"):
                result, _ = execute_contract(block, "rustqs", valid_key, android_app_id=invalid_id)
                if result.returncode == 0:
                    raise AssertionError(f"{name}: invalid android_app_id {invalid_id!r} was accepted")

    for safe_version in ("1.2.3", "1.2.3-rc.1"):
        result, _ = execute_contract(block, "rustqs", valid_key, safe_version)
        if result.returncode != 0:
            raise AssertionError(f"{name}: safe version {safe_version} was rejected: {result.stderr}")

    payload_key = "workflow-contract-key"
    plaintext = b'{"version":"1.2.3","source_sha":"' + (b"a" * 40) + b'","workflow_repo":"owner/repo"}'
    envelope = make_authenticated_payload(payload_key, plaintext)
    good = execute_payload_contract(block, base64.b64encode(envelope).decode(), payload_key)
    if good.returncode != 0 or good.stdout != plaintext.decode():
        raise AssertionError(f"{name}: authenticated payload did not decrypt: {good.stderr}")
    tampered = bytearray(envelope)
    tampered[-1] ^= 1
    bad = execute_payload_contract(block, base64.b64encode(tampered).decode(), payload_key)
    if bad.returncode == 0:
        raise AssertionError(f"{name}: tampered authenticated payload was accepted")

    legacy = base64.b64encode(b"Salted__legacy-payload").decode()
    legacy_result = execute_payload_contract(block, legacy, payload_key)
    if legacy_result.returncode == 0:
        raise AssertionError(f"{name}: legacy unauthenticated payload was accepted")


payload_key = "workflow-contract-key"
inner_sha = "a" * 40


def encrypted_inner_payload(payload):
    envelope = make_authenticated_payload(payload_key, json.dumps(payload, separators=(",", ":")).encode())
    return base64.b64encode(envelope).decode()


matching = execute_authenticated_inner_sha_guard(
    identity_blocks["rustqs-windows.yml"],
    encrypted_inner_payload({"workflow_sha": inner_sha}),
    payload_key,
    inner_sha,
    inner_sha,
)
if matching.returncode != 0:
    raise AssertionError(f"rustqs-windows.yml: matching authenticated inner workflow SHA was rejected: {matching.stderr}")
for case, payload, outer_sha, execution_sha in (
    ("missing", {}, inner_sha, inner_sha),
    ("malformed", {"workflow_sha": "not-a-sha"}, inner_sha, inner_sha),
    ("outer mismatch", {"workflow_sha": "b" * 40}, inner_sha, inner_sha),
    ("execution mismatch", {"workflow_sha": inner_sha}, inner_sha, "b" * 40),
):
    result = execute_authenticated_inner_sha_guard(
        identity_blocks["rustqs-windows.yml"],
        encrypted_inner_payload(payload),
        payload_key,
        outer_sha,
        execution_sha,
    )
    if result.returncode == 0:
        raise AssertionError(f"rustqs-windows.yml: {case} authenticated inner workflow SHA was accepted")


legacy_bridge = execute_authenticated_inner_sha_guard(
    identity_blocks["bridge.yml"], encrypted_inner_payload({}), payload_key, "", inner_sha, True
)
if legacy_bridge.returncode == 0:
    raise AssertionError("bridge.yml: legacy no-SHA caller was accepted")
matching_bridge = execute_authenticated_inner_sha_guard(
    identity_blocks["bridge.yml"],
    encrypted_inner_payload({"workflow_sha": inner_sha}),
    payload_key,
    inner_sha,
    inner_sha,
    True,
)
if matching_bridge.returncode != 0:
    raise AssertionError(f"bridge.yml: matching authenticated inner workflow SHA was rejected: {matching_bridge.stderr}")
for case, payload, outer_sha, execution_sha in (
    ("outer SHA without inner SHA", {}, inner_sha, inner_sha),
    ("inner SHA without outer SHA", {"workflow_sha": inner_sha}, "", inner_sha),
    ("malformed outer SHA", {"workflow_sha": inner_sha}, "not-a-sha", inner_sha),
    ("malformed inner SHA", {"workflow_sha": "not-a-sha"}, inner_sha, inner_sha),
    ("non-string inner SHA", {"workflow_sha": 1}, inner_sha, inner_sha),
    ("inner/outer SHA mismatch", {"workflow_sha": "b" * 40}, inner_sha, inner_sha),
    ("outer/github SHA mismatch", {"workflow_sha": inner_sha}, inner_sha, "b" * 40),
):
    result = execute_authenticated_inner_sha_guard(
        identity_blocks["bridge.yml"],
        encrypted_inner_payload(payload),
        payload_key,
        outer_sha,
        execution_sha,
        True,
    )
    if result.returncode == 0:
        raise AssertionError(f"bridge.yml: {case} was accepted")


android_workflow = root / ".github" / "workflows" / "rustqs-android.yml"
android_text = android_workflow.read_text()
main_service = (root / "flutter" / "android" / "app" / "src" / "main" / "kotlin" / "com" / "carriez" / "flutter_hbb" / "MainService.kt").read_text()
ffi_kt = (root / "flutter" / "android" / "app" / "src" / "main" / "kotlin" / "ffi.kt").read_text()
flutter_ffi = (root / "src" / "flutter_ffi.rs").read_text()
common_rs = (root / "src" / "common.rs").read_text()
for marker in (
    'assets.open("flutter_assets/assets/custom_.txt")',
    'FFI.startServer(configPath, customClientConfig)',
    'Bundled custom client config is unreadable; refusing to start',
    'external fun startServer(app_dir: String, custom_client_config: String): Boolean',
    'pub unsafe extern "system" fn Java_ffi_FFI_startServer',
    ') -> jboolean {',
    'if !custom_client_config.is_empty() && !crate::read_custom_client(&custom_client_config)',
    'pub fn read_custom_client(config: &str) -> bool',
):
    if marker not in main_service + ffi_kt + flutter_ffi + common_rs:
        raise AssertionError(f"Android custom-client runtime contract is missing {marker!r}")
for marker in (
    'RQS_ANDROID_APP_ID=$(printf',
    'validate_android_app_id() {',
     'android_app_id must be a lowercase Java package identifier',
     'app_name must not start with @ or ?',
    'write_github_env RQS_ANDROID_APP_ID "$RQS_ANDROID_APP_ID"',
    "Apply Android identity",
    'package="com.carriez.flutter_hbb"',
    'applicationId "com.carriez.flutter_hbb"',
    'assets/flutter_assets/assets/custom_.txt',
    'if [ -n "${RQS_CUSTOM_TXT_FILE:-}" ]; then',
    'custom_.txt is not packaged in Flutter Android assets',
    'custom_.txt does not match the Android native client config contract',
    'custom_.txt native client config must be a JSON object',
):
    if marker not in android_text:
        raise AssertionError(f"rustqs-android.yml: APK custom-client packaging check is missing {marker!r}")
if "best-effort" in android_text:
    raise AssertionError("rustqs-android.yml: custom-client packaging must not be best-effort")
if main_service.index("readBundledCustomClientConfig()") > main_service.index("FFI.startServer(configPath, customClientConfig)"):
    raise AssertionError("MainService starts the native server before reading custom_.txt")
if android_text.index("custom_.txt is not packaged in Flutter Android assets") > android_text.index('cp "$apk" "./output/${APP}.apk"'):
    raise AssertionError("rustqs-android.yml copies the APK to output before checking the custom asset")
if "test -f flutter/assets/custom_.txt" not in android_text:
    raise AssertionError("rustqs-android.yml does not require the staged custom_.txt asset")
manifest_text = (root / "flutter" / "android" / "app" / "src" / "main" / "AndroidManifest.xml").read_text()
if 'android:label="@string/app_name"' not in manifest_text:
    raise AssertionError("Android manifest does not use the authored app_name resource")

linux_workflow = root / ".github" / "workflows" / "rustqs-linux.yml"
linux_text = linux_workflow.read_text()
build_py = (root / "build.py").read_text()
build_call = linux_text.index("python3 ./build.py --flutter --skip-cargo")
source_guard = linux_text.index('test -f "$RQS_CUSTOM_TXT_FILE"')
if source_guard > build_call:
    raise AssertionError("Linux workflow must verify private custom_.txt before build.py packaging")
if "L2 payload: place custom_.txt into bundle" in linux_text:
    raise AssertionError("Linux workflow must not stage custom_.txt after Debian package creation")
flutter_build = build_py.index("flutter build linux --release")
stage_custom = build_py.index("stage_custom_txt_for_linux_bundle(", flutter_build)
bundle_copy = build_py.index("cp -r {flutter_build_dir}/*", stage_custom)
if not flutter_build < stage_custom < bundle_copy:
    raise AssertionError("build.py must stage custom_.txt between Flutter build and Debian bundle copy")
package_flow = build_py.index("def build_deb_from_folder")
package_stage = build_py.index("stage_custom_txt_for_linux_bundle(", package_flow)
package_copy = build_py.index("cp -r ../{binary_folder}/*", package_stage)
if not package_stage < package_copy:
    raise AssertionError("build.py --package must stage custom_.txt before copying the binary folder")
deb_assertion = linux_text.index('dpkg-deb -c "$deb_source"')
deb_copy = linux_text.index('cp -- "$deb_source" "$deb_output"')
if deb_assertion > deb_copy:
    raise AssertionError("Linux workflow must assert custom_.txt membership before publishing the Debian artifact")
for marker in ("rpmbuild -ba res/rpm-flutter.spec", "Cleanup sensitive custom_.txt"):
    if marker not in linux_text:
        raise AssertionError(f"Linux RPM/private-manifest contract is missing {marker!r}")


def run_manifest_writer(output, app_name="rustqs", platform="windows"):
    environment = {
        **os.environ,
        "RQS_SOURCE_SHA": "a" * 40,
        "MANIFEST_PUBLICATION_TIMESTAMP": "2026-08-10T12:00:00Z",
    }
    return subprocess.run(
        [
            sys.executable,
            str(root / ".github" / "scripts" / "write_artifact_manifest.py"),
            "--platform",
            platform,
            "--app-name",
            app_name,
            "--version",
            "1.2.3",
            "--output",
            str(output),
            "--workflow-sha",
            "b" * 40,
            "--workflow-ref",
            "rustqs/workflows",
        ],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
    )


def run_manifest_writer_with_mocked_provenance(output, platform="windows", app_name="rustqs"):
    module_path = root / ".github" / "scripts" / "write_artifact_manifest.py"
    spec = importlib.util.spec_from_file_location("deskforge_manifest_writer", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.source_tree_sha = lambda: "a" * 40
    module.submodules = lambda: []
    old_argv = sys.argv
    old_source_sha = os.environ.get("RQS_SOURCE_SHA")
    old_timestamp = os.environ.get("MANIFEST_PUBLICATION_TIMESTAMP")
    sys.argv = [
        str(module_path),
        "--platform",
        platform,
        "--app-name",
        app_name,
        "--version",
        "1.2.3",
        "--output",
        str(output),
        "--workflow-sha",
        "b" * 40,
        "--workflow-ref",
        "rustqs/workflows",
    ]
    os.environ["RQS_SOURCE_SHA"] = "a" * 40
    os.environ["MANIFEST_PUBLICATION_TIMESTAMP"] = "2026-08-10T12:00:00Z"
    try:
        module.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
        if old_source_sha is None:
            os.environ.pop("RQS_SOURCE_SHA", None)
        else:
            os.environ["RQS_SOURCE_SHA"] = old_source_sha
        if old_timestamp is None:
            os.environ.pop("MANIFEST_PUBLICATION_TIMESTAMP", None)
        else:
            os.environ["MANIFEST_PUBLICATION_TIMESTAMP"] = old_timestamp
    return 0


def verify_bridge_artifact(output, source_sha="a" * 40, workflow_sha="b" * 40, workflow_ref="rustqs/workflows", version="1.2.3"):
    module_path = root / ".github" / "scripts" / "write_artifact_manifest.py"
    spec = importlib.util.spec_from_file_location("deskforge_bridge_verifier", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    try:
        module.verify_bridge_artifact(Path(output), source_sha, workflow_sha, workflow_ref, version)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


def verify_bridge_artifact_with_cli(output):
    return subprocess.run(
        [
            sys.executable,
            str(root / ".github" / "scripts" / "write_artifact_manifest.py"),
            "--verify-bridge",
            "--output",
            str(output),
            "--expected-source-sha",
            "a" * 40,
            "--expected-version",
            "1.2.3",
            "--workflow-sha",
            "b" * 40,
            "--workflow-ref",
            "rustqs/workflows",
        ],
        cwd=root,
        text=True,
        capture_output=True,
    )


with tempfile.TemporaryDirectory() as output_dir:
    output = Path(output_dir)
    (output / "rustqs.exe").write_bytes(b"safe")
    result = run_manifest_writer_with_mocked_provenance(output)
    if result != 0:
        raise AssertionError(f"manifest writer rejected a regular bounded output: {result}")
    manifest = json.loads((output / "manifest.txt").read_text())
    if manifest["schema_version"] != 2 or manifest["verification_result"] != "reported" or manifest["private_filenames"] != []:
        raise AssertionError(f"manifest writer emitted an invalid v2 report: {manifest}")

with tempfile.TemporaryDirectory() as output_dir:
    output = Path(output_dir)
    (output / "rustqs.exe").write_bytes(b"safe")
    (output / "custom_.txt").write_bytes(b"private settings")
    result = run_manifest_writer_with_mocked_provenance(output)
    if result == 0 or (output / "manifest.txt").exists():
        raise AssertionError("manifest writer accepted a private custom_.txt sidecar")

with tempfile.TemporaryDirectory() as output_dir:
    output = Path(output_dir)
    (output / "rustqs.exe").write_bytes(b"safe")
    (output / "secret.txt").write_bytes(b"unexpected secret")
    result = run_manifest_writer_with_mocked_provenance(output)
    if result == 0 or (output / "manifest.txt").exists():
        raise AssertionError("manifest writer accepted an unlisted secret output file")

with tempfile.TemporaryDirectory() as output_dir:
    output = Path(output_dir)
    outside = output.parent / "outside.exe"
    outside.write_bytes(b"outside")
    (output / "rustqs.exe").symlink_to(outside)
    result = run_manifest_writer_with_mocked_provenance(output)
    if result == 0 or (output / "manifest.txt").exists():
        raise AssertionError("manifest writer accepted an escaping symlink or wrote before rejecting it")

with tempfile.TemporaryDirectory() as output_dir:
    output = Path(output_dir)
    os.mkfifo(output / "rustqs.exe")
    result = run_manifest_writer_with_mocked_provenance(output)
    if result == 0 or (output / "manifest.txt").exists():
        raise AssertionError("manifest writer accepted a special file or wrote before rejecting it")

with tempfile.TemporaryDirectory() as output_dir:
    output = Path(output_dir)
    (output / "rustqs.exe").write_bytes(b"safe")
    result = run_manifest_writer_with_mocked_provenance(output, app_name="../escape")
    if result == 0 or (output / "manifest.txt").exists():
        raise AssertionError("manifest writer accepted an output path escaping the artifact directory")

with tempfile.TemporaryDirectory() as output_dir:
    output = Path(output_dir)
    for name in bridge_files:
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    result = run_manifest_writer_with_mocked_provenance(output, "bridge", "rustdesk-bridge")
    if result != 0:
        raise AssertionError(f"manifest writer rejected safe nested bridge output: {result}")
    manifest = json.loads((output / "manifest.txt").read_text())
    if manifest["platform"] != "bridge" or manifest["output_filenames"] != sorted(bridge_files):
        raise AssertionError(f"bridge manifest output contract is invalid: {manifest}")
    if verify_bridge_artifact_with_cli(output).returncode != 0:
        raise AssertionError("bridge manifest verifier rejected valid identity and hashes")
    (output / bridge_files[0]).write_bytes(b"tampered")
    if verify_bridge_artifact(output) == 0:
        raise AssertionError("bridge manifest verifier accepted a tampered nested file")

for platform, app_name, names in (
    ("windows", "rustqs", ["rustqs.exe"]),
    ("linux", "rustqs", ["rustqs-1.2.3-0.x86_64.rpm", "rustqs-1.2.3.deb"]),
    ("android", "rustqs", ["rustqs.apk"]),
):
    with tempfile.TemporaryDirectory() as output_dir:
        output = Path(output_dir)
        for name in names:
            (output / name).write_bytes(name.encode())
        (output / "custom_.txt").write_bytes(b"private settings")
        result = run_manifest_writer_with_mocked_provenance(output, platform, app_name)
        if result == 0 or (output / "manifest.txt").exists():
            raise AssertionError(f"{platform}: manifest writer accepted a private custom_.txt sidecar")

with tempfile.TemporaryDirectory() as output_dir:
    output = Path(output_dir)
    (output / "rustqs.exe").write_bytes(b"safe")
    (output / "extra.bin").write_bytes(b"extra")
    result = run_manifest_writer_with_mocked_provenance(output)
    if result == 0 or (output / "manifest.txt").exists():
        raise AssertionError("manifest writer accepted an extra final-platform output")

with tempfile.TemporaryDirectory() as output_dir:
    output = Path(output_dir)
    for name in bridge_files:
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    (output / "flutter/escape.dart").symlink_to(output / bridge_files[0])
    result = run_manifest_writer_with_mocked_provenance(output, "bridge", "rustdesk-bridge")
    if result == 0 or (output / "manifest.txt").exists():
        raise AssertionError("manifest writer accepted an unsafe bridge symlink")

print("workflow input/YAML/shell contract checks passed")
PY
