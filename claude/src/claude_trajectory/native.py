"""User-triggered macOS recovery. Accept a session ID, never an endpoint or command.

Adapted from codex-trajectory (MIT, Copyright (c) 2026 Fly-Carrot). Uses its own URL
scheme and bundle identifier so the Codex handler is never replaced.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from .launcher import resume, healthy, open_browser
    from .broker import private_directory, read_private
except ImportError:
    from launcher import resume, healthy, open_browser
    from broker import private_directory, read_private

SCHEME = 'claude-trajectory://reconnect/'
BUNDLE_ID = 'local.claude-trajectory.recovery'
APP_NAME = 'Claude Trajectory Recovery.app'


def apple_string(value):
    if any(ord(char) < 32 for char in value):
        raise ValueError('Control characters are not allowed in recovery app paths')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def parse_uri(uri):
    match = re.fullmatch(r'claude-trajectory://reconnect/([a-zA-Z0-9][a-zA-Z0-9_-]{0,100})', uri)
    if not match:
        raise ValueError('Invalid recovery link: expected an exact session identifier')
    return match[1]


def confirm(session):
    prompt = 'Open the local claude-trajectory viewer for session ' + session + '?'
    script = 'display dialog ' + json.dumps(prompt) + ' with title "Claude Trajectory Recovery" buttons {"Cancel", "Reconnect"} default button "Reconnect" cancel button "Cancel"'
    result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True, timeout=120)
    return result.returncode == 0


def recover(uri, base, confirm=confirm, resumer=resume, checker=healthy, opener=open_browser):
    session = parse_uri(uri)
    base = Path(base)
    if not confirm(session):
        return {'status': 'cancelled', 'session_id': session}
    state, projects = base / 'runtime', base.parent / 'projects'
    result = resumer(state, session, projects)
    if result.get('session_id') != session or not isinstance(result.get('url'), str):
        raise RuntimeError('Recovery did not return this exact session')
    parsed = urlsplit(result['url'])
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or not parsed.port or parsed.username or parsed.password
            or parsed.path != '/s/' + session or parsed.query or not parsed.fragment):
        raise ValueError('Recovery returned an unsafe endpoint')
    verified = checker(state / session / 'connection.json', session)
    if not verified or verified.get('url') != result['url'] or verified.get('session_id') != session:
        raise RuntimeError('Recovered observer identity did not verify')
    opener(result['url'])
    return {'status': 'opened', 'session_id': session}


def install_app(base, dry_run=False):
    if sys.platform != 'darwin':
        raise ValueError('Native recovery app is currently macOS-only; use the CLI resume command elsewhere')
    base = Path(base).absolute()
    app = base / APP_NAME
    if dry_run:
        return {'app': str(app), 'scheme': SCHEME + 'SESSION_ID', 'bundle_id': BUNDLE_ID, 'dry_run': True}
    private_directory(base)
    private_directory(base / 'app')
    script_path = base / 'app/native.py'
    if script_path.is_symlink() or not script_path.is_file():
        raise ValueError('Install the runtime before the recovery app')
    if app.exists() or app.is_symlink():
        raise ValueError('Recovery app already exists; back it up before replacement')
    # The compiled handler pins the interpreter and script; URL text is shell-quoted.
    command = shlex.join([sys.executable, '-I', str(script_path), '--url']) + ' '
    choose = shlex.join([sys.executable, '-I', str(script_path), '--choose'])
    source = ('on open location incomingURL\n'
              'try\n do shell script ' + apple_string(command) + ' & quoted form of incomingURL\n'
              'on error messageText\n display alert "Claude trajectory recovery failed" message messageText\nend try\nend open location\n'
              'on run\ntry\n do shell script ' + apple_string(choose) + '\n'
              'on error messageText\n display alert "Claude trajectory recovery failed" message messageText\nend try\nend run\n')
    with tempfile.TemporaryDirectory(prefix='.recovery-', dir=base) as temporary:
        source_file = Path(temporary) / 'handler.applescript'
        source_file.write_text(source)
        pending = Path(temporary) / app.name
        subprocess.run(['/usr/bin/osacompile', '-o', str(pending), str(source_file)],
                       check=True, capture_output=True, timeout=30)
        info_path = pending / 'Contents/Info.plist'
        info = plistlib.loads(info_path.read_bytes())
        info.update(CFBundleIdentifier=BUNDLE_ID, LSUIElement=True,
                    CFBundleURLTypes=[{'CFBundleURLName': 'Claude Trajectory Recovery',
                                       'CFBundleURLSchemes': ['claude-trajectory']}])
        info_path.write_bytes(plistlib.dumps(info))
        pending.rename(app)
    subprocess.run(['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister',
                    '-f', str(app)], check=True, capture_output=True, timeout=15)
    return {'app': str(app), 'scheme': SCHEME + 'SESSION_ID', 'installed': True}


def choose_uri(base):
    registry = read_private(base / 'runtime/registry.json')
    sessions = sorted(s for s in registry['sessions'] if re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}', s))
    if not sessions:
        raise ValueError('No registered sessions')
    script = ('set picked to choose from list {' + ','.join(json.dumps(s) for s in sessions) +
              '} with title "Claude Trajectory Recovery" with prompt "Choose a registered session" without multiple selections allowed\n'
              'if picked is false then return ""\nreturn item 1 of picked')
    result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True, timeout=120, check=True)
    return SCHEME + result.stdout.strip() if result.stdout.strip() else None


def main():
    for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONUSERBASE'):
        os.environ.pop(name, None)
    os.environ['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--url')
    group.add_argument('--choose', action='store_true')
    args = parser.parse_args()
    base = Path(__file__).resolve().parent.parent
    try:
        private_directory(base)
        with os.fdopen(os.open(base / 'recovery.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            uri = choose_uri(base) if args.choose else args.url
            if uri:
                result = recover(uri, base)
                print(json.dumps(result))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, 'Recovery stopped: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
