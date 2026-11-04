# Public and private Robbi development

Architecture agreed before public preparation, 2026-10-26. Nothing is uploaded by this workflow.

- **Public Robbi:** reusable runtime, installer, dashboard, empty-state memory services, the full generic app library, build guide and explicitly reviewed media. A clean source tree with its own history. No export of the development repository's history.
- **Private Robbi extensions:** independent repository, not a fork of the public GitHub repository. Contains the optional training recorder and experimental additions. It depends on a versioned public runtime. Promote reviewed features as small patches; never merge private history or copy the whole private checkout into the public repository.
- **Local data:** credentials, owner configuration, memories, maps, chats, camera captures, wake samples, training datasets, logs, filming takes and custom voices remain outside version control. Even a private repository is not a secret store or a backup for camera recordings.

GitHub has repository-level visibility; there are no private branches inside a public repository. A public fork also remains public. Keep public and private remotes in separate local repositories. The prepared public tree has no push remote until publication is authorized.

The public core exposes an optional event-extension interface with no persistence by default. An explicitly enabled, separately installed private package can observe events and register its own routes/dashboard panel. The public archive contains no training recorder implementation or training dashboard bundle. Public installs cannot enable a recorder by flipping a checkbox.

User settings and secrets remain injected at first run. Hardware defaults describe the supported board, not one repaired prototype. New memories and app state are created empty on the target Pi. Experimental autonomous navigation and motion remain opt-in after calibration.

Media policy: enumerate each file, record SHA-256 and intended use, inspect visual contact sheets, and keep speech/music with unclear redistribution rights pending. Approved bytes are selected by hash, not filename alone. A photo placed in an asset directory must not silently become public. Source provenance may contain private paths; only sanitized credits accompany public media.

Storage: this snapshot keeps the reviewed code, tests, artwork and media in Git, with every file below 25,000,000 bytes. The installer downloads a pinned source commit and verifies only its runtime subset against `release-manifest.json`; a separate GitHub Release is optional. Larger future media packs can use immutable Releases or Git LFS when needed. Owner data and private Git history never belong in the public tree.

Release gate: explicit file selection, blocked secret/data paths and suffixes, text scan without printing matched secrets, asset-hash verification, clean-tree and archive scan, fresh-state boot/API tests, then human approval for publication. A preparation report lists withheld assets and unresolved licensing/specification items. Tests, .gitignore and scanning supplement selection; none proves arbitrary unreviewed files safe.

References:
- https://docs.github.com/en/repositories/creating-and-managing-repositories/about-repositories
- https://docs.github.com/en/pull-requests/reference/forks
- https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-git-large-file-storage
