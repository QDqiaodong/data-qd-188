"""离线分包接收与归档：每个本地状态目录只管理一盘棋。

沿用 v1 回放棋规（落子/提子/简单劫/双停，见 board.py 与 service.py）和
regions.py 的面积模型；不引入 SGF、联网、死活判定或其他计分规则。仅用标准库。

持久化：状态是单个 state.json，所有变更（收包合并、封存点推进、封存段清理、
回执登记、最终归档结果）都在一次原子替换中提交；异常中断并重开只能读到完整
的变更前或变更后状态。收包与封存在同一把 flock 下完成校验与提交，互不穿插。
"""
import copy
import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager

from .board import identity, place, rows
from .checks import array, decimal, fields, ident, integer
from .io import load
from .model import coordinate, setup_board
from .regions import area

SCHEMA = 'go-record-archive/1'
STATE_FILE = 'state.json'
LOCK_FILE = 'lock'
MAX_REVISION = 2147483647
FAULT_ENV = 'GO_RECORD_ARCHIVE_FAULT'
FAULT_POINTS = ('commit_before_replace', 'commit_after_replace')

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
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def fingerprint_move(record):
    """棋步内容指纹：只看 id/player/point 的值，不看 JSON 键顺序或空白。"""
    return _digest({'id': record['id'], 'player': record['player'], 'point': record['point']})


def _dump(state):
    return (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _state_path(state_dir):
    return os.path.join(state_dir, STATE_FILE)


# ---------- 状态初始化与持久化 ----------

def init_state(spec):
    """校验初始化 spec（game_id/total_moves/size/setup/next_player/komi，不带 moves）。"""
    try:
        fields(spec, ('game_id', 'total_moves', 'size', 'setup', 'next_player', 'komi'))
        ident(spec['game_id'])
        total = integer(spec['total_moves'], 2, 2000)
        board, _komi = setup_board(spec)
    except ValueError as error:
        raise Reject('invalid_structure', f'init spec: {error}')
    size = spec['size']
    return {
        'schema': SCHEMA,
        'game_id': spec['game_id'],
        'total_moves': total,
        'size': size,
        'setup': {color: sorted(list(point) for point in spec['setup'][color]) for color in ('black', 'white')},
        'next_player': spec['next_player'],
        'komi': spec['komi'],
        'sealed_ply': 0,
        'sealed': {},
        'pending': {},
        'boundary': {
            'board': rows(board, size),
            'prev_board': None,
            'next_player': spec['next_player'],
            'passes': 0,
            'captures': {'B': 0, 'W': 0},
        },
        'receipts': {},
        'final': None,
    }


def create_state(state_dir, spec):
    """初始化状态目录；已有状态必须拒绝，不得覆盖。"""
    state = init_state(spec)
    os.makedirs(state_dir, exist_ok=True)
    path = _state_path(state_dir)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        raise Reject('state_exists', f'state already exists: {path}')
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


def load_state(state_dir):
    path = _state_path(state_dir)
    if not os.path.isfile(path):
        raise Reject('state_missing', f'no archived game state in {state_dir}')
    try:
        state = load(path)
    except ValueError as error:
        raise Reject('state_corrupt', f'state file unreadable: {error}')
    if not isinstance(state, dict) or state.get('schema') != SCHEMA:
        raise Reject('state_corrupt', f'unrecognized state schema in {path}')
    return state


def commit(state_dir, state):
    """一次提交：临时文件 + fsync + 原子替换 + 目录 fsync。"""
    payload = _dump(state)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix='.state-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        _fault('commit_before_replace')
        os.replace(tmp, _state_path(state_dir))
        _fault('commit_after_replace')
        dirfd = os.open(state_dir, os.O_RDONLY)
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
def locked(state_dir):
    """收包/封存期间的互斥：校验与提交不会与其他写者穿插。"""
    if not os.path.isdir(state_dir):
        raise Reject('state_missing', f'no state directory: {state_dir}')
    fd = os.open(os.path.join(state_dir, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# ---------- 回放（从封存边界继续，与 service.process 同一棋规） ----------

def _parse_board(board_rows):
    if board_rows is None:
        return None
    board = {}
    for r, row in enumerate(board_rows):
        for c, mark in enumerate(row):
            if mark in 'BW':
                board[(r, c)] = mark
    return board


def replay(state, upto=None):
    """从封存边界回放未封存的有效棋步；结论每次现算，不缓存。"""
    size = state['size']
    boundary = state['boundary']
    board = _parse_board(boundary['board'])
    prev = _parse_board(boundary['prev_board'])
    next_player = boundary['next_player']
    passes = boundary['passes']
    captures = dict(boundary['captures'])
    pending = state['pending']
    end = state['total_moves'] if upto is None else upto
    applied = []
    blocker = None
    for ply in range(state['sealed_ply'] + 1, end + 1):
        record = pending.get(str(ply))
        if record is None:
            blocker = {'ply': ply, 'reason': 'missing'}
            break
        reason = None
        captured = []
        candidate = board
        if passes == 2:
            reason = 'game_ended'
        elif record['player'] != next_player:
            reason = 'wrong_turn'
        elif record['point'] is not None:
            candidate, captured, reason = place(board, tuple(record['point']), next_player, size)
            if reason is None and prev is not None and identity(candidate) == identity(prev):
                reason = 'simple_ko'
        if reason is not None:
            blocker = {'ply': ply, 'reason': reason, 'move_id': record['id']}
            break
        prev, board = board, candidate
        passes = passes + 1 if record['point'] is None else 0
        captures[next_player] += len(captured)
        next_player = 'W' if next_player == 'B' else 'B'
        applied.append(ply)
    return {'applied': applied, 'blocker': blocker, 'board': board, 'prev_board': prev,
            'next_player': next_player, 'passes': passes, 'captures': captures}


def _score(state, board, passes):
    score = area(board, state['size'], decimal(state['komi']))
    score['status'] = 'after_two_passes' if passes == 2 else 'provisional'
    return score


# ---------- 查看与核对 ----------

def view(state, target=None):
    """状态报告；target 非空时尝试签发该段的封存凭据（不合格则 Reject）。"""
    rep = replay(state)
    applied = set(rep['applied'])
    pending = [dict(record, ply=int(key), applied=int(key) in applied)
               for key, record in sorted(state['pending'].items(), key=lambda item: int(item[0]))]
    sealed = [dict(entry, ply=int(key))
              for key, entry in sorted(state['sealed'].items(), key=lambda item: int(item[0]))]
    status = {
        'game_id': state['game_id'],
        'total_moves': state['total_moves'],
        'sealed_ply': state['sealed_ply'],
        'prefix_ply': state['sealed_ply'] + len(rep['applied']),
        'blocker': rep['blocker'],
        'board': rows(rep['board'], state['size']),
        'next_player': rep['next_player'],
        'captures': rep['captures'],
        'double_pass': rep['passes'] == 2,
        'area': _score(state, rep['board'], rep['passes']),
        'pending': pending,
        'sealed': sealed,
        'receipts': [state['receipts'][key] for key in sorted(state['receipts'])],
        'final': state['final'],
    }
    if target is not None:
        status['credential'] = {'from': state['sealed_ply'], 'to': target,
                                'value': credential_for(state, target)}
    return status


def _check_target(state, target):
    try:
        integer(target, 1, state['total_moves'])
    except ValueError as error:
        raise Reject('target_out_of_range', str(error), target=target)
    if target <= state['sealed_ply']:
        raise Reject('target_not_after_seal', 'target must be beyond the current seal point',
                     target=target, sealed_ply=state['sealed_ply'])


def credential_for(state, target):
    """核对目标段：连续合法且不把双停/总手数矛盾锁死，才给出封存凭据。"""
    _check_target(state, target)
    rep = replay(state, upto=target)
    if rep['blocker'] is not None:
        raise Reject('segment_incomplete', 'segment is not continuous and legal up to target',
                     target=target, blocker=rep['blocker'])
    if rep['passes'] == 2 and target != state['total_moves']:
        raise Reject('double_pass_mismatch', 'two passes must land exactly on the declared last move',
                     target=target, total_moves=state['total_moves'])
    if target == state['total_moves'] and rep['passes'] != 2:
        raise Reject('double_pass_mismatch', 'sealing the declared last move requires two passes',
                     target=target, total_moves=state['total_moves'])
    segment = [dict(state['pending'][str(ply)], ply=ply)
               for ply in range(state['sealed_ply'] + 1, target + 1)]
    return _digest({'game_id': state['game_id'], 'from': state['sealed_ply'],
                    'to': target, 'moves': segment})


# ---------- 封存 ----------

def seal(state, target, credential):
    """凭目标手号与凭据封存；返回 (新状态, 回执, 是否推进)。原样重试返回原回执。"""
    try:
        integer(target, 1, state['total_moves'])
    except ValueError as error:
        raise Reject('target_out_of_range', str(error), target=target)
    key = f'{target}:{credential}'
    if key in state['receipts']:
        return state, state['receipts'][key], False
    _check_target(state, target)
    expected = credential_for(state, target)
    if not isinstance(credential, str) or credential != expected:
        raise Reject('credential_mismatch', 'credential does not match the currently verified segment',
                     target=target)
    rep = replay(state, upto=target)
    new = copy.deepcopy(state)
    boundary = new['boundary']
    boundary['board'] = rows(rep['board'], new['size'])
    boundary['prev_board'] = rows(rep['prev_board'], new['size']) if rep['prev_board'] is not None else None
    boundary['next_player'] = rep['next_player']
    boundary['passes'] = rep['passes']
    boundary['captures'] = rep['captures']
    for ply in range(state['sealed_ply'] + 1, target + 1):
        record = new['pending'].pop(str(ply))
        new['sealed'][str(ply)] = {'id': record['id'], 'revision': record['revision'],
                                   'fingerprint': fingerprint_move(record)}
    receipt = {'game_id': state['game_id'],
               'previous_sealed_ply': state['sealed_ply'],
               'sealed_ply': target,
               'credential': credential,
               'receipt_id': _digest({'game_id': state['game_id'], 'from': state['sealed_ply'],
                                      'to': target, 'credential': credential})}
    new['sealed_ply'] = target
    new['receipts'][key] = receipt
    if target == state['total_moves']:
        # 只有声明末手已合法双停且封到末手，才产出最终归档结果。
        new['final'] = {'sealed_ply': target, 'captures': rep['captures'],
                        'area': _score(new, rep['board'], rep['passes'])}
    return new, receipt, True


# ---------- 收包 ----------

def _validate_record(record, index, total_moves):
    try:
        fields(record, ('id', 'player', 'point', 'ply', 'revision'))
        ident(record['id'])
        if record['player'] not in ('B', 'W'):
            raise ValueError('B/W required')
        if record['point'] is not None:
            coordinate(record['point'])
        integer(record['ply'], 1, total_moves)
        integer(record['revision'], 1, MAX_REVISION)
    except ValueError as error:
        raise Reject('invalid_structure', f'records[{index}]: {error}', record_index=index)


def receive(state, packet):
    """合并一个分包；任何拒绝都发生在提交前，整包不留痕迹。纯函数，不改入参。"""
    try:
        fields(packet, ('game_id', 'records'))
        ident(packet['game_id'])
        records = array(packet['records'], 1, 2000)
    except ValueError as error:
        raise Reject('invalid_structure', f'packet: {error}')
    if packet['game_id'] != state['game_id']:
        raise Reject('wrong_game', 'packet belongs to a different game', game_id=packet['game_id'])
    seen = set()
    for index, record in enumerate(records):
        _validate_record(record, index, state['total_moves'])
        if record['ply'] in seen:
            raise Reject('duplicate_ply_in_packet', 'same ply reported twice in one packet',
                         record_index=index, ply=record['ply'])
        seen.add(record['ply'])
    sealed_ply = state['sealed_ply']
    sealed = state['sealed']
    pending = {key: dict(record) for key, record in state['pending'].items()}
    accepted = []
    skipped = []
    for record in records:
        ply = record['ply']
        key = str(ply)
        entry = {'ply': ply, 'id': record['id'], 'revision': record['revision']}
        if ply <= sealed_ply:
            kept = sealed[key]
            if record['revision'] > kept['revision']:
                raise Reject('sealed_ply_immutable', 'sealed ply does not accept a higher revision',
                             ply=ply, move_id=kept['id'])
            if record['revision'] == kept['revision']:
                if fingerprint_move(record) != kept['fingerprint']:
                    raise Reject('version_conflict', 'same revision but different content on a sealed ply',
                                 ply=ply, move_id=kept['id'])
                skipped.append(dict(entry, reason='sealed'))
            else:
                skipped.append(dict(entry, reason='stale'))
            continue
        current = pending.get(key)
        if current is None:
            pending[key] = {'id': record['id'], 'player': record['player'],
                            'point': record['point'], 'revision': record['revision']}
            accepted.append(dict(entry, outcome='new'))
        elif record['revision'] > current['revision']:
            pending[key] = {'id': record['id'], 'player': record['player'],
                            'point': record['point'], 'revision': record['revision']}
            accepted.append(dict(entry, outcome='upgrade'))
        elif record['revision'] == current['revision']:
            if (record['id'], record['player'], record['point']) != \
                    (current['id'], current['player'], current['point']):
                raise Reject('version_conflict', 'same revision but different content',
                             ply=ply, move_id=current['id'])
            skipped.append(dict(entry, reason='retransmit'))
        else:
            skipped.append(dict(entry, reason='stale'))
    # 整包合并后的全盘 ID 唯一性：对最终有效集合判定，不按包内处理顺序。
    owners = {}
    for key, kept in sealed.items():
        owners.setdefault(kept['id'], []).append(int(key))
    for key, record in pending.items():
        owners.setdefault(record['id'], []).append(int(key))
    for move_id, plies in owners.items():
        if len(plies) > 1:
            raise Reject('duplicate_move_id', 'move id occupied by more than one effective ply',
                         move_id=move_id, plies=sorted(plies))
    new = dict(state)
    new['pending'] = pending
    return new, {'game_id': state['game_id'], 'accepted': accepted, 'skipped': skipped}


# ---------- 加锁的整流程入口 ----------

def receive_locked(state_dir, packet):
    with locked(state_dir):
        state = load_state(state_dir)
        new, report = receive(state, packet)
        if report['accepted']:
            commit(state_dir, new)
        return report


def seal_locked(state_dir, target, credential):
    with locked(state_dir):
        state = load_state(state_dir)
        new, receipt, changed = seal(state, target, credential)
        if changed:
            commit(state_dir, new)
        return receipt, new
