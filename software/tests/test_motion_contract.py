"""The model's advertised actions must reach the actual measured planner."""
from concurrent.futures import Future
import json
from types import SimpleNamespace

import pytest

from robbi.dynamics import GESTURES
from robbi.motion_contract import DRIVE_EXAMPLES, motion_prompt_catalog
from robot.action_executor import ActionExecutorMixin
from robot.domains.engagement import EngagementDomain
from robot.domains.motion import MotionDomain
from robot.llm import LLMClient
from robot.motion import MotionManager
from robot.tools import ToolRegistry


class MotionHarness(MotionDomain, ActionExecutorMixin):
    def __init__(self):
        self.calls = []
        self.motion = SimpleNamespace(move=self.move)
        self.slam = SimpleNamespace(track_drive_action=lambda *_: None)

    def move(self, **kwargs):
        self.calls.append(kwargs)
        result = Future()
        result.set_result({'ok': True})
        return result

    def _set_presentation(self, **kwargs):
        pass

    def _publish_event(self, *args):
        pass


@pytest.mark.parametrize('utterance,drive', DRIVE_EXAMPLES)
def test_advertised_calls_survive_sanitizer_executor_and_real_path_planner(utterance, drive):
    host = MotionHarness()
    action = host._sanitize_llm_motion({'drive': dict(drive)}, user_text=utterance)
    host._execute_drive_action(action['drive'])
    spec = host.calls[0]
    for key, value in drive.items():
        assert spec[key] == value
    if spec['mode'] == 'stop':
        return
    planner = MotionManager.__new__(MotionManager)
    curve = planner._curve_for_spec(spec)
    targets = [curve.targets(planner.cm_per_count())] if curve else planner._targets_for_spec(spec)
    assert targets
    if spec['mode'] == 'distance':
        expected = planner._distance_to_ticks(drive['distance_cm'])
        assert targets == [(expected, expected)]
    elif spec['mode'] == 'turn':
        left, right = targets[0]
        assert left < 0 < right if drive['angle_deg'] > 0 else right < 0 < left
    elif spec['mode'] == 'arc':
        left, right = targets[0]
        assert 0 < left < right if drive['angle_deg'] > 0 else 0 < right < left
    elif spec['mode'] == 'wiggle':
        assert len(targets) == 6
        assert sum(left for left, _ in targets) == pytest.approx(0, abs=2)
        assert sum(right for _, right in targets) == pytest.approx(0, abs=2)


def test_old_custom_tools_cannot_remove_current_motion_contract(tmp_path):
    path = tmp_path / 'TOOLS.md'
    path.write_text('Custom notes: drive only accepts direction, speed, duration.')
    host = EngagementDomain()
    host._tools_text = host._ensure_prompt_file(path, host._default_tools_text())
    host.tools = ToolRegistry()
    prompt = host._tools_prompt_context()
    assert 'Custom notes' in prompt  # Owner's notes remain intact.
    assert prompt.index('Runtime movement contract') > prompt.index('Custom notes')
    for _, spec in DRIVE_EXAMPLES:
        assert json.dumps({'drive': spec}, separators=(',', ':')) in prompt
    assert 'not a substitute for distance or angle' in prompt
    assert {'drive', 'gesture'} <= {tool['name'] for tool in host.tools.list_tools()}


def test_every_registered_gesture_is_discoverable():
    catalog = motion_prompt_catalog()
    for name in GESTURES:
        assert json.dumps({'gesture': name}, separators=(',', ':')) in catalog


def test_motion_contract_reaches_final_provider_request(monkeypatch):
    host = EngagementDomain()
    host._tools_text = 'Legacy notes describe direction/speed/duration.'
    host.tools = ToolRegistry()
    sent = []
    client = LLMClient(SimpleNamespace(names=lambda _: []), SimpleNamespace(llm_context=lambda: {}))
    def completion(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"drive":{"mode":"distance","distance_cm":10}}'))])
    monkeypatch.setattr(client, '_ensure_chat_client', lambda _: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=completion))))
    monkeypatch.setattr(client, '_trace_openai_request_async', lambda *a, **k: None)
    try:
        result = client.ask('Can you drive forward ten centimeters?', provider='local',
                            model='test', tools_text=host._tools_prompt_context(),
                            extra_instructions='This request came from speech input.')
        assert result['drive']['distance_cm'] == 10
        system = sent[0]['messages'][0]['content']
        assert '"drive":{"mode":"distance","distance_cm":10}' in system
        assert '"drive":{"mode":"turn","angle_deg":-90}' in system
        assert '"mode":"sine"' in system and '"mode":"wiggle"' in system
        assert 'tiny left/right bursts around 0.5 seconds' not in system
    finally:
        client.shutdown()


@pytest.mark.parametrize('utterance', ['Please wiggle twice', 'Do a sine wave', 'Can you wave?'])
def test_expressive_requests_keep_the_requested_geometry(utterance):
    host = MotionHarness()
    assert host._looks_like_explicit_motion_request(utterance)
    drive = {'mode': 'sine', 'distance_cm': 30, 'cycles': 2, 'amplitude_cm': .1}
    result = host._sanitize_llm_motion({'drive': drive}, user_text=utterance)['drive']
    assert result['distance_cm'] == 30 and result['amplitude_cm'] == .1


def test_stop_is_not_removed_and_ordinary_chat_does_not_gain_forward_motion():
    host = MotionHarness()
    assert host._sanitize_llm_motion({'drive': {'mode': 'stop'}}, 'Stop')['drive'] == {'mode': 'stop'}
    assert 'drive' not in host._sanitize_llm_motion({'drive': {'mode': 'distance', 'distance_cm': 10}}, 'Hello')
