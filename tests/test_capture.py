import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from go_record import capture
from go_record.capture_cli import main as capture_main

ROOT = Path(__file__).resolve().parents[1]


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = capture_main(argv)
    return code, out.getvalue(), err.getvalue()


def move(mid, player, point):
    return {'id': mid, 'player': player, 'point': point}


def record(size, black, white, next_player, moves, komi='0'):
    return {'size': size,
            'setup': {'black': [list(p) for p in black],
                      'white': [list(p) for p in white]},
            'next_player': next_player, 'komi': komi, 'moves': moves}


class CaptureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='go-capture-')
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.counter = 0

    def write_record(self, name, data):
        path = os.path.join(self.dir, name + '.json')
        Path(path).write_text(json.dumps(data), encoding='utf-8')
        return name + '.json'

    def write_manifest(self, manifest, name='manifest.json'):
        path = os.path.join(self.dir, name)
        Path(path).write_text(json.dumps(manifest), encoding='utf-8')
        return path

    def run_study(self, manifest, expect=0, output=None):
        source = self.write_manifest(manifest)
        if output is None:
            self.counter += 1
            output = os.path.join(self.dir, f'report-{self.counter}.json')
        code, out, err = run_cli(['--source', source, '--output', output])
        self.assertEqual(code, expect, err)
        if expect == 0:
            return json.loads(Path(output).read_text(encoding='utf-8')), out
        return None, err

    def study(self, manifest):
        return capture.build_report(capture.validate_manifest(manifest), self.dir)

    def by_query(self, report):
        return {item['query_id']: item for item in report['queries']}

    def by_lookup(self, report):
        return {item['lookup_id']: item for item in report['lookups']}


# ---------------------------------------------------------------------------
# 验收 1：setup 与 played 的来源区分、逐子反查、盘数去重。
# ---------------------------------------------------------------------------

class AcceptanceOne(CaptureCase):
    def fixture(self):
        black = [[0, c] for c in range(5)] + [[2, c] for c in range(5)] + [[1, 0]]
        G = record(5, black, [[1, 2], [1, 3]], 'W',
                   [move('g1', 'W', [1, 1]), move('g2', 'B', [1, 4])])
        H = record(5, black, [[1, 2], [1, 3], [1, 1]], 'W',
                   [move('h1', 'W', None), move('h2', 'B', [1, 4])])
        self.write_record('G', G)
        self.write_record('H', H)
        return {
            'games': [{'game_id': 'G', 'path': 'G.json'},
                      {'game_id': 'H', 'path': 'H.json'}],
            'queries': [
                {'query_id': 'setup', 'capture_from': 2, 'capture_to': 2,
                 'origin': 'setup'},
                {'query_id': 'life1', 'capture_from': 2, 'capture_to': 2,
                 'origin': 'played', 'life_from': 1, 'life_to': 1},
                {'query_id': 'life2', 'capture_from': 2, 'capture_to': 2,
                 'origin': 'played', 'life_from': 2, 'life_to': 2},
            ],
            'lookups': [
                {'lookup_id': 'g11', 'game_id': 'G', 'capture_ply': 2,
                 'point': [1, 1]},
                {'lookup_id': 'h11', 'game_id': 'H', 'capture_ply': 2,
                 'point': [1, 1]},
                {'lookup_id': 'g12', 'game_id': 'G', 'capture_ply': 2,
                 'point': [1, 2]},
            ],
        }

    def test_setup_played_counts_and_lookups(self):
        report = self.study(self.fixture())
        q = self.by_query(report)
        self.assertEqual((q['setup']['stone_count'], q['setup']['game_count']), (5, 2))
        self.assertEqual((q['life1']['stone_count'], q['life1']['game_count']), (1, 1))
        self.assertEqual((q['life2']['stone_count'], q['life2']['game_count']), (0, 0))
        lookups = self.by_lookup(report)
        self.assertEqual(lookups['g11']['fact']['origin'], 'played')
        self.assertEqual(lookups['g11']['fact']['source_ply'], 1)
        self.assertEqual(lookups['g11']['fact']['source_move_id'], 'g1')
        self.assertEqual(lookups['g11']['fact']['life'], 1)
        self.assertEqual(lookups['h11']['fact']['origin'], 'setup')
        self.assertIsNone(lookups['h11']['fact']['source_ply'])
        self.assertIsNone(lookups['h11']['fact']['life'])
        self.assertEqual(lookups['g12']['fact']['origin'], 'setup')

    def test_terminal_boards_identical(self):
        from go_record.service import process
        boards = {entry['game_id']: json.loads(
            Path(self.dir, entry['path']).read_text(encoding='utf-8'))
            for entry in self.fixture()['games']}
        g, h = process(boards['G']), process(boards['H'])
        self.assertEqual(g['trace'][1]['captured'], h['trace'][1]['captured'])
        self.assertEqual(sorted(g['trace'][1]['captured']), [[1, 1], [1, 2], [1, 3]])
        self.assertEqual(g['board'], h['board'])


class MultiLife(CaptureCase):
    def fixture(self):
        # 5 路：黑第 0、2、4 行占满，另加 [1,0]、[3,0]。白 [1,1]、[3,1] 各为单子，
        # 由黑在 [1,2]、[3,2] 分别提掉。两子寿命不同（3 与 5），中间黑补一手避免
        # 双停提前结束；停手照常占手号。
        #   1 W [1,1]  2 B pass  3 W [3,1]  4 B [1,2] 提[1,1] (life 3)
        #   5 W pass   6 B [1,3]  7 W pass   8 B [3,2] 提[3,1] (life 5)
        black = ([[0, c] for c in range(5)] + [[2, c] for c in range(5)]
                 + [[4, c] for c in range(5)] + [[1, 0], [3, 0]])
        moves = [move('w1', 'W', [1, 1]), move('b2', 'B', None),
                 move('w3', 'W', [3, 1]), move('b4', 'B', [1, 2]),
                 move('w5', 'W', None), move('b6', 'B', [1, 3]),
                 move('w7', 'W', None), move('b8', 'B', [3, 2])]
        self.write_record('M', record(5, black, [], 'W', moves))
        return {'games': [{'game_id': 'M', 'path': 'M.json'}],
                'queries': [
                    {'query_id': 'wide', 'capture_from': 4, 'capture_to': 8,
                     'origin': 'played', 'life_from': 3, 'life_to': 5},
                    {'query_id': 'narrow_cap', 'capture_from': 4,
                     'capture_to': 4, 'origin': 'played', 'life_from': 3,
                     'life_to': 5},
                    {'query_id': 'narrow_life', 'capture_from': 4,
                     'capture_to': 8, 'origin': 'played', 'life_from': 5,
                     'life_to': 5},
                ],
                'lookups': [
                    {'lookup_id': 'first', 'game_id': 'M', 'capture_ply': 4,
                     'point': [1, 1]},
                    {'lookup_id': 'second', 'game_id': 'M', 'capture_ply': 8,
                     'point': [3, 1]}]}

    def test_two_axes_and_pass_life(self):
        report = self.study(self.fixture())
        q = self.by_query(report)
        self.assertEqual((q['wide']['stone_count'], q['wide']['game_count']), (2, 1))
        self.assertEqual((q['narrow_cap']['stone_count'],
                          q['narrow_cap']['game_count']), (1, 1))
        self.assertEqual((q['narrow_life']['stone_count'],
                          q['narrow_life']['game_count']), (1, 1))
        lookups = self.by_lookup(report)
        self.assertEqual(lookups['first']['fact']['life'], 3)
        self.assertEqual(lookups['second']['fact']['life'], 5)
        self.assertEqual(lookups['second']['fact']['source_ply'], 3)

    def test_same_file_two_games_counted_separately(self):
        black = [[0, c] for c in range(5)] + [[2, c] for c in range(5)] + [[1, 0]]
        self.write_record('same', record(
            5, black, [[1, 2], [1, 3]], 'W',
            [move('w1', 'W', [1, 1]), move('b2', 'B', [1, 4])]))
        report = self.study({
            'games': [{'game_id': 'A', 'path': 'same.json'},
                      {'game_id': 'B', 'path': 'same.json'}],
            'queries': [{'query_id': 'q', 'capture_from': 2, 'capture_to': 2,
                         'origin': 'setup'}],
            'lookups': [{'lookup_id': 'a', 'game_id': 'A', 'capture_ply': 2,
                         'point': [1, 1]},
                        {'lookup_id': 'b', 'game_id': 'B', 'capture_ply': 2,
                         'point': [1, 1]}]})
        self.assertEqual((report['queries'][0]['stone_count'],
                          report['queries'][0]['game_count']), (4, 2))
        lookups = self.by_lookup(report)
        self.assertEqual(lookups['a']['fact']['origin'], 'played')
        self.assertEqual(lookups['b']['fact']['origin'], 'played')


# ---------------------------------------------------------------------------
# 验收 2：同点重新落子、停手计寿命、闭区间端点、简单劫非法前缀。
# ---------------------------------------------------------------------------

class Recapture(CaptureCase):
    """同坐标先后两颗白子分别被提；第二颗来源是第二次落子。"""

    def fixture(self):
        # 7 路。白块 [1,1],[1,2] 由 setup 给定，唯一气 [1,3]。
        # 第 1 手白停手；第 2 手黑 [1,3] 提掉整个白块（两颗 setup）。
        # 第 3 手白在 [1,1] 重新落子（此时 [1,2] 空，有气，合法）。
        # 第 4 手黑 [6,6]、第 5 手白停手、第 6 手黑 [1,2] 再次提 [1,1]。
        # 第二颗子来源为第 3 手，寿命 6-3=3（中间停手照常占手号）。
        black = [[0, 1], [0, 2], [2, 1], [2, 2], [1, 0]]
        white = [[1, 1], [1, 2]]
        moves = [move('w1', 'W', None), move('b2', 'B', [1, 3]),
                 move('w3', 'W', [1, 1]), move('b4', 'B', [6, 6]),
                 move('w5', 'W', None), move('b6', 'B', [1, 2])]
        self.write_record('R', record(7, black, white, 'W', moves))
        return {'games': [{'game_id': 'R', 'path': 'R.json'}],
                'queries': [
                    # 第一次提子：两颗 setup。
                    {'query_id': 'first_setup', 'capture_from': 2,
                     'capture_to': 2, 'origin': 'setup'},
                    # 第二次提子：一颗 played，life 3，闭区间端点恰为 [3,3]。
                    {'query_id': 'second_played', 'capture_from': 6,
                     'capture_to': 6, 'origin': 'played', 'life_from': 3,
                     'life_to': 3},
                    # 端点外一格应为 0。
                    {'query_id': 'life_out', 'capture_from': 6, 'capture_to': 6,
                     'origin': 'played', 'life_from': 4, 'life_to': 4},
                    # 提子手端点外一格应为 0。
                    {'query_id': 'cap_out', 'capture_from': 5, 'capture_to': 5,
                     'origin': 'played', 'life_from': 3, 'life_to': 3},
                ],
                'lookups': [
                    {'lookup_id': 'first_11', 'game_id': 'R',
                     'capture_ply': 2, 'point': [1, 1]},
                    {'lookup_id': 'first_12', 'game_id': 'R',
                     'capture_ply': 2, 'point': [1, 2]},
                    {'lookup_id': 'second_11', 'game_id': 'R',
                     'capture_ply': 6, 'point': [1, 1]},
                    {'lookup_id': 'absent', 'game_id': 'R',
                     'capture_ply': 3, 'point': [1, 1]},
                ]}

    def test_recapture_facts(self):
        report = self.study(self.fixture())
        q = self.by_query(report)
        self.assertEqual((q['first_setup']['stone_count'],
                          q['first_setup']['game_count']), (2, 1))
        self.assertEqual((q['second_played']['stone_count'],
                          q['second_played']['game_count']), (1, 1))
        self.assertEqual((q['life_out']['stone_count'],
                          q['life_out']['game_count']), (0, 0))
        self.assertEqual((q['cap_out']['stone_count'],
                          q['cap_out']['game_count']), (0, 0))
        lookups = self.by_lookup(report)
        self.assertEqual(lookups['first_11']['fact']['origin'], 'setup')
        self.assertEqual(lookups['first_12']['fact']['origin'], 'setup')
        second = lookups['second_11']['fact']
        self.assertEqual(second['origin'], 'played')
        self.assertEqual(second['source_ply'], 3)
        self.assertEqual(second['source_move_id'], 'w3')
        self.assertEqual(second['life'], 3)
        # 第 3 手只是落子、未提子，故无该手事实。
        self.assertIsNone(lookups['absent']['fact'])


class SimpleKoPrefix(CaptureCase):
    def fixture(self):
        # 白 [1,1] 被黑 [0,1],[1,0],[2,1] 与第 1 手 [1,2] 围提；
        # 白第 2 手 [1,1] 立即回提即简单劫，非法。第 3 手及之后都不分析。
        black = [[0, 1], [1, 0], [2, 1]]
        white = [[1, 1], [0, 2], [2, 2], [1, 3]]
        moves = [move('take', 'B', [1, 2]), move('retake', 'W', [1, 1]),
                 move('later', 'B', [4, 4])]
        self.write_record('K', record(5, black, white, 'B', moves))
        return {'games': [{'game_id': 'K', 'path': 'K.json'}],
                'queries': [
                    # 第 1 手提掉的是 setup 白 [1,1]：1 颗。
                    {'query_id': 'setup_first', 'capture_from': 1,
                     'capture_to': 3, 'origin': 'setup'},
                    # 非法前缀之后的窗口不得产生事实。
                    {'query_id': 'after_block', 'capture_from': 2,
                     'capture_to': 3, 'origin': 'setup'},
                ],
                'lookups': [
                    {'lookup_id': 'ply1', 'game_id': 'K', 'capture_ply': 1,
                     'point': [1, 1]},
                    {'lookup_id': 'ply2', 'game_id': 'K', 'capture_ply': 2,
                     'point': [1, 1]},
                    {'lookup_id': 'ply3', 'game_id': 'K', 'capture_ply': 3,
                     'point': [1, 1]},
                ]}

    def test_blocked_prefix(self):
        report = self.study(self.fixture())
        game = report['games'][0]
        self.assertEqual(game['first_illegal'],
                         {'ply': 2, 'move_id': 'retake', 'reason': 'simple_ko'})
        self.assertEqual(game['legal_plies'], 1)
        q = self.by_query(report)
        self.assertEqual((q['setup_first']['stone_count'],
                          q['setup_first']['game_count']), (1, 1))
        self.assertEqual((q['after_block']['stone_count'],
                          q['after_block']['game_count']), (0, 0))
        lookups = self.by_lookup(report)
        self.assertEqual(lookups['ply1']['fact']['origin'], 'setup')
        self.assertIsNone(lookups['ply2']['fact'])
        self.assertIsNone(lookups['ply3']['fact'])

    def test_process_agrees_on_prefix(self):
        from go_record.service import process
        self.fixture()
        data = json.loads(Path(self.dir, 'K.json').read_text(encoding='utf-8'))
        result = process(data)
        self.assertEqual(result['first_illegal']['reason'], 'simple_ko')
        self.assertEqual(result['summary']['accepted_moves'], 1)


class MultiBlockCapture(CaptureCase):
    def test_white_captures_two_black_stones(self):
        # 5 路。黑 setup 横块 [1,1]、[1,2]，唯一气 [1,3]；白 setup 围住其余气位。
        # 白第 1 手落 [1,3] 提掉整块两颗黑子：白方提黑子，逐子成两条事实。
        black = [[1, 1], [1, 2]]
        white = [[0, 1], [0, 2], [2, 1], [2, 2], [1, 0]]
        moves = [move('w1', 'W', [1, 3])]
        self.write_record('B', record(5, black, white, 'W', moves))
        manifest = {'games': [{'game_id': 'B', 'path': 'B.json'}],
                    'queries': [{'query_id': 'q', 'capture_from': 1,
                                 'capture_to': 1, 'origin': 'setup'}],
                    'lookups': [{'lookup_id': 'l1', 'game_id': 'B',
                                 'capture_ply': 1, 'point': [1, 1]},
                                {'lookup_id': 'l2', 'game_id': 'B',
                                 'capture_ply': 1, 'point': [1, 2]}]}
        report = self.study(manifest)
        self.assertEqual((report['queries'][0]['stone_count'],
                          report['queries'][0]['game_count']), (2, 1))
        for item in report['lookups']:
            fact = item['fact']
            self.assertEqual(fact['capture_player'], 'W')
            self.assertEqual(fact['captured_color'], 'B')
            self.assertEqual(fact['origin'], 'setup')
            self.assertEqual(fact['capture_move_id'], 'w1')


class BeyondPrefix(CaptureCase):
    def test_query_and_lookup_beyond_legal_plies(self):
        # 一盘仅 2 手（第二手停手，未双停）；窗口与反查落在合法手数之外。
        black = [[0, c] for c in range(5)] + [[2, c] for c in range(5)] + [[1, 0]]
        self.write_record('S', record(
            5, black, [[1, 2], [1, 3]], 'W',
            [move('w1', 'W', [1, 1]), move('b2', 'B', [1, 4])]))
        manifest = {
            'games': [{'game_id': 'S', 'path': 'S.json'}],
            'queries': [{'query_id': 'far', 'capture_from': 100, 'capture_to': 200,
                         'origin': 'played', 'life_from': 1, 'life_to': 2000}],
            'lookups': [{'lookup_id': 'far', 'game_id': 'S', 'capture_ply': 100,
                         'point': [1, 1]}]}
        report = self.study(manifest)
        self.assertEqual((report['queries'][0]['stone_count'],
                          report['queries'][0]['game_count']), (0, 0))
        self.assertIsNone(report['lookups'][0]['fact'])


# ---------------------------------------------------------------------------
# 输入错误、输出保护与确定性
# ---------------------------------------------------------------------------

class Errors(CaptureCase):
    def base(self):
        black = [[0, c] for c in range(5)] + [[2, c] for c in range(5)] + [[1, 0]]
        self.write_record('G', record(
            5, black, [[1, 2], [1, 3]], 'W',
            [move('w1', 'W', [1, 1]), move('b2', 'B', [1, 4])]))
        return {'games': [{'game_id': 'G', 'path': 'G.json'}],
                'queries': [{'query_id': 'q', 'capture_from': 2, 'capture_to': 2,
                             'origin': 'setup'}],
                'lookups': []}

    def assert_reject(self, manifest, code):
        _, err = self.run_study(manifest, expect=2)
        payload = json.loads(err)
        self.assertEqual(payload['error']['code'], code)
        return payload

    def test_missing_file(self):
        manifest = self.base()
        manifest['games'][0]['path'] = 'nope.json'
        payload = self.assert_reject(manifest, 'game_unreadable')
        self.assertEqual(payload['error']['game_id'], 'G')

    def test_setup_query_with_life(self):
        manifest = self.base()
        manifest['queries'][0].update(life_from=1, life_to=1)
        payload = self.assert_reject(manifest, 'invalid_structure')
        self.assertEqual(payload['error']['query_id'], 'q')

    def test_played_query_without_life(self):
        manifest = self.base()
        manifest['queries'][0] = {'query_id': 'q', 'capture_from': 2,
                                  'capture_to': 2, 'origin': 'played'}
        self.assert_reject(manifest, 'invalid_structure')

    def test_reversed_bounds(self):
        manifest = self.base()
        manifest['queries'][0].update(capture_from=5, capture_to=4)
        payload = self.assert_reject(manifest, 'invalid_structure')
        self.assertEqual(payload['error']['query_id'], 'q')

    def test_duplicate_game_id(self):
        manifest = self.base()
        manifest['games'].append({'game_id': 'G', 'path': 'G.json'})
        self.assert_reject(manifest, 'invalid_structure')

    def test_lookup_unknown_game(self):
        manifest = self.base()
        manifest['lookups'] = [{'lookup_id': 'x', 'game_id': 'Z',
                                'capture_ply': 1, 'point': [1, 1]}]
        payload = self.assert_reject(manifest, 'invalid_structure')
        self.assertEqual(payload['error']['game_id'], 'Z')

    def test_lookup_outside_board(self):
        manifest = self.base()
        manifest['lookups'] = [{'lookup_id': 'x', 'game_id': 'G',
                                'capture_ply': 1, 'point': [9, 9]}]
        self.assert_reject(manifest, 'invalid_structure')

    def test_duplicate_query_id(self):
        manifest = self.base()
        manifest['queries'].append(dict(manifest['queries'][0]))
        self.assert_reject(manifest, 'invalid_structure')

    def test_query_out_of_range(self):
        manifest = self.base()
        manifest['queries'][0]['capture_from'] = 0
        self.assert_reject(manifest, 'invalid_structure')

    def test_move_limit_batch_total(self):
        # 单盘沿用原 2000 手上限；整批合计上限 240,000 靠跨盘累计触发。
        # 用 384 盘各 2000 手（每盘合法）：合计 768,000 > 240,000。
        moves = [move(f'p{i}', 'W' if i % 2 == 0 else 'B', None)
                 for i in range(2000)]
        self.write_record('big', record(5, [], [], 'W', moves))
        games = [{'game_id': f'g{i}', 'path': 'big.json'} for i in range(384)]
        manifest = {'games': games,
                    'queries': [{'query_id': 'q', 'capture_from': 1,
                                 'capture_to': 2, 'origin': 'setup'}],
                    'lookups': []}
        payload = self.assert_reject(manifest, 'move_limit')
        self.assertGreater(payload['error']['total_moves'], 240_000)

    def test_output_not_overwritten(self):
        manifest = self.base()
        output = os.path.join(self.dir, 'taken.json')
        Path(output).write_text('sentinel', encoding='utf-8')
        _, err = self.run_study(manifest, expect=2, output=output)
        self.assertEqual(json.loads(err)['error']['code'], 'output_exists')
        self.assertEqual(Path(output).read_text(encoding='utf-8'), 'sentinel')

    def test_output_symlink_not_overwritten(self):
        manifest = self.base()
        target = os.path.join(self.dir, 'real.json')
        Path(target).write_text('sentinel', encoding='utf-8')
        link = os.path.join(self.dir, 'link.json')
        os.symlink(target, link)
        _, err = self.run_study(manifest, expect=2, output=link)
        self.assertEqual(json.loads(err)['error']['code'], 'output_exists')
        self.assertEqual(Path(target).read_text(encoding='utf-8'), 'sentinel')

    def test_no_report_on_failure(self):
        manifest = self.base()
        manifest['games'][0]['path'] = 'nope.json'
        output = os.path.join(self.dir, 'never.json')
        self.run_study(manifest, expect=2, output=output)
        self.assertFalse(os.path.exists(output))

    def test_illegal_move_is_not_batch_error(self):
        black = [[0, 1], [1, 0], [2, 1]]
        white = [[1, 1], [0, 2], [2, 2], [1, 3]]
        self.write_record('K', record(5, black, white, 'B',
                                      [move('take', 'B', [1, 2]),
                                       move('retake', 'W', [1, 1])]))
        manifest = {'games': [{'game_id': 'K', 'path': 'K.json'}],
                    'queries': [{'query_id': 'q', 'capture_from': 1,
                                 'capture_to': 2000, 'origin': 'setup'}],
                    'lookups': []}
        report, _ = self.run_study(manifest)
        self.assertEqual(report['games'][0]['first_illegal']['reason'], 'simple_ko')
        self.assertEqual(report['queries'][0]['stone_count'], 1)

    def test_ended_game_without_double_pass_is_analysable(self):
        black = [[0, c] for c in range(5)] + [[2, c] for c in range(5)] + [[1, 0]]
        self.write_record('P', record(
            5, black, [[1, 2], [1, 3]], 'W',
            [move('w1', 'W', [1, 1])]))  # 合法无双停
        manifest = {'games': [{'game_id': 'P', 'path': 'P.json'}],
                    'queries': [{'query_id': 'q', 'capture_from': 1,
                                 'capture_to': 2000, 'origin': 'played',
                                 'life_from': 1, 'life_to': 2000}],
                    'lookups': []}
        report, _ = self.run_study(manifest)
        self.assertIsNone(report['games'][0]['first_illegal'])

    def test_manifest_order_does_not_change_report(self):
        black = [[0, c] for c in range(5)] + [[2, c] for c in range(5)] + [[1, 0]]
        self.write_record('G', record(
            5, black, [[1, 2], [1, 3]], 'W',
            [move('w1', 'W', [1, 1]), move('b2', 'B', [1, 4])]))
        self.write_record('H', record(
            5, black, [[1, 2], [1, 3], [1, 1]], 'W',
            [move('h1', 'W', None), move('h2', 'B', [1, 4])]))
        games = [{'game_id': 'G', 'path': 'G.json'},
                 {'game_id': 'H', 'path': 'H.json'}]
        queries = [{'query_id': 'b', 'capture_from': 2, 'capture_to': 2,
                    'origin': 'setup'},
                   {'query_id': 'a', 'capture_from': 2, 'capture_to': 2,
                    'origin': 'played', 'life_from': 1, 'life_to': 1}]
        lookups = [{'lookup_id': 'y', 'game_id': 'G', 'capture_ply': 2,
                    'point': [1, 1]},
                   {'lookup_id': 'x', 'game_id': 'H', 'capture_ply': 2,
                    'point': [1, 2]}]
        first = self.study({'games': games, 'queries': queries, 'lookups': lookups})
        second = self.study({'games': list(reversed(games)),
                             'queries': list(reversed(queries)),
                             'lookups': list(reversed(lookups))})
        self.assertEqual(first, second)


class FixtureExample(unittest.TestCase):
    def test_checked_in_example_matches_expected(self):
        fixtures = ROOT / 'fixtures' / 'capture'
        with tempfile.TemporaryDirectory(prefix='go-capture-fix-') as tmp:
            output = os.path.join(tmp, 'report.json')
            code, out, err = run_cli(['--source', str(fixtures / 'manifest.json'),
                                      '--output', output])
            self.assertEqual(code, 0, err)
            produced = json.loads(Path(output).read_text(encoding='utf-8'))
        expected = json.loads((fixtures / 'expected-report.json')
                              .read_text(encoding='utf-8'))
        self.assertEqual(produced, expected)


if __name__ == "__main__":
    unittest.main()



class RandomAggregation(unittest.TestCase):
    """随机事实与随机窗口上，优化聚合须与逐条暴力计数一致。"""

    def test_matches_brute_force(self):
        import random
        rng = random.Random(20261123)

        def brute(queries, games_facts):
            out = []
            for q in queries:
                cf, ct = q['capture_from'], q['capture_to']
                stones = 0
                games = set()
                for gi, facts in enumerate(games_facts):
                    for f in facts:
                        ply, _pt, kind, _sp, life = f[:5]
                        if not cf <= ply <= ct or kind != q['origin']:
                            continue
                        if kind == 'played' and not (
                                q['life_from'] <= life <= q['life_to']):
                            continue
                        stones += 1
                        games.add(gi)
                out.append((stones, len(games)))
            return out

        for _ in range(200):
            games_facts = []
            for _ in range(rng.randint(1, 6)):
                facts = []
                for _ in range(rng.randint(0, 12)):
                    ply = rng.randint(1, 30)
                    kind = rng.choice(['setup', 'played'])
                    life = source = None
                    if kind == 'played':
                        source = rng.randint(1, ply)
                        life = ply - source
                    facts.append((ply, (0, 0), kind, source, life,
                                  'm', 'B', 'W', None))
                facts.sort(key=lambda item: (item[0], item[1]))
                games_facts.append(facts)
            queries = []
            for index in range(rng.randint(1, 40)):
                capture_from = rng.randint(1, 30)
                capture_to = rng.randint(capture_from, 30)
                origin = rng.choice(['setup', 'played'])
                query = {'query_id': str(index), 'capture_from': capture_from,
                         'capture_to': capture_to, 'origin': origin,
                         'life_from': None, 'life_to': None}
                if origin == 'played':
                    life_from = rng.randint(1, 20)
                    query['life_from'] = life_from
                    query['life_to'] = rng.randint(life_from, 25)
                queries.append(query)
            queries.sort(key=lambda item: item['query_id'])
            self.assertEqual(capture.query_results(queries, games_facts),
                             brute(queries, games_facts))
