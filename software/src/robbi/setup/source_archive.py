"""Extract only a sealed runtime from a GitHub source snapshot; stdlib only."""
import argparse
import hashlib
import json
from pathlib import Path,PurePosixPath
import re
import shutil
import tarfile

MAX_FILE=25_000_000
MAX_UNPACKED=1_500_000_000
BLOCKED={'.git','.venv','var','data','datasets','recordings','captures','logs','backups','node_modules','__pycache__'}


def safe(name):
    if not isinstance(name,str):raise ValueError('Invalid runtime snapshot path')
    path=PurePosixPath(name)
    if (not name or name=='.' or path.is_absolute() or str(path)!=name or '..' in path.parts
            or '\\' in name or ':' in name or any(ord(c)<32 for c in name)
            or any(p in BLOCKED or p.startswith('.env') for p in path.parts)):
        raise ValueError('Unsafe runtime snapshot path')
    if name.startswith('config/') and name!='config/desktop.yaml':
        raise ValueError('Device-specific configuration is not part of a public runtime')
    return path


def extract(archive,destination,commit):
    if not re.fullmatch('[a-f0-9]{40}',commit):raise ValueError('Invalid source commit')
    destination=Path(destination)
    if destination.exists():raise ValueError('Snapshot destination must be new')
    prefix='Robbi-'+commit+'/software/'
    with tarfile.open(archive,'r:gz') as source:
        members={};size=0
        for entry in source:
            if len(members)>=50_000:raise ValueError('Too many snapshot entries')
            raw=entry.name.rstrip('/') if entry.isdir() else entry.name
            p=PurePosixPath(raw)
            if p.is_absolute() or '..' in p.parts or '\\' in raw or ':' in raw or any(ord(c)<32 for c in raw):
                raise ValueError('Unsafe source archive path')
            if not (entry.isfile() or entry.isdir()):raise ValueError('Links and special archive files are forbidden')
            if raw in members:raise ValueError('Duplicate source archive path')
            members[raw]=entry;size+=entry.size
            if size>MAX_UNPACKED or entry.size>MAX_FILE:raise ValueError('Snapshot exceeds the published size limit')
        entry=members.get(prefix+'release-manifest.json')
        if entry is None or entry.size>8*1024*1024:raise ValueError('The uploaded source has no sealed runtime manifest')
        manifest_bytes=source.extractfile(entry).read()
        manifest=json.loads(manifest_bytes)
        if not isinstance(manifest,dict):raise ValueError('Invalid source runtime manifest')
        files=manifest.get('files')
        if manifest.get('format')!=1 or not isinstance(files,dict) or not 1<=len(files)<=50_000:
            raise ValueError('Invalid source runtime manifest')
        for name,digest in files.items():
            safe(name)
            item=members.get(prefix+name)
            if not re.fullmatch('[a-f0-9]{64}',str(digest)) or item is None or not item.isfile():
                raise ValueError('Runtime snapshot is incomplete')
        required={'pyproject.toml','src/robbi/setup/install.py','config/desktop.yaml','web/index.html','tools/hardware/motor_disable.py'}
        if not required<=files.keys():raise ValueError('Missing required runtime files')
        destination.mkdir(parents=True)
        try:
            for name,digest in files.items():
                target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
                h=hashlib.sha256()
                with source.extractfile(members[prefix+name]) as incoming,target.open('wb') as outgoing:
                    for block in iter(lambda:incoming.read(1024*1024),b''):
                        h.update(block);outgoing.write(block)
                target.chmod(0o644)
                if h.hexdigest()!=digest:raise ValueError('Source runtime checksum mismatch')
            (destination/'release-manifest.json').write_bytes(manifest_bytes)
        except BaseException:
            shutil.rmtree(destination)
            raise
    return len(files)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--commit',required=True)
    args=parser.parse_args()
    try:count=extract(args.archive,args.destination,args.commit)
    except (ValueError,OSError,tarfile.TarError,KeyError,TypeError,json.JSONDecodeError):
        parser.exit(1,'Source verification failed; no system changes were made.\n')
    print(f'Verified {count} runtime files from pinned source commit {args.commit[:12]}.')


if __name__=='__main__':main()
