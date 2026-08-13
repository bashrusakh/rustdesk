# Upstream-Independent Build Migration

## Goal

Make RustDesk build from owned, tracked source/vendor inputs without the
RustDesk upstream submodule or a `rustdesk-org` network dependency. This plan
covers the local source/vendor migration only. External fork publication,
remote changes, commits, pushes, and release/publication work are separate
gated activities and are not part of this execution.

## Baseline (Phase 0)

- Worktree: `/home/bash/projects/rustdesk-independent`
- Base: `origin/rustqs/min-test`
- Baseline HEAD: `3fc94150f05eb41ea7b40ec0c8e69dc798795223`
- Worktree was clean before this migration.
- `libs/hbb_common` was a gitlink at `a920d00945e1d2441b3f77b2677054cb8c3d9dd2`.
- `.gitmodules` contained only `libs/hbb_common`, sourced from
  `https://github.com/rustdesk/hbb_common`.
- Established inventory: 31 Cargo git dependencies in active manifests;
  Flutter git packages; workflow third-party sources; no final offline proof
  yet.
- The checked-out `libs/hbb_common` source was clean, detached at the gitlink
  commit, and contained the existing deterministic `BUILD_DATE` generation in
  `src/lib.rs`. No new BUILD_DATE change is introduced by this phase.
- Before the change, `cargo metadata --locked --no-deps --format-version 1`
  reported 9 workspace members including `hbb_common`.

## Phases and stop gates

### Phase 0 — Baseline and inventory

Record the branch/base, worktree status, gitlink commit, submodule metadata,
and known dependency/source inventory. Scan for submodules and confirm the
scope boundary.

**Stop gate:** stop if the worktree is not clean, the target base is not
`origin/rustqs/min-test`, unexpected submodules exist, or the checked-out
`hbb_common` source is dirty or cannot be identified deterministically.

### Phase 1 — Local `hbb_common` source ownership

Copy the checked-out `libs/hbb_common` source at `a920d009` into the parent as
ordinary tracked files. Remove the `libs/hbb_common` gitlink and remove
`.gitmodules` because it has no other entries. Keep the existing Cargo path
member/dependency declarations unchanged. Preserve source content exactly,
including the existing deterministic BUILD_DATE generator; do not invent a
new BUILD_DATE modification.

**Stop gate:** stop if the source cannot be copied without loss, if another
submodule is discovered, if `.gitmodules` contains another entry, or if the
gitlink cannot be replaced safely. Do not begin Phase 2 in this phase.

### Phase 2 — Cargo dependency source migration

Copy the 44 exact locked package roots formerly supplied by git sources from the locally verified vendor tree
into tracked `third_party/` and change active manifests to relative path
dependencies, preserving package versions, features, optionality, and target
conditions. Update copied nested manifests only where required for path-owned
family members (cacao → Core Foundation, cpal → cidre, pam → pam-sys,
tfc → x11, plus nokhwa/tao/Core Foundation/webm path relationships).

Registry dependencies remain registry dependencies. `.cargo/config.vendor.toml`
continues to describe the future relative `vendor/` registry source and forces
offline mode, but no full registry vendor tree is copied in this phase.

`scripts/verify-cargo-vendor.py` records the external vendor provenance using a
deterministic file-tree SHA-256 in both directory and archive-compatible form:
sorted `vendor/<path>\0file\0SHA256(file-bytes)\n` records. Directory mode must
match `vendor-provenance.json`'s `vendor_tree.tree_sha256`; archive mode retains
the immutable archive SHA-256 and member-count checks. The available external
directory and archive produce the same tree digest.

**Stop gate:** every changed dependency must have an owned source and a
reproducible resolution; no URL substitution may be accepted without a
focused build/metadata check.

**Current blocker:** full registry-vendor ownership/publication remains open.
Metadata succeeds with the verified external vendor directory override, while
the committed relative `vendor/` directory is intentionally absent. Phase 2
does not claim registry independence.

### Phase 3 — Flutter and workflow source migration

Copy the exact resolved Flutter package roots and the exact
`RustDeskTempTopMostWindow` source into tracked `third_party/` paths. Active
Flutter dependencies use relative `path:` entries and the lockfile preserves
the package versions and dependency graph while changing only the source type
to `path`. The reusable TopMostWindow workflow checks out the repository,
validates the local owned source manifest pin, and builds the preserved
project/output paths without a source clone.

The owned source manifest is `third_party/source-ownership.yaml`. It records
these exact refs and current tree-derived integrity counts:

| Input | Ref | Source files |
| --- | --- | ---: |
| `dash_chat_2` | `bd6b5b41254e57c5bcece202ebfb234de63e6487` | 133 |
| `desktop_multi_window` | `b47e8385e5a75d38319ad706a64b0ead3108b093` | 115 |
| `dynamic_layouts` | `24cb88413fa5181d949ddacbb30a65d5c459e7d9` | 145 |
| `flutter_gpu_texture_renderer` | `08a471bb8ceccdd50483c81cdfa8b81b07b14b87` | 49 |
| `flutter_texture_rgba_renderer` (`texture_rgba_renderer`) | `42797e0f03141dc2b585f76c64a13974508058b4` | 98 |
| `uni_links` | `f416118d843a7e9ed117c7bb7bdc2deda5a9e86f` | 111 |
| `window_manager` | `85789bfe6e4cfaf4ecc00c52857467fdb7f26879` | 110 |
| `flutter-desktop-embedding/plugins/window_size` | `eb3964990cf19629c89ff8cb4a37640c7b3d5601` | 181 |
| `RustDeskTempTopMostWindow` | `ecd8d6a139eee76845ea66423fb739af450fda90` | 25 |

The copied source total is 967 current tree files across the eight Flutter
roots and TopMost root. Repository `.git` metadata is absent, and every copied
source file, including the six `window_size` example generated files, is an
integrity input; mutable `.gitignore` rules cannot omit source. Package assets,
licenses, and tracked symlinks are retained.
No Flutter SDK, Flutter engine/toolchain, full Cargo registry vendor tree, or
generated build archive was copied. Non-owned external build inputs remain
unproven/absent. The active `third_party/hwcodec` input is a separate owned
source tree and is present. Its pre-cleanup inventory was 440 files; four
untracked per-user `.vcxproj.user` outputs were removed, leaving 436 current
 integrity files and 9,010,415 bytes. The current externals-only inventory is 360 files,
8,382,509 bytes, consisting of 274 `.h`, 73 `.cpp`, 3 `.vcxproj`, 2 `.cmake`,
2 `.mk`, 2 `.txt`, and one each of `.dll`, `.filters`, `.lib`, and `.map`.
These checked-in
SDK/header/library inputs do not establish full SDK or engine independence.

**Stop gate:** active build workflows and Flutter dependency resolution must be
auditable without an unapproved RustDesk-org network dependency.

### Phase 4 — Offline/reproducibility proof (not executed here)

Run clean, locked metadata/build checks in a network-isolated environment and
record the exact cache/vendor inputs, platform scope, and residual limitations.

**Stop gate:** no final independence claim is made until the documented proof
passes for the accepted build matrix, or remaining gaps are explicitly
reported.

## Acceptance criteria

### Phase 0/1 acceptance (pre-migration baseline record)

1. Canonical `plan.md` and `todo.md` exist under this directory.
2. The parent repository records `libs/hbb_common` as ordinary files, not mode
   `160000`, and `.gitmodules` is absent because no other submodules exist.
3. The copied source matches the checked-out `a920d009` source, excluding the
   submodule's private `.git` metadata.
4. `Cargo.toml` still declares `libs/hbb_common` as a path workspace member and
   dependency.
5. `cargo metadata --locked --no-deps --format-version 1` succeeds and still
   includes `hbb_common`, when feasible in the environment.
6. `git diff --check` is run; any inherited whitespace in the exact migrated
   source is recorded without rewriting that source, and non-migrated files
   pass the check. The requested submodule scan is run and has no nested
   submodules.
7. Pre-migration baseline: no Cargo dependency URLs, Flutter package sources,
   workflows, or DeskForge files were changed by the Phase 0/1 work package.
   This is not a statement about the current Phase 2/3 worktree.

## Phase 0/1 verification record

- Source comparison against the checked-out submodule object at `a920d009`
  passed with no differences, excluding private `.git` metadata.
- The parent index now records 32 ordinary `100644` files under
  `libs/hbb_common`; the gitlink is gone.
- `.gitmodules` is absent, `git submodule status` and recursive submodule scan
  are empty, and no nested `libs/hbb_common/.git` remains.
- `cargo metadata --locked --no-deps --format-version 1` passed; it reported 9
  workspace members and one path member for `hbb_common`.
- Cargo TOML parsing passed for the parent and copied `hbb_common` manifests;
  existing parent path declarations are unchanged.
- `git diff --check` was run. It reports trailing whitespace inherited from
  the copied `a920d009` protobuf source (`message.proto` and
  `rendezvous.proto`). The source was not rewritten because Phase 1 is an
  exact source migration; `git diff --check` passes when the copied source
  path is excluded.
- The Phase 0/1 work package did not change Cargo dependency URLs, Flutter
  package sources, workflows, or DeskForge files. Later Phase 2/3 changes are
  recorded below. No commit or push was performed.

### Phase 2 verification record

- Copied 44 exact locked package roots formerly supplied by git sources from
  `/home/bash/projects/DeskForge/offline-kit/artifacts/rustdesk-src/vendor`
  into tracked `third_party/`; no full registry `vendor/` tree was copied.
- Root/library manifests and required copied nested manifests now use relative
  path dependencies. The root workspace excludes `third_party` so copied
  package workspace metadata cannot absorb the main workspace.
- Root and nested Cargo lockfiles retain their prior locked package versions,
  features, and checksums, with zero active git sources. Normal
  `cargo metadata --locked --format-version 1` and the externally overridden
  offline form both pass with 9 workspace members and 1,017 resolved metadata
  packages.
- `scripts/check-cargo-git-sources.py` passes across 54 manifests and 9
  lockfiles. `git diff --check` passes outside inherited Phase 1 whitespace in
  `libs/hbb_common/protos/message.proto` and `rendezvous.proto`.
- No DeskForge/PR #4 worktree was modified; no commit, push, fork, release, or
  publication was performed.

Remaining Phase 2 gates are the full registry vendor tree and the accepted
clean offline build matrix; Phase 2 does not claim registry independence.

### Phase 3 verification record (current)

- Exact source-tree comparisons passed for all eight Flutter package roots and
  the TopMostWindow root at the refs above. The manifest now verifies 967
  current Flutter/TopMost tree files plus 436 active hwcodec files using
  deterministic SHA-256 tree digests and byte counts; additions, removals, or
  changes fail closed.
- No `.git` directory was copied under `third_party/`.
- `python3 scripts/check-flutter-source-ownership.py` is stdlib-only and scans the active Flutter
  and copied third-party `pubspec.yaml`/`pubspec.lock` manifests, active
  workflows, every copied third-party `.gitmodules` file, and all files under
  the active owned `third_party/hwcodec` input for forbidden executable-source
  URLs. `hwcodec/build.rs` consumes `externals/`, so that tree is not inert;
  all nested `.gitmodules` declarations are classified as active/inert with
  source presence checks; an active missing target or forbidden active URL
  fails. `kcp-sys/kcp` is present and active, while tao's `deps/apk-builder`
  and webm's `src/sys/libwebm` declarations are absent and inert. The copied
  `third_party/flutter/desktop_multi_window/example` manifests are explicitly
  excluded and documented as non-build example material; their exact Git
  dependency is retained byte-for-byte. The two remaining `rustdesk-org` URLs
  in `third_party/flutter/desktop_multi_window/lib/src/window_controller.dart`
  are exact-line comment references at lines 40 and 102. They are listed in
  `scan_policy.allowed_reference_urls` with exact path, line, content, URL, and
  reason `source-preserved documentation only`. This is an allowlist for
  informational source-preserved comments only: executable code, manifests,
  workflows, and `.gitmodules` still reject every other `rustdesk-org` URL.
  Documentation/history outside declared owned roots remains outside the active
  scan scope.
- YAML parsing passed for the active workflows, Flutter manifests/lockfile,
  ownership manifest, and copied package manifests. Lock consistency checks
  confirmed all eight local paths exist and use `source: path` with unchanged
  versions.
- The reusable TopMostWindow workflow validates the exact ownership record
  name, path, and ref through the ownership scanner. The four remaining active
  checkout uses in `bridge.yml`, `rustqs-android.yml`, `rustqs-linux.yml`, and
  `rustqs-windows-min-test.yml` use the same approved full SHA
  `3d3c42e5aac5ba805825da76410c181273ba90b1`.
- Local `flutter pub get` was not run because local Flutter/Dart tooling is not
  required for this migration. Flutter dependency/build validation is deferred
  to the repository's GitHub Actions/F-Droid build workflows and their
  toolchains. No real GitHub Actions run has been performed, and the accepted
  build matrix remains unverified.
- Existing Cargo metadata and source checks remain green: locked no-deps
  metadata reports 9 workspace members; `scripts/check-cargo-git-sources.py`
  passes for 54 Cargo manifests and 9 lockfiles.
- `git diff --check` reports only inherited Phase 1 trailing whitespace in
  `libs/hbb_common/protos/message.proto` and `rendezvous.proto`; the Phase 3
  files introduce no whitespace errors.
- No other worktree was modified; no commit, push, fork, release, or
  publication was performed. Mutable non-checkout actions remain out of scope;
   this phase does not broaden action pinning beyond the documented checkout-only
   guarantee.

Repository-owned checks cover source trees, policy, manifests, workflow pins,
Cargo metadata, and available external vendor input. Approved but unavailable
 inputs remain separate gates: the committed full registry `vendor/` tree,
 CI-provided Flutter SDK/engine/toolchain validation, and the accepted offline
 build matrix.

### Final policy-fix verification (current)

- Cargo config discovery now enumerates actual files under `.cargo/`, so both
  `config.toml` and `config.vendor.toml` are structurally scanned; the Cargo
  scan count is 54 manifests, 2 config files, and 9 lockfiles.
- Nested `.gitmodules` discovery is scoped deterministically to declared owned
  roots and relevant repository metadata, pruning ignored/generated artifact
  directories such as `target/`; the three copied nested declarations remain
  discovered and classified. The tracked
  `flutter/android/flutter_hbb_android.iml` predates this migration (last
  introduced by `6de0fa781` in 2022) and is left unchanged as pre-existing IDE
  metadata.
- `.rc` and `.map` are no longer binary suffix exclusions. Text `.rc`/`.map`
  inputs are UTF-8 decoded and forbidden-URL scanned; the one copied Sciter
  `SAr` resource archive is recognized by its binary format signature and
  checked for raw forbidden URL bytes without treating the suffix itself as
  binary.
- The ownership scanner passed with the required TopMost pin
  `ecd8d6a139eee76845ea66423fb739af450fda90`: 35 active manifests, 3 nested
  `.gitmodules`, and 3,286 owned-root files scanned (including all 44 Cargo
  path-owned roots); all 10 integrity roots
  matched their recorded counts, byte counts, and SHA-256 tree digests.
- The exact documented-reference allowlist contains only
  `window_controller.dart:40` and `window_controller.dart:102`, each bound to
  its complete comment line and exact URL with reason `source-preserved
  documentation only`. A negative test rejected an unlisted
  `rustdesk-org` URL. The scanner still independently scans code, manifests,
  workflows, and every `.gitmodules`; the allowlist does not apply to those
  categories as a general URL exemption.
- A `.gitignore` bypass test confirmed ignored files are still included in
  owned-entry traversal, and a symlink/link integrity test confirmed an
  undeclared link is rejected. The positive scanner run also preserved the
  existing six-link allowlist and all integrity digests, including the six
  retained `window_size` example generated files.
- Both external Cargo vendor inputs passed:
  `/home/bash/projects/DeskForge/offline-kit/artifacts/rustdesk-src/vendor`
  and `/home/bash/projects/DeskForge/offline-kit/artifacts/vendor-1.4.8.tar.gz`
  each verified 1,005 registry packages and 44 copied package roots, including
  byte/file-list/checksum comparisons for every copied root under the explicit
  44-root contract. The Cargo
  source scan passed for 54 manifests and 9 lockfiles. Cargo metadata passed
  with 9 workspace members (`--no-deps`) and 9 members/1,017 packages (offline
  locked metadata). YAML parsing passed for 37 files; workflow checks passed
  for 5 workflow files and 5 full-SHA checkout refs.
- `git diff --check` passed when excluding only inherited whitespace in
  `libs/hbb_common/protos/message.proto` and `rendezvous.proto`. No copied
  source contents were changed, and no commit, push, or publication occurred.
- Local Flutter/Dart tooling was not used because it is not required for this
  migration. Flutter dependency/build validation remains deferred to the
  repository's GitHub Actions/F-Droid build workflows; no real GitHub Actions
  run has been performed, and the accepted network-isolated build matrix has
  not been executed. The committed relative full registry `vendor/` tree is
  intentionally absent. The external vendor directory/archive verification
  above is available-input evidence, not a claim of CI success or full offline
  independence.

### Final migration acceptance (future phases)

1. Active Rust, Flutter, and workflow inputs are owned/tracked or explicitly
   documented as accepted external inputs.
2. No RustDesk upstream submodule or `rustdesk-org` network dependency remains
   in the active build path, subject to documented third-party exceptions.
3. Locked metadata and the accepted build matrix pass from the owned inputs.
4. Offline/reproducibility proof is recorded; no proof is implied by local
   source vendoring alone.

## Scope distinction

Local migration means changing this worktree's tracked source, manifests,
vendor inputs, and validation records. External fork publication means creating
or updating a remote fork/repository, branches, commits, pushes, PRs, releases,
or other public artifacts. The latter is explicitly out of scope here and
requires separate approval.
