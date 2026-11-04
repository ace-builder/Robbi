"""A new public install cannot accidentally capture a private training dataset."""
from pathlib import Path
from types import SimpleNamespace
import threading

from robbi import activity
from robbi.config import load
from robbi.runtime import Runtime
from robbi.web import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_public_boot_has_no_recorder_routes_panel_or_capture_data(tmp_path):
    config = load(ROOT, tmp_path/'robot.yaml', tmp_path/'data')
    # Legacy configuration must not resurrect a removed feature.
    config.values['recording'] = {'enabled': True}
    runtime = Runtime(config)
    try:
        app = create_app(runtime)
        assert runtime.recording is None
        runtime._snapshot = {'test_controller': True}
        assert 'recording' not in runtime.snapshot()
        assert not any(rule.rule.startswith('/api/recording') for rule in app.url_map.iter_rules())
        assert not any(rule.rule.startswith('/api/filming') for rule in app.url_map.iter_rules())
        assert app.test_client().get('/api/runtime-extension').json == {}
        assert not (config.data_dir/'recordings').exists()
        assert not any('recording' in t.name.lower() for t in threading.enumerate())
        assert not list(config.data_dir.rglob('events.jsonl'))
        assert 'Training recordings' not in (ROOT/'web/views/home.js').read_text(encoding='utf-8')
        assert '#filming' not in (ROOT/'web/index.html').read_text(encoding='utf-8')
        for path in ('web/manual-drive.js','web/views/navigation.js','web/views/hardware.js'):
            assert '/api/filming' not in (ROOT/path).read_text(encoding='utf-8')
            assert '#filming' not in (ROOT/path).read_text(encoding='utf-8')
    finally:
        runtime.stop()


def test_extension_is_not_discovered_without_explicit_selection(monkeypatch):
    monkeypatch.setattr(activity, 'entry_points', lambda **_: (_ for _ in ()).throw(AssertionError('Must not load plugins')))
    assert activity.create_sink(SimpleNamespace(values={})) is None


def test_missing_extension_is_optional_and_no_arbitrary_import(monkeypatch):
    monkeypatch.setattr(activity, 'entry_points', lambda **_: [])
    for name in ('missing', '../anything', 'os:system'):
        assert activity.create_sink(SimpleNamespace(values={'runtime': {'event_sink': name}})) is None


def test_event_failure_does_not_break_robot_action():
    called = []
    def fail(*args, **kwargs):
        called.append('failed')
        raise RuntimeError('private event failed')
    sink = activity.OptionalSink(SimpleNamespace(emit=fail, stop=lambda: called.append('stopped')))
    class Host:
        recording = sink
        @activity.record_turn
        def respond(self, text):
            return 'Still working'
    assert Host().respond('hello') == 'Still working'
    assert Host().respond('again') == 'Still working'
    sink.stop()
    assert called == ['failed', 'stopped']
