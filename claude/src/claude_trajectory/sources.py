"""Exact, read-only Claude Code transcript paths. Identity is checked, never guessed."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat

ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}')
PROJECT = re.compile(r'[A-Za-z0-9._-]{1,255}')
HEADER_LIMIT = 1024 * 1024
HEADER_RECORDS = 256


class SourcePending(Exception):
    """The transcript exists only as a reserved path or has no identity record yet."""


def claude_home():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')


def default_projects():
    return claude_home() / 'projects'


def projects_root(path):
    """Claude Code may create the projects directory after the first SessionStart."""
    path = Path(path).expanduser().resolve(strict=False)
    if path.exists() and not path.is_dir():
        raise ValueError('Projects path is not a directory')
    return path


def check_id(value, what='session'):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('An exact ' + what + ' ID is required')
    return value


def logical_path(path, projects, session):
    """A main transcript is exactly <projects>/<encoded cwd>/<session>.jsonl."""
    check_id(session)
    path, projects = Path(path), Path(projects)
    if (not path.is_absolute() or path.resolve(strict=False) != path or path.parent.parent != projects
            or not PROJECT.fullmatch(path.parent.name) or path.parent.name in ('.', '..')
            or path.name != session + '.jsonl'):
        raise ValueError('Transcript path is not the exact registered session file')
    return path


def subagent_path(path, projects, session, agent):
    """A linked subagent transcript is <session dir>/subagents/agent-<agent>.jsonl."""
    check_id(agent, 'agent')
    path = Path(path)
    main = logical_path(path.parent.parent.parent / (session + '.jsonl'), projects, session)
    if path != main.parent / session / 'subagents' / ('agent-' + agent + '.jsonl') or path.resolve(strict=False) != path:
        raise ValueError('Subagent path is outside its parent session')
    return path


def agent_of(path):
    match = re.fullmatch(r'agent-([a-zA-Z0-9][a-zA-Z0-9_-]{0,100})\.jsonl', Path(path).name)
    return match[1] if match and Path(path).parent.name == 'subagents' else None


def open_regular(path, mode=os.O_RDONLY):
    # Directory handles prevent parent symlink swaps, not only leaf symlinks.
    path = Path(path)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(path.name, mode | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(leaf).st_mode):
                raise ValueError('Source is not a regular file')
            return os.fdopen(leaf, 'rb')
        except Exception:
            os.close(leaf)
            raise
    finally:
        os.close(fd)


def locate(path):
    with open_regular(path) as stream:
        return Path(path), os.fstat(stream.fileno())


def identity(info):
    # A storage hint only. Record proofs, not device/inode, authorize reuse after a change.
    return f'{info.st_dev}:{info.st_ino}'


def read_meta(path):
    """Bounded read of a subagent .meta.json; a local format, not an official API."""
    with open_regular(path) as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('Subagent metadata too large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid subagent metadata')
    return value


RELEASE_LIMIT = 4096
RELEASE_REASON = re.compile(r'[a-z][a-z_-]{0,31}')


def read_release(path):
    """Bounded read of the desktop app's <session>.desktop-released.json beside a missing
    main transcript. A local format observed in 2.1.309, not an official API: any
    unexpected shape is ignored rather than guessed. Returns {reason, released_at} or None."""
    path = Path(path)
    if agent_of(path) or path.suffix != '.jsonl':
        return None
    try:
        with open_regular(path.with_name(path.stem + '.desktop-released.json')) as stream:
            raw = stream.read(RELEASE_LIMIT + 1)
        if len(raw) > RELEASE_LIMIT:
            return None
        value = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get('v') != 1:
        return None
    reason, stamp = value.get('reason'), value.get('releasedAt')
    if not isinstance(reason, str) or not RELEASE_REASON.fullmatch(reason) or not isinstance(stamp, str) or len(stamp) > 40:
        return None
    try:
        released = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError:
        return None
    if released.tzinfo is None:
        return None
    return {'reason': reason, 'released_at': released.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}


class Source:
    def __init__(self, path, session, agent=None):
        self.session, self.agent = session, agent
        self.physical = Path(path)
        self.stream = open_regular(self.physical)
        try:
            self.info = os.fstat(self.stream.fileno())
            self.identity = identity(self.info)
            self.validate()
        except Exception:
            self.close()
            raise

    def validate(self):
        # Claude transcripts have no header. The first records that carry an owner
        # must belong to this exact session (and subagent).
        self.stream.seek(0)
        session_seen = agent_seen = False
        consumed = 0
        for _ in range(HEADER_RECORDS):
            raw = self.stream.readline(HEADER_LIMIT - consumed + 1)
            consumed += len(raw)
            if not raw.endswith(b'\n'):
                break
            try:
                record = json.loads(raw)
            except ValueError:
                raise ValueError('Transcript starts with an invalid record') from None
            if not isinstance(record, dict):
                raise ValueError('Transcript starts with an invalid record')
            if not session_seen and 'sessionId' in record:
                if record['sessionId'] != self.session:
                    raise ValueError('Transcript belongs to another session')
                session_seen = True
            if self.agent and not agent_seen and 'agentId' in record:
                if record['agentId'] != self.agent:
                    raise ValueError('Transcript belongs to another subagent')
                agent_seen = True
            if session_seen and (agent_seen or not self.agent):
                self.stream.seek(0)
                return
            if consumed >= HEADER_LIMIT:
                break
        raise SourcePending('Transcript has no complete identity record yet')

    def close(self):
        if getattr(self, 'stream', None) is not None:
            self.stream.close()
            self.stream = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
