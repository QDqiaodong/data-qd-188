"""离线编制“下一手”训练集与盲评集的分区计划（go-record-dataset）。

只读本批 go-record 原始输入 JSON 与管理员给的已发布历史清单，输出供数据管理员
复核的分区计划；不向外部系统发布数据，也不修改任何棋谱或归档。

- 样本特征取目标手之前的盘面与当时行棋方，答案取该手在原方向的落子点；
  合法回放沿用 service.process 的 v1 规则（提子、简单劫、停手、双停）。
- 局面键由盘宽、行棋方与棋盘在正方形八面体变换下的字典序最小行序列组成；
  不交换黑白，不含答案、贴目、提子数或劫历史。
- 同来源系列或同局面键的有效样本必须同组（关系可传递）；历史发布记录给
  来源系列与局面键留下分区约束，命中两侧的组整组隔离。
- 盲评配额按黑白先分别精确满足；未锁组按组标识字典序优先选择进入盲评。
"""
import os

from . import service
from .board import rows as board_rows
from .checks import array, fields, ident, integer
from .io import load
from .model import setup_board

SCHEMA = 'go-record-dataset/1'
MAX_RECORDS = 256
MAX_SOURCE_GROUPS = 40
MAX_SAMPLES = 4096
MAX_HISTORY = 8192
MAX_QUOTA = 512
SPLITS = ('train', 'holdout')


class Reject(Exception):
    """输入错误：CLI 退出 2，diagnostic() 给出可定位信息，不落报告。"""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def diagnostic(self):
        return {'error': {'code': self.code, 'message': str(self), **self.details}}


def _entry_error(prefix, index, error, **identity):
    details = {f'{prefix}_index': index}
    details.update(identity)
    return Reject('invalid_structure', f'{prefix}[{index}]: {error}', **details)


# ---------- 正方形八面体变换与局面键 ----------

_TRANSFORMS = (
    lambda r, c, n: (r, c),
    lambda r, c, n: (r, n - 1 - c),
    lambda r, c, n: (n - 1 - r, c),
    lambda r, c, n: (n - 1 - r, n - 1 - c),
    lambda r, c, n: (c, r),
    lambda r, c, n: (c, n - 1 - r),
    lambda r, c, n: (n - 1 - c, r),
    lambda r, c, n: (n - 1 - c, n - 1 - r),
)


def canonical_rows(rows):
    """八种旋转/镜像中行字符串序列字典序最小者；不交换黑白。"""
    n = len(rows)
    best = None
    for transform in _TRANSFORMS:
        grid = [['.'] * n for _ in range(n)]
        for r in range(n):
            for c in range(n):
                rr, cc = transform(r, c, n)
                grid[rr][cc] = rows[r][c]
        candidate = tuple(''.join(row) for row in grid)
        if best is None or candidate < best:
            best = candidate
    return best


def position_key(size, player, rows):
    """局面键：盘宽、行棋方、规范行序列；行棋方不同即不同键。"""
    return (size, player, canonical_rows(rows))


def key_rows(key):
    return list(key[2])


def key_text(key):
    """诊断用的可读局面键。"""
    return f'{key[0]}:{key[1]}:' + '/'.join(key[2])


# ---------- 清单校验 ----------

def validate_manifest(manifest):
    """严格校验清单并返回规范化结构；结构问题抛 Reject。"""
    try:
        fields(manifest, ('batch_id', 'records', 'samples', 'history', 'holdout_quota'))
        ident(manifest['batch_id'])
        records_raw = array(manifest['records'], 1, MAX_RECORDS)
        samples_raw = array(manifest['samples'], 1, MAX_SAMPLES)
        history_raw = array(manifest['history'], 0, MAX_HISTORY)
        quota = fields(manifest['holdout_quota'], ('B', 'W'))
        quota_b = integer(quota['B'], 0, MAX_QUOTA)
        quota_w = integer(quota['W'], 0, MAX_QUOTA)
    except ValueError as error:
        raise Reject('invalid_structure', f'manifest: {error}')

    records = []
    record_ids = set()
    source_groups = set()
    for index, entry in enumerate(records_raw):
        try:
            fields(entry, ('record_id', 'source_group', 'path'))
            ident(entry['record_id'])
            ident(entry['source_group'])
            if not isinstance(entry['path'], str) or not entry['path']:
                raise ValueError('path: non-empty string required')
        except ValueError as error:
            raise _entry_error('records', index, error)
        if entry['record_id'] in record_ids:
            raise _entry_error('records', index, 'duplicate record_id', record_id=entry['record_id'])
        record_ids.add(entry['record_id'])
        source_groups.add(entry['source_group'])
        records.append({'record_id': entry['record_id'], 'source_group': entry['source_group'],
                        'path': entry['path']})
    if len(source_groups) > MAX_SOURCE_GROUPS:
        raise Reject('invalid_structure',
                     f'manifest: {len(source_groups)} source groups; at most {MAX_SOURCE_GROUPS}',
                     source_group_count=len(source_groups))
    group_of = {entry['record_id']: entry['source_group'] for entry in records}

    samples = []
    sample_ids = set()
    sample_pairs = set()
    for index, entry in enumerate(samples_raw):
        try:
            fields(entry, ('sample_id', 'record_id', 'ply'))
            ident(entry['sample_id'])
            ident(entry['record_id'])
            ply = integer(entry['ply'], 1, 2000)
        except ValueError as error:
            raise _entry_error('samples', index, error)
        if entry['sample_id'] in sample_ids:
            raise _entry_error('samples', index, 'duplicate sample_id',
                               sample_id=entry['sample_id'])
        sample_ids.add(entry['sample_id'])
        if entry['record_id'] not in group_of:
            raise _entry_error('samples', index, 'record_id not declared in records',
                               sample_id=entry['sample_id'], record_id=entry['record_id'])
        pair = (entry['record_id'], ply)
        if pair in sample_pairs:
            raise _entry_error('samples', index, 'duplicate (record_id, ply)',
                               sample_id=entry['sample_id'], record_id=entry['record_id'],
                               ply=ply)
        sample_pairs.add(pair)
        samples.append({'sample_id': entry['sample_id'], 'record_id': entry['record_id'],
                        'ply': ply})

    history = []
    release_pairs = set()
    batch_id = manifest['batch_id']
    for index, entry in enumerate(history_raw):
        try:
            fields(entry, ('release_id', 'sample_id', 'source_group', 'size', 'board',
                           'next_player', 'split'))
            ident(entry['release_id'])
            ident(entry['sample_id'])
            ident(entry['source_group'])
            size = integer(entry['size'], 3, 19)
            board = entry['board']
            if not isinstance(board, list) or len(board) != size:
                raise ValueError('board: must have exactly size rows')
            for row in board:
                if not isinstance(row, str) or len(row) != size or set(row) - set('.BW'):
                    raise ValueError('board: rows must be size-length strings of .BW')
            player = entry['next_player']
            if player not in ('B', 'W'):
                raise ValueError('next_player: B/W required')
            split = entry['split']
            if split not in SPLITS:
                raise ValueError('split: train/holdout required')
            if entry['release_id'] == batch_id:
                raise ValueError('release_id must differ from this batch_id')
        except ValueError as error:
            raise _entry_error('history', index, error)
        pair = (entry['release_id'], entry['sample_id'])
        if pair in release_pairs:
            raise _entry_error('history', index, 'duplicate (release_id, sample_id)',
                               release_id=entry['release_id'], sample_id=entry['sample_id'])
        release_pairs.add(pair)
        rows = tuple(board)
        history.append({'index': index, 'release_id': entry['release_id'],
                        'sample_id': entry['sample_id'], 'source_group': entry['source_group'],
                        'size': size, 'rows': rows, 'next_player': player, 'split': split,
                        'key': position_key(size, player, rows)})

    return {
        'batch_id': batch_id,
        'records': records,
        'samples': samples,
        'history': history,
        'quota': {'B': quota_b, 'W': quota_w},
    }


# ---------- 合法回放与取样 ----------

def _initial_rows(data):
    board, _komi = setup_board(data)
    return board_rows(board, data['size'])


def read_record(record, manifest_dir):
    """读取并回放单盘记录；不可读/结构无效是输入错误，不是业务排除。"""
    path = os.path.join(manifest_dir, record['path'])
    record_id = record['record_id']
    try:
        data = load(path)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        raise Reject('record_unreadable', f'record {record_id}: cannot read {record["path"]}: {error}',
                     record_id=record_id, path=record['path'])
    try:
        result = service.process(data)
    except (ValueError, TypeError, KeyError, IndexError) as error:
        raise Reject('invalid_record', f'record {record_id}: {error}',
                     record_id=record_id, path=record['path'])
    return data, result


def extract_samples(m, manifest_dir):
    """按记录逐个取证；返回 (valid_samples, exclusions)。顺序无关。"""
    records = sorted(m['records'], key=lambda entry: entry['record_id'])
    by_record = {}
    for record in records:
        data, result = read_record(record, manifest_dir)
        by_record[record['record_id']] = {
            'record': record,
            'data': data,
            'result': result,
            'initial': _initial_rows(data),
        }

    valid = []
    exclusions = []
    for sample in sorted(m['samples'], key=lambda entry: entry['sample_id']):
        held = by_record[sample['record_id']]
        data = held['data']
        result = held['result']
        record = held['record']
        ply = sample['ply']
        sample_id = sample['sample_id']
        if ply > len(data['moves']):
            raise Reject('invalid_structure',
                         f'samples: sample {sample_id}: ply {ply} exceeds record moves',
                         sample_id=sample_id, record_id=record['record_id'], ply=ply)
        trace = result['trace']
        illegal = result['first_illegal']
        blocked = illegal is not None and ply >= illegal['index'] + 1
        if blocked:
            exclusions.append({
                'sample_id': sample_id, 'record_id': record['record_id'], 'ply': ply,
                'reason': 'illegal_move',
                'blocking_move': {
                    'ply': illegal['index'] + 1,
                    'move_id': illegal['move_id'],
                    'reason': illegal['reason'],
                },
            })
            continue
        move = trace[ply - 1]
        if move['point'] is None:
            exclusions.append({
                'sample_id': sample_id, 'record_id': record['record_id'], 'ply': ply,
                'reason': 'pass', 'move_id': move['move_id'],
                'blocking_move': None,
            })
            continue
        feature_rows = held['initial'] if ply == 1 else tuple(trace[ply - 2]['board'])
        next_player = move['player']
        key = position_key(data['size'], next_player, feature_rows)
        valid.append({
            'sample_id': sample_id,
            'record_id': record['record_id'],
            'source_group': record['source_group'],
            'ply': ply,
            'move_id': move['move_id'],
            'next_player': next_player,
            'size': data['size'],
            'board': list(feature_rows),
            'answer': list(move['point']),
            'key': key,
        })
    return valid, exclusions


# ---------- 分组与历史约束 ----------

class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, item):
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left, right):
        left, right = self.find(left), self.find(right)
        if left != right:
            # 固定以较小的 sample_id 为根，纯为保证确定性。
            if right < left:
                left, right = right, left
            self.parent[right] = left


def build_groups(valid):
    """同来源系列或同局面键的有效样本并组（可传递）。返回按组标识排序的组列表。"""
    uf = _UnionFind()
    first_by_source = {}
    first_by_key = {}
    for sample in valid:
        uf.find(sample['sample_id'])
        source = sample['source_group']
        if source in first_by_source:
            uf.union(first_by_source[source], sample['sample_id'])
        else:
            first_by_source[source] = sample['sample_id']
        key = sample['key']
        if key in first_by_key:
            uf.union(first_by_key[key], sample['sample_id'])
        else:
            first_by_key[key] = sample['sample_id']

    buckets = {}
    for sample in valid:
        buckets.setdefault(uf.find(sample['sample_id']), []).append(sample)

    groups = []
    for members in buckets.values():
        members.sort(key=lambda entry: entry['sample_id'])
        group_id = min(member['source_group'] for member in members)
        counts = {'B': 0, 'W': 0}
        for member in members:
            counts[member['next_player']] += 1
        keys = sorted({member['key'] for member in members})
        groups.append({
            'group_id': group_id,
            'source_groups': sorted({member['source_group'] for member in members}),
            'members': members,
            'counts': counts,
            'keys': keys,
        })
    groups.sort(key=lambda group: group['group_id'])
    return groups


def history_constraints(m, groups):
    """给每个组计算历史分区约束；返回 (per-group constraints, 历史匹配索引)。"""
    by_source = {}
    by_key = {}
    for row in m['history']:
        by_source.setdefault(row['source_group'], []).append(row)
        by_key.setdefault(row['key'], []).append(row)

    for group in groups:
        evidence = []
        splits = set()
        for source in group['source_groups']:
            for row in by_source.get(source, ()):
                splits.add(row['split'])
                evidence.append({'kind': 'source_group', 'value': source,
                                 'split': row['split'],
                                 'history': {'release_id': row['release_id'],
                                             'sample_id': row['sample_id'],
                                             'source_group': row['source_group'],
                                             'split': row['split']}})
        for key in group['keys']:
            for row in by_key.get(key, ()):
                splits.add(row['split'])
                evidence.append({'kind': 'position_key', 'value': key_text(key),
                                 'split': row['split'],
                                 'history': {'release_id': row['release_id'],
                                             'sample_id': row['sample_id'],
                                             'source_group': row['source_group'],
                                             'split': row['split']}})
        evidence.sort(key=lambda item: (item['kind'], item['value'], item['split'],
                                        item['history']['release_id'],
                                        item['history']['sample_id']))
        if splits == {'train', 'holdout'}:
            constraint = 'isolated'
        elif splits == {'train'}:
            constraint = 'train'
        elif splits == {'holdout'}:
            constraint = 'holdout'
        else:
            constraint = None
        group['constraint'] = constraint
        group['evidence'] = evidence
    return groups


# ---------- 联合配额选择 ----------

_BLOCK = 32


class _Reach:
    """二维精确子集和：b*Wlen+w 编码为一个 Python 大整数。"""

    def __init__(self, qb, qw):
        self.qb = qb
        self.qw = qw
        self.wlen = 2 * (qw + 1)  # 留出空列，禁止 w 进位串到下一行
        mask = 0
        row = (1 << (qw + 1)) - 1
        for b in range(qb + 1):
            mask |= row << (b * self.wlen)
        self.mask = mask
        self.zero = 1  # (0,0)

    def add(self, reachable, cb, cw):
        if cb > self.qb or cw > self.qw:
            return reachable
        return reachable | ((reachable << (cb * self.wlen + cw)) & self.mask)

    def has(self, reachable, b, w):
        if b < 0 or w < 0 or b > self.qb or w > self.qw:
            return False
        return (reachable >> (b * self.wlen + w)) & 1 == 1


def select_holdout(free, qb, qw):
    """在 free（按组标识排序）中精确凑出 (qb,qw)；返回字典序最小的选择或 None。

    字典序以未锁组的组标识顺序比较，盲评（holdout）优先于训练（train）。
    """
    if qb < 0 or qw < 0:
        return None
    reach = _Reach(qb, qw)
    n = len(free)
    counts = [(group['counts']['B'], group['counts']['W']) for group in free]

    checkpoints = {n: reach.zero}
    current = reach.zero
    for index in range(n - 1, -1, -1):
        current = reach.add(current, *counts[index])
        if index % _BLOCK == 0:
            checkpoints[index] = current
    if not reach.has(current, qb, qw):
        return None

    decisions = [None] * n
    rem_b, rem_w = qb, qw
    for start in range(0, n, _BLOCK):
        end = min(start + _BLOCK, n)
        suffix = {end: checkpoints[end]}
        current = checkpoints[end]
        for index in range(end - 1, start - 1, -1):
            current = reach.add(current, *counts[index])
            suffix[index] = current
        for index in range(start, end):
            cb, cw = counts[index]
            if reach.has(suffix[index + 1], rem_b - cb, rem_w - cw):
                decisions[index] = 'holdout'
                rem_b -= cb
                rem_w -= cw
            else:
                decisions[index] = 'train'
    if rem_b != 0 or rem_w != 0:
        raise AssertionError('selection lost exact quota after feasibility')
    return decisions


# ---------- 报告 ----------

def _sample_report(sample, group_id):
    return {
        'sample_id': sample['sample_id'],
        'record_id': sample['record_id'],
        'source_group': sample['source_group'],
        'ply': sample['ply'],
        'move_id': sample['move_id'],
        'next_player': sample['next_player'],
        'size': sample['size'],
        'board': sample['board'],
        'answer': sample['answer'],
        'position_key': {'size': sample['key'][0], 'next_player': sample['key'][1],
                         'board': key_rows(sample['key'])},
        'group_id': group_id,
    }


def _group_report(group):
    return {
        'group_id': group['group_id'],
        'source_groups': group['source_groups'],
        'sample_ids': [member['sample_id'] for member in group['members']],
        'counts': {'B': group['counts']['B'], 'W': group['counts']['W']},
        'constraint': group['constraint'],
        'position_keys': [{'size': key[0], 'next_player': key[1], 'board': key_rows(key)}
                          for key in group['keys']],
    }


def _isolation_report(group):
    return {
        'group_id': group['group_id'],
        'source_groups': group['source_groups'],
        'sample_ids': [member['sample_id'] for member in group['members']],
        'counts': {'B': group['counts']['B'], 'W': group['counts']['W']},
        'matched_keys': [
            {'kind': item['kind'], 'value': item['value'], 'split': item['split'],
             'history': item['history']}
            for item in group['evidence']
        ],
    }


def _publication(batch_id, selected, split):
    return [{
        'release_id': batch_id,
        'sample_id': sample['sample_id'],
        'source_group': sample['source_group'],
        'size': sample['size'],
        'board': list(sample['board']),
        'next_player': sample['next_player'],
        'split': split,
    } for sample in selected]


def build_report(m, manifest_dir):
    """校验、取证、分组、隔离并给出 ready 或 quota_unreachable 报告。"""
    valid, exclusions = extract_samples(m, manifest_dir)
    groups = history_constraints(m, build_groups(valid))

    isolated = [group for group in groups if group['constraint'] == 'isolated']
    locked = [group for group in groups if group['constraint'] in SPLITS]
    free = [group for group in groups if group['constraint'] is None]
    free.sort(key=lambda group: group['group_id'])

    locked_holdout = {'B': 0, 'W': 0}
    for group in locked:
        if group['constraint'] == 'holdout':
            locked_holdout['B'] += group['counts']['B']
            locked_holdout['W'] += group['counts']['W']
    target = {'B': m['quota']['B'], 'W': m['quota']['W']}

    def grouping_report():
        return {
            'groups': [_group_report(group) for group in groups],
            'isolated': [_isolation_report(group) for group in isolated],
        }

    base = {
        'schema': SCHEMA,
        'batch_id': m['batch_id'],
        'targets': target,
        'exclusions': exclusions,
        'grouping': grouping_report(),
    }

    decisions = select_holdout(free, target['B'] - locked_holdout['B'],
                               target['W'] - locked_holdout['W'])
    if decisions is None:
        return {
            **base,
            'status': 'quota_unreachable',
            'diagnostic': {
                'valid_sample_count': len(valid),
                'group_count': len(groups),
                'free_groups': [{'group_id': group['group_id'], 'counts': group['counts']}
                                for group in free],
                'isolated_group_ids': [group['group_id'] for group in isolated],
                'locked_holdout_counts': locked_holdout,
                'locked_holdout_sample_count': sum(
                    len(group['members']) for group in locked
                    if group['constraint'] == 'holdout'),
                'locked_train_sample_count': sum(
                    len(group['members']) for group in locked
                    if group['constraint'] == 'train'),
                'remaining_targets': {'B': target['B'] - locked_holdout['B'],
                                      'W': target['W'] - locked_holdout['W']},
            },
            'splits': None,
            'publication': None,
        }

    for group, decision in zip(free, decisions):
        group['constraint'] = decision

    def side(split):
        chosen = []
        for group in groups:
            if group['constraint'] != split:
                continue
            for member in group['members']:
                chosen.append((member, group['group_id']))
        chosen.sort(key=lambda item: item[0]['sample_id'])
        return chosen

    train = side('train')
    holdout = side('holdout')
    if (sum(1 for sample, _ in holdout if sample['next_player'] == 'B') != target['B']
            or sum(1 for sample, _ in holdout if sample['next_player'] == 'W') != target['W']):
        raise AssertionError('holdout color counts do not match target')

    def side_report(chosen):
        return {
            'sample_count': len(chosen),
            'counts': {'B': sum(1 for sample, _ in chosen if sample['next_player'] == 'B'),
                       'W': sum(1 for sample, _ in chosen if sample['next_player'] == 'W')},
            'samples': [_sample_report(sample, group_id) for sample, group_id in chosen],
        }

    return {
        **base,
        'status': 'ready',
        'grouping': grouping_report(),
        'splits': {'train': side_report(train), 'holdout': side_report(holdout)},
        'publication': {
            'train': _publication(m['batch_id'], [s for s, _ in train], 'train'),
            'holdout': _publication(m['batch_id'], [s for s, _ in holdout], 'holdout'),
        },
    }
