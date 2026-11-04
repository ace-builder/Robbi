"""Fresh-image installer. Standard library only until the venv is ready."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from robbi.setup.host import atomic, boot_config

PACKAGES = ('python3-venv', 'python3-pip', 'python3-picamera2', 'python3-opencv',
            'python3-numpy', 'python3-scipy', 'python3-spidev', 'python3-rpi.gpio',
            'python3-lgpio', 'python3-smbus', 'alsa-utils', 'i2c-tools', 'rpicam-apps',
            'network-manager', 'avahi-daemon', 'ca-certificates', 'bubblewrap', 'libseccomp2', 'polkitd', 'ffmpeg')
REQUIRED = ('pyproject.toml', 'src/robbi/setup/install.py', 'config/desktop.yaml',
            'web/index.html', 'tools/hardware/motor_disable.py')
RESERVED = {'.git', '.venv', 'var', 'backups', '__pycache__', 'node_modules'}


def safe_path(name):
    if not isinstance(name, str):
        raise ValueError('Invalid release path')
    p = PurePosixPath(name)
    if (not name or p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name
            or str(p) != name or any(c in name for c in '\r\n\0')
            or any(x.startswith('.env') or x in RESERVED for x in p.parts)):
        raise ValueError('Private or unsafe release path')
    if name.startswith('config/') and name != 'config/desktop.yaml':
        raise ValueError('Release contains a device-specific configuration')
    return p


def release_manifest(source):
    path = source / 'release-manifest.json'
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Use a packaged runtime release with release-manifest.json.')
    value = json.loads(path.read_text(encoding='utf-8'))
    files = value.get('files')
    if value.get('format') != 1 or not isinstance(files, dict) or not 1 <= len(files) <= 50000:
        raise ValueError('Invalid runtime manifest')
    for name, digest in files.items():
        p = source / safe_path(name)
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(source.resolve()):
            raise ValueError('Missing or unsafe runtime file')
        with p.open('rb') as incoming:
            actual = hashlib.file_digest(incoming, 'sha256').hexdigest()
        if not isinstance(digest, str) or len(digest) != 64 or actual != digest:
            raise ValueError('Runtime manifest checksum mismatch')
    if any(name not in files for name in REQUIRED):
        raise ValueError('Runtime release is incomplete')
    return files, hashlib.sha256(path.read_bytes()).hexdigest()


def platform_issues(system, machine, version, os_release, model):
    issues = []
    if system != 'Linux' or machine not in ('aarch64', 'arm64'):
        issues.append('Use Raspberry Pi OS Lite 64-bit on a Raspberry Pi Zero 2 W.')
    if os_release.get('VERSION_CODENAME') != 'bookworm':
        issues.append('This installer supports Bookworm (Legacy), not Trixie.')
    if not (3, 11) <= tuple(version[:2]) < (3, 13):
        issues.append('Python 3.11 or 3.12 is required.')
    if 'Raspberry Pi Zero 2' not in model:
        issues.append('This hardware setup is for the Raspberry Pi Zero 2 W.')
    return issues


def destination_state(target, release_id):
    marker = target / 'var/setup/install.json'
    if target.exists() and not marker.is_file():
        raise ValueError('An existing ~/robbi-v2 was found. This fresh installer will not overwrite it.')
    state = json.loads(marker.read_text()) if marker.exists() else {'release': release_id, 'steps': []}
    if state.get('release') != release_id:
        raise ValueError('A different runtime is already installed. Back up first; this is not an upgrade command.')
    return marker, state


def run(command, *, cwd=None):
    subprocess.run([str(x) for x in command], cwd=cwd, check=True)


def install(source, target):
    files, release_id = release_manifest(source)
    marker, state = destination_state(target, release_id)
    os_release = platform.freedesktop_os_release()
    model = Path('/proc/device-tree/model').read_text().rstrip('\0') if Path('/proc/device-tree/model').exists() else ''
    issues = platform_issues(platform.system(), platform.machine(), sys.version_info, os_release, model)
    if os.geteuid() == 0:
        issues.append('Run as the Linux account created in Imager, without sudo.')
    if shutil.disk_usage(Path.home()).free < 2 * 1024**3:
        issues.append('At least 2 GiB of free storage is required.')
    if source.resolve() == target.resolve() or target.is_symlink():
        issues.append('Install from a separate unpacked release; the target must not be a symlink.')
    if Path('/etc/systemd/system/robbi-v2.service').exists() and not marker.exists():
        issues.append('A Robbi service already exists. Preserve that installation; use a fresh image.')
    boot = Path('/boot/firmware/config.txt')
    if not boot.is_file():
        issues.append('Bookworm boot configuration was not found.')
    else:
        boot_config(boot.read_text())  # Reject pin conflicts before apt or copying.
    if issues:
        raise ValueError('\n'.join(issues))
    import pwd
    user = pwd.getpwuid(os.getuid()).pw_name
    if target != Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() / 'robbi-v2':
        raise ValueError('Install into the current Linux account’s ~/robbi-v2.')
    run(['sudo', '-v'])
    target.mkdir(parents=True, exist_ok=True)
    atomic(marker, json.dumps(state))

    def step(name, action):
        if name in state['steps']:
            print(f'{name}: already completed')
            return
        print(f'\n{name}…', flush=True)
        action()
        state['steps'].append(name)
        atomic(marker, json.dumps(state))

    def copy_runtime():
        for name in files:
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, dest)
        shutil.copyfile(source / 'release-manifest.json', target / 'release-manifest.json')

    def system_packages():
        run(['sudo', 'apt-get', 'update'])
        run(['sudo', 'apt-get', 'install', '-y', '--no-install-recommends', *PACKAGES])

    python = target / '.venv/bin/python'

    def dependencies():
        run([sys.executable, '-m', 'venv', '--system-site-packages', target / '.venv'])
        # Keep OS NumPy/SciPy/OpenCV/Picamera2; never build these on a 512 MB Pi.
        constraints = target / 'var/setup/os-numerical.txt'
        with constraints.open('w') as output:
            subprocess.run([str(python), '-c',
                "from importlib.metadata import version; print('numpy=='+version('numpy')); print('scipy=='+version('scipy'))"],
                stdout=output, check=True)
        pip = [python, '-m', 'pip', 'install', '--disable-pip-version-check', '-c', constraints,
               '--only-binary=numpy,scipy,Pillow,cryptography,pydantic-core,jiter']
        run([*pip, '-e', '.[providers,voice,navigation,live]'], cwd=target)
        run([*pip, '-r', 'tools/hardware/requirements-pcb.txt', 'st7789>=1,<2', 'smbus2>=0.4,<1'], cwd=target)
        run([python, '-m', 'pip', 'check'])
        with (target / 'var/setup/installed-packages.txt').open('w') as output:
            subprocess.run([str(python), '-m', 'pip', 'freeze'], stdout=output, check=True)

    step('Copy runtime', copy_runtime)
    step('Install system packages', system_packages)
    step('Install Python environment', dependencies)
    run([python, '-m', 'robbi.setup.wizard', '--root', target, '--initialize-only'])
    # Repeat these idempotent operations to repair an interrupted host setup.
    run(['sudo', python, '-m', 'robbi.setup.host', 'install', '--root', target, '--user', user])
    run([python, '-m', 'robbi.setup.wizard', '--root', target])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--plan', action='store_true')
    args = parser.parse_args()
    if args.plan:
        print('Target: Raspberry Pi Zero 2 W, Bookworm Lite 64-bit, Python 3.11/3.12.\n'
              'Install: ~/robbi-v2 (fresh install or resume of the same release only).\n'
              'Verify manifest -> apt packages -> system-site-packages venv -> setup wizard.\n'
              'Generate config/robot.yaml, .env.secrets (0600), robbi-v2.service and robbi-setup.\n'
              'Back up boot settings; enable SPI/I2C/camera/I2S. Motors remain disabled.\n'
              'Wi-Fi edits wait for reboot. Reboot manually after setup.\n'
              'No changes made. A published, audited runtime is required to install.')
        return 0
    try:
        install(args.source.resolve(), Path.home().resolve() / 'robbi-v2')
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Do not expose command arguments, provider bodies or credentials.
        print(str(error) if isinstance(error, ValueError) else
              'Installation stopped. Resolve the failed step above, then rerun the same release to resume.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nSetup interrupted. Rerun the same installer to resume.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
