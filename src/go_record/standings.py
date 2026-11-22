"""离线赛会对账与晋级核定（go-record-standings）。

按交接清单核对一批 go-record-archive 状态目录：每台必须恰好采用一份自己的
候选归档，且每份归档在全清单中恰好用一次。只采用封到声明末手的
final.area.winner（B/W/draw），并以该盘的末手封存回执 ID 为证据；尚未最终
封存的正常状态只导致 awaiting_final 报告，不用当前盘面或预览分数代替结果。

每套可行对应关系独立计算积分（胜 2、和 1、负 0）、对手分（本套终局积分之
和，同一对手重复相遇重复计）与名次（积分降序、对手分降序、抽签优先序）。
报告每名选手可达的最好/最差名次及可复核见证。全程只读归档目录，不根据
文件名、输入顺序或棋盘异同猜测身份；不表示优先级的输入顺序变化不影响报告。
"""
import os

from . import appeal, archive
from .checks import array, fields, ident, integer

SCHEMA = 'go-record-standings/1'
RESULTS = ('B', 'W', 'draw')
MAX_TABLES = 256
MAX_MULTI_CANDIDATE_TABLES = 10


class Reject(Exception):
    """输入错误：CLI 退出 2，diagnostic() 给出可定位信息，不落报告。"""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def diagnostic(self):
        return {'error': {'code': self.code, 'message': str(self), **self.details}}


# ---------- 清单校验 ----------

def validate_manifest(manifest):
    """校验并规范化清单；结构问题 Reject('invalid_structure')。

    返回的清单已规范化排序（tables/archives/candidates/players 均按 ID 排序），
    使不表示优先级的输入顺序不影响后续结果；lot_order 只化为优先下标，不重排。
    """
    try:
        fields(manifest, ('players', 'qualify_count', 'lot_order', 'tables', 'archives'))
        players = array(manifest['players'], 2, 64)
        for player in players:
            ident(player)
        if len(set(players)) != len(players):
            raise ValueError('players: duplicate player id')
        try:
            qualify = integer(manifest['qualify_count'], 1, len(players) - 1)
        except ValueError:
            raise ValueError(
                f'qualify_count: integer in [1,{len(players) - 1}] required') from None
        lot = manifest['lot_order']
        if not isinstance(lot, list) or len(lot) != len(players):
            raise ValueError('lot_order: must list every player exactly once')
        for player in lot:
            ident(player)
        if len(set(lot)) != len(lot) or set(lot) != set(players):
            raise ValueError('lot_order: must list every player exactly once')
        tables_raw = array(manifest['tables'], 1, MAX_TABLES)
        archives_raw = array(manifest['archives'], 1, MAX_TABLES)
    except ValueError as error:
        raise Reject('invalid_structure', f'manifest: {error}')
    if len(tables_raw) != len(archives_raw):
        raise Reject('invalid_structure',
                     f'manifest: {len(archives_raw)} archives for {len(tables_raw)} tables; counts must be equal')

    archives = []
    archive_ids = set()
    for index, entry in enumerate(archives_raw):
        try:
            fields(entry, ('game_id', 'state_dir'), ('appeal_dir',))
            ident(entry['game_id'])
            if not isinstance(entry['state_dir'], str):
                raise ValueError('state_dir: string required')
            if 'appeal_dir' in entry and not isinstance(entry['appeal_dir'], str):
                raise ValueError('appeal_dir: string required')
        except ValueError as error:
            raise Reject('invalid_structure', f'archives[{index}]: {error}', archive_index=index)
        if entry['game_id'] in archive_ids:
            raise Reject('invalid_structure', f'archives[{index}]: duplicate game_id',
                         archive_index=index, game_id=entry['game_id'])
        archive_ids.add(entry['game_id'])
        archives.append({'game_id': entry['game_id'], 'state_dir': entry['state_dir'],
                         'appeal_dir': entry.get('appeal_dir')})
    archives.sort(key=lambda item: item['game_id'])

    tables = []
    table_ids = set()
    round_players = {}
    multi = 0
    for index, entry in enumerate(tables_raw):
        try:
            fields(entry, ('table_id', 'round', 'black', 'white', 'candidates'))
            ident(entry['table_id'])
            rnd = integer(entry['round'], 1, 16)
            black, white = entry['black'], entry['white']
            if black not in set(players) or white not in set(players):
                raise ValueError('black/white must be registered players')
            if black == white:
                raise ValueError('black and white must be different players')
            candidates = array(entry['candidates'], 1, len(archives))
            for game_id in candidates:
                ident(game_id)
            if len(set(candidates)) != len(candidates):
                raise ValueError('candidates: duplicate game_id')
        except ValueError as error:
            raise Reject('invalid_structure', f'tables[{index}]: {error}', table_index=index)
        table_id = entry['table_id']
        if table_id in table_ids:
            raise Reject('invalid_structure', f'tables[{index}]: duplicate table_id',
                         table_index=index, table_id=table_id)
        table_ids.add(table_id)
        unknown = sorted(game_id for game_id in candidates if game_id not in archive_ids)
        if unknown:
            raise Reject('invalid_structure', f'tables[{index}]: candidate not declared in archives',
                         table_index=index, table_id=table_id, game_id=unknown)
        for player in (black, white):
            if player in round_players.setdefault(rnd, set()):
                raise Reject('invalid_structure',
                             f'tables[{index}]: player {player} plays more than once in round {rnd}',
                             table_index=index, table_id=table_id, round=rnd, player_id=player)
            round_players[rnd].add(player)
        if len(candidates) > 1:
            multi += 1
        tables.append({'table_id': table_id, 'round': rnd, 'black': black, 'white': white,
                       'candidates': tuple(sorted(candidates)),
                       'candidate_set': frozenset(candidates)})
    if multi > MAX_MULTI_CANDIDATE_TABLES:
        raise Reject('invalid_structure',
                     f'manifest: {multi} tables with multiple candidates; '
                     f'at most {MAX_MULTI_CANDIDATE_TABLES} in scope')
    tables.sort(key=lambda item: item['table_id'])
    return {
        'players': sorted(players),
        'qualify_count': qualify,
        'lot_index': {player: i for i, player in enumerate(lot)},
        'tables': tables,
        'archives': archives,
    }


# ---------- 归档读取（只读，不修改状态目录） ----------

def _read_appeal(manifest_dir, entry, arc, total, receipt_id):
    """只读核对案卷与归档证据，并确认案卷当前状态与对应裁定一致。

    一次读取内状态、轮次和胜负来自同一份已提交案卷；不修改案卷。
    返回 {'appeal_dir', 'status', 'round', 'winner', 'ruling_round'}，
    未结案时 winner 为 None。
    """
    game_id = entry['game_id']
    appeal_dir = entry['appeal_dir']
    try:
        case = appeal.load_case(os.path.join(manifest_dir, appeal_dir))
    except (appeal.Reject, OSError) as error:
        raise Reject('appeal_unreadable', f'archive {game_id}: {error}',
                     game_id=game_id, appeal_dir=appeal_dir)
    if case.get('game_id') != game_id:
        raise Reject('appeal_game_id_mismatch',
                     f'archive {game_id}: appeal case holds game {case.get("game_id")!r}',
                     game_id=game_id, appeal_dir=appeal_dir, found=case.get('game_id'))
    evidence_problem = None
    if case.get('final_receipt_id') != receipt_id:
        evidence_problem = 'appeal last-move receipt does not match the archive receipt'
    if case.get('final_sealed_ply') != total:
        evidence_problem = 'appeal sealed ply does not match the archive last-move position'
    boundary = arc.get('boundary') if isinstance(arc.get('boundary'), dict) else None
    if evidence_problem is None and case.get('original_board') != boundary.get('board'):
        evidence_problem = 'appeal opening board does not match the archived final board'
    if evidence_problem is None and case.get('size') != arc.get('size'):
        evidence_problem = 'appeal board size does not match the archive'
    if evidence_problem is None and case.get('komi') != arc.get('komi'):
        evidence_problem = 'appeal komi does not match the archive'
    if evidence_problem is not None:
        raise Reject('appeal_evidence_mismatch', f'archive {game_id}: {evidence_problem}',
                     game_id=game_id, appeal_dir=appeal_dir)
    try:
        status, round_number, winner = appeal.verify_state(case)
    except appeal.Reject as error:
        raise Reject('appeal_state_inconsistent', f'archive {game_id}: {error}',
                     game_id=game_id, appeal_dir=appeal_dir, **error.details)
    return {'appeal_dir': appeal_dir, 'status': status, 'round': round_number,
            'winner': winner, 'ruling_round': round_number if status == 'closed' else None}


def read_archives(manifest_dir, entries):
    """逐份核对归档状态（及可选案卷）；返回 {game_id: 结果与证据}。

    无案卷时维持原行为：winner 为 None 表示尚未最终封存的正常状态。配置案卷后
    引用的归档必须已有终局证据；只采用案卷当前轮裁定胜负，未结案为 pending_ruling。
    一次读取同一案卷时，状态、轮次和胜负来自同一份已提交状态。
    """
    outcomes = {}
    for entry in entries:
        game_id = entry['game_id']
        state_dir = entry['state_dir']
        path = os.path.join(manifest_dir, state_dir)
        try:
            state = archive.load_state(path)
        except (archive.Reject, OSError) as error:
            raise Reject('archive_unreadable', f'archive {game_id}: {error}',
                         game_id=game_id, state_dir=state_dir)
        if state.get('game_id') != game_id:
            raise Reject('game_id_mismatch',
                         f'archive {game_id}: state directory holds game {state.get("game_id")!r}',
                         game_id=game_id, state_dir=state_dir, found=state.get('game_id'))
        total = state.get('total_moves')
        sealed = state.get('sealed_ply')
        receipts = state.get('receipts')
        final = state.get('final')
        if type(total) is not int or type(sealed) is not int or not isinstance(receipts, dict):
            raise Reject('archive_unreadable', f'archive {game_id}: state missing core fields',
                         game_id=game_id, state_dir=state_dir)
        has_appeal = entry.get('appeal_dir') is not None
        if final is None:
            if has_appeal:
                raise Reject('appeal_archive_not_final',
                             f'archive {game_id}: appeal references an archive without final evidence',
                             game_id=game_id, state_dir=state_dir, appeal_dir=entry['appeal_dir'])
            outcomes[game_id] = {'winner': None, 'receipt_id': None,
                                 'pending': 'awaiting_final', 'appeal': None}
            continue
        problem = None
        if not isinstance(final, dict) or final.get('sealed_ply') != total or sealed != total:
            problem = 'final result is not sealed at the declared last move'
        area = final.get('area') if isinstance(final, dict) else None
        winner = area.get('winner') if isinstance(area, dict) else None
        if problem is None and (winner not in RESULTS or area.get('status') != 'after_two_passes'):
            problem = 'final result has no valid winner'
        final_receipts = [receipt for receipt in receipts.values()
                          if isinstance(receipt, dict) and receipt.get('game_id') == game_id
                          and receipt.get('sealed_ply') == total]
        receipt_id = final_receipts[0].get('receipt_id') if len(final_receipts) == 1 else None
        if problem is None and not isinstance(receipt_id, str):
            problem = 'final result lacks exactly one last-move seal receipt'
        if problem is not None:
            raise Reject('final_inconsistent', f'archive {game_id}: {problem}',
                         game_id=game_id, state_dir=state_dir)
        if not has_appeal:
            outcomes[game_id] = {'winner': winner, 'receipt_id': receipt_id,
                                 'pending': None, 'appeal': None}
            continue
        case_report = _read_appeal(manifest_dir, entry, state, total, receipt_id)
        if case_report['status'] == 'closed':
            if case_report['winner'] not in RESULTS:
                raise Reject('appeal_state_inconsistent',
                             f'archive {game_id}: closed ruling has no valid winner',
                             game_id=game_id, appeal_dir=entry['appeal_dir'])
            outcomes[game_id] = {'winner': case_report['winner'], 'receipt_id': receipt_id,
                                 'pending': None, 'appeal': case_report}
        else:
            outcomes[game_id] = {'winner': None, 'receipt_id': receipt_id,
                                 'pending': 'awaiting_ruling', 'appeal': case_report}
    return outcomes


# ---------- 二分匹配：完整对应关系与各台实际可用候选 ----------

def _kuhn(adj, n_right):
    """确定性二分最大匹配（左点与邻接均按给定顺序）；返回右→左匹配数组。"""
    match_right = [-1] * n_right

    def augment(u, seen):
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                if match_right[v] == -1 or augment(match_right[v], seen):
                    match_right[v] = u
                    return True
        return False

    for u in range(len(adj)):
        augment(u, set())
    return match_right


def _scc(adj):
    """迭代 Tarjan 强连通分量；返回 {节点: 分量号}。"""
    index = {}
    lowlink = {}
    on_stack = set()
    stack = []
    comp = {}
    counter = 0
    comp_count = 0
    for start in range(len(adj)):
        if start in index:
            continue
        todo = [(start, iter(adj[start]))]
        index[start] = lowlink[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while todo:
            node, it = todo[-1]
            descended = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = lowlink[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    todo.append((nxt, iter(adj[nxt])))
                    descended = True
                    break
                elif nxt in on_stack and index[nxt] < lowlink[node]:
                    lowlink[node] = index[nxt]
            if descended:
                continue
            todo.pop()
            if todo:
                parent = todo[-1][0]
                if lowlink[node] < lowlink[parent]:
                    lowlink[parent] = lowlink[node]
            if lowlink[node] == index[node]:
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp[w] = comp_count
                    if w == node:
                        break
                comp_count += 1
    return comp


def _reject_hall(tables, archives, adj, match_left, match_right):
    """无完整对应关系：给出一组可定位的 Hall 违约（台多候选少）。"""
    seen_tables = {u for u in range(len(adj)) if match_left[u] == -1}
    seen_archives = set()
    stack = list(seen_tables)
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v != match_left[u] and v not in seen_archives:
                seen_archives.add(v)
                w = match_right[v]
                if w != -1 and w not in seen_tables:
                    seen_tables.add(w)
                    stack.append(w)
    raise Reject('no_complete_assignment',
                 'tables cannot be matched to archives one-to-one',
                 tables=sorted(tables[u]['table_id'] for u in seen_tables),
                 candidates=sorted(archives[v]['game_id'] for v in seen_archives))


def match_tables(tables, archives):
    """每台实际可用候选：出现在至少一个完整对应关系中的候选（按 game_id 排序）。

    一个可行对应关系让每台恰好采用一份自己的候选，且每份归档恰好用一次；
    没有任何完整对应关系时 Reject('no_complete_assignment')。
    """
    n = len(tables)
    adj = [[j for j, entry in enumerate(archives) if entry['game_id'] in table['candidate_set']]
           for table in tables]
    match_right = _kuhn(adj, n)
    match_left = [-1] * n
    for v, u in enumerate(match_right):
        if u != -1:
            match_left[u] = v
    if -1 in match_left:
        _reject_hall(tables, archives, adj, match_left, match_right)
    # 未匹配边 台→盘、匹配边 盘→台：同一强连通分量内的未匹配边也在某个完美匹配中。
    digraph = [[] for _ in range(2 * n)]
    for u in range(n):
        for v in adj[u]:
            if match_left[u] == v:
                digraph[n + v].append(u)
            else:
                digraph[u].append(n + v)
    comp = _scc(digraph)
    usable = []
    for u in range(n):
        keep = [archives[v]['game_id'] for v in adj[u]
                if match_left[u] == v or comp[u] == comp[n + v]]
        usable.append(sorted(keep))
    return usable


# ---------- 单套对应关系下的名次 ----------

def _standings(players, lot_index, games):
    """games: [(black, white, winner)]；返回 (名次序, 积分, 对手分, 名次字典)。"""
    score = {player: 0 for player in players}
    met = {player: [] for player in players}
    for black, white, winner in games:
        if winner == 'B':
            score[black] += 2
        elif winner == 'W':
            score[white] += 2
        else:
            score[black] += 1
            score[white] += 1
        met[black].append(white)
        met[white].append(black)
    opponent = {player: sum(score[other] for other in met[player]) for player in players}
    order = sorted(players, key=lambda p: (-score[p], -opponent[p], lot_index[p]))
    ranks = {player: i + 1 for i, player in enumerate(order)}
    return order, score, opponent, ranks


# ---------- 汇总 ----------

def evaluate(manifest, manifest_dir):
    """核对清单、归档与案卷，产出 complete / awaiting_final / awaiting_ruling 报告。

    不修改任何目录；仍须先完成原结构、归档与完整对应关系检查，缺对应关系不会被
    awaiting_ruling 掩盖。
    """
    m = validate_manifest(manifest)
    outcomes = read_archives(manifest_dir, m['archives'])
    tables = m['tables']
    archives = m['archives']
    usable = match_tables(tables, archives)
    tables_report = [
        {'table_id': t['table_id'], 'round': t['round'], 'black': t['black'], 'white': t['white'],
         'usable_candidates': usable[i]}
        for i, t in enumerate(tables)
    ]
    awaiting_final = sorted(game_id for game_id, outcome in outcomes.items()
                            if outcome['pending'] == 'awaiting_final')
    awaiting_ruling = sorted(
        ({'game_id': game_id,
          'round': outcome['appeal']['round'],
          'table_ids': sorted(t['table_id'] for t in tables
                              if game_id in t['candidate_set'])}
         for game_id, outcome in outcomes.items()
         if outcome['pending'] == 'awaiting_ruling'),
        key=lambda item: item['game_id'])
    base = {
        'schema': SCHEMA,
        'qualify_count': m['qualify_count'],
        'tables': tables_report,
        'awaiting_game_ids': awaiting_final,
        'awaiting_ruling': awaiting_ruling,
    }
    if awaiting_final:
        return {**base, 'status': 'awaiting_final',
                'players': None, 'qualified': None, 'witnesses': None}
    if awaiting_ruling:
        return {**base, 'status': 'awaiting_ruling',
                'players': None, 'qualified': None, 'witnesses': None}
    return _complete(m, outcomes, usable, base)


def _complete(m, outcomes, usable, base):
    """全部归档已封存：枚举可行结果向量，独立核定名次并给出见证。"""
    players = m['players']
    lot_index = m['lot_index']
    qualify = m['qualify_count']
    tables = m['tables']
    # 单候选台在任何完整对应关系中都取其唯一候选；扣除后剩余归档数恰好多候选台数。
    forced = {}
    multi = []
    for t in tables:
        if len(t['candidates']) == 1:
            forced[t['table_id']] = t['candidates'][0]
        else:
            multi.append(t)
    forced_ids = set(forced.values())
    sub_archives = [a for a in m['archives'] if a['game_id'] not in forced_ids]
    sub_index = {a['game_id']: i for i, a in enumerate(sub_archives)}
    sub_results = [outcomes[a['game_id']]['winner'] for a in sub_archives]
    usable_by_table = {t['table_id']: set(usable[i]) for i, t in enumerate(tables)}
    sub_adj = [sorted(sub_index[g] for g in usable_by_table[t['table_id']] if g in sub_index)
               for t in multi]
    n_sub = len(sub_archives)
    no_used = [False] * n_sub

    def sub_perfect(required, used, start=0):
        """多候选台 start 起能否在未占用归档下按所要求的结果完美匹配。

        required[i] 为 None 表示该台结果未定，可接任何可用归档。
        """
        adj = [[a for a in sub_adj[i]
                if not used[a] and (required[i] is None or sub_results[a] == required[i])]
               for i in range(start, len(multi))]
        match_right = _kuhn(adj, n_sub)
        return sum(1 for u in match_right if u != -1) == len(adj)

    # 枚举可达结果向量：名次只取决于各台胜负，不取决于同胜负的具体盘号。
    vectors = []
    assigned = [None] * len(multi)

    def dfs(i):
        if i == len(multi):
            vectors.append(tuple(assigned))
            return
        for result in RESULTS:
            if any(sub_results[a] == result for a in sub_adj[i]):
                assigned[i] = result
                if sub_perfect(assigned, no_used):
                    dfs(i + 1)
        assigned[i] = None

    dfs(0)

    def games_for(vec):
        games = []
        at = 0
        for t in tables:
            if len(t['candidates']) == 1:
                winner = outcomes[t['candidates'][0]]['winner']
            else:
                winner = vec[at]
                at += 1
            games.append((t['black'], t['white'], winner))
        return games

    best = {}
    worst = {}
    best_vec = {}
    worst_vec = {}
    vec_order = {}
    reference = None
    identical = True
    for vec in vectors:
        vec_order[vec] = len(vec_order)
        order, _score, _opponent, ranks = _standings(players, lot_index, games_for(vec))
        qualified = frozenset(order[:qualify])
        if reference is None:
            reference = qualified
        elif qualified != reference:
            identical = False
        for player in players:
            rank = ranks[player]
            if player not in best or rank < best[player]:
                best[player] = rank
                best_vec[player] = vec
            if player not in worst or rank > worst[player]:
                worst[player] = rank
                worst_vec[player] = vec

    def witness(vec, wid):
        # 具体对应取字典序最小者（台号、盘号均按序），保证见证选择稳定。
        assignment_map = dict(forced)
        used = [False] * n_sub
        for i, t in enumerate(multi):
            for a in sub_adj[i]:
                if not used[a] and sub_results[a] == vec[i]:
                    used[a] = True
                    if sub_perfect(vec, used, start=i + 1):
                        assignment_map[t['table_id']] = sub_archives[a]['game_id']
                        break
                    used[a] = False
        assignment = []
        for table_id, game_id in sorted(assignment_map.items()):
            entry = {'table_id': table_id, 'game_id': game_id,
                     'receipt_id': outcomes[game_id]['receipt_id']}
            case_report = outcomes[game_id]['appeal']
            if case_report is not None:
                # 见证同时反查原末手回执与采用的裁定轮次；无案卷清单字段不变。
                entry['appeal_round'] = case_report['ruling_round']
            assignment.append(entry)
        order, score, opponent, _ranks = _standings(players, lot_index, games_for(vec))
        standings = [
            {'rank': i + 1, 'player_id': player,
             'score': score[player], 'opponent_score': opponent[player]}
            for i, player in enumerate(order)
        ]
        return {'id': wid, 'assignment': assignment, 'standings': standings}

    needed = sorted({best_vec[p] for p in players} | {worst_vec[p] for p in players},
                    key=lambda vec: vec_order[vec])
    witness_of = {}
    witnesses = []
    for vec in needed:
        witness_of[vec] = len(witnesses)
        witnesses.append(witness(vec, len(witnesses)))

    players_report = []
    for player in players:
        if worst[player] <= qualify:
            category = 'certain'
        elif best[player] > qualify:
            category = 'out'
        else:
            category = 'undetermined'
        players_report.append({
            'player_id': player,
            'category': category,
            'best_rank': best[player],
            'best_witness': witness_of[best_vec[player]],
            'worst_rank': worst[player],
            'worst_witness': witness_of[worst_vec[player]],
        })
    return {**base, 'status': 'complete',
            'players': players_report,
            'qualified': sorted(reference) if identical else None,
            'witnesses': witnesses}
