"""Opt-in, reversible user-level integration. Never changes Codex config or trust."""
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

BEGIN = '<!-- CODEX-TRAJECTORY:BEGIN -->'
END = '<!-- CODEX-TRAJECTORY:END -->'
FILES = ('events.py', 'server.py', 'broker.py', 'launcher.py', 'index.html', 'install.py')


def atomic(path, data):
    if path.is_symlink():
        raise ValueError('Refusing symlink: ' + str(path))
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.trajectory-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def strip_rule(text):
    if BEGIN not in text and END not in text:
        return text
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(END) < text.index(BEGIN):
        raise ValueError('Ambiguous trajectory rule markers; review manually')
    start, end = text.index(BEGIN), text.index(END) + len(END)
    return text[:start] + text[end:]


def configure(home, dry_run=False, uninstall=False):
    home = Path(home).expanduser().absolute()
    if home.is_symlink():
        raise ValueError('Refusing symlinked Codex home')
    paths = [home/'hooks.json', home/'AGENTS.md']
    if any(p.is_symlink() for p in paths):
        raise ValueError('Refusing symlinked hook or rule file')
    originals = {p: p.read_bytes() if p.exists() else None for p in paths}
    hooks = json.loads(originals[paths[0]] or b'{}')
    if not isinstance(hooks, dict) or not isinstance(hooks.get('hooks', {}), dict):
        raise ValueError('Invalid hooks.json shape')
    rules = (originals[paths[1]] or b'').decode('utf-8')
    base_rules = strip_rule(rules)
    target = home/'codex-trajectory'
    for directory in (target, target/'app', target/'backups'):
        if directory.exists() or directory.is_symlink():
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('Installation directories must be private, owned by you, and not symlinks')
    wrapper = target/'codex-trajectory'
    command = shlex.join([sys.executable, str(wrapper), '--hook'])
    manifest_path = target/'installation.json'
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    owned = {command, previous.get('hook_command', '')}
    updated = copy.deepcopy(hooks)
    groups = updated.setdefault('hooks', {}).get('UserPromptSubmit', [])
    if not isinstance(groups, list):
        raise ValueError('Invalid UserPromptSubmit hooks')
    retained = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
            raise ValueError('Invalid hook matcher group')
        other = [h for h in group['hooks'] if not (isinstance(h, dict) and h.get('command') in owned)]
        if other == group['hooks']:
            retained.append(group)
        elif other:
            retained.append({**group, 'hooks': other})
    if not uninstall:
        retained.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 10,
                                    'statusMessage': 'codex-trajectory'}]})
    updated['hooks']['UserPromptSubmit'] = retained
    rule = (BEGIN + '\n## codex-trajectory\n\n'
            'At the end of each final reply, append one `[Trajectory](verified URL)` link when the '
            'codex-trajectory hook supplies it for this exact chat. If needed, verify the current link with `'
            + shlex.join([sys.executable, str(wrapper)]) + ' --url --thread-id "$CODEX_THREAD_ID"`. '
            'Never guess a chat or URL, expose the link in public artifacts, open tabs automatically, '
            'or interrupt normal work if the observer is unavailable.\n' + END)
    if uninstall:
        new_rules = base_rules
        # Restore byte-identical pre-install prose when nobody edited around it.
        if previous.get('installed_rules') == rules:
            new_rules = previous.get('original_rules', base_rules)
    elif BEGIN in rules:
        new_rules = rules[:rules.index(BEGIN)] + rule + rules[rules.index(END)+len(END):]
    else:
        new_rules = rules + ('\n\n' if rules else '') + rule + '\n'
    result = {'action': 'uninstall' if uninstall else 'install', 'dry_run': dry_run,
              'files': [str(p) for p in paths], 'runtime': str(target),
              'hook_trust': 'Review the new hook in Codex /hooks; trust is never changed automatically.'}
    if dry_run:
        return result
    home.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError('Refusing symlinked installation')
    target.mkdir(mode=0o700, exist_ok=True)
    for part in ('app', 'backups'):
        p = target/part
        if p.is_symlink():
            raise ValueError('Refusing symlinked installation directory')
        p.mkdir(mode=0o700, exist_ok=True)
    lock = target/'install.lock'
    with os.fdopen(os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any((p.read_bytes() if p.exists() else None) != originals[p] for p in paths):
            raise ValueError('Configuration changed during install; retry after review')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        for p, data in originals.items():
            if data is not None:
                atomic(target/'backups'/(stamp+'-'+p.name), data)
        if not uninstall:
            for name in FILES:
                atomic(target/'app'/name, Path(__file__).with_name(name).read_bytes())
            entry = ('import os, sys\nfrom pathlib import Path\n'
                     'home = Path(__file__).resolve().parent.parent\n'
                     'os.environ["CODEX_HOME"] = str(home)\n'
                     'sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))\n'
                     'from launcher import main\n'
                     'sys.argv.extend(["--state-dir", str(home / "codex-trajectory" / "runtime")])\nmain()\n')
            atomic(wrapper, entry.encode())
        atomic(paths[0], (json.dumps(updated, indent=2)+'\n').encode())
        atomic(paths[1], new_rules.encode())
        atomic(manifest_path, json.dumps({'hook_command': command, 'installed_rules': new_rules,
               'original_rules': previous.get('original_rules', rules), 'uninstalled': uninstall}).encode())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(configure(args.codex_home, args.dry_run, args.uninstall), indent=2))
    except (ValueError, OSError, UnicodeError) as exc:
        parser.exit(1, 'Installation stopped: '+str(exc)+'\n')


if __name__ == '__main__':
    main()
