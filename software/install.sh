#!/usr/bin/env bash
# Run as the Linux user created in Raspberry Pi Imager, without sudo.
set -Eeuo pipefail
umask 077
REPOSITORY='ace-builder/Robbi'
VERSION='main'
SOURCE=''
PLAN=0
# Filled from the reviewed bootstrap bytes during local packaging.
BOOTSTRAP_SHA256='5bcfebd5e402114eb2960c05281fb380c225790b8d00479a4fdff791c9db9490'
usage() {
  printf '%s\n' 'Robbi setup for Raspberry Pi Zero 2 W / Bookworm Lite 64-bit.' \
    'Usage: bash install.sh [--version TAG_OR_COMMIT] [--source RUNTIME_DIRECTORY] [--plan]' \
    'Installs the sealed software snapshot, then opens the guided terminal setup.'
}
while (($#)); do
  case "$1" in
    --version|--source)
      (($# >= 2)) || { usage; exit 2; }
      if [[ "$1" == --version ]]; then VERSION="$2"; else SOURCE="$2"; fi
      shift 2 ;;
    --plan) PLAN=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown installer option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]] || { echo 'Invalid version.' >&2; exit 2; }
[[ "$VERSION" != latest ]] || VERSION=main
command -v python3 >/dev/null || { echo 'Python 3 is required. Use Bookworm Lite 64-bit.' >&2; exit 1; }
if [[ -z "$SOURCE" && -f "${BASH_SOURCE[0]:-}" ]]; then
  local_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  if [[ -f "$local_dir/software/src/robbi/setup/install.py" ]]; then SOURCE="$local_dir/software"
  elif [[ -f "$local_dir/src/robbi/setup/install.py" ]]; then SOURCE="$local_dir"; fi
fi
if ((PLAN)) && [[ -z "$SOURCE" ]]; then
  printf '%s\n' 'Resolve a pinned GitHub commit; download and verify its sealed runtime.' \
    'Install OS/Python dependencies, hardware interfaces and robbi-v2.service.' \
    'Guide owner name, dashboard password, Wi-Fi, provider keys/model, voice and optional Telegram.' \
    'Run connection/interface checks; finish in the dashboard. No system changes made.'
  exit 0
fi
temporary=''
cleanup() { if [[ -n "$temporary" && -d "$temporary" ]]; then rm -rf -- "$temporary"; fi; }
trap cleanup EXIT
if [[ -z "$SOURCE" ]]; then
  command -v curl >/dev/null || { echo 'Install curl first: sudo apt install curl' >&2; exit 1; }
  temporary="$(mktemp -d "${TMPDIR:-/tmp}/robbi-install.XXXXXXXX")"
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --silent --show-error --location \
    --connect-timeout 10 --max-time 45 --max-filesize 2097152 \
    "https://api.github.com/repos/$REPOSITORY/commits/$VERSION" -o "$temporary/commit.json"
  commit="$(python3 - "$temporary/commit.json" <<'PY'
import json,re,sys
value=json.load(open(sys.argv[1]))['sha']
if not re.fullmatch('[a-f0-9]{40}',value):raise SystemExit('Invalid source commit response.')
print(value)
PY
)"
  echo "Downloading Robbi source $commit. No separately published Release is required."
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --silent --show-error --location \
    --connect-timeout 10 --max-time 60 --max-filesize 1048576 \
    "https://raw.githubusercontent.com/$REPOSITORY/$commit/software/src/robbi/setup/source_archive.py" \
    -o "$temporary/source_archive.py"
  python3 - "$temporary/source_archive.py" "$BOOTSTRAP_SHA256" <<'PY'
import hashlib,pathlib,sys
if hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()!=sys.argv[2]:
    raise SystemExit('Installer helper checksum mismatch. Re-download install.sh; no system changes were made.')
PY
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --show-error --location \
    --connect-timeout 10 --max-time 1800 --retry 2 --max-filesize 536870912 \
    "https://codeload.github.com/$REPOSITORY/tar.gz/$commit" -o "$temporary/source.tar.gz"
  python3 "$temporary/source_archive.py" --archive "$temporary/source.tar.gz" \
    --destination "$temporary/runtime" --commit "$commit"
  SOURCE="$temporary/runtime"
fi
[[ -f "$SOURCE/src/robbi/setup/install.py" && -f "$SOURCE/release-manifest.json" ]] || {
  echo 'Choose the prepared software directory containing release-manifest.json.' >&2; exit 1;
}
if ((PLAN)); then
  python3 "$SOURCE/src/robbi/setup/install.py" --source "$SOURCE" --plan
else
  [[ "$EUID" != 0 ]] || { echo 'Run as your own Linux user, without sudo.' >&2; exit 1; }
  [[ -r /dev/tty && -w /dev/tty ]] || { echo 'Use interactive SSH (ssh -t) for guided setup.' >&2; exit 1; }
  python3 "$SOURCE/src/robbi/setup/install.py" --source "$SOURCE" </dev/tty
fi
