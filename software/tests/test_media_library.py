import io
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import Mock

from flask import Flask
from PIL import Image
import pytest

from robbi.media_library import MediaLibrary, blueprint
from robbi.errors import ValidationError, UnavailableError
from robbi.web import install_error_handlers


@pytest.fixture
def library(tmp_path):
    for folder in ('assets/images/faces', 'assets/sounds', 'assets/videos', 'assets/face_animations', 'animations'):
        (tmp_path/folder).mkdir(parents=True)
    Image.new('RGB', (280, 240), 'red').save(tmp_path/'assets/images/faces/cute.png')
    Image.new('RGB', (560, 240), 'blue').save(tmp_path/'assets/face_animations/sheet.png')
    (tmp_path/'assets/face_animations/blink.json').write_text(json.dumps({'sheet': 'sheet.png', 'columns': 2, 'rows': 1, 'order': [1, 0], 'durations_ms': [100, 200]}))
    (tmp_path/'animations/greet.yaml').write_text('- face: cute.png\n  ms: 300\n- drive: {direction: left, duration: 0.3}\n  ms: 100\n')
    return MediaLibrary(tmp_path)


def test_paging_search_and_motion_metadata(library):
    result=library.page('animations',limit=1)
    assert result['total']==2 and result['next']==1 and len(result['items'])==1
    assert result['items'][0]['requirements']==['display']
    timeline=library.page('animations','GREET')['items'][0]
    assert timeline['motion'] and timeline['requirements']==['display','motion']
    assert library.page('faces','missing')['items']==[]
    for params in [('filming_sequences','',0,18),('faces','',0,1000),('faces','',-1,18)]:
        with pytest.raises(ValidationError):library.page(*params)


@pytest.mark.parametrize('name',['../.env','.env','a/b.png','a\\b.png'])
def test_private_paths_rejected(library,name):
    with pytest.raises(ValidationError):library.path('faces',name)


def test_symlink_cannot_expose_other_file(library,tmp_path):
    outside=tmp_path/'secret.png';Image.new('RGB',(1,1)).save(outside)
    link=tmp_path/'assets/images/faces/link.png'
    try:link.symlink_to(outside)
    except OSError:pytest.skip('Symlinks unavailable')
    with pytest.raises(ValidationError):library.path('faces','link.png')
    assert 'link.png' not in library.names('faces')


def test_thumbnail_is_small_and_invalid_manifest_does_not_decode(library):
    blob=library.thumbnail('sprite','blink.json')
    with Image.open(io.BytesIO(blob)) as image:assert image.size==(168,144)
    assert library.thumbnail('sprite','blink.json')==blob
    path=library.root/'assets/face_animations/blink.json'
    path.write_text(json.dumps({'sheet':'../../.env','columns':1,'rows':1}))
    with pytest.raises(ValidationError):library.thumbnail('sprite','blink.json')
    path.write_text(json.dumps({'sheet':'sheet.png','columns':2,'rows':1,'durations_ms':[float('nan'),100]}))
    with pytest.raises(ValidationError):library.sprite('blink.json')


def test_broken_timeline_does_not_hide_other_items(library):
    (library.root/'animations/broken.yaml').write_text('invalid: [yaml')
    result=library.page('animations')
    assert next(i for i in result['items'] if i['name']=='broken.yaml')['error']
    assert len(result['items'])==3


def test_browser_previews_are_read_only_and_stop_invalidates_queued_play(library):
    controller=SimpleNamespace(_action_token=lambda:7,_action_local=threading.local(),execute_action=Mock())
    jobs=Mock();jobs.submit.side_effect=lambda op,fn:{'id':'one','state':'queued'}
    config=SimpleNamespace(root=library.root,values={'server':{'auth_enabled':False}})
    runtime=SimpleNamespace(config=config,controller=lambda:controller,jobs=jobs,_generation=2)
    app=Flask(__name__);app.config['ROBBI_RUNTIME_CONFIG']=config;install_error_handlers(app);app.register_blueprint(blueprint(runtime));client=app.test_client()
    assert client.get('/api/media-library?kind=animations').status_code==200
    assert client.get('/api/media-library/preview?kind=sprite&name=blink.json').json['order']==[1,0]
    assert client.get('/api/media-library/thumbnail?kind=faces&name=cute.png').mimetype=='image/jpeg'
    assert client.get('/api/media-library/file?kind=sprite&name=blink.json').status_code==200
    controller.execute_action.assert_not_called();jobs.submit.assert_not_called()
    response=client.post('/api/media-library/play',json={'kind':'sprite','name':'blink.json'})
    assert response.status_code==202
    execute=jobs.submit.call_args.args[1];runtime._generation+=1
    with pytest.raises(UnavailableError,match='Stop'):execute()
    controller.execute_action.assert_not_called()
    runtime._generation-=1;execute()
    controller.execute_action.assert_called_once_with({'face_animation':'blink'})
    assert controller._action_local.token is None


def test_thumbnail_cache_is_bounded(library):
    for i in range(52):
        name=f'face-{i}.png';Image.new('RGB',(4,4)).save(library.root/'assets/images/faces'/name)
        library.thumbnail('faces',name)
    assert len(library.cache)==48


def test_muting_stops_current_sound_and_unmuting_does_not_replay():
    from robot.domains.presentation import PresentationDomain
    subject=SimpleNamespace(behavior=Mock(),media=Mock())
    subject.behavior.snapshot.return_value.muted=True
    assert PresentationDomain.set_muted(subject,True) is True
    subject.media.stop_audio.assert_called_once_with()
    subject.behavior.snapshot.return_value.muted=False
    assert PresentationDomain.set_muted(subject,False) is False
    subject.media.stop_audio.assert_called_once_with()
