"""Small privileged operations; arguments are structured, credentials use stdin."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

BEGIN='# BEGIN ROBBI MANAGED INTERFACES'
END='# END ROBBI MANAGED INTERFACES'
INTERFACES=('dtparam=i2c_arm=on','dtparam=spi=on','dtoverlay=spi0-1cs,no_miso',
            'camera_auto_detect=1','dtoverlay=max98357a,no-sdmode')


def atomic(path,content,mode=0o600):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        os.fchmod(fd,mode) if hasattr(os,'fchmod') else None
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as f:
            f.write(content);f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
        path.chmod(mode)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def boot_config(text):
    if text.count(BEGIN)!=text.count(END) or text.count(BEGIN)>1:
        raise ValueError('Malformed Robbi interface block; restore the boot-config backup first.')
    original=re.sub(re.escape(BEGIN)+'.*?'+re.escape(END)+'\n?', '',text,flags=re.S).rstrip()+'\n'
    for line in original.splitlines():
        clean=line.split('#',1)[0].strip()
        if clean.startswith('dtoverlay=max98357a') and clean!='dtoverlay=max98357a,no-sdmode':
            raise ValueError('Existing MAX98357A overlay can claim motor GPIO4. Resolve it before setup.')
        if clean in ('dtparam=spi=off','dtparam=i2c_arm=off','camera_auto_detect=0') or (
                clean.startswith('dtoverlay=spi0-') and clean!='dtoverlay=spi0-1cs,no_miso'):
            raise ValueError('Conflicting SPI/I2C/camera boot setting; review config.txt before setup.')
    # End earlier board filters so these lines also apply to Zero 2 W.
    return original+'\n'+BEGIN+'\n[all]\n'+'\n'.join(INTERFACES)+'\n'+END+'\n'


def unit_text(root,user):
    if not re.fullmatch('[a-z_][a-z0-9_-]{0,31}',user):raise ValueError('Invalid Linux username')
    root=str(root)
    if not root.startswith('/') or any(c in root for c in '\n\r\0"\\%'):raise ValueError('Unsupported installation path')
    python=f'"{root}/.venv/bin/python"'
    return f'''[Unit]
Description=Robbi robot
After=network.target
StartLimitIntervalSec=120
StartLimitBurst=4

[Service]
Type=simple
User={user}
WorkingDirectory="{root}"
Environment=PYTHONUNBUFFERED=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=OMP_NUM_THREADS=1
Environment=MKL_NUM_THREADS=1
Environment=NUMEXPR_NUM_THREADS=1
Environment=BLIS_NUM_THREADS=1
Environment=MALLOC_ARENA_MAX=2
ExecStartPre=-{python} "{root}/tools/hardware/motor_disable.py"
ExecStart={python} -m robbi --root "{root}" --config "{root}/config/robot.yaml"
ExecStopPost=-{python} "{root}/tools/hardware/motor_disable.py"
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillMode=control-group
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
LimitCORE=0
MemoryAccounting=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
'''


def wifi_keyfile(ssid,password):
    if not isinstance(ssid,str) or not 1<=len(ssid.encode('utf-8'))<=32 or any(ord(c)<32 for c in ssid):
        raise ValueError('Wi-Fi network name must contain 1–32 UTF-8 bytes without control characters.')
    if not isinstance(password,str) or not (8<=len(password)<=63 and all(32<=ord(c)<=126 for c in password)):
        raise ValueError('Use an 8–63 character WPA2/WPA3 Personal password.')
    def escape(value):return value.replace('\\','\\\\').replace(' ','\\s')
    return f'''[connection]
id=Robbi setup Wi-Fi
uuid={uuid.uuid4()}
type=wifi
autoconnect=true
autoconnect-priority=100

[wifi]
mode=infrastructure
ssid={';'.join(str(b) for b in ssid.encode('utf-8'))};
band=bg

[wifi-security]
key-mgmt=wpa-psk
psk={escape(password)}

[ipv4]
method=auto

[ipv6]
method=auto
'''


def run(args):
    subprocess.run(args,check=True,timeout=90)


def power_policy(user):
    if not isinstance(user, str) or not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', user):
        raise ValueError('Invalid Linux username')
    return '''// Robbi: power off this Pi and restart only its robot service.
polkit.addRule(function(action, subject) {
    if (subject.user !== ''' + json.dumps(user) + ''') return;
    if (action.id === "org.freedesktop.login1.power-off" ||
        action.id === "org.freedesktop.login1.power-off-multiple-sessions")
        return polkit.Result.YES;
    if (action.id === "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") === "robbi-v2.service" && action.lookup("verb") === "restart")
        return polkit.Result.YES;
});
'''


def install_power_controls(user):
    import pwd
    policy = power_policy(user)
    pwd.getpwnam(user)  # Refuse misspelled or nonexistent accounts.
    destination = Path('/etc/polkit-1/rules.d/49-robbi-power.rules')
    if not Path('/usr/share/polkit-1/actions/org.freedesktop.login1.policy').is_file():
        raise ValueError('Install polkitd first: sudo apt install polkitd')
    if destination.is_symlink():
        raise ValueError('Refusing a symlinked power-control policy')
    if destination.exists() and destination.read_text() != policy:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        atomic(destination.with_name(destination.name + '.before-robbi-' + stamp), destination.read_text(), 0o644)
    atomic(destination, policy, 0o644)


def install_host(root,user):
    import pwd
    account=pwd.getpwnam(user);root=Path(root).resolve()
    if root!=Path(account.pw_dir).resolve()/'robbi-v2':raise ValueError('Install only into the selected user’s ~/robbi-v2.')
    if root.stat().st_uid!=account.pw_uid:raise ValueError('Installation must belong to its Linux user.')
    source=Path('/boot/firmware/config.txt')
    before=source.read_text();after=boot_config(before)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if after!=before:
        atomic(source.with_name('config.txt.before-robbi-'+stamp),before,0o644)
        atomic(source,after,0o644)
        atomic(root/'var/setup/reboot-required','Interfaces changed; reboot to activate.\n')
        os.chown(root/'var/setup/reboot-required',account.pw_uid,account.pw_gid)
    run(['usermod','-aG','gpio,i2c,spi,audio,video',user])
    destination=Path('/etc/systemd/system/robbi-v2.service');unit=unit_text(root,user)
    if destination.exists() and destination.read_text()!=unit:
        atomic(destination.with_name('robbi-v2.service.before-setup-'+stamp),destination.read_text(),0o644)
    atomic(destination,unit,0o644)
    launcher='#!/bin/sh\nexec '+shlex.quote(str(root/'.venv/bin/python'))+' -m robbi.setup.wizard --root '+shlex.quote(str(root))+' "$@"\n'
    atomic('/usr/local/bin/robbi-setup',launcher,0o755)
    run(['systemd-analyze','verify',str(destination)])
    run(['systemctl','daemon-reload'])
    run(['systemctl','enable','robbi-v2.service'])
    install_power_controls(user)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['install','wifi','start','restart','power-controls'])
    parser.add_argument('--root');parser.add_argument('--user');args=parser.parse_args()
    if not hasattr(os,'geteuid') or os.geteuid()!=0:raise SystemExit('This operation needs sudo.')
    try:
        if args.action=='install':install_host(args.root,args.user)
        elif args.action=='power-controls':
            install_power_controls(args.user)
            print('Power controls installed. Enable them in Dashboard Settings → Service & power.')
        elif args.action=='wifi':
            raw=sys.stdin.read(16385)
            if len(raw)>16384:raise ValueError('Network request too large')
            value=json.loads(raw);contents=wifi_keyfile(value.get('ssid'),value.get('password'))
            atomic('/etc/NetworkManager/system-connections/robbi-setup.nmconnection',contents)
            # Do not activate or reload: preserve the live SSH network until reboot.
            print('Wi-Fi saved for the next reboot. The current connection is unchanged.')
        else:run(['systemctl',args.action,'robbi-v2.service'])
    except (ValueError,TypeError,KeyError,AttributeError,OSError,subprocess.SubprocessError):
        print('Host setup failed. Check the OS, boot settings, permissions and service logs.',file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
