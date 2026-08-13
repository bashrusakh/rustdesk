# Upstream-Independent Build Migration TODO

## Phase 0 — Baseline

- [x] Record worktree, base, baseline commit, and clean status in `plan.md`.
- [x] Record the established inventory: `a920d009` gitlink, 31 active Cargo
  git dependencies, Flutter git packages, workflow third-party sources, and
  no final offline proof.
- [x] Scan submodule metadata and confirm `libs/hbb_common` is the sole
  submodule.

## Phase 1 — Local `hbb_common` source ownership

- [x] Extract the clean checked-out `a920d009` source into the parent as
  ordinary tracked files, excluding private `.git` metadata.
- [x] Remove the `libs/hbb_common` gitlink and the sole-entry `.gitmodules`.
- [x] Confirm the existing Cargo path workspace member/dependency declarations
  are unchanged and still resolve `hbb_common`.
- [x] Run `git diff --check`, submodule status/scan, and
  `cargo metadata --locked --no-deps --format-version 1` when feasible.
- [x] Review the diff for Phase 1 scope only; do not commit or push. The
  staged plan/source replacement is limited to `.gitmodules`,
  `libs/hbb_common/**`, and these canonical plan files.

### Phase 1 verification note

At the Phase 1 snapshot, full `git diff --check` reported inherited trailing
whitespace in the copied `a920d009` protobuf files. The source was left
byte-for-byte unchanged as required. Next gate: Phase 2 review before any Cargo
git dependency URL or vendor-source changes.

## Phase 2 — Cargo sources

- [x] Record the fail-closed relative `vendor/` Cargo source replacement with
  `net.offline = true` without copying the large vendor tree.
- [x] Add the read-only Cargo.lock/vendor config verification helper.
- [x] Record local vendor provenance and archive SHA-256.
- [x] Copy 44 exact package roots formerly supplied by git sources into tracked
  `third_party/`; do not copy the full registry vendor tree.
- [x] Convert active root/library and required copied nested manifests to
  relative path dependencies without changing versions/features.
- [x] Confirm zero active git sources in root and nested Cargo lockfiles while
  retaining locked registry versions/features.
- [x] Run normal and external-vendor-override offline locked metadata checks;
  each reports 9 workspace members and 1,017 packages.
- [x] Add and run the active manifest/lock git-source scan; confirm no full
  `vendor/` tree.
- [x] Confirm current worktree and index `git diff --check` checks are clean;
  record that a full commit-range check reports inherited/copied third-party
  whitespace, including the inherited protobuf whitespace and the 12 intentional
  CRLF vendor files covered by `.gitattributes -text`.
- [ ] Obtain approved ownership/publication or immutable storage for the full
  registry vendor tree and place it at relative `vendor/`.
- [ ] Complete registry-vendor verification and the accepted clean offline
  build matrix; registry independence is not claimed by Phase 2.
- [x] Migrate Flutter git packages and workflow third-party inputs in Phase 3.
- [ ] Stop for branch provenance, commit, push, PR, and publication gates.

## Phase 3 — Flutter/workflows

- [x] Inventory the eight Flutter git package inputs and the TopMostWindow
  workflow source.
- [x] Copy exact pinned source trees into `third_party/flutter/` and
  `third_party/windows/`, excluding only `.git` metadata; 967 current tree
  files copied across 9 source roots, all retained as Flutter/TopMost integrity
  inputs, including the six `window_size` example generated files.
- [x] Switch active Flutter manifests/lock entries to local `path` sources
  while preserving versions and package path semantics.
- [x] Switch the reusable TopMostWindow workflow to the tracked source and
  preserve its MSBuild project/output behavior and pin validation.
- [x] Add and run `scripts/check-flutter-source-ownership.py` over active
  manifests, locks, workflows, copied third-party manifests, and nested
  `.gitmodules`; document active owned inputs and non-build example exceptions
  in `third_party/source-ownership.yaml`.
- [x] Pin active root workflows' `actions/checkout` to the approved immutable
  SHA `3d3c42e5aac5ba805825da76410c181273ba90b1`, including TopMostWindow.
- [x] Remove the accidental `third_party/nokhwa/examples/.DS_Store` artifact.
- [x] Record exact refs, tree-derived file counts, SHA-256 digests, source
  comparisons, and limitations in `plan.md`; no Flutter SDK/engine/toolchain,
  full Cargo registry vendor tree, or generated build archive was copied.
  Non-owned external build inputs remain unproven/absent. The active
  `hwcodec/externals` tree was copied and retained as owned build input. The
  pre-cleanup hwcodec inventory was 440 files; four untracked per-user
  `.vcxproj.user` outputs were removed, leaving 436 current integrity files
  (9,010,415 bytes; header, C++, project metadata, and required binary/library
  inputs). The current externals-only inventory is 360 files and 8,382,509
  bytes; the old 361-file figure included one removed per-user output.
- [x] Run YAML/lock consistency checks, ownership integrity checks, Cargo
  metadata/source checks, and current worktree/index `git diff --check` checks;
  they are clean. The full commit-range check reports inherited/copied
  third-party whitespace, including the inherited Phase 1 protobuf whitespace
  and the 12 intentional CRLF vendor files covered by `.gitattributes -text`.
- [x] Record the two exact source-preserved informational comment URLs in
  `window_controller.dart` (lines 40 and 102) as path/line/content/URL entries
  with reason `source-preserved documentation only`; executable code,
  manifests, workflows, and `.gitmodules` remain forbidden-URL scanned.
- [ ] Defer `flutter pub get` and Flutter build validation to the repository's
  GitHub Actions/F-Droid build workflows and their toolchains; local
  Flutter/Dart tooling is not required for this migration. No real GitHub
  Actions run has been performed, and the accepted build matrix remains
  unverified.
- [ ] Stop for review before commit, push, PR, fork, release, or publication.

### Final policy-fix review

- [x] Enumerate and structurally scan both `.cargo/config.toml` and
  `.cargo/config.vendor.toml`; scope nested `.gitmodules` discovery to declared
  owned roots/repository metadata while excluding generated `target/` output.
- [x] Leave `flutter/android/flutter_hbb_android.iml` unchanged after verifying
  it is pre-existing tracked IDE metadata (`6de0fa781`, 2022), not a migration
  artifact.
- [x] Remove `.rc`/`.map` from binary suffix exclusions; UTF-8 scan text inputs
  and inspect the copied Sciter `SAr` resource archive by content signature.
- [x] Add exact path/line/content/URL allowlist records for the two preserved
  `rustdesk-org` comment references in `window_controller.dart` (lines 40 and
  102), with reason `source-preserved documentation only`.
- [x] Keep all other forbidden URLs rejected in owned code, manifests,
  workflows, and `.gitmodules`; verify the allowlist is not broadened by
  `.gitignore` and that symlink/link integrity remains fail-closed.
- [x] Re-run ownership with the TopMost pin, both external vendor verifiers,
  Cargo source/metadata checks, YAML/workflow checks, the negative forbidden
  URL test, link-integrity tests, and current worktree/index `git diff --check`
  checks. The full commit-range check reports inherited/copied third-party
  whitespace, including the inherited protobuf whitespace and the 12 intentional
  CRLF vendor files covered by `.gitattributes -text`. Exact results and
  unavailable blockers are recorded in `plan.md` above.
- [x] Preserve the 12 approved vendor files' CRLF bytes with exact `-text`
  attributes; both external vendor inputs match those bytes byte-for-byte.

### Phase 3 review-fix limitations

- The copied `desktop_multi_window/example` remains exact source-preserved
  non-build material, so its upstream Git dependency is excluded from the
  active ownership scan rather than rewritten.
- `third_party/hwcodec` is an active owned Cargo input: `build.rs` consumes
  `externals/`. Its copied `externals` contents remain. Nested `kcp-sys`, tao,
  and webm `.gitmodules` declarations are explicitly classified as active or
  inert with source-presence validation; tao and webm absent targets are
  documented inert metadata, not silently ignored.
- Integrity hashing includes all copied `window_size` source, including its six
  example generated files; changing root `.gitignore` cannot omit a source
  file. The documented-reference allowlist is separate from integrity and
  cannot be broadened by `.gitignore`. Current Flutter/TopMost integrity total
  is 967 files; current cleaned hwcodec integrity total is 436
  (pre-cleanup: 440). There are 10 integrity roots, not 11.

## Phase 4 — Proof (not executed)

- [ ] Define the accepted platform/build matrix and required caches/vendor
  inputs.
- [ ] Run network-isolated locked metadata/build checks.
- [ ] Document residual external dependencies and do not claim final offline
  independence until acceptance criteria pass.
