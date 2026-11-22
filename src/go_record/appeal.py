"""离线终局死子申诉案卷：每个本地案卷目录管理一盘棋。

案卷只从一份已最终封存（合法双停、封到声明末手）的 go-record-archive 归档开案，
开案时固定该盘的末手回执 ID、原方向终局盘面、盘宽、贴目与原面积胜负；此后录入、
裁定、重开与历史核查都不再读取原归档——归档移到冷库后案卷仍可独立工作。案卷不
复制整份归档、不要求已被清理的棋步、不回写原归档。

裁判意见是一组待移除棋块，以开案原盘面上同色四邻连通棋块（board.group）内任一
棋子坐标代表；结案时先从开案盘面同时移去全部确认棋块，再用 regions.area 重新算
整盘。工具不自行判断死活，不改变落子、劫、贴目或面积规则，移除不是走棋或提子，
不额外加俘虏分。

持久化：案卷是单个 case.json，投票、结案、重开都在一次原子替换中提交，并在同一
把 flock 下完成校验与提交；异常中断并重开只能读到完整的变更前或变更后状态。
两个携带同一预期版本的写入不可能都成功。仅用标准库。
"""
import copy
import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager

from . import archive
from .board import group as board_group
from .board import rows as board_rows
from .checks import array, decimal, fields, ident, integer
from .io import load
from .regions import area

SCHEMA = 'go-record-appeal/1'
STATE_FILE = 'case.json'
LOCK_FILE = 'lock'
MAX_REVISION = 2147483647
FAULT_ENV = 'GO_RECORD_APPEAL_FAULT'
FAULT_POINTS = ('commit_before_replace', 'commit_after_replace')
WINNERS = ('B', 'W', 'draw')

# 测试用故障注入钩子：在提交边界回调，可抛出异常模拟受控中断。
_FAULT_HOOK = None


def _fault(point):
    hook = _FAULT_HOOK
    if hook is not None:
        hook(point)
    if os.environ.get(FAULT_ENV) == point:
        raise RuntimeError(f'injected fault at {point}')


class Reject(Exception):
    """业务拒绝：CLI 退出 2，diagnostic() 给出可定位信息。"""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def diagnostic(self):
        return {'error': {'code': self.code, 'message': str(self), **self.details}}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _dump(state):
    return (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + '\n').encode('utf-8')


def _case_path(case_dir):
    return os.path.join(case_dir, STATE_FILE)


def _parse_board(board_rows):
    board = {}
    for r, row in enumerate(board_rows):
        for c, mark in enumerate(row):
            if mark in 'BW':
                board[(r, c)] = mark
    return board


# ---------- 开案取证 ----------

def _final_evidence(arc, game_id):
    """核对归档的终局证据，返回 (末手回执 ID, 声明总手数)；不合格即 Reject。"""
    total = arc.get('total_moves')
    sealed = arc.get('sealed_ply')
    receipts = arc.get('receipts')
    final = arc.get('final')
    if type(total) is not int or type(sealed) is not int or not isinstance(receipts, dict):
        raise Reject('archive_evidence_invalid', 'archive state missing core fields',
                     game_id=game_id)
    problem = None
    if final is None:
        problem = 'archive has no final seal result'
    elif not isinstance(final, dict) or final.get('sealed_ply') != total or sealed != total:
        problem = 'final result is not sealed at the declared last move'
    final_area = final.get('area') if isinstance(final, dict) else None
    winner = final_area.get('winner') if isinstance(final_area, dict) else None
    if problem is None and (winner not in WINNERS
                            or final_area.get('status') != 'after_two_passes'):
        problem = 'final result has no valid winner'
    final_receipts = [receipt for receipt in receipts.values()
                      if isinstance(receipt, dict) and receipt.get('game_id') == game_id
                      and receipt.get('sealed_ply') == total]
    receipt_id = final_receipts[0].get('receipt_id') if len(final_receipts) == 1 else None
    if problem is None and not isinstance(receipt_id, str):
        problem = 'final result lacks exactly one last-move seal receipt'
    if problem is not None:
        raise Reject('archive_evidence_invalid', f'archive {game_id}: {problem}',
                     game_id=game_id)
    return receipt_id, total, final_area


def open_case(case_dir, spec):
    """按 spec 开案：只接受合法双停、封到声明末手的归档；不覆盖已有案卷。"""
    try:
        fields(spec, ('case_ref', 'game_id', 'archive_dir', 'referees'))
        case_ref = ident(spec['case_ref'])
        game_id = ident(spec['game_id'])
        if not isinstance(spec['archive_dir'], str) or not spec['archive_dir']:
            raise ValueError('archive_dir: non-empty string required')
        referees = array(spec['referees'], 2, 2)
        for referee in referees:
            ident(referee)
        if len(set(referees)) != 2:
            raise ValueError('referees: two distinct referee ids required')
    except ValueError as error:
        raise Reject('invalid_structure', f'open spec: {error}')
    archive_dir = spec['archive_dir']
    try:
        arc = archive.load_state(archive_dir)
    except (archive.Reject, OSError) as error:
        raise Reject('archive_unreadable', f'case {case_ref}: {error}',
                     case_ref=case_ref, game_id=game_id, archive_dir=archive_dir)
    if arc.get('game_id') != game_id:
        raise Reject('game_id_mismatch',
                     f'case {case_ref}: archive holds game {arc.get("game_id")!r}',
                     case_ref=case_ref, game_id=game_id, found=arc.get('game_id'))
    receipt_id, total, final_area = _final_evidence(arc, game_id)
    size = arc.get('size')
    komi = arc.get('komi')
    board_rows = arc.get('boundary', {}).get('board') if isinstance(arc.get('boundary'), dict) \
        else None
    if type(size) is not int or not isinstance(komi, str) or not isinstance(board_rows, list) \
            or len(board_rows) != size or any(not isinstance(row, str) or len(row) != size
                                              for row in board_rows):
        raise Reject('archive_evidence_invalid',
                     f'archive {game_id}: boundary board/size/komi unusable', game_id=game_id)
    board = _parse_board(board_rows)
    # 开案即独立复算一次原面积，与归档终局面积逐项核对，不一致不允许开案。
    expected_area = area(board, size, decimal(komi))
    expected_area['status'] = 'after_two_passes'
    if _canonical(expected_area) != _canonical(final_area):
        raise Reject('archive_evidence_invalid',
                     f'archive {game_id}: final area does not recompute from the sealed board',
                     game_id=game_id)
    state = {
        'schema': SCHEMA,
        'case_ref': case_ref,
        'game_id': game_id,
        'size': size,
        'komi': komi,
        'original_board': list(board_rows),
        'final_receipt_id': receipt_id,
        'final_sealed_ply': total,
        'original_area': copy.deepcopy(final_area),
        'referees': list(referees),
        'round': 1,
        'version': 1,
        'status': 'open',
        'votes': {},
        'rounds': [],
    }
    os.makedirs(case_dir, exist_ok=True)
    path = _case_path(case_dir)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        raise Reject('case_exists', f'case already exists: {path}', case_ref=case_ref)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(_dump(state))
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return state


def load_case(case_dir):
    path = _case_path(case_dir)
    if not os.path.isfile(path):
        raise Reject('case_missing', f'no appeal case in {case_dir}', case_dir=case_dir)
    try:
        state = load(path)
    except ValueError as error:
        raise Reject('case_corrupt', f'case file unreadable: {error}', case_dir=case_dir)
    if not isinstance(state, dict) or state.get('schema') != SCHEMA:
        raise Reject('case_corrupt', f'unrecognized case schema in {path}', case_dir=case_dir)
    return state


def commit(case_dir, state):
    """一次提交：临时文件 + fsync + 原子替换 + 目录 fsync。"""
    payload = _dump(state)
    fd, tmp = tempfile.mkstemp(dir=case_dir, prefix='.case-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        _fault('commit_before_replace')
        os.replace(tmp, _case_path(case_dir))
        _fault('commit_after_replace')
        dirfd = os.open(case_dir, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@contextmanager
def locked(case_dir):
    """投票/结案/重开期间互斥：校验与提交不会与其他写者穿插。"""
    if not os.path.isdir(case_dir):
        raise Reject('case_missing', f'no case directory: {case_dir}')
    fd = os.open(os.path.join(case_dir, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# ---------- 意见（棋块代表坐标） ----------

def _parse_coordinates(coordinates, size):
    if not isinstance(coordinates, list) or len(coordinates) > size * size:
        raise Reject('invalid_structure', 'groups: array of [row, col] coordinates required')
    points = []
    for value in coordinates:
        if not isinstance(value, list) or len(value) != 2 \
                or any(type(x) is not int for x in value):
            raise Reject('invalid_structure', 'group representative must be [row, col] integers')
        row, col = value
        if not (0 <= row < size and 0 <= col < size):
            raise Reject('point_out_of_board', 'representative coordinate is outside the board',
                         point=[row, col], size=size)
        points.append((row, col))
    return points


def _resolve_groups(board, size, points):
    """把代表坐标解析为开案盘面上的棋块；空点/重复标同块整次拒绝。"""
    resolved = []
    seen = set()
    for point in points:
        if point not in board:
            raise Reject('point_not_stone', 'representative must land on a stone of the opening board',
                         point=[point[0], point[1]])
        stones, _liberties = board_group(board, point, size)
        key = frozenset(stones)
        if key in seen:
            raise Reject('duplicate_group_in_opinion',
                         'two representatives name the same group in one opinion',
                         point=[point[0], point[1]])
        seen.add(key)
        resolved.append({'representative': [point[0], point[1]],
                         'color': board[point],
                         'stones': [list(p) for p in sorted(stones)]})
    resolved.sort(key=lambda entry: entry['stones'][0])
    return resolved


def _group_key(vote_entry):
    return frozenset(tuple(p) for group in vote_entry['groups'] for p in group['stones'])


def derive_phase(state):
    if state['status'] == 'closed':
        return 'closed'
    votes = state['votes']
    referees = state['referees']
    if any(referee not in votes for referee in referees):
        return 'awaiting_votes'
    if _group_key(votes[referees[0]]) != _group_key(votes[referees[1]]):
        return 'divergent'
    return 'agreed'


# ---------- 纯函数状态变更（不加锁、不落盘，便于单测与复核） ----------

def _check_version(state, expected_version):
    if type(expected_version) is not int or not 1 <= expected_version <= MAX_REVISION:
        raise Reject('invalid_structure', 'expected_version: positive integer required')
    if expected_version != state['version']:
        raise Reject('version_conflict', 'expected version does not match the case version',
                     expected_version=expected_version, actual_version=state['version'])


def submit_vote(state, referee, expected_version, coordinates):
    """登记/替换一名裁判本轮意见；成功一次推进一版，返回 (新状态, 报告)。"""
    _check_version(state, expected_version)
    if not isinstance(referee, str) or referee not in state['referees']:
        raise Reject('unknown_referee', 'referee is not registered on this case', referee=referee)
    if state['status'] != 'open':
        raise Reject('case_closed', 'the current round is closed; reopen before changing votes',
                     round=state['round'])
    size = state['size']
    points = _parse_coordinates(coordinates, size)
    board = _parse_board(state['original_board'])
    groups = _resolve_groups(board, size, points)
    new = copy.deepcopy(state)
    new['version'] += 1
    new['votes'][referee] = {'groups': groups, 'written_version': new['version']}
    report = {'round': new['round'], 'version': new['version'], 'referee': referee,
              'phase': derive_phase(new),
              'groups': [entry['representative'] for entry in groups]}
    return new, report


def _ruling_identity(record):
    return _digest({
        'case_ref': record['case_ref'],
        'game_id': record['game_id'],
        'final_receipt_id': record['final_receipt_id'],
        'round': record['round'],
        'referees': record['referees'],
        'groups': [{'color': g['color'], 'stones': g['stones']} for g in record['groups']],
        'board_before': record['board_before'],
        'size': record['size'],
        'komi': record['komi'],
    })


def close_round(state, expected_version):
    """两人意见的棋块集合完全相同才结案；固化不可改写裁定，返回 (新状态, 裁定)。"""
    _check_version(state, expected_version)
    if state['status'] != 'open':
        raise Reject('case_closed', 'the current round is already closed', round=state['round'])
    referees = state['referees']
    missing = [referee for referee in referees if referee not in state['votes']]
    if missing:
        raise Reject('votes_incomplete', 'both registered referees must vote before closing',
                     round=state['round'], missing_referees=missing)
    first = state['votes'][referees[0]]
    second = state['votes'][referees[1]]

    def group_key(entry):
        return frozenset(tuple(p) for p in entry['stones'])

    if {group_key(g) for g in first['groups']} != \
            {group_key(g) for g in second['groups']}:
        raise Reject('votes_divergent',
                     'closing requires the exact same set of groups from both referees',
                     round=state['round'])
    size = state['size']
    komi = state['komi']
    board_before = _parse_board(state['original_board'])
    removed = {tuple(p) for entry in first['groups'] for p in entry['stones']}
    board_after = {point: color for point, color in board_before.items()
                   if point not in removed}
    area_before = area(board_before, size, decimal(komi))
    area_after = area(board_after, size, decimal(komi))
    second_by_key = {group_key(g): g for g in second['groups']}
    groups = []
    for entry in first['groups']:
        key = group_key(entry)
        reps = sorted({tuple(entry['representative']),
                       tuple(second_by_key[key]['representative'])})
        groups.append({'color': entry['color'],
                       'stones': copy.deepcopy(entry['stones']),
                       'representatives': [list(p) for p in reps]})
    groups.sort(key=lambda entry: entry['stones'][0])
    record = {
        'case_ref': state['case_ref'],
        'game_id': state['game_id'],
        'final_receipt_id': state['final_receipt_id'],
        'round': state['round'],
        'referees': list(referees),
        'groups': groups,
        'board_before': list(state['original_board']),
        'board_after': board_rows(board_after, size),
        'size': size,
        'komi': komi,
        'area_before': area_before,
        'area_after': area_after,
        'winner_before': area_before['winner'],
        'winner_after': area_after['winner'],
    }
    record['ruling_id'] = _ruling_identity(record)
    new = copy.deepcopy(state)
    new['version'] += 1
    new['status'] = 'closed'
    new['rounds'].append(record)
    return new, copy.deepcopy(record)


def reopen_case(state, expected_version):
    """只准重开已结案案卷：建立下一轮，两人均须重投；旧裁定只留作历史。"""
    _check_version(state, expected_version)
    if state['status'] != 'closed':
        raise Reject('case_open', 'only a closed case can be reopened', round=state['round'])
    new = copy.deepcopy(state)
    new['version'] += 1
    new['round'] += 1
    new['status'] = 'open'
    new['votes'] = {}
    report = {'round': new['round'], 'version': new['version'], 'phase': 'awaiting_votes'}
    return new, report


# ---------- 加锁的整流程入口 ----------

def vote_locked(case_dir, referee, expected_version, coordinates):
    with locked(case_dir):
        state = load_case(case_dir)
        new, report = submit_vote(state, referee, expected_version, coordinates)
        commit(case_dir, new)
        return report


def close_locked(case_dir, expected_version):
    with locked(case_dir):
        state = load_case(case_dir)
        new, record = close_round(state, expected_version)
        commit(case_dir, new)
        return record


def reopen_locked(case_dir, expected_version):
    with locked(case_dir):
        state = load_case(case_dir)
        new, report = reopen_case(state, expected_version)
        commit(case_dir, new)
        return report


# ---------- 查看与历史 ----------

def current_ruling(state):
    if state['status'] != 'closed' or not state['rounds']:
        return None
    return state['rounds'][-1]


def view_state(state):
    phase = derive_phase(state)
    votes_report = []
    for referee in state['referees']:
        vote = state['votes'].get(referee)
        if vote is None:
            votes_report.append({'referee': referee, 'submitted': False})
        else:
            votes_report.append({
                'referee': referee,
                'submitted': True,
                'written_version': vote['written_version'],
                'groups': [entry['representative'] for entry in vote['groups']],
            })
    ruling = current_ruling(state)
    return {
        'schema': SCHEMA,
        'case_ref': state['case_ref'],
        'game_id': state['game_id'],
        'size': state['size'],
        'komi': state['komi'],
        'original_board': list(state['original_board']),
        'final_receipt_id': state['final_receipt_id'],
        'final_sealed_ply': state['final_sealed_ply'],
        'original_winner': state['original_area']['winner'],
        'referees': list(state['referees']),
        'round': state['round'],
        'version': state['version'],
        'status': state['status'],
        'phase': phase,
        'votes': votes_report,
        'current_ruling_round': ruling['round'] if ruling is not None else None,
        'current_winner': ruling['winner_after'] if ruling is not None else None,
        'closed_rounds': [record['round'] for record in state['rounds']],
    }


def history(state, round_number=None):
    """按轮次交付足以独立复算的裁定证据；不借最新投票或最新胜负补足。"""
    if round_number is None:
        return {'case_ref': state['case_ref'], 'game_id': state['game_id'],
                'rounds': [{'round': record['round'],
                            'ruling_id': record['ruling_id'],
                            'final_receipt_id': record['final_receipt_id'],
                            'winner_before': record['winner_before'],
                            'winner_after': record['winner_after']}
                           for record in state['rounds']]}
    if type(round_number) is not int:
        raise Reject('invalid_structure', 'round: integer required')
    for record in state['rounds']:
        if record['round'] == round_number:
            return copy.deepcopy(record)
    raise Reject('round_not_found', f'no closed ruling for round {round_number}',
                 round=round_number, closed_rounds=[r['round'] for r in state['rounds']])


def recompute_ruling(record):
    """仅凭一份历史裁定记录独立复算：重建裁定前后盘面与面积胜负。"""
    size = record['size']
    komi = decimal(record['komi'])
    board_before = _parse_board(record['board_before'])
    removed = {tuple(point) for group in record['groups'] for point in group['stones']}
    board_after = {point: color for point, color in board_before.items()
                   if point not in removed}
    area_before = area(board_before, size, komi)
    area_after = area(board_after, size, komi)
    return {
        'board_before': board_rows(board_before, size),
        'board_after': board_rows(board_after, size),
        'area_before': area_before,
        'area_after': area_after,
        'winner_before': area_before['winner'],
        'winner_after': area_after['winner'],
    }


def verify_ruling(state, record):
    """核对固化裁定未被改写且可独立复算；不一致即 Reject。"""
    expected_identity = _ruling_identity(record)
    if record.get('ruling_id') != expected_identity:
        raise Reject('ruling_inconsistent', 'stored ruling id does not match its content',
                     round=record.get('round'))
    # 裁定记录里的字段名与案卷顶层略有不同：board_before 对应开案 original_board。
    state_fields = {'case_ref': 'case_ref', 'game_id': 'game_id',
                    'final_receipt_id': 'final_receipt_id', 'size': 'size', 'komi': 'komi',
                    'board_before': 'original_board'}
    for record_key, state_key in state_fields.items():
        if record.get(record_key) != state.get(state_key):
            raise Reject('ruling_inconsistent',
                         f'ruling field {record_key} diverges from the case file',
                         round=record.get('round'), field=record_key)
    recomputed = recompute_ruling(record)
    for key in ('board_before', 'board_after', 'area_before', 'area_after',
                'winner_before', 'winner_after'):
        if _canonical(record.get(key)) != _canonical(recomputed[key]):
            raise Reject('ruling_inconsistent',
                         f'stored ruling {key} does not recompute from the recorded evidence',
                         round=record.get('round'), field=key)
    return True


def verify_state(state):
    """案卷整体一致性：状态与历史裁定互相对得上。返回 (status, round, 当前胜负或 None)。"""
    try:
        rounds = state['rounds']
        if not isinstance(rounds, list) or state['round'] < 1:
            raise ValueError('bad rounds')
        if [record['round'] for record in rounds] != list(range(1, len(rounds) + 1)):
            raise ValueError('round numbering gap')
        for record in rounds:
            verify_ruling(state, record)
        if state['status'] == 'closed':
            if len(rounds) != state['round']:
                raise ValueError('closed status without a current-round ruling')
            return 'closed', state['round'], rounds[-1]['winner_after']
        if state['status'] != 'open' or len(rounds) != state['round'] - 1:
            raise ValueError('open status without exactly the previous-round rulings')
        return 'open', state['round'], None
    except (KeyError, TypeError, ValueError) as error:
        raise Reject('case_corrupt', f'case state inconsistent: {error}')
