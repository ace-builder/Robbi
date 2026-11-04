"""Package a reviewed public snapshot locally. This tool never uses Git/network.

First review the source and each media file, then create approved-files.json
with --seal. Changed/new bytes require another review; --output never approves.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src'))
from robbi.setup.install import safe_path, REQUIRED

TEXT = {'.py','.js','.mjs','.css','.html','.json','.jsonl','.yaml','.yml','.toml','.txt','.md','.sh','.svg','.csv'}
CACHE = {'.git','.venv','__pycache__','.pytest_cache','node_modules','test-results','playwright-report'}
FORBIDDEN = {'var','data','datasets','recordings','captures','logs','backups','.ssh'}
SECRET = re.compile(r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|tvly-(?:dev-|prod-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|AIza[A-Za-z0-9_-]{30,})|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----')
HOST = re.compile(r'192\.168\.\d+\.\d+|\b[A-Z]:[/\\](?!(?:Windows|Program Files)[/\\])[\w. -]+[/\\]|/home/(?!USER/|user/)[a-z][\w-]*/',re.I)
LEDGER='approved-files.json'
AUDIO = {'.wav','.mp3','.ogg','.flac','.m4a','.aac','.aiff'}
MAX_FILE = 25_000_000
PRIVATE_SPEECH_DIRS = {'voice-samples','voice_samples','speech-cache','speech_cache','tts-cache','tts_cache'}


def private_speech_path(name):
    """Keep legacy dialogue and personal voice exports out, including reviewed copies.

    Ordinary reaction sounds and reviewed app soundtracks remain eligible for
    the exact-byte media allowlist. This check never discovers live robot data.
    """
    path=Path(name)
    parts=[part.casefold() for part in path.parts]
    if any(part in PRIVATE_SPEECH_DIRS or part.startswith(('voice-samples-', 'robbi-voice-samples-')) for part in parts):
        return True
    if name.casefold().startswith('assets/voices/'):
        return True
    return (path.suffix.casefold() in AUDIO
            and path.stem.casefold().startswith(('film_line_', 'filming_line_', 'speech_take_', 'tts_take_')))


def candidate_files(root):
    files={}
    for p in root.rglob('*'):
        rel=p.relative_to(root)
        if any(x in CACHE or x.endswith('.egg-info') for x in rel.parts):continue
        if p.is_symlink():raise ValueError('Symlink requires review: '+rel.as_posix())
        if p.is_file() and p.name not in {LEDGER,'release-manifest.json'}:files[rel.as_posix()]=p
    return files


def digest(data):
    return hashlib.sha256(data).hexdigest()


def audit(files, reviewed):
    """Return filenames/reasons only. Never print potentially secret values."""
    errors=[]
    for name,p in files.items():
        if private_speech_path(name):
            errors.append('Private speech export: '+name);continue
        if (name.startswith(('src/robbi_store/', 'filming_sequences/', 'app_projects/', 'tools/legacy/', 'animation_agent_kit/'))
                or name in {'SOUL.md', 'HEARTBEAT.md', 'TOOLS.md'}):
            errors.append('Not part of the robot distribution: '+name);continue
        path=safe_path(name)
        if (any(x.lower() in FORBIDDEN or x.lower().startswith('.env') for x in path.parts)
                or p.suffix.lower() in {'.db','.sqlite','.sqlite3','.log','.pem','.key','.p12','.pfx','.pyc'}):
            errors.append('Private data path: '+name);continue
        if not p.is_file():errors.append('Missing file: '+name);continue
        data=p.read_bytes()
        if len(data) >= MAX_FILE:
            errors.append('File reaches the public 25 MB ceiling: '+name);continue
        if p.suffix.lower() in TEXT or p.name in {'.gitignore','.gitattributes','LICENSE','NOTICE'}:
            try:text=data.decode('utf-8')
            except UnicodeError:errors.append('Non-UTF8 text: '+name);continue
            if SECRET.search(text):errors.append('Credential pattern: '+name)
            if not name.startswith('tests/') and HOST.search(text):errors.append('Device-specific location: '+name)
        else:
            row=reviewed.get(name,{}) if isinstance(reviewed,dict) else {}
            if row.get('sha256')!=digest(data):errors.append('Media requires review of these exact bytes: '+name)
    return errors


def review_files(root):
    media_path=root/'approved-media.json'
    media=json.loads(media_path.read_text(encoding='utf-8'))
    if media.get('format')!=1 or not isinstance(media.get('files'),dict):raise ValueError('Invalid media review manifest')
    files=candidate_files(root)
    if any(name not in files for name in REQUIRED):raise ValueError('Incomplete runtime')
    errors=audit(files,media['files'])
    if errors:raise ValueError('\n'.join(errors[:60])+f'\n{len(errors)} issue(s); nothing packaged.')
    hashes={name:digest(p.read_bytes()) for name,p in sorted(files.items())}
    return files,hashes


def seal(root):
    files,hashes=review_files(root)
    (root/LEDGER).write_text(json.dumps({'format':1,'files':hashes},indent=2)+'\n',encoding='utf-8')
    print(f'Sealed {len(files)} reviewed local files; no upload.')


def runtime_files(files):
    """One selection shared by GitHub source installs and optional releases."""
    return {n:p for n,p in files.items() if not n.endswith(('.test.js','.test.mjs'))
            and not n.startswith(('build-guide/','tests/','assets/dynamics_sources/',
            'assets/app-experience-sources/','tools/assets/','tools/apps/',
            'tools/accounts/','tools/release/','tools/qa/'))}


def sealed_files(root):
    expected=json.loads((root/LEDGER).read_text(encoding='utf-8'))
    files,hashes=review_files(root)
    if expected.get('format')!=1 or expected.get('files')!=hashes:
        raise ValueError('Source changed since review. Review and seal again.')
    return files,hashes


def manifest(root):
    files,hashes=sealed_files(root)
    selected=runtime_files(files)
    value={'format':1,'files':{n:hashes[n] for n in sorted(selected)}}
    (root/'release-manifest.json').write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
    print(f'Wrote source-install manifest for {len(selected)} verified runtime files; no upload.')


def build(root,destination):
    root=root.resolve();destination=destination.resolve()
    if destination.exists() or destination.is_relative_to(root):raise ValueError('Use a new output folder outside the source tree')
    files,hashes=sealed_files(root)
    # Authoring originals and CAD do not consume storage on every robot.
    selected=runtime_files(files)
    source_assets={n:p for n,p in files.items() if n.startswith(('assets/dynamics_sources/','assets/app-experience-sources/','build-guide/hardware/','tools/assets/'))}
    destination.mkdir(parents=True)
    archive(selected,hashes,destination,'robbi-runtime.tar.gz')
    archive(source_assets,hashes,destination,'robbi-source-assets.tar.gz')


def archive(selected,hashes,destination,filename):
    manifest=json.dumps({'format':1,'files':{n:hashes[n] for n in selected}},indent=2,sort_keys=True).encode()
    output=destination/filename
    with tarfile.open(output,'w:gz') as tar:
        for name,p in sorted(selected.items()):
            blob=p.read_bytes()
            if digest(blob)!=hashes[name]:raise ValueError('File changed during packaging: '+name)
            info=tarfile.TarInfo('robbi-v2/'+name);info.size=len(blob);info.mode=0o644
            tar.addfile(info,io.BytesIO(blob))
        info=tarfile.TarInfo('robbi-v2/release-manifest.json');info.size=len(manifest);info.mode=0o644
        tar.addfile(info,io.BytesIO(manifest))
    # Inspect the artifact, not only the input list.
    with tarfile.open(output,'r:gz') as tar:
        members=tar.getmembers()
        if len(members)!=len(selected)+1:raise ValueError('Archive entry mismatch')
        for m in members:
            if not m.isfile():raise ValueError('Non-file archive entry')
            name=m.name.removeprefix('robbi-v2/');safe_path(name)
            blob=tar.extractfile(m).read()
            expected_hash=digest(manifest) if name=='release-manifest.json' else hashes[name]
            if digest(blob)!=expected_hash:raise ValueError('Archive checksum mismatch')
    checksum=digest(output.read_bytes())
    (destination/(filename+'.sha256')).write_text(checksum+'  '+filename+'\n',encoding='utf-8')
    print(f'{filename}: {len(selected)} files, {output.stat().st_size} bytes; verified locally. Nothing uploaded.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[2])
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--seal',action='store_true',help='Record current reviewed bytes after review')
    action.add_argument('--manifest',action='store_true',help='Write source-install manifest from sealed reviewed bytes')
    action.add_argument('--output',type=Path,help='Build from an unchanged sealed tree')
    args=parser.parse_args()
    try:
        if args.seal:seal(args.source)
        elif args.manifest:manifest(args.source)
        else:build(args.source,args.output)
    except (ValueError,OSError,KeyError) as error:
        print(str(error),file=sys.stderr);return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
