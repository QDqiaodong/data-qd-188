"""离线溯源被提棋子来源并汇总任意窗口（go-record-capture-study）。

一次读入多份 go-record 原始棋谱和一批查询（capture_from / capture_to / origin /
life_from / life_to），同时支持少量逐子反查。对每盘：

- 在 ``service`` 使用的 v1 规则上回放合法前缀，为盘上每颗棋子实例记住来源：
  初始设置里的子来自 ``setup``；落下的子来自落子手号与步 ID。同点被提后再落子
  是新实例，不继承旧来源。
- 每次合法提子，对每个被提棋块逐子产出一条事实，身份是
  ``(game_id, capture_ply, point)``；一次提多个棋块、多颗子分别成条。
- 首个非法步及之后不产生任何事实；停手照常占手号，双停后或首非法步之后的查询
  窗口按合法前缀截断。

全部查询一次性回答，不逐查询遍历逐子事实：

- ``stone_count``：总计匹配事实条数。played 用按提子手离线扫描 + 寿命 Fenwick
  树，一次扫描回答全部查询；setup 用排好序的提子手号二分。
- ``game_count``：按盘取“是否至少有一条匹配事实”。把每个查询固定到结果位序，
  预先算出「提子手 p 落在查询窗口内」与「寿命 l 落在查询窗口内」的位掩码；一盘
  把自身各事实的掩码 OR 起来，再按置位一次性累加该盘对每个查询的 0/1 贡献。
  同一盘命中多少条只计一次，两个盘号引用同一棋谱仍分别计数。

逐子反查直接按 ``(game_id, capture_ply, point)`` 查表。标准库实现，无第三方依赖。
"""
import os
from bisect import bisect_left, bisect_right

from . import service
from .checks import array, fields, ident, integer
from .io import load
from .model import validate

SCHEMA = 'go-record-capture-study/1'
MAX_GAMES = 384
MAX_QUERIES = 40_000
MAX_LOOKUPS = 128
MAX_MOVES_TOTAL = 240_000
MAX_PLY = 2_000
ORIGINS = ('setup', 'played')


class Reject(Exception):
    """输入错误：CLI 退出 2，diagnostic() 给出可定位信息，不落报告。"""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def diagnostic(self):
        return {'error': {'code': self.code, 'message': str(self), **self.details}}


# ---------- 清单校验 ----------

def _entry(prefix, index, error, **identity):
    details = {f'{prefix}_index': index}
    details.update(identity)
    return Reject('invalid_structure', f'{prefix}[{index}]: {error}', **details)


def _bounds(entry, prefix, index, names, **identity):
    values = []
    for name in names:
        try:
            values.append(integer(entry[name], 1, MAX_PLY))
        except ValueError as error:
            raise _entry(prefix, index, f'{name}: {error}', **identity)
    for left, right in ((0, 1), (2, 3)):
        if right < len(values) and values[left] > values[right]:
            raise _entry(prefix, index,
                         f'{names[left]} is greater than {names[right]}', **identity)
    return values


def validate_manifest(manifest):
    """严格校验清单并规范化排序；结构问题抛 Reject。"""
    try:
        fields(manifest, ('games', 'queries', 'lookups'))
        games_raw = array(manifest['games'], 1, MAX_GAMES)
        queries_raw = array(manifest['queries'], 1, MAX_QUERIES)
        lookups_raw = array(manifest['lookups'], 0, MAX_LOOKUPS)
    except ValueError as error:
        raise Reject('invalid_structure', f'manifest: {error}')

    games = []
    game_ids = set()
    for index, entry in enumerate(games_raw):
        try:
            fields(entry, ('game_id', 'path'))
            ident(entry['game_id'])
            if not isinstance(entry['path'], str) or not entry['path']:
                raise ValueError('path: non-empty string required')
        except ValueError as error:
            raise _entry('games', index, error)
        if entry['game_id'] in game_ids:
            raise _entry('games', index, 'duplicate game_id', game_id=entry['game_id'])
        game_ids.add(entry['game_id'])
        games.append({'game_id': entry['game_id'], 'path': entry['path']})
    games.sort(key=lambda item: item['game_id'])

    queries = []
    query_ids = set()
    for index, entry in enumerate(queries_raw):
        identity = {'query_id': entry.get('query_id')} \
            if isinstance(entry, dict) and isinstance(entry.get('query_id'), str) else {}
        try:
            fields(entry, ('query_id', 'capture_from', 'capture_to', 'origin'),
                   ('life_from', 'life_to'))
            ident(entry['query_id'])
            if entry['origin'] not in ORIGINS:
                raise ValueError('origin: setup/played required')
            has_life = 'life_from' in entry or 'life_to' in entry
            if entry['origin'] == 'setup' and has_life:
                raise ValueError('setup query must not carry life_from/life_to')
            if entry['origin'] == 'played' and not has_life:
                raise ValueError('played query requires life_from and life_to')
        except ValueError as error:
            raise _entry('queries', index, error, **identity)
        if entry['query_id'] in query_ids:
            raise _entry('queries', index, 'duplicate query_id', **identity)
        query_ids.add(entry['query_id'])
        capture_from, capture_to = _bounds(entry, 'queries', index,
                                           ('capture_from', 'capture_to'), **identity)
        record = {'query_id': entry['query_id'], 'origin': entry['origin'],
                  'capture_from': capture_from, 'capture_to': capture_to,
                  'life_from': None, 'life_to': None}
        if entry['origin'] == 'played':
            record['life_from'], record['life_to'] = _bounds(
                entry, 'queries', index, ('life_from', 'life_to'), **identity)
        queries.append(record)
    queries.sort(key=lambda item: item['query_id'])

    lookup_records = []
    lookup_ids = set()
    for index, entry in enumerate(lookups_raw):
        try:
            fields(entry, ('lookup_id', 'game_id', 'capture_ply', 'point'))
            ident(entry['lookup_id'])
            ident(entry['game_id'])
            ply = integer(entry['capture_ply'], 1, MAX_PLY)
            point = entry['point']
            array(point, 2, 2)
            point = tuple(integer(c, -100, 100) for c in point)
        except ValueError as error:
            raise _entry('lookups', index, error)
        identity = {'lookup_id': entry['lookup_id']}
        if entry['lookup_id'] in lookup_ids:
            raise _entry('lookups', index, 'duplicate lookup_id', **identity)
        lookup_ids.add(entry['lookup_id'])
        if entry['game_id'] not in game_ids:
            raise _entry('lookups', index, 'game_id not declared in games',
                         lookup_id=entry['lookup_id'], game_id=entry['game_id'])
        lookup_records.append({'lookup_id': entry['lookup_id'], 'game_id': entry['game_id'],
                               'capture_ply': ply, 'point': point})
    lookup_records.sort(key=lambda item: item['lookup_id'])

    return {'games': games, 'queries': queries, 'lookups': lookup_records}


# ---------- 单盘合法回放与逐子事实 ----------

def capture_facts(data, board):
    """在 v1 合法前缀上回放，返回 (facts, blocked, legal_plies)。

    facts 每项为 ``(capture_ply, point, origin, source_ply, life, capture_move_id,
    player, captured_color, source_move_id)``；``origin`` 为 'setup' 或 'played'，
    setup 的 ``source_ply`` / ``life`` / ``source_move_id`` 为 None。``blocked``
    是首个非法步（含 ply / move_id / reason），无则 None。停手占手号；双停与首
    非法步之后都不产生事实。调用前必须已用 ``model.validate`` 校验结构。
    """
    size = data['size']
    origin = {point: ('setup', None, None) for point in board}
    # 简单劫只需比较“前一手之前的盘面”，保留两个滚动身份即可（与 service 的
    # history[-2] 判定等价，含停手更新）。
    two_back = None
    current = frozenset(board.items())
    next_player = data['next_player']
    passes = 0
    facts = []
    blocked = None
    accepted = 0
    for index, move in enumerate(data['moves']):
        reason = None
        captured = []
        candidate = board
        if passes == 2:
            reason = 'game_ended'
        elif move['player'] != next_player:
            reason = 'wrong_turn'
        elif move['point'] is not None:
            candidate, captured, reason = service.place(
                board, tuple(move['point']), next_player, size)
            if reason is None and two_back is not None and \
                    frozenset(candidate.items()) == two_back:
                reason = 'simple_ko'
        if reason:
            blocked = {'ply': index + 1, 'move_id': move['id'], 'reason': reason}
            break
        accepted += 1
        ply = index + 1
        if move['point'] is None:
            passes += 1
        else:
            passes = 0
            point = tuple(move['point'])
            if captured:
                captured_color = board[tuple(captured[0])]
                # 一次提子可能涉及多个棋块；逐子追溯，每颗子各成一条事实。
                for stone in captured:
                    kind, source_ply, source_move_id = origin[stone]
                    life = None if kind == 'setup' else ply - source_ply
                    facts.append((ply, stone, kind, source_ply, life,
                                  move['id'], next_player, captured_color,
                                  source_move_id))
                for stone in captured:
                    del origin[stone]
            origin[point] = ('played', ply, move['id'])
        board = candidate
        two_back = current
        current = frozenset(board.items())
        next_player = 'W' if next_player == 'B' else 'B'
    facts.sort(key=lambda item: (item[0], item[1]))
    return facts, blocked, accepted


def load_games(manifest, manifest_dir):
    """读取并校验全部棋谱；总量受限。返回每盘的 data 与结构校验结果。"""
    loaded = []
    total = 0
    for entry in manifest['games']:
        try:
            data = load(os.path.join(manifest_dir, entry['path']))
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            raise Reject('game_unreadable',
                         f'game {entry["game_id"]}: cannot read {entry["path"]}: {error}',
                         game_id=entry['game_id'], path=entry['path'])
        moves = data.get('moves') if isinstance(data, dict) else None
        if isinstance(moves, list):
            total += len(moves)
        if total > MAX_MOVES_TOTAL:
            raise Reject('move_limit',
                         f'games total {total} raw moves; at most {MAX_MOVES_TOTAL}',
                         game_id=entry['game_id'], total_moves=total,
                         limit=MAX_MOVES_TOTAL)
        try:
            board, _komi = validate(data)
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise Reject('invalid_game', f'game {entry["game_id"]}: {error}',
                         game_id=entry['game_id'], path=entry['path'])
        loaded.append({'game_id': entry['game_id'], 'path': entry['path'],
                       'data': data, 'board': board})
    return loaded


# ---------- 事实视图 ----------

def _fact_view(fact):
    (ply, point, kind, source_ply, life, move_id, player, captured_color,
     source_move_id) = fact
    return {
        'capture_ply': ply,
        'capture_move_id': move_id,
        'capture_player': player,
        'captured_color': captured_color,
        'point': list(point),
        'origin': kind,
        'source_ply': source_ply,
        'source_move_id': source_move_id,
        'life': life,
    }


# ---------- 查询求解 ----------

class _Fenwick:
    """一维寿命计数树；只用于 played 事实的 stone_count 离线扫描。"""

    def __init__(self, size):
        self.size = size
        self.tree = [0] * (size + 1)

    def add(self, index):
        if index <= 0:
            return
        while index <= self.size:
            self.tree[index] += 1
            index += index & -index

    def prefix(self, index):
        total = 0
        while index > 0:
            total += self.tree[index]
            index -= index & -index
        return total

    def range(self, low, high):
        if high < low:
            return 0
        return self.prefix(high) - self.prefix(low - 1)


def _axis_masks(queries, low_name, high_name, limit, include):
    """返回长度 limit+1 的位掩码：第 v 位掩码的第 i 位表示查询 i 的窗口含 v。

    仅对 ``include(query)`` 为真的查询置位。窗口上界超过 limit 的查询在处理到
    limit 时仍保持置位（其真实上界不会低于当前值）。
    """
    add_at = [[] for _ in range(limit + 2)]
    remove_at = [[] for _ in range(limit + 2)]
    for index, query in enumerate(queries):
        if not include(query):
            continue
        add_at[query[low_name]].append(index)
        upper = query[high_name] + 1
        if upper <= limit:
            remove_at[upper].append(index)
    masks = [0] * (limit + 1)
    active = 0
    for value in range(1, limit + 1):
        for index in add_at[value]:
            active |= 1 << index
        for index in remove_at[value]:
            active &= ~(1 << index)
        masks[value] = active
    return masks


def _window_masks(queries):
    """返回提子手与寿命两轴掩码，以及 setup / played 的来源掩码。"""
    max_capture = max(query['capture_to'] for query in queries)
    played = [query for query in queries if query['origin'] == 'played']
    max_life = max((query['life_to'] for query in played), default=1)
    capture_masks = _axis_masks(queries, 'capture_from', 'capture_to',
                                max_capture, lambda query: True)
    life_masks = _axis_masks(queries, 'life_from', 'life_to',
                             max_life, lambda query: query['origin'] == 'played')
    setup_bits = 0
    played_bits = 0
    for index, query in enumerate(queries):
        if query['origin'] == 'setup':
            setup_bits |= 1 << index
        else:
            played_bits |= 1 << index
    return capture_masks, life_masks, setup_bits, played_bits


def _played_stone_counts(queries, per_game):
    """played 的 stone_count：按提子手离线扫描 + 寿命 Fenwick 树。

    per_game 是每盘 played 事实的 ``(capture_ply, life)`` 列表。扫描按提子手上界
    推进 Fenwick 树，每个查询用 ``[capture_from-1, capture_to]`` 两次前缀差取得
    寿命窗口内的条数，全部查询一次扫描回答。
    """
    facts = []
    for played in per_game:
        facts.extend(played)
    facts.sort()
    played_queries = [query for query in queries if query['origin'] == 'played']
    max_life = max((query['life_to'] for query in played_queries), default=1)
    events = []
    for index, query in enumerate(queries):
        if query['origin'] != 'played':
            continue
        events.append((query['capture_to'], 1, index))
        events.append((query['capture_from'] - 1, -1, index))
    events.sort()
    tree = _Fenwick(max_life)
    result = [0] * len(queries)
    cursor = 0
    for bound, sign, index in events:
        while cursor < len(facts) and facts[cursor][0] <= bound:
            life = facts[cursor][1]
            if life <= max_life:  # 超出所有查询寿命上界的事实不可能命中
                tree.add(life)
            cursor += 1
        query = queries[index]
        low = min(query['life_from'], max_life + 1)
        high = min(query['life_to'], max_life)
        if low <= high:
            result[index] += sign * tree.range(low, high)
    return result


def _game_counts(queries, per_game_facts):
    """game_count：逐盘把命中查询掩码 OR 起来，再按位累加一次贡献。

    逐盘 OR 得到“本盘命中此查询”的 0/1 位向量后，用位切片计数器把该向量加到
    全批计数：每个 game 只做约 ``log2(盘数)+1`` 次大整数按位运算，而不是对每个
    置位逐位累加。盘数 ≤ 384，计数至多 9 位。
    """
    capture_masks, life_masks, setup_bits, played_bits = _window_masks(queries)
    max_capture = len(capture_masks) - 1
    max_life = len(life_masks) - 1
    levels = []  # 第 j 个整数保存所有查询计数第 j 位的位向量
    for facts in per_game_facts:
        hit = 0
        for fact in facts:
            ply, _point, kind, _source_ply, life = fact[:5]
            if ply > max_capture:
                continue
            if kind == 'setup':
                hit |= capture_masks[ply] & setup_bits
            elif life <= max_life:
                hit |= capture_masks[ply] & life_masks[life] & played_bits
        carry = hit
        level = 0
        while carry:
            if level == len(levels):
                levels.append(0)
            below = levels[level]
            levels[level] = below ^ carry
            carry = below & carry
            level += 1
    return _unpack_levels(levels, len(queries))


def _unpack_levels(levels, count):
    """把位切片计数器拆成逐查询整数计数。"""
    if not levels:
        return [0] * count
    lanes = [level.to_bytes((count + 7) // 8, 'little') for level in levels]
    counts = [0] * count
    for level_index, data in enumerate(lanes):
        weight = 1 << level_index
        for index in range(count):
            if data[index >> 3] >> (index & 7) & 1:
                counts[index] += weight
    return counts


def query_results(queries, per_game_facts):
    """返回按查询顺序的 (stone_count, game_count) 列表。"""
    played_by_game = []
    setup_plies = []
    for facts in per_game_facts:
        played = []
        for fact in facts:
            if fact[2] == 'played':
                played.append((fact[0], fact[4]))
            else:
                setup_plies.append(fact[0])
        played_by_game.append(played)
    setup_plies.sort()
    played_stones = _played_stone_counts(queries, played_by_game)
    games_hit = _game_counts(queries, per_game_facts)
    results = []
    for index, query in enumerate(queries):
        if query['origin'] == 'setup':
            stones = (bisect_right(setup_plies, query['capture_to'])
                      - bisect_left(setup_plies, query['capture_from']))
        else:
            stones = played_stones[index]
        results.append((stones, games_hit[index]))
    return results


# ---------- 报告 ----------

def _query_report(query, result):
    return {
        'query_id': query['query_id'],
        'origin': query['origin'],
        'capture_from': query['capture_from'],
        'capture_to': query['capture_to'],
        'life_from': query['life_from'],
        'life_to': query['life_to'],
        'stone_count': result[0],
        'game_count': result[1],
    }


def build_report(manifest, manifest_dir):
    """校验清单、读取全部棋谱、回答查询与反查，返回报告对象。"""
    loaded = load_games(manifest, manifest_dir)
    sizes = {record['game_id']: record['data']['size'] for record in loaded}
    for entry in manifest['lookups']:
        size = sizes[entry['game_id']]
        row, col = entry['point']
        if not (0 <= row < size and 0 <= col < size):
            raise Reject('invalid_structure',
                         f'lookup {entry["lookup_id"]}: point outside board',
                         lookup_id=entry['lookup_id'], game_id=entry['game_id'],
                         point=list(entry['point']), size=size)
    per_game_facts = []
    lookups_wanted = {(entry['game_id'], entry['capture_ply'], entry['point'])
                      for entry in manifest['lookups']}
    lookup_facts = {}
    game_reports = []
    for record in loaded:
        facts, blocked, accepted = capture_facts(record['data'], record['board'])
        per_game_facts.append(facts)
        for fact in facts:
            key = (record['game_id'], fact[0], fact[1])
            if key in lookups_wanted:
                lookup_facts[key] = fact
        game_reports.append({
            'game_id': record['game_id'],
            'path': record['path'],
            'legal_plies': accepted,
            'first_illegal': blocked,
        })
    results = query_results(manifest['queries'], per_game_facts)
    return {
        'schema': SCHEMA,
        'games': game_reports,
        'queries': [_query_report(query, result)
                    for query, result in zip(manifest['queries'], results)],
        'lookups': [{
            'lookup_id': entry['lookup_id'],
            'game_id': entry['game_id'],
            'capture_ply': entry['capture_ply'],
            'point': list(entry['point']),
            'fact': _fact_view(lookup_facts[key])
                    if (key := (entry['game_id'], entry['capture_ply'],
                                entry['point'])) in lookup_facts else None,
        } for entry in manifest['lookups']],
    }
