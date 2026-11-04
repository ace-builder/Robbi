# Portable app projects and package metadata

An app's executable behavior belongs inside its project folder. Its ZIP includes `app.yaml`, the entrypoint, project-relative helper modules, data files and optional images/dashboard files. The 26 bundled apps that previously imported central app handlers now carry their implementation in `implementation.py`; game case/card data also travels with the relevant project. The remaining bundled apps already contained their own handlers.

There is no central registry to edit when adding an ordinary project. Discovery reads the manifest, creates settings/tool forms and makes enabled tools available to conversations. Use relative imports inside a project. Shared SDK capabilities, error types, schema bounds and provider adapters remain runtime services. Importing a robot controller or another app's implementation creates avoidable coupling; call a declared read-only tool through `context.call_app()` when composition is needed.

The core retains compatibility voice parsers for six games. They only route to installed, enabled projects and contain no game execution or rendering engine. Ordinary new apps use the manifest tools/triggers and generic conversation catalog. Removing a game never causes an import of its missing source.

## Manifest metadata

Existing API v2 manifests still work with defaults. A complete new manifest can include:

```yaml
api_version: 2
id: my-notebook
name: My Notebook
version: 1.2.0
description: Keep short local notes and retrieve them by topic.
author:
  name: maker Example
  url: https://example.org/maker
category: productivity
tags: [notes, offline]
homepage: https://example.org/my-notebook
license: MIT
images:
  icon: assets/icon.png
  screen: assets/screen.png
requires:
  sdk: 2
  runtime: '>=2.0.0,<3.0.0'
  python: '>=3.11,<3.13'
  dependencies:
    - name: Pillow
      version: '>=11.3,<13'
entrypoint: main:run
permissions: [state]
# tools and config_schema follow the normal API v2 schema.
```

`author` is an object with a required nonempty name and optional HTTP(S) URL. Homepage and author links cannot embed credentials. Categories use lowercase letters, digits and hyphens; up to 12 distinct tags have at most 40 characters each. License is bounded descriptive/SPDX text; the runtime does not infer a license when none is supplied. Older manifests default to author `Unknown`, category `other`, empty tags/links/license/images and SDK 2.

Release versions are canonical numeric `major.minor.patch` values and compare numerically: `1.10.0` is newer than `1.9.0`. Legacy `1`/`1.0` values normalize to `1.0.0`. Leading zeros and prerelease/build labels are not supported for app-store release ordering. Package updates reject downgrades; an explicitly imported same-version replacement remains possible for local development. A store can impose stricter immutable-version rules.

Images must be confined local static PNG, JPEG or WebP files. The icon is exactly **1024×1024**, the screen image exactly **280×240**, and each file is at most 4 MiB. Inspection checks dimensions, format and actual decoding. SVG, animated files, wrong dimensions and corrupt images are rejected. Declared images must be included by the packer. Optional absent images allow existing apps to use dashboard fallbacks; a publishing server may require both images before accepting an app.

`requires.dependencies` lists **Python distributions**, not other app IDs and not shell commands. Dependency names and numeric version rules are validated. Runtime, Python and SDK compatibility are checked before install/restore; distribution versions use installed package metadata and PEP 440 comparison. Missing or incompatible requirements are reported in preview. The importer never runs pip, package installers, setup scripts or app Python. Cross-app composition checks the target's current availability at invocation time and remains subject to its permissions.

## Authoring and preview

```console
python tools/apps/app_project.py new ../my-notebook --id my-notebook --author "maker Example"
python tools/apps/app_project.py check ../my-notebook
python tools/apps/app_project.py pack ../my-notebook --output ../my-notebook.zip
python tools/apps/app_project.py preview ../my-notebook.zip
```

`check` and `preview` never execute app code. Preview reports normalized manifest metadata, SHA-256 of the original ZIP, compressed/expanded sizes, file names, image metadata and compatibility findings. The reusable `robbi.app_runtime.packages.inspect_archive(bytes)` API returns JSON-safe metadata; store ingestion can pass `include_images=True` to also receive the validated image bytes. Library-specific `Packages.preview()` adds install/update/restore intent, the previous manifest and capability additions/removals.

The ZIP is bounded to 16 MiB compressed, 32 MiB expanded and 256 entries. Only unencrypted stored/deflated files are accepted. Traversal, duplicate case-insensitive paths, links, special files, Windows device names, path normalization ambiguities and files outside the single project are rejected. The packer additionally excludes local secrets, environment files and caches; authors still need to inspect their source before publishing. Imported app Python executes in an isolated process after installation and enablement; archive validation and signatures remain separate checks.

## Updates, removal and restore

Installed code lives separately from app-owned settings, state and artifacts. Updates stage a validated tree, retain the old tree during the rename/commit sequence and restore it if the operation fails. An installed update to a bundled app is an overlay; the original bundled source is never overwritten. The host manages enabled status, compatible settings and capability consent separately. Newly requested capabilities need an owner decision; an update does not silently gain additional access.

Removing a bundled app writes a persistent tombstone, hiding it from discovery without deleting source. Removing an installed app moves its code into retained storage and records the tombstone. Restore revalidates and returns the retained version. Removing an installed overlay also hides the underlying bundled version, so it does not unexpectedly reappear. Restore returns that retained overlay. The package layer never deletes app state/configuration or automatically reinstates permission grants; the host applies its explicit restore policy.

The library allows 48 active projects and retains at most 48 removed-app records. Retained metadata is bounded to 4 MiB. Remove a currently installed app to make room for another, or restore a removed app before removing another when retained history is full. Old code is not silently discarded. Corrupt tombstone metadata blocks discovery/changes rather than reactivating removed apps. Automatic purging of user data and removed projects is deliberately outside this lifecycle.

`tests/test_app_packages_metadata.py` covers metadata, images, compatibility, no-execution preview, overlays, persistent tombstones, restoration and failure rollback. `tests/test_app_portability.py` installs each migrated project ZIP into an otherwise empty app library and loads its handler; representative projects also execute through the real SDK. The existing app suites patch the actual project modules and continue testing behavior, persistence, permissions and undo.

Current setup and isolation contract: [security and review notes](../security-and-review.txt). Imported apps cannot bypass OS isolation with the SDK development permission switch.
