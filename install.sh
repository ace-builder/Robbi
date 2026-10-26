#!/usr/bin/env bash
# Robbi first-install entry point. Run as your own Linux user, without sudo.
set -Eeuo pipefail
umask 077
REPOSITORY='ace-builder/Robbi'
VERSION='latest'
SOURCE=''
PLAN=0
usage() {
  printf '%s\n' 'Robbi installer' \
    'Usage: bash install.sh [--version TAG] [--source DIRECTORY] [--plan]' \
    'Target: Raspberry Pi Zero 2 W, Raspberry Pi OS Bookworm Lite 64-bit.' \
    'A published runtime release is required for downloads. --source uses a local release.'
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
[[ "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]] || { echo 'Invalid release tag.' >&2; exit 2; }
command -v python3 >/dev/null || { echo 'Python 3 is required. Use Raspberry Pi OS Bookworm Lite 64-bit.' >&2; exit 1; }
if [[ -z "$SOURCE" && -f "${BASH_SOURCE[0]:-}" ]]; then
  local_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  if [[ -f "$local_dir/src/robbi/setup/install.py" ]]; then SOURCE="$local_dir"; fi
fi
temporary=''
cleanup() { if [[ -n "$temporary" && -d "$temporary" ]]; then rm -rf -- "$temporary"; fi; }
trap cleanup EXIT
if [[ -z "$SOURCE" ]]; then
  command -v curl >/dev/null || { echo 'curl is required: sudo apt install curl' >&2; exit 1; }
  temporary="$(mktemp -d "${TMPDIR:-/tmp}/robbi-install.XXXXXXXX")"
  if [[ "$VERSION" == latest ]]; then
    release_url="https://api.github.com/repos/$REPOSITORY/releases/latest"
  else
    release_url="https://api.github.com/repos/$REPOSITORY/releases/tags/$VERSION"
  fi
  if ! curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --connect-timeout 10 --max-time 45 \
      --proto-redir '=https' --max-filesize 2097152 "$release_url" -o "$temporary/release.json"; then
    echo 'No downloadable Robbi runtime release is available at this address yet.' >&2
    echo 'The installer is ready; runtime assets may still be in preparation. No system changes were made.' >&2
    exit 1
  fi
  python3 - "$temporary" "$REPOSITORY" <<'PY'
import json,pathlib,sys,urllib.parse
folder=pathlib.Path(sys.argv[1]);repo=sys.argv[2]
release=json.loads((folder/'release.json').read_text())
if release.get('draft') or release.get('prerelease'):
    raise SystemExit('Choose a published stable runtime release.')
assets={a['name']:a.get('browser_download_url','') for a in release.get('assets',[])}
urls=[]
for name in ('robbi-runtime.tar.gz','robbi-runtime.tar.gz.sha256'):
    url=assets.get(name,'');p=urllib.parse.urlsplit(url)
    if p.scheme!='https' or p.netloc!='github.com' or not p.path.startswith('/'+repo+'/releases/download/'):
        raise SystemExit('This release does not contain the complete Robbi installer assets. No system changes were made.')
    urls.append(url)
(folder/'urls').write_text('\n'.join(urls)+'\n')
PY
  mapfile -t urls < "$temporary/urls"
  for n in 0 1; do
    name='robbi-runtime.tar.gz'; limit=1073741824
    if ((n == 1)); then name+='.sha256'; limit=4096; fi
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --silent --show-error --location \
      --connect-timeout 10 --max-time 1800 --retry 2 --max-filesize "$limit" "${urls[$n]}" -o "$temporary/$name"
  done
  python3 - "$temporary" <<'PY'
import hashlib,pathlib,re,sys,tarfile
folder=pathlib.Path(sys.argv[1]);archive=folder/'robbi-runtime.tar.gz'
tokens=(folder/'robbi-runtime.tar.gz.sha256').read_text().split()
if not tokens:raise SystemExit('Missing runtime checksum.')
expected=tokens[0]
if not re.fullmatch('[a-fA-F0-9]{64}',expected):raise SystemExit('Invalid runtime checksum file.')
h=hashlib.sha256()
with archive.open('rb') as f:
    for block in iter(lambda:f.read(1048576),b''):h.update(block)
if h.hexdigest()!=expected.lower():raise SystemExit('Runtime checksum mismatch; nothing was installed.')
destination=folder/'release';destination.mkdir();seen=set();total=0
with tarfile.open(archive,'r:gz') as source:
    members=[]
    for entry in source:
        if len(members)>=50000:raise SystemExit('Runtime archive contains too many files.')
        p=pathlib.PurePosixPath(entry.name)
        name=str(p)
        if p.is_absolute() or '..' in p.parts or '\\' in entry.name or ':' in entry.name or name in seen or name=='.' or any(ord(c)<32 for c in entry.name):
            raise SystemExit('Unsafe runtime archive path.')
        if not (entry.isfile() or entry.isdir()) or any(part.startswith('.env') for part in p.parts):
            raise SystemExit('Runtime archive contains unsupported or private files.')
        seen.add(name);members.append(entry);total+=entry.size
        if total>2147483648:raise SystemExit('Unpacked runtime exceeds the size limit.')
    # Write regular files ourselves; no symlinks, ownership or special modes.
    for entry in members:
        target=destination/entry.name
        if entry.isdir():target.mkdir(parents=True,exist_ok=True);continue
        target.parent.mkdir(parents=True,exist_ok=True)
        with source.extractfile(entry) as incoming,target.open('wb') as outgoing:
            import shutil
            shutil.copyfileobj(incoming,outgoing)
SOURCE=destination/'robbi-v2'
if not (SOURCE/'src/robbi/setup/install.py').is_file():raise SystemExit('Runtime release is incomplete.')
PY
  SOURCE="$temporary/release/robbi-v2"
fi
[[ -f "$SOURCE/src/robbi/setup/install.py" && -f "$SOURCE/pyproject.toml" ]] || { echo 'Choose a complete Robbi runtime release with setup support.' >&2; exit 1; }
if ((PLAN)); then
  python3 "$SOURCE/src/robbi/setup/install.py" --source "$SOURCE" --plan
else
  [[ "$EUID" != 0 ]] || { echo 'Run this installer as your own Linux user, without sudo.' >&2; exit 1; }
  [[ -r /dev/tty && -w /dev/tty ]] || { echo 'Use an interactive SSH terminal (ssh -t) for setup.' >&2; exit 1; }
  python3 "$SOURCE/src/robbi/setup/install.py" --source "$SOURCE" </dev/tty
fi
