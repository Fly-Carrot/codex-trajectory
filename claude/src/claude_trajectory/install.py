"""Opt-in, reversible user-level integration. Adds only hooks and files this tool owns.

Adapted from codex-trajectory (MIT, Copyright (c) 2026 Fly-Carrot).
"""
import argparse
import copy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile

FILES = ('events.py', 'sources.py', 'server.py', 'history.py', 'broker.py', 'launcher.py', 'native.py', 'index.html', 'install.py')
EVENTS = ('SessionStart', 'UserPromptSubmit')
MARKER = '<!-- claude-trajectory: owned command file -->'
BEGIN = '<!-- CLAUDE-TRAJECTORY:BEGIN -->'
END = '<!-- CLAUDE-TRAJECTORY:END -->'
WRAPPER = ('import sys\nfrom pathlib import Path\n'
           'base = Path(__file__).resolve().parent\n'
           'sys.path.insert(0, str(base / "app"))\n'
           'from launcher import main\n'
           'main(["--state-dir", str(base / "runtime"), "--projects", str(base.parent / "projects"), *sys.argv[1:]])\n')


def atomic(path, data, mode=0o600):
    if path.is_symlink():
        raise ValueError('Refusing symlink: ' + str(path))
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.claude-trajectory-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def private_dirs(*paths):
    for directory in paths:
        if directory.exists() or directory.is_symlink():
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('Installation directories must be private, owned by you, and not symlinks')


def command_text(wrapper):
    run = shlex.join([sys.executable, '-I', str(wrapper)])
    return ('---\ndescription: Show the verified claude-trajectory link for this session\n---\n' + MARKER + '\n'
            'Run `' + run + ' url` with the Bash tool. If it prints JSON with a "url", reply with only '
            '`[Trajectory](<url>)`. If it fails, run `' + run + ' resume` once and reply with that link, or '
            'report its one-line error. Do not open a browser, guess a session, or write the link into files.\n')


def rule_text(wrapper):
    run = shlex.join([sys.executable, '-I', str(wrapper)])
    return (BEGIN + '\n## claude-trajectory\n\n'
            'At the end of each final reply, append one `[Trajectory](verified URL)` link when the '
            'claude-trajectory hook supplies it for this exact session. If needed, verify the current link with `'
            + run + ' url --session-id "$CLAUDE_CODE_SESSION_ID"`. Never guess a session or URL, expose the link '
            'in public artifacts, open tabs automatically, or interrupt normal work if the observer is unavailable.\n'
            + END)


def strip_rule(text):
    """Remove the owned block and the blank lines the installer put around it."""
    if BEGIN not in text and END not in text:
        return text
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(END) < text.index(BEGIN):
        raise ValueError('Ambiguous claude-trajectory rule markers in CLAUDE.md; review manually')
    start, end = text.index(BEGIN), text.index(END) + len(END)
    before, after = text[:start], text[end:]
    if after.startswith('\n'):
        after = after[1:]
    if before.endswith('\n\n'):
        before = before[:-1]
    return before + after


def configure(home, dry_run=False, uninstall=False, command=True, rule=True):
    home = Path(home).expanduser().absolute()
    if home.is_symlink():
        raise ValueError('Refusing symlinked Claude home')
    settings_path = home / 'settings.json'
    command_path = home / 'commands' / 'trajectory.md'
    rules_path = home / 'CLAUDE.md'
    if settings_path.is_symlink() or command_path.is_symlink() or rules_path.is_symlink():
        raise ValueError('Refusing symlinked settings, command or CLAUDE.md file')
    original = settings_path.read_bytes() if settings_path.exists() else None
    settings = json.loads(original or b'{}')
    if not isinstance(settings, dict) or not isinstance(settings.get('hooks', {}), dict):
        raise ValueError('Invalid settings.json shape')
    target = home / 'claude-trajectory'
    private_dirs(target, target / 'app', target / 'backups')
    wrapper = target / 'claude-trajectory'
    hook_command = shlex.join([sys.executable, '-I', '-B', str(wrapper), 'hook'])
    manifest_path = target / 'installation.json'
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    owned = {hook_command, previous.get('hook_command', '')} - {''}
    # Keys this tool created are removed again on uninstall if they end up empty.
    created = set(previous.get('created_keys', [])) if not previous.get('uninstalled') else set()
    created |= {key for key in ('hooks', *EVENTS) if key not in settings and key not in settings.get('hooks', {})}
    updated = copy.deepcopy(settings)
    hooks = updated.setdefault('hooks', {})
    changes = []
    for event in EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError('Invalid ' + event + ' hooks')
        retained = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                raise ValueError('Invalid hook matcher group')
            other = [h for h in group['hooks'] if not (isinstance(h, dict) and h.get('command') in owned)]
            if len(other) != len(group['hooks']):
                changes.append('remove owned ' + event + ' hook')
            if other == group['hooks']:
                retained.append(group)
            elif other:
                retained.append({**group, 'hooks': other})
        if not uninstall:
            retained.append({'hooks': [{'type': 'command', 'command': hook_command, 'timeout': 10}]})
            changes.append('add ' + event + ' hook')
        if retained or event not in created:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    if not hooks and 'hooks' in created:
        updated.pop('hooks')
    existing_command = command_path.read_text() if command_path.exists() else None
    command_owned = existing_command is not None and MARKER in existing_command
    if existing_command is not None and not command_owned and command and not uninstall:
        raise ValueError('commands/trajectory.md exists and is not owned by claude-trajectory; rerun with --no-command')
    write_command = command and not uninstall
    remove_command = command_owned and (uninstall or not command)
    if write_command:
        changes.append('write /trajectory command')
    if remove_command:
        changes.append('remove owned /trajectory command')
    rules = rules_path.read_text(encoding='utf-8') if rules_path.exists() else None
    base_rules = strip_rule(rules or '')
    if rule and not uninstall:
        gap = '' if not base_rules.strip() else '\n' if base_rules.endswith('\n') else '\n\n'
        new_rules = base_rules + gap + rule_text(wrapper) + '\n'
        if BEGIN in (rules or ''):
            start, end = rules.index(BEGIN), rules.index(END) + len(END)
            new_rules = rules[:start] + rule_text(wrapper) + rules[end:]
    else:
        new_rules = base_rules if rules is not None else None
    if new_rules != rules:
        had, has = BEGIN in (rules or ''), BEGIN in (new_rules or '')
        changes.append(('update' if had and has else 'add' if has else 'remove owned') + ' CLAUDE.md rule block')
    result = {'action': 'uninstall' if uninstall else 'install', 'dry_run': dry_run,
              'settings': str(settings_path), 'rules': str(rules_path), 'runtime': str(target), 'hook_command': hook_command,
              'changes': changes,
              'preserved': 'All other settings, hooks, permissions, MCP servers and rules are left unchanged.'}
    if dry_run:
        return result
    home.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError('Refusing symlinked installation')
    target.mkdir(mode=0o700, exist_ok=True)
    for part in ('app', 'backups'):
        directory = target / part
        if directory.is_symlink():
            raise ValueError('Refusing symlinked installation directory')
        directory.mkdir(mode=0o700, exist_ok=True)
    lock = target / 'install.lock'
    with os.fdopen(os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (settings_path.read_bytes() if settings_path.exists() else None) != original:
            raise ValueError('settings.json changed during install; retry after review')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        if original is not None:
            atomic(target / 'backups' / (stamp + '-settings.json'), original)
        if existing_command is not None and (write_command or remove_command):
            atomic(target / 'backups' / (stamp + '-trajectory.md'), existing_command.encode())
        if rules is not None and new_rules != rules:
            atomic(target / 'backups' / (stamp + '-CLAUDE.md'), rules.encode('utf-8'))
        if not uninstall:
            for name in FILES:
                atomic(target / 'app' / name, Path(__file__).with_name(name).read_bytes())
            atomic(wrapper, WRAPPER.encode(), 0o700)
        if updated != settings or original is None:
            mode = stat.S_IMODE(settings_path.stat().st_mode) if original is not None else 0o600
            atomic(settings_path, (json.dumps(updated, indent=2, ensure_ascii=False) + '\n').encode(), mode)
        if write_command:
            command_path.parent.mkdir(exist_ok=True)
            atomic(command_path, command_text(wrapper).encode(), 0o644)
        elif remove_command:
            command_path.unlink()
        if new_rules != rules:
            if (rules_path.read_text(encoding='utf-8') if rules_path.exists() else None) != rules:
                raise ValueError('CLAUDE.md changed during install; retry after review')
            mode = stat.S_IMODE(rules_path.stat().st_mode) if rules is not None else 0o644
            atomic(rules_path, new_rules.encode('utf-8'), mode)
        atomic(manifest_path, json.dumps({'hook_command': hook_command, 'command_file': str(command_path) if write_command else None,
                                          'created_keys': sorted(created), 'uninstalled': uninstall}).encode())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claude-home', default=os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--no-command', action='store_true', help='Do not add the /trajectory command file')
    parser.add_argument('--no-rule', action='store_true', help='Do not add the rule block to CLAUDE.md')
    parser.add_argument('--recovery-only', action='store_true', help='Install the optional macOS recovery app; do not change hooks')
    args = parser.parse_args()
    try:
        if args.recovery_only:
            if args.uninstall:
                raise ValueError('Do not combine --recovery-only and --uninstall')
            try:
                from .native import install_app
            except ImportError:
                from native import install_app
            result = install_app(Path(args.claude_home).expanduser() / 'claude-trajectory', args.dry_run)
        else:
            result = configure(args.claude_home, args.dry_run, args.uninstall, command=not args.no_command,
                               rule=not args.no_rule)
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, UnicodeError) as exc:
        parser.exit(1, 'Installation stopped: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
