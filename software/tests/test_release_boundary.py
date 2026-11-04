import importlib.util
import json
from pathlib import Path
import tarfile
import pytest
from robbi.setup.install import release_manifest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('public_builder',ROOT/'tools/release/build_public_release.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


def test_binary_approval_is_for_bytes_not_name(tmp_path):
    p=tmp_path/'face.png';p.write_bytes(b'reviewed pixels')
    approved={'assets/face.png':{'sha256':builder.digest(p.read_bytes())}}
    assert builder.audit({'assets/face.png':p},approved)==[]
    p.write_bytes(b'a new private photo under the same name')
    assert builder.audit({'assets/face.png':p},approved)


def test_file_size_ceiling_is_enforced_even_for_approved_media(tmp_path,monkeypatch):
    p=tmp_path/'face.png';p.write_bytes(b'reviewed pixels')
    approved={'assets/face.png':{'sha256':builder.digest(p.read_bytes())}}
    monkeypatch.setattr(builder,'MAX_FILE',len(p.read_bytes()))
    assert any('25 MB ceiling' in error for error in builder.audit({'assets/face.png':p},approved))


def test_location_audit_allows_https_but_blocks_workstation_paths(tmp_path):
    p=tmp_path/'source.py'
    p.write_text('HELP="https://example.org/docs"')
    assert builder.audit({'src/source.py':p},{})==[]
    p.write_text('FONT="C:/Windows/Fonts/arial.ttf"\nREGEX=r"I:\\s+(-?[\\d.]+) LUFS"')
    assert builder.audit({'src/source.py':p},{})==[]
    p.write_text('OUTPUT="D:/private/output"')
    assert any('Device-specific location' in error for error in builder.audit({'src/source.py':p},{}))


def test_no_env_or_memory_even_if_in_review_list(tmp_path):
    p=tmp_path/'blob';p.write_text('private')
    for name in ('.env.secrets','var/memory.json','config/robot.yaml'):
        with pytest.raises(ValueError):builder.audit({name:p},{name:{'sha256':builder.digest(p.read_bytes())}})
    assert builder.audit({'data/memory.json':p},{})


def test_private_speech_is_rejected_even_if_approved(tmp_path):
    p=tmp_path/'line.wav';p.write_bytes(b'private spoken passage')
    for name in ('assets/sounds/film_line_example.wav', 'assets/sounds/tts_take_01.mp3',
                 'assets/voices/example.wav', 'voice-samples-20990101/example.mp3',
                 'assets/robbi-voice-samples-demo/example.wav'):
        approved={name:{'sha256':builder.digest(p.read_bytes())}}
        assert any('Private speech export' in error for error in builder.audit({name:p},approved))
    for name in ('assets/sounds/okay.wav','assets/sounds/grumpy.wav'):
        approved={name:{'sha256':builder.digest(p.read_bytes())}}
        assert builder.audit({name:p},approved)==[]


def test_sealed_release_rejects_added_file(tmp_path):
    for name in builder.REQUIRED:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture')
    (tmp_path/'approved-media.json').write_text(json.dumps({'format':1,'files':{}}))
    builder.seal(tmp_path)
    (tmp_path/'new.py').write_text('unreviewed')
    with pytest.raises(ValueError,match='changed since review'):
        builder.build(tmp_path,tmp_path.parent/(tmp_path.name+'-out'))


def test_source_manifest_is_repeatable_and_excludes_authoring_and_itself(tmp_path):
    for name in builder.REQUIRED:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture')
    source=tmp_path/'assets/app-experience-sources/notes.txt'
    source.parent.mkdir(parents=True);source.write_text('editable originals')
    (tmp_path/'approved-media.json').write_text(json.dumps({'format':1,'files':{}}))
    builder.seal(tmp_path)
    builder.manifest(tmp_path)
    first=(tmp_path/'release-manifest.json').read_bytes()
    files,_=release_manifest(tmp_path)
    assert 'release-manifest.json' not in files and 'approved-files.json' not in files
    assert not any(n.startswith('assets/app-experience-sources/') for n in files)
    builder.manifest(tmp_path)
    assert first==(tmp_path/'release-manifest.json').read_bytes()
    (tmp_path/'web/index.html').write_text('changed after seal')
    with pytest.raises(ValueError,match='changed since review'):
        builder.manifest(tmp_path)


def test_archives_split_runtime_and_authoring_and_validate_with_installer(tmp_path):
    root=tmp_path/'source';root.mkdir()
    entries={name:'fixture' for name in builder.REQUIRED}
    entries.update({
        'assets/face.svg':'<svg/>',
        'assets/dynamics_sources/LICENSE.txt':'source license',
        'build-guide/hardware/board.json':'{}',
        'build-guide/README.md':'guide',
        'tests/test_example.py':'# test',
        'approved-media.json':json.dumps({'format':1,'files':{}}),
    })
    for name,value in entries.items():
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value)
    builder.seal(root)
    output=tmp_path/'release';builder.build(root,output)
    unpacked=tmp_path/'unpacked';unpacked.mkdir()
    with tarfile.open(output/'robbi-runtime.tar.gz') as tar:
        for member in tar.getmembers():
            assert member.isfile() and member.uid==member.gid==0
            assert not member.uname and not member.gname
            name=member.name.removeprefix('robbi-v2/')
            builder.safe_path(name)
            path=unpacked/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(tar.extractfile(member).read())
    files,_=release_manifest(unpacked)
    assert 'assets/face.svg' in files
    assert not any(n.startswith(('build-guide/','tests/','assets/dynamics_sources/')) for n in files)
    with tarfile.open(output/'robbi-source-assets.tar.gz') as tar:
        names={m.name.removeprefix('robbi-v2/') for m in tar.getmembers()}
        assert names=={'assets/dynamics_sources/LICENSE.txt','build-guide/hardware/board.json','release-manifest.json'}
    for filename in ('robbi-runtime.tar.gz','robbi-source-assets.tar.gz'):
        expected=(output/(filename+'.sha256')).read_text().split()[0]
        assert builder.digest((output/filename).read_bytes())==expected
    (unpacked/'assets/face.svg').write_text('<svg>modified</svg>')
    with pytest.raises(ValueError,match='checksum mismatch'):
        release_manifest(unpacked)
