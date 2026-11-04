"""Small first-run terminal UI; normal operation lives in the web dashboard."""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import checks
from .configuration import Configuration
from .host import wifi_keyfile


def ask(label, default=''):
    return input(label + (f' [{default}]' if default else '') + ': ').strip() or default


def yes(label, default=False):
    return ask(label + ' (y/n)', 'y' if default else 'n').lower() in ('y', 'yes')


def hidden(label):
    if not sys.stdin.isatty():
        raise ValueError('Use an interactive SSH terminal for hidden credential entry.')
    return getpass.getpass(label + ' (hidden; Enter keeps existing): ')


def privileged(action, root, payload=None):
    subprocess.run(['sudo', '-v'], check=True)
    subprocess.run(['sudo', '-n', sys.executable, '-m', 'robbi.setup.host', action, '--root', str(root)],
                   input=json.dumps(payload) if payload else None, text=True, check=True, timeout=95)


class Wizard:
    def __init__(self, root):
        self.config = Configuration(root)
        self.root = Path(root)

    def identity(self):
        print(f'\nLinux login: {getpass.getuser()} (created in Raspberry Pi Imager).')
        name = ask('What should Robbi call you?', self.config.owner())
        if name:
            self.config.owner(name)

    def network(self):
        print('\nKeep your current Wi-Fi, or save another 2.4 GHz Personal network for the next reboot.')
        ok, message = checks.network_test()
        print(('OK: ' if ok else 'CHECK: ') + message)
        if not yes('Save a different network for next reboot?'):
            return
        ssid = ask('Network name (SSID)')
        password = hidden('Wi-Fi password')
        wifi_keyfile(ssid, password)  # Validate before sudo; never print credentials.
        privileged('wifi', self.root, {'ssid': ssid, 'password': password})
        print('The new network has not been tested. Reconnect after reboot using its router address or hostname.')

    def dashboard(self):
        from ..dashboard_security import password_value
        enabled = self.config.read().get('server', {}).get('auth_enabled', True)
        print('\nDashboard sign-in protects camera, microphone, driving and settings. Username: robbi.')
        enabled = yes('Require a dashboard password?', enabled)
        if enabled:
            value = hidden('Choose dashboard password (10–128 characters)')
            if not value and not (self.root / 'var/setup/wizard-complete').exists():
                import secrets
                value = secrets.token_urlsafe(15)
                self.config.save_key('ROBBI_WEB_TOKEN', value)
                print('Generated dashboard password (save it now): ' + value)
                self.config.save({'server': {'auth_enabled': True}})
                return
            if value:
                password_value(value)
                if value != hidden('Repeat dashboard password'):
                    raise ValueError('Passwords do not match')
                self.config.save_key('ROBBI_WEB_TOKEN', value)
            self.config.save({'server': {'auth_enabled': True}})
            self.config.ensure_dashboard_password()
            if not value:
                print('Existing/generated password retained; use the dashboard settings or rerun this step to set your own.')
        elif yes('Allow every device on this trusted network to control Robbi without sign-in?'):
            self.config.save({'server': {'auth_enabled': False}})

    def telegram(self):
        print('\nTelegram is optional. Only your private Telegram user ID will be allowed.')
        if not yes('Set up Telegram now?'):
            return
        print('1. Open Telegram @BotFather, use /newbot, then copy the bot token.')
        print('2. Message @userinfobot with /start and copy the numeric Id it reports for you.')
        print('3. Enter that positive user ID below, then open your own bot and send /start.')
        print('Never share the bot token. Group chats are not added by this setup.')
        token = hidden('Telegram bot token')
        user_id = ask('Your numeric Telegram user ID')
        if not user_id.isdecimal() or not 0 < int(user_id) < 2**53:
            raise ValueError('Enter your positive numeric Telegram user ID, not a username')
        if token:
            import re
            if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', token):
                raise ValueError('Invalid Telegram bot token')
            self.config.save_key('TELEGRAM_BOT_TOKEN', token)
        if not self.config.keys().get('TELEGRAM_BOT_TOKEN'):
            raise ValueError('A Telegram bot token is required')
        self.config.save({'telegram': {'enabled': True, 'allowed_chat_ids': str(int(user_id)),
                                      'allow_photos': False, 'allow_robot_actions': False}})
        print('Only your private chat is allowed. Photos and robot actions stay off; enable them later in Settings if wanted.')

    def providers(self):
        current = self.config.read()
        old_provider = current.get('llm', {}).get('chat_provider', 'openai')
        print('\nLLM provider: openai / openrouter / local. Blank fields keep existing settings.')
        provider = ask('Provider', old_provider)
        if provider not in checks.PROVIDERS:
            raise ValueError('Choose openai, openrouter or local.')
        default_base, key_name = checks.PROVIDERS[provider]
        field = 'llm_base_url' if provider == 'local' else 'base_url'
        base = checks.endpoint(ask('API base URL', current.get('providers', {}).get(provider, {}).get(field, default_base)), provider)
        model = ask('LLM model', current.get('llm', {}).get('chat_model') or 'gpt-6-luna')
        if not model or len(model) > 160 or any(ord(c) < 32 for c in model):
            raise ValueError('Use a model ID supported by your provider.')
        self.config.save_key(key_name, hidden(key_name))
        patch = {'llm': {key: value for route in ('chat', 'ui_text', 'speech_input')
                         for key, value in ((route + '_provider', provider), (route + '_model', model))},
                 'providers': {provider: {field: base}}}
        self.config.save(patch)
        if provider != 'openai':
            print('OpenAI speech recognition uses its own key; local STT/TTS can be selected later in the dashboard.')
            if yes('Add or update an OpenAI key for speech?'):
                self.config.save_key('OPENAI_API_KEY', hidden('OPENAI_API_KEY'))
        print('Speech defaults to OpenAI; ElevenLabs is optional. Voice listening stays off until wake enrollment.')
        if yes('Configure ElevenLabs voice?'):
            voice = ask('Your ElevenLabs voice ID', current.get('tts', {}).get('elevenlabs_voice_id', ''))
            if not voice or len(voice) > 128 or not voice.isalnum():
                raise ValueError('Enter a voice ID available to your ElevenLabs account.')
            model = ask('ElevenLabs model', current.get('tts', {}).get('elevenlabs_model', 'eleven_v4'))
            self.config.save_key('ELEVENLABS_API_KEY', hidden('ELEVENLABS_API_KEY'))
            self.config.save({'tts': {'provider': 'elevenlabs', 'elevenlabs_voice_id': voice, 'elevenlabs_model': model}})
        if yes('Test the LLM now? (one small billable request)', True):
            ok, message = checks.llm_test(self.config.read(), self.config.keys())
            print(('OK: ' if ok else 'CHECK: ') + message)

    def diagnostics(self):
        ok, message = checks.network_test()
        print(('OK: ' if ok else 'CHECK: ') + message)
        for found, label in checks.inventory():
            print(('FOUND: ' if found else 'CHECK: ') + label)
        print('These are interface checks. Use dashboard Overview for camera/ToF/IMU health and the build guide for physical tests.')
        print('Missing peripherals do not prevent dashboard startup. No motors were moved.')

    def web_search(self):
        print('\nWeb search is optional. Weather uses a free endpoint without an API key by default.')
        if not yes('Configure Tavily web search?'):
            return
        self.config.save_key('TAVILY_API_KEY',hidden('TAVILY_API_KEY'))
        if not self.config.keys().get('TAVILY_API_KEY'):
            raise ValueError('A Tavily API key is required to enable this provider')
        self.config.save({'web_search':{'provider':'tavily'}})
        print('Web search configured. No billable search was made; test it later in Interact.')

    def finish(self):
        self.config.save({})
        privileged('restart', self.root)
        port = self.config.read().get('server', {}).get('port', 8082)
        ready = False
        for _ in range(10):
            if checks.runtime_health(port, token=self.config.keys().get('ROBBI_WEB_TOKEN', '')):
                ready = True
                break
            time.sleep(1)
        print('\nDashboard: ' + checks.dashboard_url(port))
        print('Runtime responded.' if ready else 'Runtime is still starting or needs attention: journalctl -u robbi-v2.service -n 60')
        print('Use the Pi’s router-assigned IP if .local is unavailable. Reconfigure later with: robbi-setup')
        print('Dashboard: calibrate IMU and wheels, enroll “Hey Robbi”, then enable voice and movement when ready.')
        enabled = self.config.read().get('server', {}).get('auth_enabled', True)
        print('Dashboard sign-in: ' + ('enabled (username robbi).' if enabled else 'disabled by owner choice for a trusted LAN.'))
        print('Change the password or access choice in Settings > Dashboard access, or robbi-setup.')
        marker = self.root / 'var/setup/reboot-required'
        if marker.exists():
            print('Reboot once to activate hardware interfaces: sudo reboot')
        print('Wi-Fi changes also require a deliberate reboot; this wizard does not disconnect SSH.')

    def run(self):
        completed = self.root / 'var/setup/wizard-complete'
        print('\nRobbi setup — minimal first run, then the dashboard\n')
        if not completed.exists():
            for name, action in [('Owner', self.identity), ('Dashboard', self.dashboard), ('Network', self.network), ('Providers', self.providers), ('Web search',self.web_search), ('Telegram', self.telegram)]:
                while True:
                    try:
                        action()
                        break
                    except (ValueError, OSError, subprocess.SubprocessError):
                        print(f'{name} was not fully configured. Existing saved settings are preserved.')
                        if not yes('Try this step again?', True):
                            break
            self.diagnostics()
        else:
            while True:
                choice = ask('\n1 Owner  2 Wi-Fi  3 Providers  4 Checks  5 Save and start  6 Dashboard password  7 Telegram  8 Web search  0 Exit', '5')
                if choice == '0':
                    return
                if choice == '5':
                    break
                action = {'1': self.identity, '2': self.network, '3': self.providers, '4': self.diagnostics, '6': self.dashboard, '7': self.telegram,'8':self.web_search}.get(choice)
                if action:
                    try:
                        action()
                    except (ValueError, OSError, subprocess.SubprocessError):
                        print('Step could not be completed. Check the values and retry; saved settings are preserved.')
        self.finish()
        from .host import atomic
        atomic(completed, 'Use robbi-setup to edit initial settings again.\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.home() / 'robbi-v2')
    parser.add_argument('--initialize-only', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        parser.error('Run robbi-setup as your normal Linux user, without sudo.')
    try:
        config = Configuration(args.root)
        config.save({})
        config.ensure_dashboard_password()
        if not args.initialize_only:
            Wizard(args.root).run()
    except (KeyboardInterrupt, EOFError):
        print('\nSetup stopped. Saved settings remain; run robbi-setup to continue.')
        return 130
    except (ValueError, OSError, subprocess.SubprocessError):
        print('Setup could not finish. Check configuration and service logs; run robbi-setup to retry.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
