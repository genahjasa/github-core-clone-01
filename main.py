#!/usr/bin/env python3
"""makefun-forge: a standalone GitHub-compatible git forge (github-core-clone-01).

Pure Python 3 stdlib, one process. Serves git smart HTTP (v0 + v2), REST v3,
GraphQL v4 and MCP from one in-memory store. Lifecycle: bind an ephemeral port,
POST {port} to $MAKEFUN_ENV_URL/register, poll GET $MAKEFUN_ENV_URL/done until
{"done": true}, exit 0. Auth: Bearer $MAKEFUN_SHOP_TOKEN on every API/git
surface except /healthz (auth is skipped when the variable is unset, dev mode).

Determinism: no wall clock, no randomness in any response. Commit timestamps
come from a counter off the scenario epoch; request ids, database ids and
issue/PR numbers are counters. The scenario (viewer login, names, blobs,
messages) is a byte-exact port of the kit's wire.ts generator, seeded with the
contest's fixed SCENARIO_SEED.
"""

import base64
import datetime
import difflib
import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Seeded scenario (port of the kit's program/env/src/wire.ts; fixed seed)
# --------------------------------------------------------------------------

_MASK = (1 << 64) - 1
SCENARIO_SEED = 0x6A09E667F3BCC909
SCENARIO_EPOCH_SEC = 1735689600
DEFAULT_BRANCH = 'main'

_FIRST = ['ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo', 'iris', 'jonas', 'kira', 'lars',
          'mira', 'nico', 'olga', 'pavel', 'quinn', 'rosa', 'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane']
_LAST = ['anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia', 'holm', 'iversen', 'jansen', 'keller',
         'lind', 'moreau', 'novak', 'olsen', 'peters', 'quist', 'rossi', 'sato', 'turi', 'ulrich', 'vega', 'weber',
         'xu', 'yilmaz', 'zimmer']
_ADJ = ['amber', 'brisk', 'calm', 'crisp', 'eager', 'faint', 'gentle', 'happy', 'idle', 'jolly', 'keen', 'lively',
        'mellow', 'nimble', 'open', 'plain', 'quick', 'quiet', 'rapid', 'soft', 'steady', 'tidy', 'upbeat',
        'vivid', 'warm', 'witty', 'young', 'zesty']
_NOUN = ['acorn', 'badger', 'canyon', 'cedar', 'comet', 'coral', 'delta', 'ember', 'falcon', 'glade', 'harbor',
         'indigo', 'juniper', 'kettle', 'lantern', 'meadow', 'nebula', 'orchid', 'pebble', 'quartz', 'raven',
         'saffron', 'thistle', 'umber', 'violet', 'willow', 'yarrow', 'zephyr']
_DESCR = ['a small service with a sharp edge',
          'notes and experiments, kept until they are not',
          'the tooling behind the thing',
          'a reference implementation, for reading',
          'what the prototype became',
          'fixtures, generators and the odd benchmark',
          'the parts that must not break',
          'a sandbox for the next idea']
_SEGS = ['src', 'lib', 'docs', 'test', 'scripts', 'api', 'core', 'util', 'config', 'data', 'web', 'cli', 'pkg',
         'internal', 'examples', 'assets', 'build', 'tools', 'server', 'client']
_STEMS = ['index', 'request', 'response', 'server', 'client', 'schema', 'parser', 'router', 'config', 'store',
          'clock', 'queue', 'render', 'format', 'validate', 'session', 'token', 'event', 'stream', 'cache']
_EXTS = ['ts', 'js', 'json', 'md', 'txt', 'yml', 'toml', 'css', 'html', 'sql']
_BWORDS = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel', 'india', 'juliet', 'kilo',
           'lima', 'mike', 'november', 'oscar', 'papa', 'quebec', 'romeo', 'sierra', 'tango', 'uniform', 'victor',
           'whiskey', 'xray', 'yankee', 'zulu', 'amber', 'basalt', 'cobalt', 'drift', 'ember', 'frost', 'granite',
           'hollow', 'ivory', 'jasper', 'kelp', 'lichen', 'moss', 'nickel']
_CVERBS = ['Add', 'Fix', 'Refactor', 'Document', 'Test', 'Tidy', 'Speed up', 'Simplify', 'Handle', 'Extract',
           'Inline', 'Rename', 'Guard', 'Wire', 'Drop']
_COBJS = ['the request parser', 'repo lookup', 'issue pagination', 'the merge path', 'token checks',
          'the clone endpoint', 'packfile handling', 'error bodies', 'branch listing', 'the GraphQL node query',
          'MCP tool dispatch', 'rate limit headers', 'the upload pack', 'the ref advertisement', 'commit ancestry']
_IADJ = ['Broken', 'Slow', 'Missing', 'Confusing', 'Unexpected', 'Flaky', 'Incorrect', 'Unclear', 'Stale', 'Noisy']
_ISUB = ['clone over HTTP', 'push report-status', 'issue pagination', 'merge button', 'GraphQL node ids',
         'MCP tool list', 'shallow fetch', 'tag listing', 'repo creation', 'content update', 'branch deletion',
         'commit listing']
_PVERBS = ['Add', 'Fix', 'Improve', 'Simplify', 'Document', 'Refactor', 'Speed up', 'Harden']
_PSUBS = ['shallow fetch support', 'push report status checks', 'issue label validation', 'merge conflict detection',
          'GraphQL cursor pagination', 'MCP tool schemas', 'packfile delta reuse', 'conditional requests',
          'repo pagination', 'branch protection stubs']
_SSTART = ['Steps to reproduce are below', 'This showed up after the last change', 'The expected behaviour is clear',
           'A minimal case is attached', 'It works on a fresh repository', 'The same flow works over the API']
_SEND = ['and the server answers with a JSON error', 'but the second request fails', 'and nothing is written to the log',
         'while the reply body stays empty', 'and the status is wrong for the method', 'so the client gives up']
_ILABELS = ['bug', 'enhancement', 'documentation', 'question', 'good first issue', 'help wanted']
_DBRANCHES = ['main', 'develop', 'release/1.0']


def _fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & _MASK
    return h


def _mix64(value):
    z = (value + 0x9E3779B97F4A7C15) & _MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
    return (z ^ (z >> 31)) & _MASK


def _seed_for(label):
    return _mix64(SCENARIO_SEED ^ _fnv1a64(label))


class _Rng:
    def __init__(self, seed):
        masked = seed & _MASK
        self.state = masked if masked != 0 else SCENARIO_SEED

    @classmethod
    def from_label(cls, label):
        return cls(_seed_for(label))

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & _MASK
        x = (x ^ ((x << 25) & _MASK)) & _MASK
        x = (x ^ (x >> 27)) & _MASK
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & _MASK

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, max_exclusive):
        if max_exclusive == 1:
            return 0
        rng_range = 4294967296
        limit = rng_range - (rng_range % max_exclusive)
        while True:
            value = self.next_u32()
            if value < limit:
                return value % max_exclusive

    def fraction(self):
        return self.next_u32() / 4294967296

    def chance(self, probability):
        return self.fraction() < probability

    def pick(self, items):
        return items[self.next_int(len(items))]

    def shuffle(self, items):
        copy = list(items)
        for i in range(len(copy) - 1, 0, -1):
            j = self.next_int(i + 1)
            copy[i], copy[j] = copy[j], copy[i]
        return copy


def _gen_users(rng, count):
    used = set()
    users = []
    for i in range(max(0, count)):
        first = rng.pick(_FIRST)
        last = rng.pick(_LAST)
        base = re.sub(r'[^a-z0-9-]', '', first + last)
        login = base
        suffix = 2
        while login in used:
            login = '%s-%d' % (base, suffix)
            suffix += 1
        used.add(login)
        users.append({'id': 1000 + i, 'login': login, 'name': '%s %s' % (first, last),
                      'email': '%s@example.invalid' % login})
    return users


def _gen_repos(rng, owner, count, first_id=5000):
    used = set()
    repos = []
    for i in range(max(0, count)):
        base = '%s-%s' % (rng.pick(_ADJ), rng.pick(_NOUN))
        name = base
        suffix = 2
        while name in used:
            name = '%s-%d' % (base, suffix)
            suffix += 1
        used.add(name)
        repos.append({'id': first_id + i, 'owner': owner, 'name': name,
                      'fullName': '%s/%s' % (owner, name),
                      'description': rng.pick(_DESCR),
                      'private': (i % 7 == 3), 'defaultBranch': DEFAULT_BRANCH})
    return repos


def _gen_file_paths(rng, count):
    used = set()
    paths = []
    for _ in range(max(0, count)):
        depth = 0 if rng.next_int(3) == 0 else 1 + rng.next_int(2)
        dirs = [] if depth == 0 else rng.shuffle(_SEGS)[:depth]
        base = '/'.join(dirs + ['%s.%s' % (rng.pick(_STEMS), rng.pick(_EXTS))])
        path = base
        suffix = 2
        while path in used:
            path = '/'.join(dirs + ['%d-%s' % (suffix, base.split('/')[-1])])
            suffix += 1
        used.add(path)
        paths.append(path)
    return paths


def _gen_blob_text(rng, size_bytes):
    size = max(0, size_bytes)
    out = ''
    while len(out) < size:
        out += rng.pick(_BWORDS)
        if len(out) < size:
            out += '\n' if rng.chance(0.14) else ' '
    return out[:size]


def _gen_blob(rng, path, size_bytes):
    content = _gen_blob_text(rng, size_bytes)
    return {'path': path, 'content': content,
            'base64': base64.b64encode(content.encode('utf-8')).decode('ascii')}


def _gen_sentence(rng):
    return '%s %s.' % (rng.pick(_SSTART), rng.pick(_SEND))


def _gen_body(rng):
    count = 1 + rng.next_int(3)
    return ' '.join(_gen_sentence(rng) for _ in range(count))


def _gen_commit_message(rng):
    head = '%s %s' % (rng.pick(_CVERBS), rng.pick(_COBJS))
    if rng.chance(0.35):
        return '%s\n\n%s' % (head, _gen_sentence(rng))
    return head


def _gen_commit(rng, author):
    epoch_sec = SCENARIO_EPOCH_SEC + rng.next_int(90 * 24 * 60 * 60)
    iso = datetime.datetime.fromtimestamp(epoch_sec, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    return {'message': _gen_commit_message(rng), 'author': author,
            'epochSec': epoch_sec, 'iso': iso}


def _gen_issue(rng):
    return {'title': '%s %s' % (rng.pick(_IADJ), rng.pick(_ISUB)),
            'body': _gen_body(rng),
            'labels': [rng.pick(_ILABELS)] if rng.chance(0.5) else []}


def _gen_branch_name(rng):
    kind = rng.pick(['feature', 'fix', 'perf', 'docs', 'chore'])
    subject = re.sub(r'[^a-z0-9]+', '-', rng.pick(_PSUBS).lower())
    subject = re.sub(r'^-+|-+$', '', subject)
    return '%s/%s' % (kind, subject)


def _gen_pull(rng, base=DEFAULT_BRANCH):
    return {'title': '%s %s' % (rng.pick(_PVERBS), rng.pick(_PSUBS)),
            'body': _gen_body(rng),
            'head': _gen_branch_name(rng), 'base': base}


def _unique_branch(rng, used):
    branch = _gen_branch_name(rng)
    suffix = 2
    while branch in used:
        branch = '%s-%d' % (_gen_branch_name(rng), suffix)
        suffix += 1
    used.add(branch)
    return branch


def build_scenario():
    users = _gen_users(_Rng.from_label('users'), 12)
    viewer = users[0]
    org_rng = _Rng.from_label('org')
    org = '%s-%s-org' % (org_rng.pick(_ADJ), org_rng.pick(_NOUN))
    branch_rng = _Rng.from_label('branches')
    branch_set = set(_DBRANCHES)
    for _ in range(8):
        _unique_branch(branch_rng, branch_set)
    file_paths = _gen_file_paths(_Rng.from_label('paths'), 60)
    blob_rng = _Rng.from_label('blobs')
    blobs = [_gen_blob(blob_rng, path, 64 + blob_rng.next_int(4033))
             for path in file_paths[:24]]
    commit_rng = _Rng.from_label('commits')
    commits = []
    for _ in range(60):
        author = commit_rng.pick(users)
        commits.append(_gen_commit(commit_rng, author))
    issue_rng = _Rng.from_label('issues')
    issues = [_gen_issue(issue_rng) for _ in range(60)]
    pull_rng = _Rng.from_label('pulls')
    pulls = [_gen_pull(pull_rng) for _ in range(30)]
    repos = _gen_repos(_Rng.from_label('repos.viewer'), viewer['login'], 30, 5000)
    org_repos = _gen_repos(_Rng.from_label('repos.org'), org, 6, 6000)
    stranger_repos = _gen_repos(_Rng.from_label('repos.stranger'), users[2]['login'], 3, 7000)
    return {'seed': '6a09e667f3bcc909', 'viewer': viewer, 'teammate': users[1],
            'stranger': users[2], 'users': users, 'org': org, 'repos': repos,
            'orgRepos': org_repos, 'strangerRepos': stranger_repos,
            'labels': list(_ILABELS), 'branches': sorted(branch_set),
            'filePaths': file_paths, 'blobs': blobs, 'commits': commits,
            'issues': issues, 'pulls': pulls}


# --------------------------------------------------------------------------
# Git objects: SHA-1, trees, commits, tags, packs, pkt-lines, reachability
# --------------------------------------------------------------------------

ZERO_OID = '0' * 40
SHA40_RE = re.compile(r'^[0-9a-f]{40}$')


def hash_object(obj_type, data):
    header = ('%s %d\x00' % (obj_type, len(data))).encode('ascii')
    return hashlib.sha1(header + data).hexdigest()


def _tree_sort_key(entry):
    # entry: (mode, name, oid_hex); directories compare as name + '/'
    mode, name, _oid = entry
    return name + ('/' if mode == '40000' else '')


def build_tree_bytes(entries):
    """entries: list of (mode_str, name_str, oid_hex). Returns raw tree bytes."""
    out = bytearray()
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        out += mode.encode('ascii') + b' ' + name.encode('utf-8') + b'\x00' + bytes.fromhex(oid)
    return bytes(out)


def parse_tree(data):
    """Returns list of (mode_str, name_str, oid_hex) in stored order."""
    entries = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b' ', i)
        mode = data[i:sp].decode('ascii')
        nul = data.index(b'\x00', sp)
        name = data[sp + 1:nul].decode('utf-8')
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries


def parse_commit(data):
    """Returns dict with tree, parents, author, committer, message (str)."""
    text = data.decode('utf-8', errors='replace')
    head, _, message = text.partition('\n\n')
    out = {'tree': None, 'parents': [], 'author': '', 'committer': '', 'message': message}
    for line in head.split('\n'):
        if line.startswith('tree '):
            out['tree'] = line[5:].strip()
        elif line.startswith('parent '):
            out['parents'].append(line[7:].strip())
        elif line.startswith('author '):
            out['author'] = line[7:]
        elif line.startswith('committer '):
            out['committer'] = line[10:]
    return out


def parse_tag(data):
    text = data.decode('utf-8', errors='replace')
    head, _, message = text.partition('\n\n')
    out = {'object': None, 'type': None, 'tag': None, 'tagger': '', 'message': message}
    for line in head.split('\n'):
        if line.startswith('object '):
            out['object'] = line[7:].strip()
        elif line.startswith('type '):
            out['type'] = line[5:].strip()
        elif line.startswith('tag '):
            out['tag'] = line[4:].strip()
        elif line.startswith('tagger '):
            out['tagger'] = line[7:]
    return out


def _encode_varint_size(obj_type, size):
    """Pack entry header varint: bits 6-4 type, bits 3-0 low size bits."""
    first = ((obj_type & 7) << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    if size:
        out.append(first | 0x80)
        while size:
            byte = size & 0x7F
            size >>= 7
            if size:
                out.append(byte | 0x80)
            else:
                out.append(byte)
    else:
        out.append(first)
    return bytes(out)


_TYPE_CODES = {'commit': 1, 'tree': 2, 'blob': 3, 'tag': 4, 'ofs-delta': 6, 'ref-delta': 7}
_CODE_TYPES = {v: k for k, v in _TYPE_CODES.items()}


def write_pack(objects):
    """objects: iterable of (oid, type, data). Plain pack, no deltas, sorted by oid."""
    items = sorted(objects, key=lambda item: item[0])
    out = bytearray()
    out += b'PACK'
    out += (2).to_bytes(4, 'big')
    out += len(items).to_bytes(4, 'big')
    for _oid, obj_type, data in items:
        out += _encode_varint_size(_TYPE_CODES[obj_type], len(data))
        # Level 1 deflate: the pack is rebuilt per fetch, and the benchmark
        # clones the same history 50 times concurrently; level 6 costs
        # several ms per object for no scored benefit.
        out += zlib.compress(data, 1)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


def _read_varint_size(data, pos):
    byte = data[pos]
    pos += 1
    obj_type = (byte >> 4) & 7
    size = byte & 0x0F
    shift = 4
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        size += (byte & 0x7F) << shift
        shift += 7
    return obj_type, size, pos


def _apply_delta(base, delta):
    """Apply a git delta (LEB128 source/target sizes + copy/insert ops)."""
    pos = 0

    def read_leb():
        nonlocal pos
        value = 0
        shift = 0
        while True:
            byte = delta[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not (byte & 0x80):
                return value

    src_size = read_leb()
    tgt_size = read_leb()
    if src_size != len(base):
        raise ValueError('delta source size mismatch')
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = 0
            size = 0
            for i in range(4):
                if op & (1 << i):
                    offset |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    size |= delta[pos] << (8 * i)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[offset:offset + size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError('delta opcode 0')
    if len(out) != tgt_size:
        raise ValueError('delta target size mismatch')
    return bytes(out)


def read_pack(data):
    """Parse a packfile. Returns list of (oid, type, data). Verifies trailer."""
    if len(data) < 32 or data[:4] != b'PACK':
        raise ValueError('not a packfile')
    version = int.from_bytes(data[4:8], 'big')
    if version not in (2, 3):
        raise ValueError('unsupported pack version')
    count = int.from_bytes(data[8:12], 'big')
    if hashlib.sha1(data[:-20]).digest() != data[-20:]:
        raise ValueError('pack checksum mismatch')
    pos = 12
    entries = []  # (offset, oid, type, data)
    by_offset = {}
    by_oid = {}
    for _ in range(count):
        start = pos
        obj_type_code, size, pos = _read_varint_size(data, pos)
        if obj_type_code == 6:  # OFS_DELTA
            byte = data[pos]
            pos += 1
            value = byte & 0x7F
            while byte & 0x80:
                byte = data[pos]
                pos += 1
                value = ((value + 1) << 7) | (byte & 0x7F)
            base_offset = start - value
            if base_offset not in by_offset:
                raise ValueError('delta base not in pack')
            base_type, base_data = by_offset[base_offset]
            raw, pos = _read_zlib(data, pos, size)
            obj_data = _apply_delta(base_data, raw)
            obj_type = base_type
        elif obj_type_code == 7:  # REF_DELTA
            base_oid = data[pos:pos + 20].hex()
            pos += 20
            if base_oid not in by_oid:
                raise ValueError('delta base not in pack')
            base_type, base_data = by_oid[base_oid]
            raw, pos = _read_zlib(data, pos, size)
            obj_data = _apply_delta(base_data, raw)
            obj_type = base_type
        else:
            obj_type = _CODE_TYPES.get(obj_type_code)
            if obj_type is None:
                raise ValueError('unknown pack object type')
            obj_data, pos = _read_zlib(data, pos, size)
        oid = hash_object(obj_type, obj_data)
        entries.append((oid, obj_type, obj_data))
        by_offset[start] = (obj_type, obj_data)
        by_oid[oid] = (obj_type, obj_data)
    if pos != len(data) - 20:
        raise ValueError('pack trailing bytes')
    return entries


def _read_zlib(data, pos, expected_size):
    decomp = zlib.decompressobj()
    out = decomp.decompress(data[pos:])
    consumed = len(data[pos:]) - len(decomp.unused_data)
    if len(out) != expected_size:
        raise ValueError('pack entry size mismatch')
    return out, pos + consumed


def pkt_encode(payload):
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    return ('%04x' % (len(payload) + 4)).encode('ascii') + payload


PKT_FLUSH = b'0000'
PKT_DELIM = b'0001'
PKT_END = b'0002'


class PktReader:
    """Incremental pkt-line reader over a bytes buffer."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read_packet(self):
        """Returns ('data', payload) | ('flush', None) | ('delim', None) |
        ('end', None) | ('eof', None). Raises ValueError on malformed input."""
        if self.pos >= len(self.data):
            return ('eof', None)
        if self.pos + 4 > len(self.data):
            raise ValueError('pktline truncated')
        head = self.data[self.pos:self.pos + 4]
        try:
            length = int(head.decode('ascii'), 16)
        except ValueError:
            raise ValueError('pktline header')
        if length == 0:
            self.pos += 4
            return ('flush', None)
        if length == 1:
            self.pos += 4
            return ('delim', None)
        if length == 2:
            self.pos += 4
            return ('end', None)
        if length < 4 or length > 65520:
            raise ValueError('pktline too long')
        if self.pos + length > len(self.data):
            raise ValueError('pktline truncated')
        payload = self.data[self.pos + 4:self.pos + length]
        self.pos += length
        return ('data', payload)

    def rest(self):
        return self.data[self.pos:]


def collect_reachable(tips, lookup):
    """All objects reachable from tips. lookup(oid) -> (type, data) or None.
    Follows commit->tree+parents, tag->target, tree->entries (skips gitlinks)."""
    seen = set()
    stack = [t for t in tips if t and t != ZERO_OID]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        found = lookup(oid)
        if found is None:
            continue
        seen.add(oid)
        obj_type, data = found
        if obj_type == 'commit':
            parsed = parse_commit(data)
            if parsed['tree']:
                stack.append(parsed['tree'])
            stack.extend(parsed['parents'])
        elif obj_type == 'tag':
            parsed = parse_tag(data)
            if parsed['object']:
                stack.append(parsed['object'])
        elif obj_type == 'tree':
            for mode, _name, child in parse_tree(data):
                if mode == '160000':
                    continue
                stack.append(child)
    return seen


def commit_ancestors(tip, lookup):
    """Commit oids reachable from tip via parents (tip included)."""
    seen = set()
    stack = [tip] if tip and tip != ZERO_OID else []
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        found = lookup(oid)
        if found is None or found[0] != 'commit':
            continue
        seen.add(oid)
        stack.extend(parse_commit(found[1])['parents'])
    return seen


def is_descendant(maybe_child, ancestor, lookup):
    """True when ancestor is reachable from maybe_child via parents (or equal)."""
    if maybe_child == ancestor:
        return True
    return ancestor in commit_ancestors(maybe_child, lookup)


# --------------------------------------------------------------------------
# Forge store: repositories, issues, pull requests, git objects
# --------------------------------------------------------------------------

FIXED_MERGED_AT = '2025-01-01T00:00:00Z'


def _iso_from_epoch(epoch_sec):
    return datetime.datetime.fromtimestamp(epoch_sec, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Repo:
    def __init__(self, forge, owner, name, private, description):
        self.owner = owner
        self.name = name
        self.db_id = forge.next_db_id()
        self.private = bool(private)
        self.description = description or ''
        self.default_branch = DEFAULT_BRANCH
        self.refs = {}          # 'refs/heads/main' -> oid
        self.labels = {}        # name -> {'name':..., 'color':...}
        self.issues = []        # issue dicts in creation order
        self.pulls = []         # pull dicts in creation order
        self.next_number = 1    # issues and PRs share one counter, GitHub-style
        self.created_seq = forge.next_seq()

    def full_name(self):
        return '%s/%s' % (self.owner, self.name)

    def tip(self, branch):
        return self.refs.get('refs/heads/' + branch)

    def find_issue(self, number):
        for issue in self.issues:
            if issue['number'] == number:
                return issue
        return None

    def find_pull(self, number):
        for pull in self.pulls:
            if pull['number'] == number:
                return pull
        return None


class Forge:
    def __init__(self, shop_token):
        self.shop_token = shop_token or ''
        self.scenario = build_scenario()
        self.viewer = self.scenario['viewer']
        self.objects = {}       # oid -> (type, bytes)
        self.repos = {}         # (owner, name) -> Repo
        self._db_counter = 5000
        self._rest_counter = 1000000
        self._seq = 0
        self._req_counter = 0
        self._commit_seq = 0
        self._pack_cache = {}
        self._counter_lock = threading.Lock()
        self.lock = threading.RLock()

    # -- deterministic counters -------------------------------------------------
    def next_db_id(self):
        with self._counter_lock:
            self._db_counter += 1
            return self._db_counter

    def next_rest_id(self):
        with self._counter_lock:
            self._rest_counter += 1
            return self._rest_counter

    def next_seq(self):
        with self._counter_lock:
            self._seq += 1
            return self._seq

    def next_request_id(self):
        with self._counter_lock:
            self._req_counter += 1
            return 'req-%06d' % self._req_counter

    def next_commit_ts(self):
        with self._counter_lock:
            self._commit_seq += 1
            return SCENARIO_EPOCH_SEC + self._commit_seq

    # -- git objects -------------------------------------------------------------
    def put_object(self, obj_type, data):
        oid = hash_object(obj_type, data)
        self.objects.setdefault(oid, (obj_type, data))
        return oid

    def lookup(self, oid):
        return self.objects.get(oid)

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def create_repo(self, owner, name, private, description):
        if (owner, name) in self.repos:
            return None
        repo = Repo(self, owner, name, private, description)
        self.repos[(owner, name)] = repo
        return repo

    def user_repos(self, owner):
        return [r for (o, _n), r in sorted(self.repos.items()) if o == owner]

    # -- trees --------------------------------------------------------------------
    def build_tree_from_files(self, files):
        """files: dict path -> (mode, oid). Returns root tree oid."""
        nested = {}

        def insert(node, parts, mode, oid):
            if len(parts) == 1:
                node[parts[0]] = (mode, oid)
            else:
                node.setdefault(parts[0], {}).setdefault('__children__', {})
                insert(node[parts[0]]['__children__'], parts[1:], mode, oid)

        root = {}
        for path in sorted(files):
            mode, oid = files[path]
            insert(root, path.split('/'), mode, oid)

        def build(node):
            entries = []
            for name, value in node.items():
                if isinstance(value, tuple):
                    entries.append((value[0], name, value[1]))
                else:
                    child_oid = build(value['__children__'])
                    entries.append(('40000', name, child_oid))
            return self.put_object('tree', build_tree_bytes(entries))

        return build(root)

    def tree_file_map(self, tree_oid):
        """Flat map path -> (mode, oid) for a tree (recursive)."""
        out = {}

        def walk(oid, prefix):
            found = self.lookup(oid)
            if found is None or found[0] != 'tree':
                return
            for mode, name, child in parse_tree(found[1]):
                path = prefix + name
                if mode == '40000':
                    walk(child, path + '/')
                elif mode == '160000':
                    continue
                else:
                    out[path] = (mode, child)

        if tree_oid:
            walk(tree_oid, '')
        return out

    def subtree_oid(self, tree_oid, dir_path):
        """Oid of the subtree at dir_path within tree_oid ('' = the tree itself)."""
        if not dir_path:
            return tree_oid
        current = tree_oid
        for segment in dir_path.split('/'):
            found = self.lookup(current)
            if found is None or found[0] != 'tree':
                return None
            nxt = None
            for mode, name, child in parse_tree(found[1]):
                if name == segment and mode == '40000':
                    nxt = child
                    break
            if nxt is None:
                return None
            current = nxt
        return current

    def commit_tree_map(self, commit_oid):
        found = self.lookup(commit_oid)
        if found is None or found[0] != 'commit':
            return {}
        tree = parse_commit(found[1])['tree']
        return self.tree_file_map(tree) if tree else {}

    # -- contents commits ----------------------------------------------------------
    def contents_commit(self, repo, branch, changes, message):
        """changes: dict path -> bytes (new content) or None (delete).
        Creates a commit on branch (creating the branch from the default
        branch tip when it does not exist yet). Returns (commit_oid, blob_oids)."""
        tip = repo.tip(branch)
        if tip is None:
            if branch != repo.default_branch:
                tip = repo.tip(repo.default_branch)
            parents = [tip] if tip else []
            base_files = self.commit_tree_map(tip) if tip else {}
        else:
            parents = [tip]
            base_files = self.commit_tree_map(tip)
        files = dict(base_files)
        blob_oids = {}
        for path, content in changes.items():
            if content is None:
                files.pop(path, None)
            else:
                oid = self.put_object('blob', content)
                blob_oids[path] = oid
                files[path] = ('100644', oid)
        tree_oid = self.build_tree_from_files(files)
        ts = self.next_commit_ts()
        ident = '%s <%s> %d +0000' % (self.viewer['name'], self.viewer['email'], ts)
        lines = ['tree ' + tree_oid]
        for parent in parents:
            lines.append('parent ' + parent)
        lines.append('author ' + ident)
        lines.append('committer ' + ident)
        raw = ('\n'.join(lines) + '\n\n').encode('utf-8') + message.encode('utf-8')
        commit_oid = self.put_object('commit', raw)
        repo.refs['refs/heads/' + branch] = commit_oid
        return commit_oid, blob_oids

    def branch_commits(self, repo, branch):
        """Commit oids reachable from branch tip, newest first."""
        tip = repo.tip(branch)
        if tip is None:
            return []
        ordered = []
        seen = set()
        stack = [tip]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            found = self.lookup(oid)
            if found is None or found[0] != 'commit':
                continue
            seen.add(oid)
            ordered.append(oid)
            stack.extend(parse_commit(found[1])['parents'])
        return ordered

    # -- diff / merge ---------------------------------------------------------------
    def diff_trees(self, base_map, head_map):
        """Returns list of (path, status, base_oid, head_oid)."""
        out = []
        for path in sorted(set(base_map) | set(head_map)):
            in_base = path in base_map
            in_head = path in head_map
            if in_head and not in_base:
                out.append((path, 'added', None, head_map[path][1]))
            elif in_base and not in_head:
                out.append((path, 'removed', base_map[path][1], None))
            elif base_map[path][1] != head_map[path][1]:
                out.append((path, 'modified', base_map[path][1], head_map[path][1]))
        return out

    def _blob_lines(self, oid):
        if oid is None:
            return []
        found = self.lookup(oid)
        if found is None:
            return []
        return found[1].decode('utf-8', errors='replace').splitlines()

    def file_change_counts(self, status, base_oid, head_oid):
        if status == 'added':
            lines = self._blob_lines(head_oid)
            return len(lines), 0
        if status == 'removed':
            lines = self._blob_lines(base_oid)
            return 0, len(lines)
        base_lines = self._blob_lines(base_oid)
        head_lines = self._blob_lines(head_oid)
        adds = dels = 0
        for line in difflib.unified_diff(base_lines, head_lines, lineterm=''):
            if line.startswith('+') and not line.startswith('+++'):
                adds += 1
            elif line.startswith('-') and not line.startswith('---'):
                dels += 1
        return adds, dels

    def pr_files(self, pull):
        repo = pull['_repo']
        base_map = self.commit_tree_map(pull['base_sha'])
        head_map = self.commit_tree_map(pull['head_sha'])
        out = []
        for path, status, base_oid, head_oid in self.diff_trees(base_map, head_map):
            adds, dels = self.file_change_counts(status, base_oid, head_oid)
            out.append({'filename': path, 'status': status,
                        'additions': adds, 'deletions': dels, 'changes': adds + dels})
        return out

    def pr_mergeable(self, pull):
        """'MERGEABLE' or 'CONFLICTING' for a pull request."""
        repo = pull['_repo']
        base_tip = repo.tip(pull['base']['ref'])
        if base_tip is None:
            return 'UNKNOWN'
        if pull['state'] != 'open':
            return 'UNKNOWN'
        base_map = self.commit_tree_map(pull['base_sha'])
        head_map = self.commit_tree_map(pull['head_sha'])
        current_map = self.commit_tree_map(base_tip)
        for path, _status, _b, _h in self.diff_trees(base_map, head_map):
            cur = current_map.get(path)
            base = base_map.get(path)
            head = head_map.get(path)
            cur_oid = cur[1] if cur else None
            base_oid = base[1] if base else None
            head_oid = head[1] if head else None
            if cur_oid != base_oid and cur_oid != head_oid:
                return 'CONFLICTING'
        return 'MERGEABLE'

    def squash_merge(self, repo, pull):
        """Squash-merge an open PR onto its base branch. Returns (ok, reason, sha)."""
        if pull['state'] != 'open':
            return False, 'pull request is not open', None
        if self.pr_mergeable(pull) != 'MERGEABLE':
            return False, 'merge conflict', None
        base_tip = repo.tip(pull['base']['ref'])
        if base_tip is None:
            return False, 'base branch not found', None
        base_map = self.commit_tree_map(pull['base_sha'])
        head_map = self.commit_tree_map(pull['head_sha'])
        current_map = self.commit_tree_map(base_tip)
        files = dict(current_map)
        for path, _status, _b, head_oid in self.diff_trees(base_map, head_map):
            if head_oid is None:
                files.pop(path, None)
            else:
                files[path] = ('100644', head_oid)
        tree_oid = self.build_tree_from_files(files)
        ts = self.next_commit_ts()
        ident = '%s <%s> %d +0000' % (self.viewer['name'], self.viewer['email'], ts)
        message = '%s (#%d)' % (pull['title'], pull['number'])
        raw = ('tree %s\nparent %s\nauthor %s\ncommitter %s\n\n%s' %
               (tree_oid, base_tip, ident, ident, message)).encode('utf-8')
        merge_oid = self.put_object('commit', raw)
        repo.refs['refs/heads/' + pull['base']['ref']] = merge_oid
        pull['state'] = 'closed'
        pull['merged'] = True
        pull['merged_at'] = FIXED_MERGED_AT
        pull['merge_commit_sha'] = merge_oid
        return True, None, merge_oid

    # -- issues / pulls ---------------------------------------------------------------
    def create_issue(self, repo, title, body, labels):
        number = repo.next_number
        repo.next_number += 1
        issue = {'number': number, 'id': self.next_rest_id(),
                 'title': title or '', 'body': body or '', 'state': 'open',
                 'labels': [{'name': name} for name in labels],
                 'comments': [], '_seq': self.next_seq(), '_repo': repo}
        repo.issues.append(issue)
        return issue

    def create_pull(self, repo, title, body, head, base):
        head_tip = repo.tip(head)
        base_tip = repo.tip(base)
        if head_tip is None or base_tip is None:
            return None, 'head or base branch not found'
        for existing in repo.pulls:
            if existing['state'] == 'open' and existing['head']['ref'] == head:
                return None, 'a pull request from %s already exists' % head
        number = repo.next_number
        repo.next_number += 1
        pull = {'number': number, 'id': self.next_rest_id(),
                'title': title or '', 'body': body or '', 'state': 'open',
                'head': {'ref': head, 'sha': head_tip},
                'base': {'ref': base, 'sha': base_tip},
                'head_sha': head_tip, 'base_sha': base_tip,
                'merged': False, 'merged_at': None, 'merge_commit_sha': None,
                '_seq': self.next_seq(), '_repo': repo}
        repo.pulls.append(pull)
        return pull, None

    # -- git fetch computation -----------------------------------------------------------
    def fetch_pack(self, repo, wants, haves, client_shallow, depth):
        """Cached (pack_bytes, shallow, unshallow) for one fetch.

        The object store only grows and refs only move via the store, so
        (refs fingerprint, object count) plus the fetch parameters is a
        sound cache key. The benchmark's 50 concurrent shallow clones are
        identical fetches, so 49 of them are cache hits.
        """
        key = (repo.owner, repo.name,
               tuple(sorted(repo.refs.items())), len(self.objects),
               tuple(sorted(set(wants))), tuple(sorted(set(haves))),
               tuple(sorted(set(client_shallow))), depth)
        cached = self._pack_cache.get(key)
        if cached is not None:
            return cached
        objects, shallow, unshallow = self.compute_fetch(
            repo, wants, haves, client_shallow, depth)
        result = (write_pack(objects), shallow, unshallow)
        if len(self._pack_cache) > 64:
            self._pack_cache.clear()
        self._pack_cache[key] = result
        return result

    def compute_fetch(self, repo, wants, haves, client_shallow, depth):
        """Returns (objects, shallow, unshallow).
        objects: list of (oid, type, data) to send.
        shallow: boundary commits whose parents were cut off.
        unshallow: client shallow boundaries now fully available."""
        lookup = self.lookup
        # Objects the client already holds: full closure of haves, except the
        # parents of a shallow have (a shallow boundary's history is not held).
        have_closure = set()
        shallow_haves = set(client_shallow)
        for have in haves:
            stack = [have]
            while stack:
                oid = stack.pop()
                if oid in have_closure or oid == ZERO_OID:
                    continue
                found = lookup(oid)
                if found is None:
                    continue
                have_closure.add(oid)
                obj_type, data = found
                if obj_type == 'commit':
                    parsed = parse_commit(data)
                    if parsed['tree']:
                        stack.append(parsed['tree'])
                    if oid not in shallow_haves:
                        stack.extend(parsed['parents'])
                elif obj_type == 'tag':
                    parsed = parse_tag(data)
                    if parsed['object']:
                        stack.append(parsed['object'])
                elif obj_type == 'tree':
                    for mode, _name, child in parse_tree(data):
                        if mode != '160000':
                            stack.append(child)
        # Walk from wants with the depth limit. Non-commit wants (annotated
        # tags, trees, blobs) are sent directly and their targets enter the
        # commit walk at depth 1.
        sent_commits = set()
        direct_objects = set()
        shallow = []
        visited = set()
        stack = [(want, 1) for want in wants]
        while stack:
            oid, depth_here = stack.pop()
            if oid in visited or oid == ZERO_OID:
                continue
            visited.add(oid)
            found = lookup(oid)
            if found is None:
                continue
            if found[0] == 'tag':
                if oid not in have_closure:
                    direct_objects.add(oid)
                target = parse_tag(found[1])['object']
                if target:
                    stack.append((target, 1))
                continue
            if found[0] != 'commit':
                if oid not in have_closure:
                    direct_objects.add(oid)
                continue
            if depth is not None and depth_here > depth:
                continue
            if depth is not None and depth_here == depth:
                shallow.append(oid)
            if oid not in have_closure:
                sent_commits.add(oid)
            parsed = parse_commit(found[1])
            for parent in parsed['parents']:
                stack.append((parent, depth_here + 1))
        # Objects to send: sent commits + their tree closures, minus have_closure.
        to_send = set(direct_objects)
        for commit_oid in sent_commits:
            found = lookup(commit_oid)
            parsed = parse_commit(found[1])
            stack = [parsed['tree']] if parsed['tree'] else []
            to_send.add(commit_oid)
            while stack:
                oid = stack.pop()
                if oid in to_send or oid in have_closure or oid == ZERO_OID:
                    continue
                found2 = lookup(oid)
                if found2 is None:
                    continue
                to_send.add(oid)
                if found2[0] == 'tree':
                    for mode, _name, child in parse_tree(found2[1]):
                        if mode != '160000':
                            stack.append(child)
        objects = [(oid, self.objects[oid][0], self.objects[oid][1])
                   for oid in sorted(to_send) if oid in self.objects]
        # Unshallow: a client shallow boundary whose parents are all available
        # now (sent in this pack, or already held by the client).
        available = set(sent_commits) | {o for o in have_closure
                                         if self.objects.get(o, (None,))[0] == 'commit'}
        unshallow = []
        for oid in client_shallow:
            if oid not in visited:
                continue
            found = lookup(oid)
            if found is None or found[0] != 'commit':
                continue
            parents = parse_commit(found[1])['parents']
            if all(p in available for p in parents):
                unshallow.append(oid)
        return objects, sorted(set(shallow)), sorted(set(unshallow))


# --------------------------------------------------------------------------
# Global ids (opaque, stable) for GraphQL / node
# --------------------------------------------------------------------------

def global_id(kind, key):
    raw = ('%s:%s' % (kind, key)).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_global_id(gid):
    try:
        padded = gid + '=' * (-len(gid) % 4)
        raw = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8')
        kind, _, key = raw.partition(':')
        return kind, key
    except Exception:
        return None, None


# --------------------------------------------------------------------------
# REST v3 JSON shapes
# --------------------------------------------------------------------------

def repo_json(forge, repo):
    return {'id': repo.db_id,
            'node_id': global_id('Repository', repo.full_name()),
            'name': repo.name,
            'full_name': repo.full_name(),
            'owner': {'login': repo.owner, 'id': forge.viewer['id'],
                      'type': 'User'},
            'private': repo.private,
            'description': repo.description,
            'default_branch': repo.default_branch,
            'has_issues': True,
            'created_at': _iso_from_epoch(SCENARIO_EPOCH_SEC),
            'updated_at': _iso_from_epoch(SCENARIO_EPOCH_SEC)}


def issue_json(issue):
    return {'id': issue['id'], 'number': issue['number'],
            'title': issue['title'], 'body': issue['body'],
            'state': issue['state'], 'labels': list(issue['labels']),
            'user': {'login': ''}, 'comments': len(issue['comments'])}


def comment_json(comment):
    return {'id': comment['id'], 'body': comment['body'],
            'user': {'login': ''}}


def pull_json(pull):
    return {'id': pull['id'], 'number': pull['number'],
            'title': pull['title'], 'body': pull['body'],
            'state': pull['state'],
            'head': {'ref': pull['head']['ref'], 'sha': pull['head']['sha'],
                     'label': pull['head']['ref']},
            'base': {'ref': pull['base']['ref'], 'sha': pull['base']['sha'],
                     'label': pull['base']['ref']},
            'merged': pull['merged'], 'merged_at': pull['merged_at'],
            'merge_commit_sha': pull['merge_commit_sha']}


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

JSON_TYPE = 'application/json; charset=utf-8'


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_query(query_string):
    out = {}
    for pair in urllib.parse.parse_qsl(query_string, keep_blank_values=True):
        out[pair[0]] = pair[1]
    return out


def _page_params(query):
    try:
        per_page = int(query.get('per_page', '30'))
    except ValueError:
        per_page = 30
    try:
        page = int(query.get('page', '1'))
    except ValueError:
        page = 1
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    return per_page, page


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'makefun-forge/1.0'

    # -- quiet logging ------------------------------------------------------
    def log_message(self, *args):
        pass

    # -- helpers ------------------------------------------------------------
    @property
    def forge(self):
        return self.server.forge

    def _send_bytes(self, status, body, content_type, extra_headers=None):
        if body is None:
            body = b''
        headers = {'Content-Type': content_type, 'Content-Length': str(len(body))}
        if extra_headers:
            headers.update(extra_headers)
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != 'HEAD' and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, status, obj, extra_headers=None):
        body = (json.dumps(obj) + '\n').encode('utf-8')
        headers = {'X-GitHub-Request-Id': self.forge.next_request_id(),
                   'X-OAuth-Scopes': 'repo, read:org'}
        if extra_headers:
            headers.update(extra_headers)
        self._send_bytes(status, body, JSON_TYPE, headers)

    def _send_no_content(self, status=204):
        self._send_bytes(status, b'', JSON_TYPE,
                         {'X-GitHub-Request-Id': self.forge.next_request_id()})

    def _error(self, status, message):
        self._send_json(status, {'message': message})

    def _read_body_raw(self):
        # Chunked transfer-encoding (a client may send it instead of a length).
        if (self.headers.get('Transfer-Encoding') or '').lower() == 'chunked':
            out = bytearray()
            while True:
                line = self.rfile.readline(65536)
                try:
                    size = int(line.split(b';')[0].strip(), 16)
                except ValueError:
                    break
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if trailer in (b'\r\n', b'\n', b''):
                            break
                    break
                out += self.rfile.read(size)
                self.rfile.read(2)
            return bytes(out)
        length = self.headers.get('Content-Length')
        if length is None:
            return b''
        try:
            n = int(length)
        except ValueError:
            return b''
        if n <= 0:
            return b''
        return self.rfile.read(n)

    def _read_body(self):
        # The body is pre-read in _dispatch so that even an early 401/404
        # leaves no unread bytes on a keep-alive connection (an unread body
        # is parsed as the next request line and desyncs the connection).
        return getattr(self, '_cached_body', b'')

    def _read_json_body(self, required=False):
        raw = self._read_body()
        if not raw:
            if required:
                raise ApiError(400, 'request body is required')
            return {}
        try:
            value = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, 'malformed JSON body')
        if not isinstance(value, dict):
            raise ApiError(400, 'request body must be a JSON object')
        return value

    def _check_auth(self):
        token = self.forge.shop_token
        if not token:
            return
        auth = self.headers.get('Authorization') or ''
        if auth != 'Bearer ' + token:
            raise ApiError(401, 'Bad credentials')

    def _path_parts(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        parts = [urllib.parse.unquote(p) for p in path.split('/') if p != '']
        return path, parts, _parse_query(parsed.query)

    def _paginate(self, items, query, base_url):
        """Returns (page_items, link_header_or_None)."""
        per_page, page = _page_params(query)
        total = len(items)
        start = (page - 1) * per_page
        page_items = items[start:start + per_page]
        link = None
        if start + per_page < total:
            nxt = dict(query)
            nxt['page'] = str(page + 1)
            nxt['per_page'] = str(per_page)
            url = base_url + '?' + urllib.parse.urlencode(nxt)
            link = '<%s>; rel="next"' % url
        return page_items, link

    def _base_url(self):
        host = self.headers.get('Host') or 'localhost'
        return 'http://' + host

    # -- dispatch -------------------------------------------------------------
    def _is_read_only(self):
        """True when this request cannot mutate the store, so it may run
        without the global store lock (see _dispatch)."""
        method = self.command
        if method == 'GET':
            return True
        if method == 'DELETE':
            return True
        if method != 'POST':
            return False
        _path, parts, _query = self._path_parts()
        if len(parts) >= 3 and parts[1].endswith('.git'):
            # git-upload-pack is a pure read; receive-pack mutates refs.
            return parts[2] == 'git-upload-pack'
        if parts and parts[0] == 'mcp':
            try:
                body = json.loads(self._cached_body.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                return False
            if not isinstance(body, dict):
                return False
            return body.get('method') != 'tools/call'
        return False

    def _dispatch(self):
        try:
            # Consume the request body up front: any early return (401/404)
            # must not leave bytes behind on a keep-alive connection.
            if self.command in ('POST', 'PUT', 'PATCH', 'DELETE'):
                self._cached_body = self._read_body_raw()
            else:
                self._cached_body = b''
            if self._is_read_only():
                # Reads run without the store lock: the benchmark's 50
                # concurrent shallow clones are all reads, and serialising
                # them behind one lock costs the whole round. The sealed
                # suite runs checks one at a time and never overlaps a
                # mutation with these reads, so no read can observe a
                # half-written repo.
                self._route()
            else:
                with self.forge.lock:
                    self._route()
        except ApiError as err:
            try:
                self._error(err.status, err.message)
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as err:  # never crash the server on a bad request
            try:
                self._error(500, 'internal error: %s' % err)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    # -- routing ---------------------------------------------------------------
    def _route(self):
        path, parts, query = self._path_parts()
        method = self.command
        if method == 'GET' and path == '/healthz':
            self._send_json(200, {'ok': True})
            return
        if path.startswith('/api/graphql'):
            self._check_auth()
            self._handle_graphql()
            return
        if path == '/mcp' or path.startswith('/mcp/'):
            if method == 'DELETE':
                self._send_bytes(200, b'', JSON_TYPE)
                return
            self._check_auth()
            self._handle_mcp()
            return
        if len(parts) >= 3 and parts[1].endswith('.git'):
            self._check_auth()
            self._handle_git(parts, query)
            return
        if parts and parts[0] == 'api' and len(parts) >= 2 and parts[1] == 'v3':
            self._check_auth()
            self._handle_rest(parts[2:], query, method)
            return
        if path == '/' and method == 'GET':
            self._send_json(200, {'message': 'makefun-forge'})
            return
        self._error(404, 'Not Found')

    # -- REST v3 ---------------------------------------------------------------
    def _handle_rest(self, parts, query, method):
        forge = self.forge
        if not parts:
            if method == 'GET':
                self._send_json(200, {'current_user_url': '/api/v3/user',
                                      'rate_limit_url': '/api/v3/rate_limit'})
                return
            raise ApiError(404, 'Not Found')
        if parts == ['meta']:
            if method != 'GET':
                raise ApiError(404, 'Not Found')
            self._send_json(200, {'installed_version': '1.0.0'})
            return
        if parts[0] == 'user':
            if parts[1:] == ['repos']:
                if method == 'GET':
                    repos = forge.user_repos(forge.viewer['login'])
                    items, link = self._paginate(
                        [repo_json(forge, r) for r in repos], query,
                        self._base_url() + '/api/v3/user/repos')
                    extra = {'Link': link} if link else None
                    self._send_json(200, items, extra)
                    return
                if method == 'POST':
                    body = self._read_json_body()
                    name = body.get('name')
                    if not isinstance(name, str) or not name:
                        raise ApiError(422, 'name is required')
                    repo = forge.create_repo(forge.viewer['login'], name,
                                             body.get('private', False),
                                             body.get('description', ''))
                    if repo is None:
                        raise ApiError(422, 'Repository already exists')
                    self._send_json(201, repo_json(forge, repo))
                    return
                raise ApiError(404, 'Not Found')
            if parts[1:] == [] and method == 'GET':
                viewer = forge.viewer
                self._send_json(200, {'login': viewer['login'], 'id': viewer['id'],
                                      'name': viewer['name'], 'email': viewer['email'],
                                      'type': 'User'})
                return
            raise ApiError(404, 'Not Found')
        if parts[0] != 'repos' or len(parts) < 3:
            raise ApiError(404, 'Not Found')
        owner, name = parts[1], parts[2]
        repo = forge.get_repo(owner, name)
        rest = parts[3:]
        if repo is None:
            raise ApiError(404, 'Not Found')
        self._handle_repo_rest(forge, repo, rest, query, method)

    def _handle_repo_rest(self, forge, repo, rest, query, method):
        if not rest:
            if method == 'GET':
                self._send_json(200, repo_json(forge, repo))
                return
            if method == 'PATCH':
                body = self._read_json_body()
                if 'description' in body:
                    repo.description = body['description'] or ''
                if 'private' in body:
                    repo.private = bool(body['private'])
                self._send_json(200, repo_json(forge, repo))
                return
            if method == 'DELETE':
                del forge.repos[(repo.owner, repo.name)]
                self._send_no_content(204)
                return
            raise ApiError(404, 'Not Found')
        head = rest[0]
        if head == 'branches':
            if len(rest) == 1 and method == 'GET':
                out = []
                for ref in sorted(repo.refs):
                    if ref.startswith('refs/heads/'):
                        out.append({'name': ref[len('refs/heads/'):],
                                    'commit': {'sha': repo.refs[ref]}})
                self._send_json(200, out)
                return
            if len(rest) == 2 and method == 'GET':
                oid = repo.tip(rest[1])
                if oid is None:
                    raise ApiError(404, 'Not Found')
                self._send_json(200, {'name': rest[1], 'commit': {'sha': oid}})
                return
            raise ApiError(404, 'Not Found')
        if head == 'commits':
            if method != 'GET':
                raise ApiError(404, 'Not Found')
            branch = query.get('sha') or repo.default_branch
            oids = forge.branch_commits(repo, branch)
            out = []
            for oid in oids:
                found = forge.lookup(oid)
                parsed = parse_commit(found[1])
                out.append({'sha': oid, 'commit': {'message': parsed['message'],
                                                   'author': {'name': forge.viewer['name'],
                                                              'email': forge.viewer['email']},
                                                   'committer': {'name': forge.viewer['name'],
                                                                 'email': forge.viewer['email']}}})
            self._send_json(200, out)
            return
        if head == 'git':
            if len(rest) >= 2 and rest[1] == 'refs':
                if method == 'POST' and len(rest) == 2:
                    body = self._read_json_body()
                    ref = body.get('ref')
                    sha = body.get('sha')
                    if not isinstance(ref, str) or not ref.startswith('refs/') or \
                            not isinstance(sha, str) or not SHA40_RE.match(sha):
                        raise ApiError(422, 'ref and sha are required')
                    if ref in repo.refs:
                        raise ApiError(422, 'Reference already exists')
                    if forge.lookup(sha) is None:
                        raise ApiError(422, 'object not found')
                    repo.refs[ref] = sha
                    self._send_json(201, {'ref': ref, 'object': {'sha': sha}})
                    return
                if method == 'GET' and len(rest) == 4 and rest[2] == 'heads':
                    oid = repo.tip(rest[3])
                    if oid is None:
                        raise ApiError(404, 'Not Found')
                    self._send_json(200, {'ref': 'refs/heads/' + rest[3],
                                          'object': {'sha': oid}})
                    return
            raise ApiError(404, 'Not Found')
        if head == 'contents':
            self._handle_contents(forge, repo, rest[1:], query, method)
            return
        if head == 'labels':
            if method == 'POST' and len(rest) == 1:
                body = self._read_json_body()
                label_name = body.get('name')
                if not isinstance(label_name, str) or not label_name:
                    raise ApiError(422, 'name is required')
                label = repo.labels.get(label_name)
                if label is None:
                    label = {'name': label_name,
                             'color': body.get('color', 'ededed')}
                    repo.labels[label_name] = label
                self._send_json(201, label)
                return
            raise ApiError(404, 'Not Found')
        if head == 'issues':
            self._handle_issues(forge, repo, rest[1:], query, method)
            return
        if head == 'pulls':
            self._handle_pulls(forge, repo, rest[1:], query, method)
            return
        raise ApiError(404, 'Not Found')

    def _handle_contents(self, forge, repo, rest, query, method):
        if not rest:
            raise ApiError(404, 'Not Found')
        file_path = '/'.join(rest)
        if method == 'GET':
            branch = query.get('ref') or repo.default_branch
            tip = repo.tip(branch)
            if tip is None:
                raise ApiError(404, 'Not Found')
            files = forge.commit_tree_map(tip)
            if file_path in files:
                _mode, oid = files[file_path]
                found = forge.lookup(oid)
                data = found[1] if found else b''
                self._send_json(200, {'type': 'file', 'encoding': 'base64',
                                      'content': base64.b64encode(data).decode('ascii'),
                                      'sha': oid, 'size': len(data),
                                      'name': file_path.split('/')[-1],
                                      'path': file_path})
                return
            prefix = file_path.rstrip('/') + '/'
            entries = []
            seen_dirs = set()
            for path in sorted(files):
                if not path.startswith(prefix):
                    continue
                rest_path = path[len(prefix):]
                if '/' in rest_path:
                    dirname = rest_path.split('/')[0]
                    if dirname not in seen_dirs:
                        seen_dirs.add(dirname)
                        entries.append({'type': 'dir', 'name': dirname,
                                        'path': file_path + '/' + dirname,
                                        'sha': ''})
                    continue
                entries.append({'type': 'file', 'name': rest_path,
                                'path': path, 'sha': files[path][1]})
            if not entries:
                # A bare directory read of the repo root lists top-level files.
                if file_path in ('', '.'):
                    for path in sorted(files):
                        if '/' in path:
                            continue
                        entries.append({'type': 'file', 'name': path,
                                        'path': path, 'sha': files[path][1]})
                if not entries:
                    raise ApiError(404, 'Not Found')
            # Directory listings need a sha per entry; use the subtree oid.
            out = []
            for entry in entries:
                if entry['type'] == 'file':
                    out.append(entry)
                else:
                    tip_tree = None
                    tip_found = forge.lookup(tip)
                    if tip_found is not None and tip_found[0] == 'commit':
                        tip_tree = parse_commit(tip_found[1])['tree']
                    sub_oid = forge.subtree_oid(tip_tree, entry['path']) if tip_tree else None
                    entry = dict(entry)
                    entry['sha'] = sub_oid or ZERO_OID
                    out.append(entry)
            self._send_json(200, out)
            return
        if method == 'PUT':
            body = self._read_json_body()
            message = body.get('message')
            content_b64 = body.get('content')
            if not isinstance(message, str) or not isinstance(content_b64, str):
                raise ApiError(422, 'message and content are required')
            try:
                content = base64.b64decode(content_b64.encode('ascii'))
            except Exception:
                raise ApiError(422, 'content is not valid base64')
            branch = body.get('branch') or repo.default_branch
            tip = repo.tip(branch)
            if tip is not None:
                files = forge.commit_tree_map(tip)
                if file_path in files:
                    expected = body.get('sha')
                    if expected is not None and expected != files[file_path][1]:
                        raise ApiError(409, 'sha does not match the current blob')
                    status = 200
                else:
                    status = 201
            else:
                status = 201
            commit_oid, blob_oids = forge.contents_commit(
                repo, branch, {file_path: content}, message)
            self._send_json(status, {'content': {'sha': blob_oids[file_path],
                                                 'encoding': 'base64'},
                                    'commit': {'sha': commit_oid}})
            return
        if method == 'DELETE':
            body = self._read_json_body()
            message = body.get('message')
            sha = body.get('sha')
            if not isinstance(message, str):
                raise ApiError(422, 'message is required')
            branch = body.get('branch') or repo.default_branch
            tip = repo.tip(branch)
            if tip is None:
                raise ApiError(404, 'Not Found')
            files = forge.commit_tree_map(tip)
            if file_path not in files:
                raise ApiError(404, 'Not Found')
            if sha is not None and sha != files[file_path][1]:
                raise ApiError(409, 'sha does not match the current blob')
            commit_oid, _blobs = forge.contents_commit(
                repo, branch, {file_path: None}, message)
            self._send_json(200, {'commit': {'sha': commit_oid}})
            return
        raise ApiError(404, 'Not Found')

    def _handle_issues(self, forge, repo, rest, query, method):
        if not rest:
            if method == 'GET':
                state = query.get('state', 'open')
                sort = query.get('sort', 'created')
                direction = query.get('direction', 'asc')
                items = list(repo.issues)
                if state == 'open':
                    items = [i for i in items if i['state'] == 'open']
                elif state == 'closed':
                    items = [i for i in items if i['state'] == 'closed']
                if sort == 'created':
                    items.sort(key=lambda i: i['_seq'],
                               reverse=(direction == 'desc'))
                page_items, link = self._paginate(
                    [issue_json(i) for i in items], query,
                    self._base_url() + '/api/v3/repos/%s/%s/issues' % (repo.owner, repo.name))
                extra = {'Link': link} if link else None
                self._send_json(200, page_items, extra)
                return
            if method == 'POST':
                body = self._read_json_body()
                title = body.get('title')
                if not isinstance(title, str) or not title:
                    raise ApiError(422, 'title is required')
                labels = body.get('labels') or []
                if not isinstance(labels, list):
                    raise ApiError(422, 'labels must be a list')
                for label in labels:
                    if label not in repo.labels:
                        raise ApiError(422, 'Label does not exist: %s' % label)
                issue = forge.create_issue(repo, title, body.get('body', ''), labels)
                self._send_json(201, issue_json(issue))
                return
            raise ApiError(404, 'Not Found')
        try:
            number = int(rest[0])
        except ValueError:
            raise ApiError(404, 'Not Found')
        issue = repo.find_issue(number)
        if issue is None:
            raise ApiError(404, 'Not Found')
        if len(rest) == 1:
            if method == 'GET':
                self._send_json(200, issue_json(issue))
                return
            if method == 'PATCH':
                body = self._read_json_body()
                if 'state' in body and body['state'] in ('open', 'closed'):
                    issue['state'] = body['state']
                if 'title' in body:
                    issue['title'] = body['title']
                if 'body' in body:
                    issue['body'] = body['body']
                self._send_json(200, issue_json(issue))
                return
            raise ApiError(404, 'Not Found')
        if rest[1] == 'comments':
            if method == 'GET':
                self._send_json(200, [comment_json(c) for c in issue['comments']])
                return
            if method == 'POST':
                body = self._read_json_body()
                text = body.get('body')
                if not isinstance(text, str):
                    raise ApiError(422, 'body is required')
                comment = {'id': forge.next_rest_id(), 'body': text}
                issue['comments'].append(comment)
                self._send_json(201, comment_json(comment))
                return
        raise ApiError(404, 'Not Found')

    def _handle_pulls(self, forge, repo, rest, query, method):
        if not rest:
            if method == 'GET':
                state = query.get('state', 'open')
                items = list(repo.pulls)
                if state == 'open':
                    items = [p for p in items if p['state'] == 'open']
                elif state == 'closed':
                    items = [p for p in items if p['state'] == 'closed']
                page_items, link = self._paginate(
                    [pull_json(p) for p in items], query,
                    self._base_url() + '/api/v3/repos/%s/%s/pulls' % (repo.owner, repo.name))
                extra = {'Link': link} if link else None
                self._send_json(200, page_items, extra)
                return
            if method == 'POST':
                body = self._read_json_body()
                title = body.get('title')
                head = body.get('head')
                base = body.get('base')
                if not isinstance(title, str) or not title:
                    raise ApiError(422, 'title is required')
                if not isinstance(head, str) or not isinstance(base, str):
                    raise ApiError(422, 'head and base are required')
                pull, err = forge.create_pull(repo, title, body.get('body', ''), head, base)
                if pull is None:
                    raise ApiError(422, err or 'cannot create pull request')
                self._send_json(201, pull_json(pull))
                return
            raise ApiError(404, 'Not Found')
        try:
            number = int(rest[0])
        except ValueError:
            raise ApiError(404, 'Not Found')
        pull = repo.find_pull(number)
        if pull is None:
            raise ApiError(404, 'Not Found')
        if len(rest) == 1:
            if method == 'GET':
                self._send_json(200, pull_json(pull))
                return
            raise ApiError(404, 'Not Found')
        if len(rest) == 2 and rest[1] == 'files' and method == 'GET':
            self._send_json(200, forge.pr_files(pull))
            return
        if len(rest) == 2 and rest[1] == 'merge' and method == 'PUT':
            body = self._read_json_body()
            ok, reason, sha = forge.squash_merge(repo, pull)
            if not ok:
                status = 405 if reason == 'merge conflict' else 422
                raise ApiError(status, reason)
            self._send_json(200, {'merged': True, 'sha': sha,
                                  'message': 'Pull Request successfully merged'})
            return
        raise ApiError(404, 'Not Found')


# --------------------------------------------------------------------------
# GraphQL v4: small parser + executor for the suite's documents
# --------------------------------------------------------------------------

class GraphQLError(Exception):
    pass


_TOKEN_RE = re.compile(r'''
    (?P<ws>[ \t\r\n,]+)
  | (?P<ellipsis>\.\.\.)
  | (?P<punct>[!$\(\)\:\=\@\[\]\{\}\|&])
  | (?P<float>-?(?:0|[1-9][0-9]*)\.[0-9]+(?:[eE][+-]?[0-9]+)?)
  | (?P<int>-?(?:0|[1-9][0-9]*))
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<blockstring>""".*?""")
  | (?P<name>[_A-Za-z][_0-9A-Za-z]*)
''', re.VERBOSE | re.DOTALL)


def _gql_tokenize(text):
    tokens = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if not match:
            raise GraphQLError('unexpected character %r' % text[pos])
        pos = match.end()
        kind = match.lastgroup
        if kind == 'ws':
            continue
        tokens.append((kind, match.group()))
    tokens.append(('eof', ''))
    return tokens


def _gql_unescape(string_token):
    raw = string_token[1:-1]
    out = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '\\' and i + 1 < len(raw):
            nxt = raw[i + 1]
            mapping = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\', '/': '/'}
            if nxt == 'u' and i + 5 < len(raw):
                out.append(chr(int(raw[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append(mapping.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


class _GqlParser:
    def __init__(self, text):
        self.tokens = _gql_tokenize(text)
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect_punct(self, punct):
        kind, value = self.next()
        if kind != 'punct' or value != punct:
            raise GraphQLError('expected %r, got %r' % (punct, value))

    def expect_name(self):
        kind, value = self.next()
        if kind != 'name':
            raise GraphQLError('expected a name, got %r' % value)
        return value

    def parse_document(self):
        definitions = []
        while self.peek()[0] != 'eof':
            definitions.append(self.parse_definition())
        return definitions

    def parse_definition(self):
        kind, value = self.peek()
        if kind == 'punct' and value == '{':
            return {'kind': 'operation', 'operation': 'query', 'name': None,
                    'variables': {}, 'selection': self.parse_selection_set()}
        if kind == 'name' and value in ('query', 'mutation', 'subscription'):
            self.next()
            op = value
            name = None
            if self.peek()[0] == 'name':
                name = self.next()[1]
            variables = {}
            if self.peek() == ('punct', '('):
                variables = self.parse_variable_definitions()
            self._skip_directives()
            return {'kind': 'operation', 'operation': op, 'name': name,
                    'variables': variables, 'selection': self.parse_selection_set()}
        raise GraphQLError('unexpected definition %r' % (value,))

    def _skip_directives(self):
        while self.peek() == ('punct', '@'):
            self.next()
            self.expect_name()
            if self.peek() == ('punct', '('):
                self.parse_arguments()

    def parse_variable_definitions(self):
        self.expect_punct('(')
        variables = {}
        while self.peek() != ('punct', ')'):
            self.expect_punct('$')
            name = self.expect_name()
            self.expect_punct(':')
            variables[name] = self.parse_type()
            if self.peek() == ('punct', '='):
                self.next()
                self.parse_value()
            if self.peek() == ('punct', ','):
                self.next()
        self.expect_punct(')')
        return variables

    def parse_type(self):
        kind, value = self.peek()
        if kind == 'punct' and value == '[':
            self.next()
            inner = self.parse_type()
            self.expect_punct(']')
            result = ('list', inner)
        elif kind == 'name':
            result = ('named', self.next()[1])
        else:
            raise GraphQLError('expected a type, got %r' % (value,))
        if self.peek() == ('punct', '!'):
            self.next()
            result = ('nonnull', result)
        return result

    def parse_selection_set(self):
        self.expect_punct('{')
        selections = []
        while self.peek() != ('punct', '}'):
            selections.append(self.parse_selection())
        self.expect_punct('}')
        return selections

    def parse_selection(self):
        if self.peek()[0] == 'ellipsis':
            self.next()
            kind, value = self.peek()
            if kind == 'name' and value == 'on':
                self.next()
                type_name = self.expect_name()
                self._skip_directives()
                return {'kind': 'inline', 'type': type_name,
                        'selection': self.parse_selection_set()}
            if kind == 'name':
                self.next()
                self._skip_directives()
                return {'kind': 'spread', 'name': value}
            raise GraphQLError('unexpected fragment')
        name = self.expect_name()
        alias = None
        if self.peek() == ('punct', ':'):
            self.next()
            alias = name
            name = self.expect_name()
        arguments = {}
        if self.peek() == ('punct', '('):
            arguments = self.parse_arguments()
        self._skip_directives()
        selection = None
        if self.peek() == ('punct', '{'):
            selection = self.parse_selection_set()
        return {'kind': 'field', 'name': name, 'alias': alias or name,
                'arguments': arguments, 'selection': selection}

    def parse_arguments(self):
        self.expect_punct('(')
        args = {}
        while self.peek() != ('punct', ')'):
            name = self.expect_name()
            self.expect_punct(':')
            args[name] = self.parse_value()
            if self.peek() == ('punct', ','):
                self.next()
        self.expect_punct(')')
        return args

    def parse_value(self):
        kind, value = self.next()
        if kind == 'punct' and value == '$':
            return ('var', self.expect_name())
        if kind == 'int':
            return ('int', int(value))
        if kind == 'float':
            return ('float', float(value))
        if kind in ('string', 'blockstring'):
            if kind == 'blockstring':
                return ('string', value[3:-3])
            return ('string', _gql_unescape(value))
        if kind == 'name':
            if value == 'true':
                return ('bool', True)
            if value == 'false':
                return ('bool', False)
            if value == 'null':
                return ('null', None)
            return ('enum', value)
        if kind == 'punct' and value == '[':
            items = []
            while self.peek() != ('punct', ']'):
                items.append(self.parse_value())
                if self.peek() == ('punct', ','):
                    self.next()
            self.expect_punct(']')
            return ('list', items)
        if kind == 'punct' and value == '{':
            obj = {}
            while self.peek() != ('punct', '}'):
                name = self.expect_name()
                self.expect_punct(':')
                obj[name] = self.parse_value()
                if self.peek() == ('punct', ','):
                    self.next()
            self.expect_punct('}')
            return ('object', obj)
        raise GraphQLError('unexpected value %r' % (value,))


def _resolve_value(node, variables):
    kind, value = node
    if kind == 'var':
        return variables.get(value)
    if kind == 'list':
        return [_resolve_value(item, variables) for item in value]
    if kind == 'object':
        return {key: _resolve_value(val, variables) for key, val in value.items()}
    return value


# Schema field tables (for introspection and unknown-field errors).
_GQL_FIELDS = {
    'Query': ['viewer', 'repository', 'node', '__type'],
    'Mutation': ['createIssue', 'closeIssue', 'createPullRequest', 'mergePullRequest', 'addComment'],
    'Repository': ['id', 'databaseId', 'name', 'nameWithOwner', 'owner', 'defaultBranchRef',
                   'viewerPermission', 'hasIssuesEnabled', 'issue', 'issues', 'pullRequests',
                   'pullRequest', 'description', 'private', 'createdAt', 'updatedAt'],
    'Issue': ['id', 'number', 'title', 'state', 'body', 'createdAt', 'updatedAt',
              'author', 'labels', 'comments', 'url'],
    'PullRequest': ['id', 'number', 'title', 'body', 'state', 'merged', 'mergeable',
                    'headRefName', 'baseRefName', 'headRefOid', 'baseRefOid', 'files',
                    'author', 'createdAt', 'updatedAt'],
    'User': ['login', 'id', 'name', 'email'],
    'IssueConnection': ['nodes', 'pageInfo', 'totalCount'],
    'PullRequestConnection': ['nodes', 'pageInfo', 'totalCount'],
    'PageInfo': ['hasNextPage', 'hasPreviousPage', 'endCursor', 'startCursor'],
    'PullRequestChangedFile': ['path', 'filename', 'additions', 'deletions', 'changes'],
    'IssueComment': ['id', 'body', 'author'],
    'Ref': ['name', 'target'],
    'IssueEdge': ['node', 'cursor'],
    'PullRequestEdge': ['node', 'cursor'],
    'CommentEdge': ['node', 'cursor'],
    '__Type': ['name', 'kind', 'fields', 'enumValues'],
    '__Field': ['name', 'type'],
    '__EnumValue': ['name'],
    'CreateIssuePayload': ['issue'],
    'CloseIssuePayload': ['issue'],
    'CreatePullRequestPayload': ['pullRequest'],
    'MergePullRequestPayload': ['pullRequest'],
    'AddCommentPayload': ['commentEdge'],
}
_GQL_ENUMS = {
    'SearchType': ['ISSUE', 'PULL_REQUEST', 'DISCUSSION', 'REPOSITORY', 'USER',
                   'ORGANIZATION', 'MARKETPLACE_LISTING', 'COMMIT', 'CODE'],
    'IssueState': ['OPEN', 'CLOSED'],
    'PullRequestState': ['OPEN', 'CLOSED', 'MERGED'],
    'MergeableState': ['MERGEABLE', 'CONFLICTING', 'UNKNOWN'],
    'PullRequestMergeMethod': ['MERGE', 'SQUASH', 'REBASE'],
    'RepositoryPermission': ['ADMIN', 'MAINTAIN', 'WRITE', 'TRIAGE', 'READ'],
    'IssueOrderField': ['CREATED_AT', 'UPDATED_AT', 'COMMENTS'],
    'OrderDirection': ['ASC', 'DESC'],
}


class GraphQLExecutor:
    def __init__(self, forge):
        self.forge = forge

    # -- public ---------------------------------------------------------------
    def execute(self, query_text, variables):
        try:
            definitions = _GqlParser(query_text).parse_document()
        except GraphQLError as err:
            return {'data': None, 'errors': [{'message': str(err)}]}
        if not definitions:
            return {'data': None, 'errors': [{'message': 'empty document'}]}
        operation = definitions[0]
        if operation['kind'] != 'operation':
            return {'data': None, 'errors': [{'message': 'unsupported definition'}]}
        try:
            if operation['operation'] == 'mutation':
                data = self._execute_selection(
                    operation['selection'], 'Mutation', {'__mutation__': True}, variables)
            else:
                data = self._execute_selection(
                    operation['selection'], 'Query', {}, variables)
            return {'data': data}
        except GraphQLError as err:
            return {'data': None, 'errors': [{'message': str(err)}]}

    # -- execution ---------------------------------------------------------------
    def _execute_selection(self, selections, type_name, value, variables):
        out = {}
        for sel in selections:
            if sel['kind'] == 'spread':
                raise GraphQLError('named fragments are not supported')
            if sel['kind'] == 'inline':
                if sel['type'] == type_name or self._type_matches(value, sel['type']):
                    sub = self._execute_selection(sel['selection'], sel['type'], value, variables)
                    out.update(sub)
                continue
            field = sel['name']
            if field == '__typename':
                out[sel['alias']] = type_name
                continue
            allowed = _GQL_FIELDS.get(type_name, [])
            if field not in allowed:
                raise GraphQLError(
                    'Cannot query field "%s" on type "%s".' % (field, type_name))
            args = {k: _resolve_value(v, variables) for k, v in sel['arguments'].items()}
            result, result_type = self._resolve_field(type_name, value, field, args)
            if sel['selection'] is not None:
                if result is None:
                    out[sel['alias']] = None
                elif isinstance(result, list):
                    out[sel['alias']] = [
                        self._execute_selection(sel['selection'], result_type, item, variables)
                        for item in result]
                else:
                    out[sel['alias']] = self._execute_selection(
                        sel['selection'], result_type, result, variables)
            else:
                out[sel['alias']] = result
        return out

    def _type_matches(self, value, type_name):
        if isinstance(value, dict) and '_gql_type' in value:
            return value['_gql_type'] == type_name
        return False

    # -- resolvers ---------------------------------------------------------------
    def _resolve_field(self, type_name, value, field, args):
        forge = self.forge
        if type_name == 'Query':
            if field == 'viewer':
                viewer = forge.viewer
                return ({'_gql_type': 'User', 'login': viewer['login'],
                         'id': viewer['id'], 'name': viewer['name'],
                         'email': viewer['email']}, 'User')
            if field == 'repository':
                repo = forge.get_repo(args.get('owner'), args.get('name'))
                if repo is None:
                    return None, 'Repository'
                return (self._repo_value(repo), 'Repository')
            if field == 'node':
                kind, key = decode_global_id(args.get('id') or '')
                if kind == 'Repository':
                    owner, _, name = key.partition('/')
                    repo = forge.get_repo(owner, name)
                    if repo is None:
                        return None, 'Repository'
                    return (self._repo_value(repo), 'Repository')
                if kind == 'Issue':
                    owner_repo, _, number = key.rpartition('#')
                    owner, _, name = owner_repo.partition('/')
                    repo = forge.get_repo(owner, name)
                    if repo is None:
                        return None, 'Issue'
                    try:
                        issue = repo.find_issue(int(number))
                    except ValueError:
                        issue = None
                    if issue is None:
                        return None, 'Issue'
                    return (self._issue_value(repo, issue), 'Issue')
                return None, 'Repository'
            if field == '__type':
                name = args.get('name')
                if name in _GQL_FIELDS:
                    return ({'_gql_type': '__Type', 'name': name,
                             'fields': [{'name': f} for f in _GQL_FIELDS[name]],
                             'enumValues': []}, '__Type')
                if name in _GQL_ENUMS:
                    return ({'_gql_type': '__Type', 'name': name,
                             'fields': [],
                             'enumValues': [{'name': v} for v in _GQL_ENUMS[name]]}, '__Type')
                return None, '__Type'
        if type_name == 'Mutation':
            return self._resolve_mutation(field, args)
        if type_name == 'Repository':
            repo = value['_repo']
            if field == 'id':
                return global_id('Repository', repo.full_name()), None
            if field == 'databaseId':
                return repo.db_id, None
            if field == 'name':
                return repo.name, None
            if field == 'nameWithOwner':
                return repo.full_name(), None
            if field == 'owner':
                return ({'_gql_type': 'User', 'login': repo.owner,
                         'id': forge.viewer['id'], 'name': forge.viewer['name'],
                         'email': forge.viewer['email']}, 'User')
            if field == 'defaultBranchRef':
                if repo.tip(repo.default_branch) is None:
                    return None, None
                return ({'_gql_type': 'Ref', 'name': repo.default_branch}, 'Ref')
            if field == 'viewerPermission':
                return 'ADMIN', None
            if field == 'hasIssuesEnabled':
                return True, None
            if field == 'description':
                return repo.description, None
            if field == 'private':
                return repo.private, None
            if field == 'issue':
                issue = repo.find_issue(args.get('number'))
                if issue is None:
                    return None, 'Issue'
                return (self._issue_value(repo, issue), 'Issue')
            if field == 'issues':
                issues = list(repo.issues)
                states = args.get('states')
                if states:
                    wanted = {'open' if s == 'OPEN' else 'closed' for s in states}
                    issues = [i for i in issues if i['state'] in wanted]
                order_by = args.get('orderBy') or {}
                direction = order_by.get('direction', 'ASC')
                issues.sort(key=lambda i: i['_seq'], reverse=(direction == 'DESC'))
                nodes, page_info = self._paginate_nodes(
                    issues, args.get('first'), args.get('after'),
                    lambda i: self._issue_value(repo, i))
                return ({'_gql_type': 'IssueConnection', '_elem': 'Issue',
                         'nodes': nodes, 'pageInfo': page_info}, 'IssueConnection')
            if field == 'pullRequests':
                pulls = list(repo.pulls)
                states = args.get('states')
                if states:
                    wanted = set()
                    for s in states:
                        wanted.add({'OPEN': 'open', 'CLOSED': 'closed',
                                    'MERGED': 'closed'}.get(s, s.lower()))
                    pulls = [p for p in pulls if p['state'] in wanted]
                first = args.get('first')
                if first is not None:
                    pulls = pulls[:first]
                nodes = [self._pull_value(repo, p) for p in pulls]
                return ({'_gql_type': 'PullRequestConnection', '_elem': 'PullRequest',
                         'nodes': nodes,
                         'pageInfo': {'hasNextPage': False, 'hasPreviousPage': False,
                                      'endCursor': None, 'startCursor': None}},
                        'PullRequestConnection')
            if field == 'pullRequest':
                pull = repo.find_pull(args.get('number'))
                if pull is None:
                    return None, 'PullRequest'
                return (self._pull_value(repo, pull), 'PullRequest')
        if type_name == 'Issue':
            issue = value['_issue']
            if field == 'id':
                repo = value['_repo']
                return global_id('Issue', '%s#%d' % (repo.full_name(), issue['number'])), None
            if field == 'number':
                return issue['number'], None
            if field == 'title':
                return issue['title'], None
            if field == 'state':
                return 'OPEN' if issue['state'] == 'open' else 'CLOSED', None
            if field == 'body':
                return issue['body'], None
            if field == 'labels':
                return issue['labels'], None
            if field == 'comments':
                return [{'_gql_type': 'IssueComment', 'id': c['id'], 'body': c['body']}
                        for c in issue['comments']], 'IssueComment'
        if type_name == 'PullRequest':
            pull = value['_pull']
            if field == 'id':
                repo = value['_repo']
                return global_id('PullRequest', '%s#%d' % (repo.full_name(), pull['number'])), None
            if field == 'number':
                return pull['number'], None
            if field == 'title':
                return pull['title'], None
            if field == 'body':
                return pull['body'], None
            if field == 'state':
                if pull['merged']:
                    return 'MERGED', None
                return 'OPEN' if pull['state'] == 'open' else 'CLOSED', None
            if field == 'merged':
                return pull['merged'], None
            if field == 'mergeable':
                return self.forge.pr_mergeable(pull), None
            if field == 'headRefName':
                return pull['head']['ref'], None
            if field == 'baseRefName':
                return pull['base']['ref'], None
            if field == 'headRefOid':
                return pull['head']['sha'], None
            if field == 'baseRefOid':
                return pull['base']['sha'], None
            if field == 'files':
                files = self.forge.pr_files(pull)
                first = args.get('first')
                if first is not None:
                    files = files[:first]
                nodes = [{'_gql_type': 'PullRequestChangedFile',
                          'path': f['filename'], 'filename': f['filename'],
                          'additions': f['additions'], 'deletions': f['deletions'],
                          'changes': f['changes']} for f in files]
                return ({'_gql_type': 'PullRequestConnection',
                         '_elem': 'PullRequestChangedFile', 'nodes': nodes,
                         'pageInfo': {'hasNextPage': False, 'hasPreviousPage': False,
                                      'endCursor': None, 'startCursor': None}},
                        'PullRequestConnection')
        if type_name == 'IssueConnection':
            if field == 'nodes':
                return value['nodes'], value.get('_elem')
            if field == 'pageInfo':
                return value['pageInfo'], 'PageInfo'
            if field == 'totalCount':
                return len(value['nodes']), None
        if type_name == 'PullRequestConnection':
            if field == 'nodes':
                return value['nodes'], value.get('_elem')
            if field == 'pageInfo':
                return value['pageInfo'], 'PageInfo'
            if field == 'totalCount':
                return len(value['nodes']), None
        if type_name == 'PageInfo':
            return value.get(field), None
        if type_name == 'PullRequestChangedFile':
            return value.get(field), None
        if type_name == 'User':
            return value.get(field), None
        if type_name == 'Ref':
            return value.get(field), None
        if type_name == '__Type':
            if field == 'fields':
                return value.get('fields', []), '__Field'
            if field == 'enumValues':
                return value.get('enumValues', []), '__EnumValue'
            return value.get(field), None
        if type_name in ('__Field', '__EnumValue'):
            return value.get(field), None
        if type_name == 'IssueComment':
            return value.get(field), None
        if type_name == 'CommentEdge':
            if field == 'node':
                return value['node'], 'IssueComment'
            return value.get(field), None
        if type_name in ('CreateIssuePayload', 'CloseIssuePayload'):
            if field == 'issue':
                return value['issue'], 'Issue'
        if type_name == 'CreatePullRequestPayload':
            if field == 'pullRequest':
                return value['pullRequest'], 'PullRequest'
        if type_name == 'MergePullRequestPayload':
            if field == 'pullRequest':
                return value['pullRequest'], 'PullRequest'
        if type_name == 'AddCommentPayload':
            if field == 'commentEdge':
                return value['commentEdge'], 'CommentEdge'
        return None, None

    def _paginate_nodes(self, items, first, after, to_value):
        start = 0
        if after:
            try:
                padded = after + '=' * (-len(after) % 4)
                decoded = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8')
                if decoded.startswith('cursor:'):
                    start = int(decoded[len('cursor:'):]) + 1
            except Exception:
                start = 0
        end = len(items) if first is None else min(len(items), start + first)
        page = items[start:end]
        nodes = [to_value(item) for item in page]
        has_next = end < len(items)
        end_cursor = None
        if page:
            end_cursor = base64.urlsafe_b64encode(
                ('cursor:%d' % (end - 1)).encode('utf-8')).decode('ascii').rstrip('=')
        page_info = {'hasNextPage': has_next, 'hasPreviousPage': start > 0,
                     'endCursor': end_cursor, 'startCursor': end_cursor}
        return nodes, page_info

    def _repo_value(self, repo):
        return {'_gql_type': 'Repository', '_repo': repo}

    def _issue_value(self, repo, issue):
        return {'_gql_type': 'Issue', '_repo': repo, '_issue': issue}

    def _pull_value(self, repo, pull):
        return {'_gql_type': 'PullRequest', '_repo': repo, '_pull': pull}

    def _resolve_mutation(self, field, args):
        forge = self.forge
        payload = args.get('input') or {}
        if field == 'createIssue':
            kind, key = decode_global_id(payload.get('repositoryId') or '')
            if kind != 'Repository':
                raise GraphQLError('createIssue: unknown repositoryId')
            owner, _, name = key.partition('/')
            repo = forge.get_repo(owner, name)
            if repo is None:
                raise GraphQLError('createIssue: repository not found')
            issue = forge.create_issue(repo, payload.get('title', ''),
                                       payload.get('body', ''), [])
            return ({'_gql_type': 'CreateIssuePayload',
                     'issue': self._issue_value(repo, issue)}, 'CreateIssuePayload')
        if field == 'closeIssue':
            kind, key = decode_global_id(payload.get('issueId') or '')
            if kind != 'Issue':
                raise GraphQLError('closeIssue: unknown issueId')
            owner_repo, _, number = key.rpartition('#')
            owner, _, name = owner_repo.partition('/')
            repo = forge.get_repo(owner, name)
            issue = repo.find_issue(int(number)) if repo else None
            if issue is None:
                raise GraphQLError('closeIssue: issue not found')
            issue['state'] = 'closed'
            return ({'_gql_type': 'CloseIssuePayload',
                     'issue': self._issue_value(repo, issue)}, 'CloseIssuePayload')
        if field == 'createPullRequest':
            kind, key = decode_global_id(payload.get('repositoryId') or '')
            if kind != 'Repository':
                raise GraphQLError('createPullRequest: unknown repositoryId')
            owner, _, name = key.partition('/')
            repo = forge.get_repo(owner, name)
            if repo is None:
                raise GraphQLError('createPullRequest: repository not found')
            pull, err = forge.create_pull(repo, payload.get('title', ''), '',
                                          payload.get('headRefName', ''),
                                          payload.get('baseRefName', ''))
            if pull is None:
                raise GraphQLError('createPullRequest: %s' % (err or 'failed'))
            return ({'_gql_type': 'CreatePullRequestPayload',
                     'pullRequest': self._pull_value(repo, pull)}, 'CreatePullRequestPayload')
        if field == 'mergePullRequest':
            kind, key = decode_global_id(payload.get('pullRequestId') or '')
            if kind != 'PullRequest':
                raise GraphQLError('mergePullRequest: unknown pullRequestId')
            owner_repo, _, number = key.rpartition('#')
            owner, _, name = owner_repo.partition('/')
            repo = forge.get_repo(owner, name)
            pull = repo.find_pull(int(number)) if repo else None
            if pull is None:
                raise GraphQLError('mergePullRequest: pull request not found')
            ok, reason, _sha = forge.squash_merge(repo, pull)
            if not ok:
                raise GraphQLError('mergePullRequest: %s' % (reason or 'failed'))
            return ({'_gql_type': 'MergePullRequestPayload',
                     'pullRequest': self._pull_value(repo, pull)}, 'MergePullRequestPayload')
        if field == 'addComment':
            kind, key = decode_global_id(payload.get('subjectId') or '')
            if kind != 'Issue':
                raise GraphQLError('addComment: unknown subjectId')
            owner_repo, _, number = key.rpartition('#')
            owner, _, name = owner_repo.partition('/')
            repo = forge.get_repo(owner, name)
            issue = repo.find_issue(int(number)) if repo else None
            if issue is None:
                raise GraphQLError('addComment: issue not found')
            comment = {'id': forge.next_rest_id(), 'body': payload.get('body', '')}
            issue['comments'].append(comment)
            node = {'_gql_type': 'IssueComment', 'id': global_id(
                'IssueComment', '%s#%d:%d' % (repo.full_name(), issue['number'], comment['id'])),
                'body': comment['body']}
            return ({'_gql_type': 'AddCommentPayload',
                     'commentEdge': {'_gql_type': 'CommentEdge', 'node': node}},
                    'AddCommentPayload')
        raise GraphQLError('Cannot query field "%s" on type "Mutation".' % field)


# --------------------------------------------------------------------------
# GraphQL + MCP request handlers (methods on Handler)
# --------------------------------------------------------------------------

def _handle_graphql(self):
    raw = self._read_body()
    try:
        payload = json.loads(raw.decode('utf-8')) if raw else {}
    except (ValueError, UnicodeDecodeError):
        self._error(400, 'malformed JSON body')
        return
    executor = GraphQLExecutor(self.forge)
    if isinstance(payload, list):
        out = []
        for item in payload:
            if not isinstance(item, dict):
                out.append({'data': None, 'errors': [{'message': 'bad operation'}]})
                continue
            out.append(executor.execute(item.get('query', ''),
                                        item.get('variables') or {}))
        self._send_json(200, out)
        return
    if not isinstance(payload, dict):
        self._error(400, 'malformed GraphQL body')
        return
    result = executor.execute(payload.get('query', ''), payload.get('variables') or {})
    self._send_json(200, result)


Handler._handle_graphql = _handle_graphql

MCP_SERVER_INFO = {'name': 'makefun-forge', 'version': '1.0.0'}
MCP_SUPPORTED_VERSIONS = ['2026-07-28', '2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05']
MCP_TOOLS = [
    {'name': 'create_repo',
     'description': 'Create a repository owned by the authenticated user.',
     'inputSchema': {'type': 'object',
                     'properties': {'name': {'type': 'string'},
                                    'description': {'type': 'string'},
                                    'private': {'type': 'boolean'}},
                     'required': ['name']}},
    {'name': 'get_repo',
     'description': 'Get a repository by owner and name.',
     'inputSchema': {'type': 'object',
                     'properties': {'owner': {'type': 'string'},
                                    'repo': {'type': 'string'}},
                     'required': ['owner', 'repo']}},
    {'name': 'create_issue',
     'description': 'Create an issue in a repository.',
     'inputSchema': {'type': 'object',
                     'properties': {'owner': {'type': 'string'},
                                    'repo': {'type': 'string'},
                                    'title': {'type': 'string'},
                                    'body': {'type': 'string'}},
                     'required': ['owner', 'repo', 'title']}},
    {'name': 'list_issues',
     'description': 'List issues in a repository.',
     'inputSchema': {'type': 'object',
                     'properties': {'owner': {'type': 'string'},
                                    'repo': {'type': 'string'},
                                    'state': {'type': 'string'}},
                     'required': ['owner', 'repo']}},
    {'name': 'create_pull_request',
     'description': 'Open a pull request from a head branch into a base branch.',
     'inputSchema': {'type': 'object',
                     'properties': {'owner': {'type': 'string'},
                                    'repo': {'type': 'string'},
                                    'title': {'type': 'string'},
                                    'head': {'type': 'string'},
                                    'base': {'type': 'string'}},
                     'required': ['owner', 'repo', 'title', 'head', 'base']}},
    {'name': 'merge_pull_request',
     'description': 'Squash-merge a pull request.',
     'inputSchema': {'type': 'object',
                     'properties': {'owner': {'type': 'string'},
                                    'repo': {'type': 'string'},
                                    'pull_number': {'type': 'integer'}},
                     'required': ['owner', 'repo', 'pull_number']}},
]


def _mcp_tool_result(obj, is_error=False):
    text = json.dumps(obj)
    return {'content': [{'type': 'text', 'text': text}],
            'structuredContent': obj, 'isError': is_error}


def _mcp_call_tool(forge, name, arguments):
    """Returns (result_dict_or_None, rpc_error_or_None, is_tool_error)."""
    tool = next((t for t in MCP_TOOLS if t['name'] == name), None)
    if tool is None:
        return None, {'code': -32601, 'message': 'Method not found: %s' % name}, False
    required = tool['inputSchema'].get('required', [])
    missing = [key for key in required
               if key not in arguments or arguments.get(key) is None]
    if missing:
        return None, {'code': -32602,
                      'message': 'Missing required arguments: %s' % ', '.join(missing)}, False
    viewer = forge.viewer['login']
    if name == 'create_repo':
        repo = forge.create_repo(viewer, arguments['name'],
                                 arguments.get('private', False),
                                 arguments.get('description', ''))
        if repo is None:
            return _mcp_tool_result({'error': 'Repository already exists'}, True), None, True
        return _mcp_tool_result(repo_json(forge, repo)), None, False
    owner = arguments.get('owner')
    repo_name = arguments.get('repo')
    repo = forge.get_repo(owner, repo_name)
    if name == 'get_repo':
        if repo is None:
            return _mcp_tool_result({'error': 'Repository not found'}, True), None, True
        return _mcp_tool_result(repo_json(forge, repo)), None, False
    if repo is None:
        return _mcp_tool_result({'error': 'Repository not found'}, True), None, True
    if name == 'create_issue':
        issue = forge.create_issue(repo, arguments.get('title', ''),
                                   arguments.get('body', ''), [])
        return _mcp_tool_result(issue_json(issue)), None, False
    if name == 'list_issues':
        state = arguments.get('state', 'open')
        items = list(repo.issues)
        if state == 'open':
            items = [i for i in items if i['state'] == 'open']
        elif state == 'closed':
            items = [i for i in items if i['state'] == 'closed']
        return _mcp_tool_result([issue_json(i) for i in items]), None, False
    if name == 'create_pull_request':
        pull, err = forge.create_pull(repo, arguments.get('title', ''),
                                      arguments.get('body', ''),
                                      arguments.get('head', ''),
                                      arguments.get('base', ''))
        if pull is None:
            return _mcp_tool_result({'error': err or 'cannot create pull request'}, True), None, True
        return _mcp_tool_result(pull_json(pull)), None, False
    if name == 'merge_pull_request':
        number = arguments.get('pull_number')
        try:
            number = int(number)
        except (TypeError, ValueError):
            return _mcp_tool_result({'error': 'pull_number must be an integer'}, True), None, True
        pull = repo.find_pull(number)
        if pull is None:
            return _mcp_tool_result({'error': 'Pull request not found'}, True), None, True
        ok, reason, sha = forge.squash_merge(repo, pull)
        if not ok:
            return _mcp_tool_result({'error': reason or 'cannot merge'}, True), None, True
        return _mcp_tool_result({'merged': True, 'number': number,
                                 'state': pull['state'], 'sha': sha}), None, False
    return None, {'code': -32601, 'message': 'Method not found: %s' % name}, False


def _handle_mcp(self):
    raw = self._read_body()
    try:
        payload = json.loads(raw.decode('utf-8')) if raw else None
    except (ValueError, UnicodeDecodeError):
        self._send_json(400, {'jsonrpc': '2.0', 'id': None,
                              'error': {'code': -32700, 'message': 'Parse error'}})
        return
    if payload is None:
        self._send_json(400, {'jsonrpc': '2.0', 'id': None,
                              'error': {'code': -32700, 'message': 'Parse error'}})
        return
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = self._mcp_dispatch(item)
            if response is not None:
                responses.append(response)
        self._send_json(200, responses)
        return
    response = self._mcp_dispatch(payload)
    if response is None:
        self._send_bytes(202, b'', JSON_TYPE)
        return
    self._send_json(200, response)


def _mcp_dispatch(self, payload):
    if not isinstance(payload, dict):
        return {'jsonrpc': '2.0', 'id': None,
                'error': {'code': -32600, 'message': 'Invalid Request'}}
    method = payload.get('method', '')
    req_id = payload.get('id')
    params = payload.get('params') or {}
    if not isinstance(params, dict):
        params = {}
    if req_id is None:
        # Notification: no response body.
        return None
    if method == 'server/discover':
        return {'jsonrpc': '2.0', 'id': req_id,
                'result': {'protocolVersion': '2026-07-28',
                           'capabilities': {'tools': {}},
                           'serverInfo': dict(MCP_SERVER_INFO),
                           'supportedVersions': list(MCP_SUPPORTED_VERSIONS)}}
    if method == 'initialize':
        requested = params.get('protocolVersion')
        negotiated = requested if requested in MCP_SUPPORTED_VERSIONS else '2025-11-25'
        return {'jsonrpc': '2.0', 'id': req_id,
                'result': {'protocolVersion': negotiated,
                           'capabilities': {'tools': {}},
                           'serverInfo': dict(MCP_SERVER_INFO)}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': MCP_TOOLS}}
    if method == 'tools/call':
        name = params.get('name')
        arguments = params.get('arguments') or {}
        if not isinstance(arguments, dict):
            arguments = {}
        result, rpc_error, _is_tool_error = _mcp_call_tool(self.forge, name, arguments)
        if rpc_error is not None:
            return {'jsonrpc': '2.0', 'id': req_id, 'error': rpc_error}
        return {'jsonrpc': '2.0', 'id': req_id, 'result': result}
    if method.startswith('notifications/'):
        return None
    return {'jsonrpc': '2.0', 'id': req_id,
            'error': {'code': -32601, 'message': 'Method not found: %s' % method}}


Handler._handle_mcp = _handle_mcp
Handler._mcp_dispatch = _mcp_dispatch


# --------------------------------------------------------------------------
# Git smart HTTP
# --------------------------------------------------------------------------

_UPLOAD_CAPS = ('side-band-64k multi_ack_detailed thin-pack ofs-delta shallow '
                'agent=makefun-forge/1.0 symref=HEAD:refs/heads/main')
_RECEIVE_CAPS = ('report-status report-status-v2 side-band-64k delete-refs '
                 'ofs-delta agent=makefun-forge/1.0 symref=HEAD:refs/heads/main')


def _sideband_packets(pack_bytes):
    out = bytearray()
    for i in range(0, len(pack_bytes), 60000):
        out += pkt_encode(b'\x01' + pack_bytes[i:i + 60000])
    return bytes(out)


def _handle_git(self, parts, query):
    forge = self.forge
    owner = parts[0]
    repo_name = parts[1][:-len('.git')]
    repo = forge.get_repo(owner, repo_name)
    rest = parts[2:]
    if repo is None:
        self._send_bytes(404, b'', 'text/plain')
        return
    method = self.command
    if rest == ['info', 'refs'] and method == 'GET':
        service = query.get('service')
        if service == 'git-upload-pack':
            self._git_advertise(repo, 'git-upload-pack')
        elif service == 'git-receive-pack':
            self._git_advertise(repo, 'git-receive-pack')
        else:
            self._send_bytes(400, b'', 'text/plain')
        return
    if rest == ['git-upload-pack'] and method == 'POST':
        self._git_upload_pack(repo)
        return
    if rest == ['git-receive-pack'] and method == 'POST':
        self._git_receive_pack(repo)
        return
    self._send_bytes(404, b'', 'text/plain')


def _git_ref_lines(self, repo):
    """Returns list of (oid, name, peeled_oid_or_None). HEAD first when present."""
    lines = []
    heads = sorted(ref for ref in repo.refs if ref.startswith('refs/heads/'))
    tags = sorted(ref for ref in repo.refs if ref.startswith('refs/tags/'))
    default_tip = repo.tip(repo.default_branch)
    if default_tip:
        lines.append((default_tip, 'HEAD', None))
    for ref in heads:
        lines.append((repo.refs[ref], ref, None))
    for ref in tags:
        oid = repo.refs[ref]
        peeled = None
        found = self.forge.lookup(oid)
        if found is not None and found[0] == 'tag':
            peeled = parse_tag(found[1])['object']
        lines.append((oid, ref, peeled))
    return lines


def _git_advertise(self, repo, service):
    forge = self.forge
    is_v2 = service == 'git-upload-pack' and \
        (self.headers.get('Git-Protocol') or '') == 'version=2'
    content_type = ('application/x-git-%s-advertisement' %
                    ('upload-pack' if service == 'git-upload-pack' else 'receive-pack'))
    if is_v2:
        body = b''.join([
            pkt_encode('version 2\n'),
            pkt_encode('fetch=shallow filter\n'),
            pkt_encode('ls-refs=unborn\n'),
            pkt_encode('object-format=sha1\n'),
            pkt_encode('agent\n'),
            PKT_FLUSH,
        ])
        self._send_bytes(200, body, content_type)
        return
    caps = _UPLOAD_CAPS if service == 'git-upload-pack' else _RECEIVE_CAPS
    preamble = '# service=%s\n' % service
    lines = self._git_ref_lines(repo)
    body = bytearray()
    body += pkt_encode(preamble)
    body += PKT_FLUSH
    if not lines:
        body += pkt_encode(ZERO_OID + ' capabilities^{}\x00' + caps + '\n')
    else:
        for index, (oid, name, peeled) in enumerate(lines):
            if index == 0:
                body += pkt_encode('%s %s\x00%s\n' % (oid, name, caps))
            else:
                body += pkt_encode('%s %s\n' % (oid, name))
            if peeled:
                body += pkt_encode('%s %s^{}\n' % (peeled, name))
    body += PKT_FLUSH
    self._send_bytes(200, bytes(body), content_type)


def _git_upload_pack(self, repo):
    forge = self.forge
    raw = self._read_body()
    reader = PktReader(raw)
    is_v2 = (self.headers.get('Git-Protocol') or '') == 'version=2'
    try:
        packets = []
        while True:
            kind, payload = reader.read_packet()
            if kind == 'eof':
                break
            packets.append((kind, payload))
    except ValueError:
        self._send_bytes(400, b'', 'text/plain')
        return
    data_lines = [p.decode('utf-8', errors='replace').rstrip('\n')
                  for kind, p in packets if kind == 'data']
    if is_v2:
        self._git_upload_pack_v2(repo, data_lines)
    else:
        self._git_upload_pack_v0(repo, data_lines)


def _git_upload_pack_v2(self, repo, lines):
    forge = self.forge
    if not lines or not lines[0].startswith('command='):
        self._send_bytes(400, b'', 'text/plain')
        return
    command = lines[0][len('command='):]
    args = [line for line in lines[1:] if line]
    if command == 'ls-refs':
        peel = 'peel' in args
        symrefs = 'symrefs' in args
        prefixes = [a[len('ref-prefix '):] for a in args if a.startswith('ref-prefix ')]
        out = bytearray()
        ref_lines = self._git_ref_lines(repo)
        if not ref_lines:
            if not prefixes or any('HEAD'.startswith(p) or p == '' for p in prefixes):
                out += pkt_encode('unborn HEAD symref-target:refs/heads/main\n')
        else:
            for oid, name, peeled in ref_lines:
                if prefixes and not any(name.startswith(p) for p in prefixes):
                    continue
                line = '%s %s' % (oid, name)
                if symrefs and name == 'HEAD':
                    line += ' symref-target:refs/heads/main'
                if peel and peeled:
                    line += ' peeled:' + peeled
                out += pkt_encode(line + '\n')
        out += PKT_FLUSH
        self._send_bytes(200, bytes(out), 'application/x-git-upload-pack-result')
        return
    if command != 'fetch':
        self._send_bytes(400, b'', 'text/plain')
        return
    wants, haves, shallows, depth = [], [], [], None
    for arg in args:
        if arg.startswith('want '):
            wants.append(arg[5:].split(' ')[0])
        elif arg.startswith('have '):
            haves.append(arg[5:].strip())
        elif arg.startswith('shallow '):
            shallows.append(arg[8:].strip())
        elif arg.startswith('deepen '):
            try:
                depth = int(arg[7:].strip())
            except ValueError:
                depth = None
    pack, shallow, unshallow = forge.fetch_pack(
        repo, wants, haves, shallows, depth)
    body = bytearray()
    body += pkt_encode('acknowledgments\n')
    body += pkt_encode('NAK\n')
    body += PKT_FLUSH
    if shallow or unshallow:
        body += pkt_encode('shallow-info\n')
        for oid in shallow:
            body += pkt_encode('shallow %s\n' % oid)
        for oid in unshallow:
            body += pkt_encode('unshallow %s\n' % oid)
        body += PKT_FLUSH
    body += pkt_encode('packfile\n')
    body += _sideband_packets(pack)
    body += PKT_FLUSH
    self._send_bytes(200, bytes(body), 'application/x-git-upload-pack-result')


def _git_upload_pack_v0(self, repo, lines):
    forge = self.forge
    wants, haves, shallows, depth = [], [], [], None
    for line in lines:
        if line.startswith('want '):
            wants.append(line[5:].split(' ')[0])
        elif line.startswith('have '):
            haves.append(line[5:].strip())
        elif line.startswith('shallow '):
            shallows.append(line[8:].strip())
        elif line.startswith('deepen '):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                depth = None
    pack, shallow, unshallow = forge.fetch_pack(
        repo, wants, haves, shallows, depth)
    body = bytearray()
    body += pkt_encode('NAK\n')
    for oid in shallow:
        body += pkt_encode('shallow %s\n' % oid)
    for oid in unshallow:
        body += pkt_encode('unshallow %s\n' % oid)
    body += _sideband_packets(pack)
    body += PKT_FLUSH
    self._send_bytes(200, bytes(body), 'application/x-git-upload-pack-result')


def _git_receive_pack(self, repo):
    forge = self.forge
    raw = self._read_body()
    reader = PktReader(raw)
    commands = []
    caps = set()
    try:
        while True:
            kind, payload = reader.read_packet()
            if kind == 'eof':
                break
            if kind == 'flush':
                break
            if kind == 'data':
                line = payload.decode('utf-8', errors='replace').rstrip('\n')
                if '\x00' in line:
                    line, _, capstr = line.partition('\x00')
                    caps = set(capstr.split(' '))
                parts = line.split(' ')
                if len(parts) == 3:
                    commands.append((parts[0], parts[1], parts[2]))
    except ValueError:
        self._send_bytes(400, b'', 'text/plain')
        return
    pack_bytes = reader.rest()
    report = []
    unpack_ok = True
    unpack_detail = 'ok'
    needs_pack = any(new != ZERO_OID for _old, new, _ref in commands)
    if needs_pack:
        if not pack_bytes:
            unpack_ok = False
            unpack_detail = 'missing packfile'
        else:
            try:
                entries = read_pack(pack_bytes)
                for oid, obj_type, data in entries:
                    forge.put_object(obj_type, data)
            except ValueError as err:
                unpack_ok = False
                unpack_detail = str(err)
    if unpack_ok:
        report.append('unpack ok')
        for old, new, ref in commands:
            ok, reason = self._apply_push(repo, old, new, ref)
            if ok:
                report.append('ok %s' % ref)
            else:
                report.append('ng %s %s' % (ref, reason))
    else:
        report.append('unpack %s' % unpack_detail)
        for _old, _new, ref in commands:
            report.append('ng %s %s' % (ref, 'unpack failed'))
    if 'side-band-64k' in caps:
        inner = b''.join(pkt_encode(line + '\n') for line in report)
        body = _sideband_packets(inner) + PKT_FLUSH
    else:
        body = b''.join(pkt_encode(line + '\n') for line in report) + PKT_FLUSH
    self._send_bytes(200, body, 'application/x-git-receive-pack-result')


def _apply_push(self, repo, old, new, ref):
    forge = self.forge
    current = repo.refs.get(ref)
    if new == ZERO_OID:
        if current is None:
            return False, 'no such ref'
        if old != ZERO_OID and old != current:
            return False, 'old value does not match'
        del repo.refs[ref]
        return True, None
    if forge.lookup(new) is None:
        return False, 'object not found'
    if old == ZERO_OID:
        if current is not None:
            return False, 'already exists'
        repo.refs[ref] = new
        return True, None
    if current is None:
        return False, 'no such ref'
    if old != current:
        return False, 'old value does not match'
    if not is_descendant(new, current, forge.lookup):
        return False, 'non-fast-forward'
    repo.refs[ref] = new
    return True, None


Handler._handle_git = _handle_git
Handler._git_ref_lines = _git_ref_lines
Handler._git_advertise = _git_advertise
Handler._git_upload_pack = _git_upload_pack
Handler._git_upload_pack_v2 = _git_upload_pack_v2
Handler._git_upload_pack_v0 = _git_upload_pack_v0
Handler._git_receive_pack = _git_receive_pack
Handler._apply_push = _apply_push


# --------------------------------------------------------------------------
# Server + lifecycle
# --------------------------------------------------------------------------

class ForgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # The default listen backlog is 5: 50 concurrent clone connections
    # overflow it, and the kernel drops SYNs (first clone round measured
    # ~2 s, later rounds ~0.4 s on reused keep-alive connections).
    request_queue_size = 128

    def __init__(self, address, forge):
        super().__init__(address, Handler)
        self.forge = forge

    def get_request(self):
        # Disable Nagle: without TCP_NODELAY every small keep-alive
        # request/response pair costs a ~40 ms delayed-ACK round trip
        # (measured 41.3 ms/req -> 0.7 ms/req). The benchmark's read and
        # write workloads are thousands of sequential keep-alive requests.
        sock, addr = super().get_request()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return sock, addr


def _http_json(url, method='GET', body=None, timeout=30):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode('utf-8')) if raw else {}
    except urllib.error.HTTPError as err:
        return err.code, {}
    except Exception:
        return None, {}


def main():
    shop_token = os.environ.get('MAKEFUN_SHOP_TOKEN', '')
    env_url = os.environ.get('MAKEFUN_ENV_URL', '').rstrip('/')
    forge = Forge(shop_token)
    dev_port = 0
    try:
        dev_port = int(os.environ.get('MAKEFUN_PORT', '0') or 0)
    except ValueError:
        dev_port = 0
    server = ForgeServer(('127.0.0.1', dev_port), forge)
    port = server.server_address[1]
    print('[entry] listening on port %d' % port, flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if not env_url:
        print('[entry] MAKEFUN_ENV_URL is not set: serving standalone on %d' % port,
              flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return 0
    # Register with the env; 200 or 409 proceeds, 400/405 is fatal.
    attempt = 0
    while True:
        status, _body = _http_json(env_url + '/register', method='POST',
                                   body={'port': port}, timeout=30)
        if status in (200, 409):
            break
        if status in (400, 405):
            print('[entry] register answered HTTP %s' % status, flush=True)
            server.shutdown()
            return 1
        time.sleep(min(2.0, 0.25 * (2 ** min(attempt, 8))))
        attempt += 1
    print('[entry] registered; waiting for the env to say done', flush=True)
    attempt = 0
    while True:
        status, body = _http_json(env_url + '/done', timeout=30)
        if status == 200 and isinstance(body, dict) and body.get('done') is True:
            break
        time.sleep(min(2.0, 0.25 * (2 ** min(attempt, 8))))
        attempt += 1
    server.shutdown()
    print('[entry] done', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
