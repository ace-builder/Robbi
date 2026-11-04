"""Fresh robot defaults and non-destructive edits; no hardware imports."""
import json
import secrets
from pathlib import Path
import time
import yaml

from ..config import load,merge,parse_yaml,validate
from .host import atomic

KEYS={'OPENAI_API_KEY','OPENROUTER_API_KEY','LOCAL_API_KEY','ELEVENLABS_API_KEY',
      'TAVILY_API_KEY','GEMINI_API_KEY','ALIBABA_API_KEY','ROBBI_WEB_TOKEN','TELEGRAM_BOT_TOKEN'}


def new_config():
    return {
        'server':{'host':'0.0.0.0','port':8082,'auth_enabled':True,'allowed_hosts':[]},
        'runtime':{'calibration_confirmed':False,'allow_host_management':True},
        'hardware':{'enable_lcd':True,'enable_motors':False,'enable_servos':False,'right_encoder_b_pin':16},
        'camera':{'enabled':True,'width':640,'height':480,'fps':3},
        'voice':{'enabled':False,'wake_engine':'local_wake','local_wake_max_references':8,
                 'local_wake_max_reference_s':4.,'local_wake_max_reference_bytes':2097152,'local_wake_max_total_feature_frames':1600},
        'tof':{'enabled':True,'h2_enabled':False,'poll_hz':200,'i2c_address':48,'distance_mode':2,'timing_budget_ms':33,'intermeasurement_ms':35,'roi_size':16,'max_age_s':.75},
        'imu':{'enabled':True,'poll_hz':50},
        'motion':{'control_interval_s':.01,'encoder_enabled':True,'obstacle_stop_enabled':True,'obstacle_stop_cm':12,'pwm_max_duty':100,
                  'turn_max_duty':100,'wheel_diameter_cm':6.2,'wheel_base_cm':10.2,'encoder_counts_per_rev':2800},
        'llm':{'chat_provider':'openai','chat_model':'gpt-6-luna','ui_text_provider':'openai','ui_text_model':'gpt-6-luna',
               'speech_input_provider':'openai','speech_input_model':'gpt-6-luna'},
        'stt':{'provider':'openai','model':'whisper-1'},
        'tts':{'provider':'openai','model':'tts-1','voice':'alloy'},
        'autonomy':{'enabled':False,'allow_vision':False,'allow_exploration':False},
        'heartbeat':{'enabled':False},'daily_loop':{'enabled':False,'telegram_brief_enabled':False},
        'startup_warmup':{'enabled':False,'chat_enabled':False},
        'telegram':{'enabled':False,'allowed_chat_ids':'','allow_photos':False,'allow_robot_actions':False,'notify_reminders':False,'notify_proactive':False,'speak_remote_messages':False},
        'apps':{'permissions_enabled':True},
    }


class Configuration:
    def __init__(self,root):
        self.root=Path(root);self.path=self.root/'config/robot.yaml';self.secret_path=self.root/'.env.secrets'

    def read(self):
        return parse_yaml(self.path.read_text(encoding='utf-8')) if self.path.exists() else new_config()

    def ensure_dashboard_password(self):
        """Called by setup only; never changes an existing trusted-LAN choice."""
        if self.read().get('server', {}).get('auth_enabled', True) and not self.keys().get('ROBBI_WEB_TOKEN'):
            self.save_key('ROBBI_WEB_TOKEN', secrets.token_urlsafe(18))

    def save(self,patch):
        value=merge(self.read(),patch)
        defaults=load(root=self.root,source=self.path).values
        validate(merge(defaults,value))
        atomic(self.path,yaml.safe_dump(value,sort_keys=False,allow_unicode=True))
        return value

    def keys(self):
        from ..configuration.secrets import read_env
        return {key: value for key, value in read_env(self.secret_path).items() if key in KEYS}


    def save_key(self,name,value):
        if name not in KEYS or not isinstance(value,str) or len(value)>4096 or any(c in value for c in '\r\n\0"\''):
            raise ValueError('Invalid API key. Paste only the single-line key, without quotes.')
        # Empty input means keep; an accidental Enter never deletes a working key.
        if not value:return
        original=self.secret_path.read_text(encoding='utf-8').splitlines() if self.secret_path.exists() else []
        lines=[line for line in original if line.partition('=')[0].strip()!=name]
        atomic(self.secret_path,'\n'.join(lines+[name+'='+value.strip()])+'\n')

    def owner(self,name=None):
        path=self.root/'var/state/profile.json'
        value=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if name is None:return value.get('user_name','')
        if not isinstance(name,str) or not 1<=len(name.strip())<=80 or any(ord(c)<32 for c in name):raise ValueError('Use an owner name up to 80 characters.')
        value.update(user_name=name.strip(),robot_name=value.get('robot_name','Robbi'),wake_phrase='Hey Robbi',wake_phrase_fixed=True,updated_at=int(time.time()))
        atomic(path,json.dumps(value,ensure_ascii=False,indent=2)+'\n')
