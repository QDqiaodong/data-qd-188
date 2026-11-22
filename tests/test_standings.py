import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from go_record import archive
from go_record.standings_cli import main as standings_main

# 3 路盘、komi 0 的最小终局：黑胜 / 白胜 / 和棋
BLACK_WIN = [('x1', 'B', [0, 0]), ('x2', 'W', None), ('x3', 'B', None)]
WHITE_WIN = [('x1', 'B', None), ('x2', 'W', [0, 0]), ('x3', 'B', None), ('x4', 'W', None)]
DRAW = [('x1', 'B', None), ('x2', 'W', None)]


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = standings_main(argv)
    return code, out.getvalue(), err.getvalue()


class StandingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='go-standings-')
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.out_counter = 0

    # ---- 工具 ----

    def make_game(self, game_id, moves, seal=True):
        """通过原归档流程建一盘；seal=False 时只收包到双停、不封存。"""
        state_dir = os.path.join(self.dir, 'games', game_id)
        spec = {'game_id': game_id, 'total_moves': len(moves), 'size': 3,
                'setup': {'black': [], 'white': []}, 'next_player': 'B', 'komi': '0'}
        archive.create_state(state_dir, spec)
        archive.receive_locked(state_dir, {'game_id': game_id, 'records': [
            {'ply': i + 1, 'id': mv[0], 'player': mv[1], 'point': mv[2], 'revision': 1}
            for i, mv in enumerate(moves)]})
        if seal:
            self.seal_final(game_id)
        return os.path.join('games', game_id)

    def seal_final(self, game_id):
        state_dir = os.path.join(self.dir, 'games', game_id)
        state = archive.load_state(state_dir)
        target = state['total_moves']
        credential = archive.credential_for(state, target)
        receipt, new_state = archive.seal_locked(state_dir, target, credential)
        self.assertIsNotNone(new_state['final'])
        return receipt

    def table(self, table_id, rnd, black, white, candidates):
        return {'table_id': table_id, 'round': rnd, 'black': black, 'white': white,
                'candidates': list(candidates)}

    def manifest(self, players, qualify, lot, tables, game_ids):
        return {'players': list(players), 'qualify_count': qualify, 'lot_order': list(lot),
                'tables': tables,
                'archives': [{'game_id': g, 'state_dir': os.path.join('games', g)} for g in game_ids]}

    def write_manifest(self, data, name='manifest.json'):
        path = os.path.join(self.dir, name)
        Path(path).write_text(json.dumps(data), encoding='utf-8')
        return path

    def run_standings(self, manifest_path, expect=0, output=None):
        if output is None:
            self.out_counter += 1
            output = os.path.join(self.dir, f'report-{self.out_counter}.json')
        code, out, err = run_cli(['--source', manifest_path, '--output', output])
        self.assertEqual(code, expect, err)
        if expect == 0:
            return json.loads(Path(output).read_text(encoding='utf-8')), output
        self.assertFalse(os.path.exists(output), '失败不得留下半份报告')
        return None, err

    def players_by_id(self, report):
        return {p['player_id']: p for p in report['players']}

    def recheck_witnesses(self, report, manifest):
        """独立重算每个见证：一盘一用、末手回执证据、逐台成绩、积分/对手分/名次。"""
        tables = {t['table_id']: t for t in manifest['tables']}
        archives = {a['game_id']: a for a in manifest['archives']}
        lot = {p: i for i, p in enumerate(manifest['lot_order'])}
        players = manifest['players']
        states = {}
        for witness in report['witnesses']:
            seen_games = set()
            covered = set()
            score = {p: 0 for p in players}
            met = {p: [] for p in players}
            for entry in witness['assignment']:
                table = tables[entry['table_id']]
                covered.add(entry['table_id'])
                self.assertIn(entry['game_id'], table['candidates'])
                self.assertNotIn(entry['game_id'], seen_games, '一盘不得二用')
                seen_games.add(entry['game_id'])
                gid = entry['game_id']
                if gid not in states:
                    states[gid] = archive.load_state(
                        os.path.join(self.dir, archives[gid]['state_dir']))
                state = states[gid]
                finals = [r for r in state['receipts'].values()
                          if r['sealed_ply'] == state['total_moves']]
                self.assertEqual(len(finals), 1)
                self.assertEqual(finals[0]['receipt_id'], entry['receipt_id'])
                winner = state['final']['area']['winner']
                black, white = table['black'], table['white']
                if winner == 'B':
                    score[black] += 2
                elif winner == 'W':
                    score[white] += 2
                else:
                    score[black] += 1
                    score[white] += 1
                met[black].append(white)
                met[white].append(black)
            self.assertEqual(covered, set(tables), '见证必须覆盖全部台次')
            opponent = {p: sum(score[o] for o in met[p]) for p in players}
            order = sorted(players, key=lambda p: (-score[p], -opponent[p], lot[p]))
            expect = [{'rank': i + 1, 'player_id': p,
                       'score': score[p], 'opponent_score': opponent[p]}
                      for i, p in enumerate(order)]
            self.assertEqual(witness['standings'], expect)
        # 最好/最差名次与所引见证一致
        for entry in report['players']:
            best_w = report['witnesses'][entry['best_witness']]
            worst_w = report['witnesses'][entry['worst_witness']]
            self.assertEqual(best_w['standings'][entry['best_rank'] - 1]['player_id'],
                             entry['player_id'])
            self.assertEqual(worst_w['standings'][entry['worst_rank'] - 1]['player_id'],
                             entry['player_id'])

    # ---- 场景一：一轮两台候选互换 ----

    def test_swap_witnesses_then_elimination(self):
        self.make_game('X', BLACK_WIN)
        self.make_game('Y', WHITE_WIN)
        tables = [self.table('T1', 1, 'A', 'B', ['X', 'Y']),
                  self.table('T2', 1, 'C', 'D', ['X', 'Y'])]
        data = self.manifest(['A', 'B', 'C', 'D'], 1, ['A', 'C', 'D', 'B'], tables, ['X', 'Y'])
        report, _ = self.run_standings(self.write_manifest(data))
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['tables'][0]['usable_candidates'], ['X', 'Y'])
        self.assertEqual(report['tables'][1]['usable_candidates'], ['X', 'Y'])
        by_id = self.players_by_id(report)
        # A、C 各有一套见证能排第一；B、D 确定无缘
        self.assertEqual((by_id['A']['best_rank'], by_id['A']['worst_rank']), (1, 3))
        self.assertEqual((by_id['C']['best_rank'], by_id['C']['worst_rank']), (1, 3))
        self.assertEqual((by_id['B']['best_rank'], by_id['B']['worst_rank']), (2, 4))
        self.assertEqual((by_id['D']['best_rank'], by_id['D']['worst_rank']), (2, 4))
        self.assertEqual(by_id['A']['category'], 'undetermined')
        self.assertEqual(by_id['C']['category'], 'undetermined')
        self.assertEqual(by_id['B']['category'], 'out')
        self.assertEqual(by_id['D']['category'], 'out')
        # 两套对应关系晋级者不同 → 名单为 null
        self.assertIsNone(report['qualified'])
        # 不能用同一个 Y 把 D 推到第一：D 的最好名次是 2
        self.assertEqual(by_id['D']['best_rank'], 2)
        # A、C 的第一名见证是两套不同的对应关系
        self.assertNotEqual(by_id['A']['best_witness'], by_id['C']['best_witness'])
        self.recheck_witnesses(report, data)

        # 删去 T1 对 Y 的候选（不改任何归档）：T2 只能用 Y，名单确定为 A
        tables2 = [self.table('T1', 1, 'A', 'B', ['X']),
                   self.table('T2', 1, 'C', 'D', ['X', 'Y'])]
        data2 = self.manifest(['A', 'B', 'C', 'D'], 1, ['A', 'C', 'D', 'B'], tables2, ['X', 'Y'])
        report2, _ = self.run_standings(self.write_manifest(data2, 'manifest2.json'))
        self.assertEqual(report2['tables'][0]['usable_candidates'], ['X'])
        self.assertEqual(report2['tables'][1]['usable_candidates'], ['Y'])
        self.assertEqual(report2['qualified'], ['A'])
        by_id2 = self.players_by_id(report2)
        self.assertEqual(by_id2['A']['category'], 'certain')
        self.assertEqual((by_id2['A']['best_rank'], by_id2['A']['worst_rank']), (1, 1))
        self.assertEqual(by_id2['D']['category'], 'out')
        self.recheck_witnesses(report2, data2)

    # ---- 场景二：每台非空、每盘被提名，仍无全局解 ----

    def test_no_complete_assignment(self):
        for gid, moves in [('X', BLACK_WIN), ('Y', WHITE_WIN), ('Z', DRAW)]:
            self.make_game(gid, moves)
        tables = [self.table('T1', 1, 'P1', 'P2', ['X']),
                  self.table('T2', 1, 'P3', 'P4', ['X']),
                  self.table('T3', 1, 'P5', 'P6', ['Y', 'Z'])]
        players = ['P1', 'P2', 'P3', 'P4', 'P5', 'P6']
        data = self.manifest(players, 1, players, tables, ['X', 'Y', 'Z'])
        _, err = self.run_standings(self.write_manifest(data), expect=2)
        self.assertIn('no_complete_assignment', err)
        self.assertIn('T1', err)
        self.assertIn('T2', err)

    # ---- 场景三：10 个多候选台对 10 份同胜负归档 ----

    def test_ten_multi_tables_same_result_archives(self):
        game_ids = [f'G{i}' for i in range(10)]
        for gid in game_ids:
            self.make_game(gid, BLACK_WIN)
        players = []
        tables = []
        for i in range(10):
            black, white = f'B{i}', f'W{i}'
            players += [black, white]
            tables.append(self.table(f'T{i}', 1, black, white, game_ids))
        # 抽签序刻意不按 ID 排序
        lot = list(reversed(players))
        data = self.manifest(players, 3, lot, tables, game_ids)
        report, _ = self.run_standings(self.write_manifest(data))
        self.assertEqual(report['status'], 'complete')
        # 每台保留全部真实可用的盘号：同胜负不合并
        for table in report['tables']:
            self.assertEqual(table['usable_candidates'], sorted(game_ids))
        # 所有对应关系结果相同 → 唯一见证，名单确定
        self.assertEqual(len(report['witnesses']), 1)
        expect_qualified = sorted([p for p in lot if p.startswith('B')][:3])
        self.assertEqual(report['qualified'], sorted(expect_qualified))
        by_id = self.players_by_id(report)
        for player in players:
            entry = by_id[player]
            self.assertEqual(entry['best_rank'], entry['worst_rank'])
            self.assertEqual(entry['best_witness'], entry['worst_witness'])
            self.assertEqual(entry['category'],
                             'certain' if player in expect_qualified else 'out')
        # 见证是 10 台对 10 盘的一一对应
        assignment = report['witnesses'][0]['assignment']
        self.assertEqual(len(assignment), 10)
        self.assertEqual(len({e['game_id'] for e in assignment}), 10)
        self.recheck_witnesses(report, data)

    # ---- 场景四：多轮、对手分按本套计算、和棋得分 ----

    def test_multi_round_opponent_score_and_draw(self):
        self.make_game('G1', DRAW)        # T1：A、B 战平
        self.make_game('G2', BLACK_WIN)   # T2：C 胜 D
        self.make_game('G3', BLACK_WIN)   # 第二轮黑胜
        self.make_game('G4', WHITE_WIN)   # 第二轮白胜
        tables = [self.table('T1', 1, 'A', 'B', ['G1']),
                  self.table('T2', 1, 'C', 'D', ['G2']),
                  self.table('T3', 2, 'A', 'C', ['G3', 'G4']),
                  self.table('T4', 2, 'B', 'D', ['G3', 'G4'])]
        data = self.manifest(['A', 'B', 'C', 'D'], 1, ['A', 'B', 'C', 'D'], tables,
                             ['G1', 'G2', 'G3', 'G4'])
        report, _ = self.run_standings(self.write_manifest(data))
        self.assertEqual(report['status'], 'complete')
        self.assertIsNone(report['qualified'])
        by_id = self.players_by_id(report)
        # v1（T3=G3 黑胜、T4=G4 白胜）：A3 C2 D2 B1；对手分 A3 C5 D3 B5
        #   名次 A1 C2 D3 B4
        # v2（T3=G4 白胜、T4=G3 黑胜）：C4 B3 A1 D0；对手分 C1 B1 A7 D7
        #   名次 C1 B2 A3 D4
        self.assertEqual((by_id['A']['best_rank'], by_id['A']['worst_rank']), (1, 3))
        self.assertEqual((by_id['B']['best_rank'], by_id['B']['worst_rank']), (2, 4))
        self.assertEqual((by_id['C']['best_rank'], by_id['C']['worst_rank']), (1, 2))
        self.assertEqual((by_id['D']['best_rank'], by_id['D']['worst_rank']), (3, 4))
        self.assertEqual(by_id['A']['category'], 'undetermined')
        self.assertEqual(by_id['C']['category'], 'undetermined')
        self.assertEqual(by_id['B']['category'], 'out')
        self.assertEqual(by_id['D']['category'], 'out')
        # 两台各自可用候选仍是 [G3, G4]，但两套向量不能任意拼接（D 最好只有 3）
        self.assertEqual(report['tables'][2]['usable_candidates'], ['G3', 'G4'])
        self.assertEqual(report['tables'][3]['usable_candidates'], ['G3', 'G4'])
        # 和棋得分：每位见证里 A、B 第一轮各得 1 分
        for witness in report['witnesses']:
            standings = {s['player_id']: s for s in witness['standings']}
            self.assertGreaterEqual(standings['A']['score'], 1)
            self.assertGreaterEqual(standings['B']['score'], 1)
        # 逐套对手分核验（v1：C 的对手分 5 = D2 + A3；v2：C 的对手分 1 = D0 + A1）
        c_scores = sorted(
            (w['standings'][[s['player_id'] for s in w['standings']].index('C')]['score'],
             w['standings'][[s['player_id'] for s in w['standings']].index('C')]['opponent_score'])
            for w in report['witnesses'])
        self.assertEqual(c_scores, [(2, 5), (4, 1)])
        self.recheck_witnesses(report, data)

    # ---- 场景五：完全同分时才用抽签序，且抽签序不被重排 ----

    def test_lot_order_decides_only_after_full_tie(self):
        self.make_game('G1', BLACK_WIN)  # A 胜 B；C、D 本轮缺席
        tables = [self.table('T1', 1, 'A', 'B', ['G1'])]
        # 抽签序 D 在 C 前，刻意非字典序
        data = self.manifest(['A', 'B', 'C', 'D'], 3, ['D', 'C', 'B', 'A'], tables, ['G1'])
        report, _ = self.run_standings(self.write_manifest(data))
        by_id = self.players_by_id(report)
        # A(2,对手分0) 第一；B(0,对手分2) 第二；C、D 全同分 → 抽签序 D 第三、C 第四
        self.assertEqual((by_id['A']['best_rank'], by_id['B']['best_rank'],
                          by_id['D']['best_rank'], by_id['C']['best_rank']), (1, 2, 3, 4))
        self.assertEqual(report['qualified'], ['A', 'B', 'D'])
        self.assertEqual(by_id['C']['category'], 'out')
        self.assertEqual(by_id['D']['category'], 'certain')
        self.recheck_witnesses(report, data)

    # ---- 场景六：双停未封 → awaiting_final；封存后 → complete ----

    def test_awaiting_final_then_complete(self):
        self.make_game('G1', BLACK_WIN)
        self.make_game('G2', DRAW, seal=False)
        # G2 已合法走到双停但尚未封到末手
        state = archive.load_state(os.path.join(self.dir, 'games', 'G2'))
        self.assertIsNone(state['final'])
        status = archive.view(state)
        self.assertTrue(status['double_pass'])
        tables = [self.table('T1', 1, 'A', 'B', ['G1']),
                  self.table('T2', 1, 'C', 'D', ['G2'])]
        data = self.manifest(['A', 'B', 'C', 'D'], 1, ['A', 'B', 'C', 'D'], tables, ['G1', 'G2'])
        path = self.write_manifest(data)
        report, out_path = self.run_standings(path)
        self.assertEqual(report['status'], 'awaiting_final')
        self.assertEqual(report['awaiting_game_ids'], ['G2'])
        self.assertEqual(report['tables'][0]['usable_candidates'], ['G1'])
        self.assertEqual(report['tables'][1]['usable_candidates'], ['G2'])
        # 不给分、不列晋级结论
        self.assertIsNone(report['players'])
        self.assertIsNone(report['qualified'])
        self.assertIsNone(report['witnesses'])
        # 既有输出保护：同一目标复跑退出 2 且原报告字节不变
        before = Path(out_path).read_bytes()
        code, _out, err = run_cli(['--source', path, '--output', out_path])
        self.assertEqual(code, 2)
        self.assertIn('output_exists', err)
        self.assertEqual(Path(out_path).read_bytes(), before)
        # G2 通过原流程真正封到末手后，同一清单得到 complete
        self.seal_final('G2')
        report2, _ = self.run_standings(path)
        self.assertEqual(report2['status'], 'complete')
        self.assertEqual(report2['awaiting_game_ids'], [])
        self.assertEqual(report2['qualified'], ['A'])
        by_id = self.players_by_id(report2)
        # A 胜、C/D 战平各 1 分、B 负
        self.assertEqual((by_id['A']['best_rank'], by_id['B']['best_rank']), (1, 4))
        self.assertEqual((by_id['C']['best_rank'], by_id['D']['best_rank']), (2, 3))
        self.recheck_witnesses(report2, data)

    # ---- 归档证据错误 ----

    def test_game_id_mismatch(self):
        self.make_game('REAL', BLACK_WIN)
        self.make_game('G2', DRAW)
        tables = [self.table('T1', 1, 'A', 'B', ['X']),
                  self.table('T2', 1, 'C', 'D', ['G2'])]
        # 清单声明盘号 X，但状态目录里是 REAL
        data = self.manifest(['A', 'B', 'C', 'D'], 1, ['A', 'B', 'C', 'D'], tables, ['X', 'G2'])
        data['archives'][0]['state_dir'] = os.path.join('games', 'REAL')
        _, err = self.run_standings(self.write_manifest(data), expect=2)
        self.assertIn('game_id_mismatch', err)

    def test_archive_unreadable(self):
        self.make_game('G2', DRAW)
        tables = [self.table('T1', 1, 'A', 'B', ['G1']),
                  self.table('T2', 1, 'C', 'D', ['G2'])]
        data = self.manifest(['A', 'B', 'C', 'D'], 1, ['A', 'B', 'C', 'D'], tables, ['G1', 'G2'])
        _, err = self.run_standings(self.write_manifest(data), expect=2)
        self.assertIn('archive_unreadable', err)
        self.assertIn('G1', err)

    def test_final_inconsistent(self):
        self.make_game('G1', BLACK_WIN)
        self.make_game('G2', DRAW)
        # 篡改 G1 状态副本：final 还在但末手封存回执被移除
        src = os.path.join(self.dir, 'games', 'G1')
        forged = os.path.join(self.dir, 'games', 'FORGED')
        os.makedirs(forged)
        state = archive.load_state(src)
        state['receipts'] = {}
        Path(forged, 'state.json').write_text(json.dumps(state), encoding='utf-8')
        tables = [self.table('T1', 1, 'A', 'B', ['G1']),
                  self.table('T2', 1, 'C', 'D', ['G2'])]
        data = self.manifest(['A', 'B', 'C', 'D'], 1, ['A', 'B', 'C', 'D'], tables, ['G1', 'G2'])
        data['archives'][0]['state_dir'] = os.path.join('games', 'FORGED')
        _, err = self.run_standings(self.write_manifest(data), expect=2)
        self.assertIn('final_inconsistent', err)
        # 胜负值无效同样拒绝
        state2 = archive.load_state(src)
        state2['final']['area']['winner'] = 'X'
        Path(forged, 'state.json').write_text(json.dumps(state2), encoding='utf-8')
        _, err = self.run_standings(self.write_manifest(data, 'm2.json'), expect=2)
        self.assertIn('final_inconsistent', err)

    # ---- 清单结构错误 ----

    def test_invalid_structure_cases(self):
        self.make_game('G1', BLACK_WIN)
        self.make_game('G2', WHITE_WIN)
        players = ['A', 'B', 'C', 'D']
        lot = players

        def expect_invalid(data, needle):
            _, err = self.run_standings(self.write_manifest(data), expect=2)
            self.assertIn('invalid_structure', err)
            self.assertIn(needle, err)

        base_tables = [self.table('T1', 1, 'A', 'B', ['G1']),
                       self.table('T2', 1, 'C', 'D', ['G2'])]
        # 黑白同人
        expect_invalid(self.manifest(players, 1, lot,
                                     [self.table('T1', 1, 'A', 'A', ['G1']),
                                      self.table('T2', 1, 'C', 'D', ['G2'])], ['G1', 'G2']),
                       'different')
        # 同人同轮两次上场
        expect_invalid(self.manifest(players, 1, lot,
                                     [self.table('T1', 1, 'A', 'B', ['G1']),
                                      self.table('T2', 1, 'A', 'D', ['G2'])], ['G1', 'G2']),
                       'more than once')
        # 候选不在归档清单中
        expect_invalid(self.manifest(players, 1, lot,
                                     [self.table('T1', 1, 'A', 'B', ['G9']),
                                      self.table('T2', 1, 'C', 'D', ['G2'])], ['G1', 'G2']),
                       'not declared')
        # 归档数与台数不等
        expect_invalid(self.manifest(players, 1, lot, base_tables, ['G1']), 'equal')
        # qualify_count 越界
        expect_invalid(self.manifest(players, 4, lot, base_tables, ['G1', 'G2']), 'qualify')
        # 抽签序缺人
        expect_invalid(self.manifest(players, 1, ['A', 'B', 'C'], base_tables, ['G1', 'G2']),
                       'lot_order')
        # 重复台号
        expect_invalid(self.manifest(players, 1, lot,
                                     [self.table('T1', 1, 'A', 'B', ['G1']),
                                      self.table('T1', 1, 'C', 'D', ['G2'])], ['G1', 'G2']),
                       'duplicate table_id')
        # 未登记选手
        expect_invalid(self.manifest(players, 1, lot,
                                     [self.table('T1', 1, 'A', 'Z', ['G1']),
                                      self.table('T2', 1, 'C', 'D', ['G2'])], ['G1', 'G2']),
                       'registered')
        # 11 个多候选台超出交接范围
        many_players = [f'P{i}' for i in range(22)]
        many_games = [f'M{i}' for i in range(11)]
        for gid in many_games:
            self.make_game(gid, DRAW)
        many_tables = [self.table(f'T{i}', 1, many_players[2 * i], many_players[2 * i + 1],
                                  many_games) for i in range(11)]
        expect_invalid(self.manifest(many_players, 1, many_players, many_tables, many_games),
                       'at most 10')

    def test_duplicate_json_key_rejected(self):
        self.make_game('G1', BLACK_WIN)
        path = os.path.join(self.dir, 'dup.json')
        Path(path).write_text(
            '{"players":["A","B"],"players":["A","B"],"qualify_count":1,'
            '"lot_order":["A","B"],"tables":[],"archives":[]}', encoding='utf-8')
        _, err = self.run_standings(path, expect=2)
        self.assertIn('duplicate JSON key', err)

    # ---- 输入顺序稳定性 ----

    def test_input_order_permutation_keeps_report_bytes(self):
        for gid, moves in [('G1', DRAW), ('G2', BLACK_WIN), ('G3', BLACK_WIN),
                           ('G4', WHITE_WIN)]:
            self.make_game(gid, moves)
        tables = [self.table('T1', 1, 'A', 'B', ['G1']),
                  self.table('T2', 1, 'C', 'D', ['G2']),
                  self.table('T3', 2, 'A', 'C', ['G3', 'G4']),
                  self.table('T4', 2, 'B', 'D', ['G3', 'G4'])]
        data = self.manifest(['A', 'B', 'C', 'D'], 1, ['A', 'B', 'C', 'D'], tables,
                             ['G1', 'G2', 'G3', 'G4'])
        report1, out1 = self.run_standings(self.write_manifest(data))
        # 只打乱不表示优先级的顺序：players/tables/archives/candidates；lot_order 不动
        shuffled = json.loads(json.dumps(data))
        shuffled['players'] = list(reversed(data['players']))
        shuffled['tables'] = list(reversed(data['tables']))
        shuffled['archives'] = list(reversed(data['archives']))
        for table in shuffled['tables']:
            table['candidates'] = list(reversed(table['candidates']))
        report2, out2 = self.run_standings(self.write_manifest(shuffled, 'shuffled.json'))
        self.assertEqual(Path(out1).read_bytes(), Path(out2).read_bytes())
        self.assertEqual(report1, report2)

    # ---- 无解检查不因有盘未封存而跳过 ----

    def test_no_matching_check_not_skipped_by_pending_games(self):
        self.make_game('X', BLACK_WIN)
        self.make_game('Y', WHITE_WIN, seal=False)  # 尚未封存
        self.make_game('Z', DRAW)
        tables = [self.table('T1', 1, 'P1', 'P2', ['X']),
                  self.table('T2', 1, 'P3', 'P4', ['X']),
                  self.table('T3', 1, 'P5', 'P6', ['Y', 'Z'])]
        players = ['P1', 'P2', 'P3', 'P4', 'P5', 'P6']
        data = self.manifest(players, 1, players, tables, ['X', 'Y', 'Z'])
        _, err = self.run_standings(self.write_manifest(data), expect=2)
        self.assertIn('no_complete_assignment', err)

    def test_awaiting_reports_usable_candidates_for_multi_tables(self):
        self.make_game('G1', BLACK_WIN)
        self.make_game('G2', WHITE_WIN, seal=False)
        tables = [self.table('T1', 1, 'A', 'B', ['G1', 'G2']),
                  self.table('T2', 1, 'C', 'D', ['G1', 'G2'])]
        data = self.manifest(['A', 'B', 'C', 'D'], 1, ['A', 'B', 'C', 'D'], tables, ['G1', 'G2'])
        report, _ = self.run_standings(self.write_manifest(data))
        self.assertEqual(report['status'], 'awaiting_final')
        self.assertEqual(report['awaiting_game_ids'], ['G2'])
        self.assertEqual(report['tables'][0]['usable_candidates'], ['G1', 'G2'])
        self.assertEqual(report['tables'][1]['usable_candidates'], ['G1', 'G2'])

    # ---- 同一对手遇到两次算两次 ----

    def test_same_opponent_twice_counts_twice(self):
        self.make_game('G1', BLACK_WIN)   # 第一轮 A 胜 B
        self.make_game('G2', WHITE_WIN)   # 第二轮 B 胜 A
        self.make_game('G3', DRAW)        # C、D 战平
        tables = [self.table('T1', 1, 'A', 'B', ['G1']),
                  self.table('T2', 2, 'A', 'B', ['G2']),
                  self.table('T3', 1, 'C', 'D', ['G3'])]
        data = self.manifest(['A', 'B', 'C', 'D'], 1, ['A', 'B', 'C', 'D'], tables,
                             ['G1', 'G2', 'G3'])
        report, _ = self.run_standings(self.write_manifest(data))
        witness = report['witnesses'][0]
        standings = {s['player_id']: s for s in witness['standings']}
        # A、B 各 2 分；对手分 = 对方积分加两次 = 4，不是 2
        self.assertEqual(standings['A']['opponent_score'], 4)
        self.assertEqual(standings['B']['opponent_score'], 4)
        # 积分、对手分全同 → 抽签序 A 在 B 前
        self.assertEqual((standings['A']['rank'], standings['B']['rank']), (1, 2))
        self.assertEqual(report['qualified'], ['A'])
        self.recheck_witnesses(report, data)

    # ---- 归档目录只读 ----

    def test_archives_are_not_modified(self):
        self.make_game('X', BLACK_WIN)
        self.make_game('Y', WHITE_WIN)
        tables = [self.table('T1', 1, 'A', 'B', ['X', 'Y']),
                  self.table('T2', 1, 'C', 'D', ['X', 'Y'])]
        data = self.manifest(['A', 'B', 'C', 'D'], 1, ['A', 'C', 'D', 'B'], tables, ['X', 'Y'])
        before = {gid: Path(self.dir, 'games', gid, 'state.json').read_bytes()
                  for gid in ('X', 'Y')}
        self.run_standings(self.write_manifest(data))
        for gid in ('X', 'Y'):
            self.assertEqual(Path(self.dir, 'games', gid, 'state.json').read_bytes(),
                             before[gid])


if __name__ == '__main__':
    unittest.main()
