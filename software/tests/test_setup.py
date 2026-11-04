import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import httpx
import pytest

from robbi.setup import checks, host, install
from robbi.setup.configuration import Configuration, new_config
from robbi.config import validate, RuntimeConfig


def test_configuration_preserves_calibration_secrets_and_owner(tmp_path):
    config = Configuration(tmp_path)
    config.save({'runtime': {'calibration_confirmed': True}, 'motion': {'wheel_base_cm': 12.3}})
    config.save_key('OPENAI_API_KEY', 'dummy-credential-for-test')
    config.save_key('OPENAI_API_KEY', '')
    config.save({'llm': {'chat_model': 'different-model'}})
    assert config.read()['motion']['wheel_base_cm'] == 12.3
    assert config.read()['runtime']['calibration_confirmed'] is True
    assert config.keys() == {'OPENAI_API_KEY': 'dummy-credential-for-test'}
    assert 'dummy-credential' not in config.path.read_text()
    config.owner('Maker')
    config.owner('New maker')
    assert config.owner() == 'New maker'
    if os.name != 'nt':
        assert config.secret_path.stat().st_mode & 0o777 == 0o600


def test_invalid_key_and_yaml_never_clobber_existing(tmp_path):
    c = Configuration(tmp_path)
    c.save({}); before = c.path.read_bytes()
    with pytest.raises(ValueError):
        c.save_key('OPENAI_API_KEY', 'value\nOTHER=bad')
    with pytest.raises(ValueError):
        c.save({'hardware': {'enable_motors': True}})
    assert c.path.read_bytes() == before
    c.path.write_text('invalid: [')
    with pytest.raises(ValueError):
        c.save({})
    assert c.path.read_text() == 'invalid: ['


def test_new_setup_and_legacy_encoder_pin(monkeypatch, tmp_path):
    monkeypatch.setattr(os, 'environ', os.environ.copy())
    fresh = new_config()
    validate(fresh)
    assert fresh['hardware']['right_encoder_b_pin'] == 16
    assert fresh['hardware']['enable_motors'] is False
    assert fresh['tof']['h2_enabled'] is False
    for pin in (0, 15, True, '16'):
        with pytest.raises(ValueError):
            validate({'hardware': {'right_encoder_b_pin': pin}})
    monkeypatch.setenv('ROBBI_RIGHT_ENCODER_B_PIN', '16')
    c = RuntimeConfig(tmp_path, tmp_path/'var', tmp_path/'config.yaml', {})
    c.install_environment()
    assert os.environ['ROBBI_RIGHT_ENCODER_B_PIN'] == '16'
    c.values = {'hardware': {'right_encoder_b_pin': 7}}; c.install_environment()
    assert os.environ['ROBBI_RIGHT_ENCODER_B_PIN'] == '7'
    c.values = fresh; c.install_environment()
    assert os.environ['ROBBI_RIGHT_ENCODER_B_PIN'] == '16'


def test_interfaces_idempotent_and_reject_conflicts():
    value = host.boot_config('# existing\n[pi4]\ndtoverlay=vc4-kms-v3d\n')
    assert host.boot_config(value) == value
    assert value.count(host.BEGIN) == 1
    assert '\n[all]\n' in value
    for bad in ('dtoverlay=max98357a\n', 'dtparam=spi=off\n', host.BEGIN):
        with pytest.raises(ValueError):
            host.boot_config(bad)


def test_service_has_current_owner_and_bounded_workers():
    text = host.unit_text('/home/maker/robbi-v2', 'maker')
    assert 'User=maker' in text and 'config/robot.yaml' in text
    assert 'Environment=OPENBLAS_NUM_THREADS=1' in text
    assert 'EnvironmentFile' not in text
    for path, user in [('/tmp/test%h', 'maker'), ('/tmp/test\nInjected=yes', 'maker'), ('/home/user', 'bad\n')]:
        with pytest.raises(ValueError):
            host.unit_text(path, user)


def test_wifi_accepts_unicode_ssid_and_punctuation_without_injection():
    value = host.wifi_keyfile('Mäker; Network', 'pass word;\\123')
    assert 'ssid=' + ';'.join(str(b) for b in 'Mäker; Network'.encode()) + ';' in value
    assert r'psk=pass\sword;\\123' in value
    for ssid, password in [('x\n[connection]', 'abcdefgh'), ('a'*33, 'abcdefgh'), ('ok', 'short')]:
        with pytest.raises(ValueError):
            host.wifi_keyfile(ssid, password)


def fake_client(handler):
    return lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), **kwargs)


def test_llm_probe_matches_runtime_and_does_not_expose_response_secrets():
    def good(request):
        assert request.headers['Authorization'] == 'Bearer test-key'
        body = json.loads(request.content)
        assert body['model'] == 'gpt-6-luna' and body['response_format']['type'] == 'json_object'
        assert len(body['messages']) == 1
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}}]})
    assert checks.llm_test(new_config(), {'OPENAI_API_KEY': 'test-key'}, fake_client(good))[0]
    for code in (302, 401, 403, 404, 429, 500):
        ok, message = checks.llm_test(new_config(), {'OPENAI_API_KEY': 'test-key'},
            fake_client(lambda r: httpx.Response(code, text='test-key private-provider-body', headers={'Location': 'https://elsewhere.invalid'})))
        assert not ok and 'test-key' not in message and 'private-provider-body' not in message


def test_offline_setup_probe_and_invalid_url():
    def offline(request):
        raise httpx.ConnectError('secret error body', request=request)
    assert not checks.network_test(fake_client(offline))[0]
    assert not checks.llm_test(new_config(), {}, fake_client(offline))[0]
    for url in ('https://user:password@example.com/v1', 'http://example.com/v1', 'https://example.com/?token=x'):
        with pytest.raises(ValueError):
            checks.endpoint(url, 'openai')


def test_platform_and_resume_refuse_existing_or_different_release(tmp_path):
    assert not install.platform_issues('Linux', 'aarch64', (3,11), {'VERSION_CODENAME': 'bookworm'}, 'Raspberry Pi Zero 2 W')
    assert install.platform_issues('Linux', 'aarch64', (3,13), {'VERSION_CODENAME': 'trixie'}, 'Raspberry Pi Zero 2 W')
    target = tmp_path/'robbi-v2'
    marker, state = install.destination_state(target, 'one')
    assert state['steps'] == []
    target.mkdir()
    with pytest.raises(ValueError): install.destination_state(target, 'one')
    host.atomic(marker, json.dumps(state))
    assert install.destination_state(target, 'one')[1] == state
    with pytest.raises(ValueError): install.destination_state(target, 'two')


def test_manifest_rejects_mutation_and_private_files(tmp_path):
    files = {}
    for name in install.REQUIRED:
        p = tmp_path/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'test')
        files[name] = hashlib.sha256(b'test').hexdigest()
    (tmp_path/'release-manifest.json').write_text(json.dumps({'format': 1, 'files': files}))
    assert install.release_manifest(tmp_path)[0] == files
    (tmp_path/'pyproject.toml').write_text('changed')
    with pytest.raises(ValueError): install.release_manifest(tmp_path)
    for name in ('.env.secrets', '../escape', '/absolute', 'var/state/profile.json', 'config/pi.yaml', 'web/../data', 'a\\b', 'C:/escape'):
        with pytest.raises(ValueError): install.safe_path(name)


def test_plan_runs_without_dependencies_or_machine_changes(tmp_path):
    path = Path(install.__file__)
    result = subprocess.run([sys.executable, '-S', str(path), '--source', str(tmp_path), '--plan'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'No changes made' in result.stdout
    assert not list(tmp_path.iterdir())


def source_snapshot(tmp_path, *, extra=(), manifest_patch=None, tamper=False):
    commit = 'a'*40
    prefix = 'Robbi-'+commit+'/software/'
    files = {name: b'public runtime' for name in install.REQUIRED}
    manifest = {'format': 1, 'files': {n: hashlib.sha256(b).hexdigest() for n,b in files.items()}}
    if manifest_patch:
        manifest_patch(manifest)
    archive = tmp_path/'source.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        rows = [(prefix+n, b if not tamper else b'changed', tarfile.REGTYPE) for n,b in files.items()]
        rows += [(prefix+'release-manifest.json', json.dumps(manifest).encode(), tarfile.REGTYPE)]
        rows += [('Robbi-'+commit+'/hardware/example.txt', b'not a runtime dependency', tarfile.REGTYPE)]
        for name,blob,kind in rows+list(extra):
            entry = tarfile.TarInfo(name); entry.type = kind
            entry.size = len(blob) if kind == tarfile.REGTYPE else 0
            entry.linkname = '../escape' if kind != tarfile.REGTYPE else ''
            tar.addfile(entry, io.BytesIO(blob) if entry.size else None)
    return archive,commit


def test_source_install_extracts_only_sealed_runtime(tmp_path):
    from robbi.setup import source_archive
    archive,commit = source_snapshot(tmp_path)
    output = tmp_path/'runtime'
    assert source_archive.extract(archive,output,commit) == len(install.REQUIRED)
    assert install.release_manifest(output)[0].keys() == set(install.REQUIRED)
    assert not (output/'hardware').exists()
    with pytest.raises(ValueError,match='must be new'):
        source_archive.extract(archive,output,commit)


@pytest.mark.parametrize('name,kind', [('../escape',tarfile.REGTYPE),
    ('/absolute',tarfile.REGTYPE), ('Robbi-'+'a'*40+'/link',tarfile.SYMTYPE),
    ('Robbi-'+'a'*40+'/hardlink',tarfile.LNKTYPE),
    ('Robbi-'+'a'*40+'/software/pyproject.toml',tarfile.REGTYPE)])
def test_source_install_rejects_unsafe_archive_before_extraction(tmp_path,name,kind):
    from robbi.setup import source_archive
    archive,commit = source_snapshot(tmp_path,extra=[(name,b'x',kind)])
    with pytest.raises(ValueError):
        source_archive.extract(archive,tmp_path/'runtime',commit)
    assert not (tmp_path/'runtime').exists()
    assert not (tmp_path/'escape').exists()


@pytest.mark.parametrize('name', ['.env.secrets','var/state/profile.json','config/pi.yaml','../escape','.'])
def test_source_install_rejects_owner_data_in_manifest(tmp_path,name):
    from robbi.setup import source_archive
    prefix = 'Robbi-'+'a'*40+'/software/'
    archive,commit = source_snapshot(tmp_path,extra=[(prefix+name,b'x',tarfile.REGTYPE)],
        manifest_patch=lambda m: m['files'].update({name:hashlib.sha256(b'x').hexdigest()}))
    with pytest.raises(ValueError):
        source_archive.extract(archive,tmp_path/'runtime',commit)
    assert not (tmp_path/'runtime').exists()


def test_source_install_removes_partial_output_on_corruption(tmp_path):
    from robbi.setup import source_archive
    archive,commit = source_snapshot(tmp_path,tamper=True)
    with pytest.raises(ValueError,match='checksum'):
        source_archive.extract(archive,tmp_path/'runtime',commit)
    assert not (tmp_path/'runtime').exists()


def test_source_install_enforces_size_and_completeness(tmp_path,monkeypatch):
    from robbi.setup import source_archive
    archive,commit = source_snapshot(tmp_path,manifest_patch=lambda m: m['files'].pop('pyproject.toml'))
    with pytest.raises(ValueError,match='required'):
        source_archive.extract(archive,tmp_path/'runtime',commit)
    archive,commit = source_snapshot(tmp_path)
    monkeypatch.setattr(source_archive,'MAX_FILE',1)
    with pytest.raises(ValueError,match='size limit'):
        source_archive.extract(archive,tmp_path/'runtime',commit)


def test_bootstrap_pins_the_exact_source_verifier():
    import re
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256((root/'src/robbi/setup/source_archive.py').read_bytes()).hexdigest()
    assert re.search(r"BOOTSTRAP_SHA256='([a-f0-9]{64})'",(root/'install.sh').read_text())[1] == digest


def test_tavily_setup_saves_key_locally_and_never_echoes_it(monkeypatch,tmp_path,capsys):
    from robbi.setup import wizard
    monkeypatch.setattr(wizard,'yes',lambda *args:True)
    monkeypatch.setattr(wizard,'hidden',lambda *args:'synthetic-search-credential')
    wizard.Wizard(tmp_path).web_search()
    config = Configuration(tmp_path)
    assert config.keys()['TAVILY_API_KEY'] == 'synthetic-search-credential'
    assert config.read()['web_search']['provider'] == 'tavily'
    assert 'synthetic-search-credential' not in capsys.readouterr().out
    assert 'synthetic-search-credential' not in config.path.read_text()


def test_wizard_saves_correct_voice_fields_and_all_chat_routes(monkeypatch, tmp_path):
    from robbi.setup import wizard
    answers = iter(['openai', 'https://api.openai.com/v1', 'gpt-6-luna', 'voice123', 'eleven_v4'])
    monkeypatch.setattr(wizard, 'ask', lambda *args: next(answers))
    choices = iter([True, False])
    monkeypatch.setattr(wizard, 'yes', lambda *args: next(choices))
    monkeypatch.setattr(wizard, 'hidden', lambda *args: 'test-provider-key')
    wizard.Wizard(tmp_path).providers()
    config = Configuration(tmp_path).read()
    assert config['tts']['elevenlabs_voice_id'] == 'voice123'
    assert config['tts']['elevenlabs_model'] == 'eleven_v4'
    assert config['tts']['provider'] == 'elevenlabs'
    for route in ('chat', 'ui_text', 'speech_input'):
        assert config['llm'][route + '_provider'] == 'openai'


def test_release_audit_refuses_media_and_personal_content(tmp_path):
    import importlib.util
    path = Path(__file__).resolve().parents[1]/'tools/release/build_public_release.py'
    spec = importlib.util.spec_from_file_location('release_builder', path)
    builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
    text = tmp_path/'app.py'; text.write_text('HOST="192.168.1.99"')
    media = tmp_path/'voice.wav'; media.write_bytes(b'private')
    errors = builder.audit({'src/app.py': text, 'assets/voice.wav': media}, set())
    assert len(errors) == 2
    with pytest.raises(ValueError): builder.audit({'.env.secrets': text}, set())
