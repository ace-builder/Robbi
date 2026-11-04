# Publish the repository

Keep the repository root limited to `README.md`, `build/` and `software/`. Copy the contents of the prepared `Robbi` folder, rather than uploading the outer delivery ZIP or adding another nested `Robbi/` directory.

Use Git or GitHub Desktop for the complete source tree. A browser upload is limited to 100 files at a time and 25 MiB per file. Keep the enclosure STL compressed at `build/hardware/prints/robo_4_astra.zip`.

The public installer is `software/install.sh`. Its pinned source snapshot must include the matching `software/release-manifest.json` and every listed runtime file. The hardware guide belongs under `build/`, with images under `build/assets/` and manufacturing/print files under `build/hardware/`.

Follow [release preparation](RELEASING.md) after intentional source or asset changes. Integrity files must describe the final bytes and paths. Verify repository-wide checksums from the repository root with `sha256sum -c build/SHA256SUMS`.

Never publish credentials or generated owner data. Confirm a fresh-card installation on the supported Pi before claiming a fully tested installation. See [licensing](../../LICENSE.md) and [first setup](../../SETUP.md).
