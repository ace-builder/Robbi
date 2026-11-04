"""Spoken game replies cross the microphone and conversation boundary locally."""
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from robbi.app_runtime.host import AppHost
from robot import microphone
from robot.config import settings
from robot.domains.conversation import ConversationDomain
from robot.domains.voice import VoiceDomain
from test_app_conversation import Harness


class Controller(VoiceDomain, ConversationDomain, Harness):
    def __init__(self, tmp_path):
        super().__init__()
        self._conversation_lock = threading.Lock()
        self._conversation_turns = 0
        self._filming_mode = self._voice_processing = False
        self.focus_timer = SimpleNamespace(is_active=lambda: False)
        self.media = SimpleNamespace(is_video_playing=lambda: False, is_audio_playing=lambda: False)
        self.behavior = SimpleNamespace(note_interaction=lambda: None)
        self._touch_activity = lambda: None
        self._set_presentation = lambda **kwargs: None
        self.parse_debug_command = lambda text: None
        self.executed = []
        self.execute_action = lambda action, **kwargs: self.executed.append(action) or action
        self.memory = SimpleNamespace(add_user_message=lambda *args, **kwargs: None,
                                      add_assistant_message=lambda *args, **kwargs: None)
        self._route_background_request = lambda *args, **kwargs: pytest.fail('Game reached background planner')
        self._ask_llm_with_deadline = lambda *args, **kwargs: pytest.fail('Game reached LLM')
        self.llm = SimpleNamespace(classify_followup=lambda *args, **kwargs: pytest.fail("Game used model admission"))
        self._voice_executor = SimpleNamespace(submit=lambda callback, *args: callback(*args))
        self._maybe_arm_followup_conversation = lambda: None
        self.microphone = microphone.MicrophoneService()
        self.microphone._running = True
        self.microphone.configure(command_callback=self._handle_voice_command_transcript,
                                  followup_check_callback=self._accept_spoken_followup)
        robot = SimpleNamespace(capture_token=self._action_token,
                                show_image=lambda *args, **kwargs: {'displayed': True},
                                play_sound=lambda *args, **kwargs: {'ok': True})
        self.app_host = AppHost(Path(__file__).resolve().parents[1], tmp_path, robot,
                                permissions_enabled=False)
        self.app_host.scan()

    def hear(self, text, *, followup=False):
        mic = self.microphone
        mic._transcribe = lambda purpose, wav: text
        options = {}
        if followup:
            options['followup'] = {'token': mic.followup_token(), 'reply': self.executed[-1]['speak'],
                                   'game': True, 'cancelled': lambda: self.app_host.game_session() is None}
        before = len(self.executed)
        mic._process_command_capture(b'fake audio', **options)
        assert len(self.executed) == before + 1, f'Spoken reply was not dispatched: {text}'
        assert mic._last_command_transcript == text
        return self.executed[-1]


@pytest.fixture
def game(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'voice_enabled', True)
    monkeypatch.setattr(settings, 'conversation_followup_enabled', True)
    subject = Controller(tmp_path)
    try:
        yield subject
    finally:
        subject.app_host.stop()
        subject.app_host._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("followup", [False, True])
def test_exact_reported_spoken_conversation_stays_local(game, followup):
    start = game.hear("Let's play tic-tac-toe.")
    assert start['app_result']['result']['data']['active']
    assert game.app_host.game_session() is not None
    move = game.hear('Number one please.', followup=followup)
    board = move['app_result']['result']['data']['board']
    assert board[0] == 'X'
    assert board.count('O') == 1
    occupied = game.hear('Number one please.', followup=followup)
    assert not occupied['app_result']['result']['data']['accepted']
    assert occupied['app_result']['result']['data']['board'] == board
    assert game.app_host.game_session() is not None
    empty = board.index('')
    resumed = game.hear(f'Number {empty + 1} please.', followup=followup)
    resumed_board = resumed['app_result']['result']['data']['board']
    assert resumed['app_result']['result']['data']['accepted']
    assert resumed_board[empty] == 'X'
    assert resumed_board.count('X') == 2 and resumed_board.count('O') == 2
    assert game.provider_calls == []


@pytest.mark.parametrize('phrase, square', [
    ('put it in square 3 please', 2),
    ("I'll take five", 4),
    ('Nummer eins bitte.', 0),
])
def test_polite_game_reply_is_admitted_and_executed(game, phrase, square):
    game.hear("Let's play tic-tac-toe.")
    move = game.hear(phrase, followup=True)
    board = move['app_result']['result']['data']['board']
    assert board[square] == 'X'
    assert board.count('O') == 1


@pytest.mark.parametrize('followup', [False, True])
def test_out_of_range_turn_retries_then_accepts_move_and_stop(game, followup):
    start = game.hear("Let's play tic-tac-toe.")
    board = start['app_result']['result']['data']['board']
    retry = game.hear('Number ten please.', followup=followup)
    assert not retry['app_result']['result']['data']['accepted']
    assert retry['app_result']['result']['data']['board'] == board
    assert game.app_host.game_session() is not None
    move = game.hear('Number one please.', followup=followup)
    assert move['app_result']['result']['data']['board'][0] == 'X'
    stopped = game.hear('stop game please', followup=followup)
    assert not stopped['app_result']['result']['data']['active']
    assert game.app_host.game_session() is None
    assert game.provider_calls == []

