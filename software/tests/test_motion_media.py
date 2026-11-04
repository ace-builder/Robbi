"""Portable media and owner-scoped cue playback without network or GPIO."""
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import wave
import zipfile

from flask import Flask
from PIL import Image
import pytest

from robbi.errors import BusyError, ValidationError
from robbi.motion_media import MotionMedia, MAX_FILE
from robbi.motion_studio import MotionStudio, FORMAT, DEFAULTS, validate_project, compile_project
from robbi.motion_studio_web import blueprint
from robbi.orchestration import Orchestrator
from robbi.web import install_error_handlers

ROOT = Path(__file__).resolve().parents[1]


def png():
    output = io.BytesIO()
    Image.new('RGB', (280, 240), '#8fbdb0').save(output, format='PNG')
    return output.getvalue()


def wav():
    output = io.BytesIO()
    with wave.open(output, 'wb') as sound:
        sound.setnchannels(1); sound.setsampwidth(2); sound.setframerate(8000)
        sound.writeframes(b'\0\0'*400)
    return output.getvalue()


def project(cue=None, *, face='happy'):
    return validate_project(dict(format=FORMAT, name='Media hello', settings=DEFAULTS, waypoints=[
        dict(x=0, y=0, heading=0, face=face, cue=cue or {})]))


def test_uploaded_media_is_validated_and_catalog_has_no_filesystem_paths(tmp_path):
    store = MotionMedia(ROOT, tmp_path)
    image = store.install('my picture.png', png())
    sound = store.install('hello.wav', wav())
    assert image['kind'] == 'image' and sound['duration'] == pytest.approx(.05)
    assert store.resolve(image['id'], 'image')['_path'].read_bytes() == png()
    assert store.install('same bytes.png', png())['id'] == image['id']
    assert all('_path' not in entry for entry in store.catalog())
    with pytest.raises(ValidationError):
        store.resolve(image['id'], 'video')
    for name, data in [('script.html', b'<script>'), ('fake.png', b'not an image'),
                       ('fake.wav', b'not audio'), ('fake.mp4', b'not video'), ('big.png', b'x'*(MAX_FILE+1))]:
        with pytest.raises(ValidationError):
            store.install(name, data)


def test_streaming_cached_media_does_not_rescan_the_library(tmp_path, monkeypatch):
    store = MotionMedia(ROOT, tmp_path)
    image = store.install('picture.png', png())
    store.catalog()
    monkeypatch.setattr(store, 'catalog', lambda **kw: pytest.fail('A media stream rescanned the library'))
    assert store.resolve(image['id'])['_path'].read_bytes() == png()


def test_browsing_media_does_not_open_every_built_in_sound(tmp_path, monkeypatch):
    root = tmp_path/'root'
    folder = root/'assets/sounds';folder.mkdir(parents=True)
    file = folder/'hello.wav';file.write_bytes(wav())
    store = MotionMedia(root, tmp_path/'data')
    monkeypatch.setattr(wave, 'open', lambda *a, **kw: pytest.fail('Browsing eagerly opened an audio file'))
    assert store.catalog()[0]['name'] == 'hello.wav'
    store.catalog(refresh=True)


def test_media_and_speech_survive_bundle_transfer_between_devices(tmp_path):
    source, target = MotionMedia(ROOT, tmp_path/'pc'), MotionMedia(tmp_path/'no-assets', tmp_path/'robot')
    image, sound = source.install('custom.png', png()), source.install('custom.wav', wav())
    original = project(dict(screen='image', media=image['id'], sound=sound['id'], speech='Hello from Robbi.', duration=.5, wait=True))
    imported = target.import_bundle(source.export_bundle(original).getvalue(), validate_project)
    assert imported == original
    assert target.resolve(imported['waypoints'][0]['cue']['media'])['_path'].read_bytes() == png()
    assert target.resolve(imported['waypoints'][0]['cue']['sound'])['_path'].read_bytes() == wav()


def test_built_in_assets_become_portable_content_identifiers(tmp_path):
    source, target = MotionMedia(ROOT, tmp_path/'pc'), MotionMedia(tmp_path/'empty-root', tmp_path/'robot')
    builtin = next(e for e in source.catalog() if e['kind'] == 'image' and e['id'].startswith('builtin-'))
    imported = target.import_bundle(source.export_bundle(project(dict(screen='image', media=builtin['id']))).getvalue(), validate_project)
    media_id = imported['waypoints'][0]['cue']['media']
    assert media_id.startswith('media-')
    assert target.resolve(media_id)['_path'].read_bytes() == source.resolve(builtin['id'])['_path'].read_bytes()


def test_bundle_deduplicates_identical_built_in_and_uploaded_media(tmp_path):
    store = MotionMedia(ROOT, tmp_path)
    builtin = next(e for e in store.catalog() if e['kind'] == 'image')
    uploaded = store.install(builtin['name'], store.resolve(builtin['id'])['_path'].read_bytes())
    clip = project(dict(screen='image', media=builtin['id']))
    second = dict(clip['waypoints'][0]);second['cue'] = dict(second['cue'], media=uploaded['id'])
    clip['waypoints'].append(second)
    data = store.export_bundle(clip).getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        assert len(bundle.namelist()) == len(set(bundle.namelist()))
    imported = store.import_bundle(data, validate_project)
    assert imported['waypoints'][0]['cue']['media'] == imported['waypoints'][1]['cue']['media']


@pytest.mark.parametrize('extra', ['../escape.png', '/absolute.wav', 'media/unknown.png'])
def test_bundles_reject_untrusted_paths_before_writing(tmp_path, extra):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('animation.json', json.dumps(project()))
        archive.writestr(extra, png())
    store = MotionMedia(ROOT, tmp_path)
    with pytest.raises(ValidationError):
        store.import_bundle(output.getvalue(), validate_project)
    assert not store.directory.exists()


def test_bundle_missing_media_or_wrong_digest_is_rejected(tmp_path):
    for wrong_digest in (False, True):
        media_id = 'media-'+'a'*64
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('animation.json', json.dumps(project(dict(screen='image', media=media_id))))
            if wrong_digest:
                archive.writestr('media/'+media_id+'.png', png())
        with pytest.raises(ValidationError):
            MotionMedia(ROOT, tmp_path).import_bundle(output.getvalue(), validate_project)


@pytest.mark.parametrize('cue_kind', ['video', 'sound'])
def test_bundle_rejects_wrong_media_type_before_installing_files(tmp_path, cue_kind):
    source = MotionMedia(ROOT, tmp_path/'source')
    image = source.install('picture.png', png())
    original = source.export_bundle(project(dict(screen='image', media=image['id']))).getvalue()
    broken = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as incoming, zipfile.ZipFile(broken, 'w') as outgoing:
        for item in incoming.infolist():
            data = incoming.read(item)
            if item.filename == 'animation.json':
                value = json.loads(data)
                value['waypoints'][0]['cue'] = (dict(screen='video', media=image['id'])
                                               if cue_kind == 'video' else dict(sound=image['id']))
                data = json.dumps(value)
            outgoing.writestr(item, data)
    target = MotionMedia(ROOT, tmp_path/'target')
    with pytest.raises(ValidationError, match=f'needs a {cue_kind}'):
        target.import_bundle(broken.getvalue(), validate_project)
    assert not target.directory.exists()


def test_media_routes_upload_preview_range_and_bundle_roundtrip(tmp_path):
    app = Flask(__name__);install_error_handlers(app);app.register_blueprint(blueprint(ROOT, tmp_path))
    client = app.test_client()
    uploaded = client.post('/api/motion-studio/media', data={'file':(io.BytesIO(png()), 'test.png')})
    assert uploaded.status_code == 201
    media_id = uploaded.json['media']['id']
    response = client.get('/api/motion-studio/media/'+media_id+'/file')
    assert response.content_type == 'image/png' and response.data == png()
    partial = client.get('/api/motion-studio/media/'+media_id+'/file', headers={'Range':'bytes=0-9'})
    assert partial.status_code == 206 and partial.data == png()[:10]
    export = client.post('/api/motion-studio/bundle/export', json={'project':project(dict(screen='image', media=media_id))})
    assert export.status_code == 200 and export.content_type == 'application/zip'
    imported = client.post('/api/motion-studio/bundle/import', data={'file':(io.BytesIO(export.data),'media.robbi.zip')})
    assert imported.status_code == 200 and imported.json['project']['waypoints'][0]['cue']['media'] == media_id


class Done:
    def wait(self, timeout=None):
        return 0


class Media:
    def __init__(self):
        self.images, self.sounds, self.speech, self.videos, self.stops = [], [], [], [], []
    def health(self):
        return {'display':{'available':True}, 'audio':{'available':True}}
    def show_face(self, path, **kwargs):
        self.images.append(path);return True
    def play_sound(self, path, **kwargs):
        self.sounds.append(path);return Done()
    def speak_wav_bytes(self, data, **kwargs):
        self.speech.append(data);return Done()
    def play_video(self, path, **kwargs):
        self.videos.append(path);return {'ok':True}
    def stop_audio(self, **kwargs):
        self.stops.append(kwargs)


def rig(tmp_path, synthesize=lambda text: wav()):
    media = Media()
    motion = SimpleNamespace(status=lambda:{'available':False}, stop=lambda **kw:pytest.fail('Media-only clip touched motors'))
    controller = SimpleNamespace(media=media, motion=motion, llm=SimpleNamespace(synthesize=synthesize), orchestrator=Orchestrator())
    runtime = SimpleNamespace(config=SimpleNamespace(values={'runtime':{'calibration_confirmed':False}}), controller=lambda:controller)
    return MotionStudio(ROOT, tmp_path, runtime), media


def test_screen_sound_and_speech_run_without_enabling_motors(tmp_path):
    studio, media = rig(tmp_path)
    image, sound = studio.media.install('hello.png', png()), studio.media.install('hello.wav', wav())
    saved = studio.save(project(dict(screen='image', media=image['id'], sound=sound['id'], speech='Hi!', duration=.05)))
    studio.play(saved['id']);studio._thread.join(2)
    assert studio.status()['playback']['state'] == 'completed'
    assert media.images[0].name.endswith('.png') and media.sounds and media.speech == [wav()]
    assert studio.status()['capabilities']['display'] and not studio.status()['ready']


def test_video_does_not_get_overwritten_by_a_final_face(tmp_path):
    studio, media = rig(tmp_path)
    source = ROOT/'assets/videos/happy-blink.mp4'
    if not source.exists():
        pytest.skip('Bundled test video is unavailable')
    video = studio.media.install('clip.mp4', source.read_bytes())
    saved = studio.save(project(dict(screen='video', media=video['id'], duration=1)))
    studio.play(saved['id']);studio._thread.join(3)
    assert studio.status()['playback']['state'] == 'completed'
    assert media.videos and not media.images


def test_stop_during_synthesis_cannot_emit_late_speech(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def delayed(text):
        entered.set();release.wait(3);return wav()
    studio, media = rig(tmp_path, delayed)
    saved = studio.save(project(dict(speech='Hello!', duration=.1)))
    studio.play(saved['id']);assert entered.wait(1)
    studio.stop();studio._thread.join(1)
    assert studio.status()['playback']['state'] == 'cancelled'
    assert media.speech == []
    with pytest.raises(BusyError):
        studio.play(saved['id'])
    release.set()
    for thread in studio._cue_player.threads:
        thread.join(1)
    assert media.speech == []


def test_legacy_v1_face_clips_upgrade_to_v2(tmp_path):
    old = project();old['format'] = 'robbi.motion/v1';old['waypoints'][0].pop('cue')
    clean = validate_project(old)
    assert clean['format'] == FORMAT and clean['waypoints'][0]['cue']['screen'] == 'face'
    assert compile_project(clean)['beats'][0]['kind'] == 'cue'
