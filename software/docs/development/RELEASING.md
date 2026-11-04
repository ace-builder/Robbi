# Prepare a public release

Do development in the public source tree; install it alongside private extensions for personal experiments. Keep credentials and generated data outside both repositories. Changes needed by all robots belong in focused public patches. Never merge private Git history into the public repository.

1. Review changed source, tests, configuration defaults and assets. Run tests. Check text for secrets, personal details, private addresses and prototype-only defaults. Review images/video content and metadata and audio provenance. Automated scans cannot establish that an arbitrary photo or spoken clip is safe to publish.
2. For reviewed media, update `approved-media.json` with its path, SHA-256, byte count and review basis. A renamed camera photo or modified approved file must not inherit an old approval.
3. Run the local-only commands below. `--seal` is an explicit review step, not a command to run automatically in CI.

   ```sh
   python tools/release/build_public_release.py --seal
   python tools/release/build_public_release.py --output ../release-candidate
   ```

4. Inspect the resulting archive and checksum and run a fresh-state smoke test. Validate on a fresh Bookworm card and the supported board. Confirm the software/art license and generated-media terms before making a release public.
5. Publish only after the owner authorizes it. No tool here pushes, creates a repository or uploads a release.

`approved-files.json` seals every selected source/asset hash. Added files and changed bytes cause packaging to fail until reviewed again. Secret/data paths, credential patterns and unapproved binary files are blocked. The archive is reopened and checked against its per-file manifest. Source/user names are not written into tar ownership fields.

The repository root contains the project overview. The illustrated hardware guide is in `build/`; `software/` is the runtime source tree. The installer in `software/install.sh` resolves an immutable GitHub commit, downloads its source snapshot and extracts only the file set in `software/release-manifest.json`. A GitHub Release is optional, not a prerequisite. Keep each repository file below 25,000,000 bytes for browser upload; use the existing compressed STL distribution. Do not commit the outer delivery ZIP.

After `--seal`, generate the source-install manifest with `python tools/release/build_public_release.py --manifest`. This rechecks the approved source bytes and writes only the runtime subset. The integrity manifests exclude themselves from their own file lists. Regenerate both after any intentional change; never approve unseen media automatically.

The runtime archive excludes build-guide files, Python/browser tests, development tools and authoring source media because they are not runtime dependencies. A separate `robbi-source-assets.tar.gz` contains the software tree’s reviewed artwork originals, their authoring recipes and source licenses, with its own checksum. Use those on the development computer; the installer does not put them on the Pi. Keep the illustrated guide and manufacturing source together when preparing the repository; do not publish stale links to withheld assets. Release checksums detect integrity changes; they do not replace trusting the publisher.

Some media-authoring tests additionally inspect exported preview GIFs or isolated audio masters. Those optional personal-workstation outputs are not bundled. Tests explicitly skip that preview portion when it is absent; checks of the shipped sprites, audio and video still run. Generate new previews locally when working on the art. Original account voice masters are not a prerequisite for running the robot.

The repository-wide checksums are stored in `build/SHA256SUMS`; verify them from the repository root with `sha256sum -c build/SHA256SUMS`. Refresh these after the software manifests and any build-guide changes.
