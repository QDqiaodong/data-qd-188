import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from go_record import dataset
from go_record.dataset_cli import main as dataset_main
from go_record.service import process

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'dataset'


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = dataset_main(argv)
    return code, out.getvalue(), err.getvalue()


def move(mid, player, point):
    return {'id': mid, 'player': player, 'point': point}


def record(size, setup, next_player, moves, komi='0'):
    setup = {'black': list(setup.get('black', [])), 'white': list(setup.get('white', []))}
    return {'size': size, 'setup': setup, 'next_player': next_player,
            'komi': komi, 'moves': moves}


class DatasetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='go-dataset-')
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.counter = 0

    # ---- 工具 ----

    def write_record(self, name, data):
        path = os.path.join(self.dir, name + '.json')
        Path(path).write_text(json.dumps(data), encoding='utf-8')
        return name + '.json'

    def manifest(self, records, samples, history=(), quota=(0, 0), batch='batch-1'):
        return {'batch_id': batch, 'records': list(records), 'samples': list(samples),
                'history': list(history), 'holdout_quota': {'B': quota[0], 'W': quota[1]}}

    def entry(self, record_id, source_group, path):
        return {'record_id': record_id, 'source_group': source_group, 'path': path}

    def sample(self, sample_id, record_id, ply):
        return {'sample_id': sample_id, 'record_id': record_id, 'ply': ply}

    def history_row(self, release_id, sample_id, source_group, board, player, split,
                    size=None):
        board = list(board)
        return {'release_id': release_id, 'sample_id': sample_id,
                'source_group': source_group, 'size': size or len(board),
                'board': board, 'next_player': player, 'split': split}

    def evaluate(self, manifest):
        return dataset.build_report(dataset.validate_manifest(manifest), self.dir)

    def write_manifest(self, manifest, name='manifest.json'):
        path = os.path.join(self.dir, name)
        Path(path).write_text(json.dumps(manifest), encoding='utf-8')
        return path

    def run_dataset(self, manifest, expect=0, output=None, name='manifest.json',
                    preexisting=False):
        source = self.write_manifest(manifest, name)
        if output is None:
            self.counter += 1
            output = os.path.join(self.dir, f'report-{self.counter}.json')
        code, out, err = run_cli(['--source', source, '--output', output])
        self.assertEqual(code, expect, err)
        if expect == 0:
            return json.loads(Path(output).read_text(encoding='utf-8')), output, out
        if not preexisting:
            self.assertFalse(os.path.exists(output), '失败不得留下报告')
        return None, output, err

    def sample_ids(self, report, split):
        return [entry['sample_id'] for entry in report['splits'][split]['samples']]


class ExtractionTests(DatasetCase):
    def test_capture_target_uses_real_replay_before_move_board(self):
        """用真实合法回放取到提子前的一手：特征是该手提子前的盘面，答案是原落子点。"""
        data = json.loads((ROOT / 'fixtures' / 'input.json').read_text(encoding='utf-8'))
        replay = process(data)
        self.assertEqual(replay['trace'][0]['captured'], [[1, 1]])
        path = self.write_record('capture', data)
        manifest = self.manifest([self.entry('R', 'S', path)],
                                 [self.sample('S1', 'R', 1)], [], (0, 0))
        report = self.evaluate(manifest)
        self.assertEqual(report['status'], 'ready')
        target = report['splits']['train']['samples'][0]
        self.assertEqual(target['move_id'], 'capture')
        self.assertEqual(target['next_player'], 'B')
        # 提子前的盘面：初始设置，白子 [1,1] 仍在。
        self.assertEqual(target['board'], ['.B...', 'BW...', '.B...', '.....', '....W'])
        self.assertEqual(target['answer'], [1, 2])

    def test_first_ply_uses_initial_setup_and_later_ply_uses_legal_prefix(self):
        data = record(4, {'black': [[0, 0]], 'white': [[3, 3]]}, 'B',
                      [move('m1', 'B', [1, 1]), move('m2', 'W', [1, 2])])
        path = self.write_record('prefix', data)
        manifest = self.manifest([self.entry('R', 'S', path)],
                                 [self.sample('P1', 'R', 1), self.sample('P2', 'R', 2)],
                                 [], (0, 0))
        report = self.evaluate(manifest)
        by_id = {entry['sample_id']: entry
                 for entry in report['splits']['train']['samples']}
        self.assertEqual(by_id['P1']['board'], ['B...', '....', '....', '...W'])
        self.assertEqual(by_id['P1']['next_player'], 'B')
        self.assertEqual(by_id['P2']['board'], ['B...', '.B..', '....', '...W'])
        self.assertEqual(by_id['P2']['next_player'], 'W')
        self.assertEqual(by_id['P2']['answer'], [1, 2])

    def test_immediate_ko_retake_and_all_later_plies_are_excluded(self):
        """立即回提被简单劫拦住时，该步及之后即使出现在原始文件也不能成为样本。"""
        data = record(5, {'black': [[0, 1], [1, 0], [2, 1]],
                          'white': [[1, 1], [0, 2], [2, 2], [1, 3]]}, 'B',
                      [move('take', 'B', [1, 2]), move('retake', 'W', [1, 1]),
                       move('later', 'B', [4, 0])])
        path = self.write_record('ko', data)
        manifest = self.manifest([self.entry('R', 'S', path)],
                                 [self.sample('S1', 'R', 1), self.sample('S2', 'R', 2),
                                  self.sample('S3', 'R', 3)], [], (0, 0))
        report = self.evaluate(manifest)
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(self.sample_ids(report, 'train'), ['S1'])
        exclusions = {item['sample_id']: item for item in report['exclusions']}
        self.assertEqual(exclusions['S2']['reason'], 'illegal_move')
        self.assertEqual(exclusions['S3']['reason'], 'illegal_move')
        self.assertEqual(exclusions['S2']['blocking_move'],
                         {'ply': 2, 'move_id': 'retake', 'reason': 'simple_ko'})
        self.assertEqual(exclusions['S3']['blocking_move'], exclusions['S2']['blocking_move'])

    def test_legal_pass_is_not_a_prompt_but_later_legal_moves_survive(self):
        data = record(5, {'black': [[0, 1], [1, 0], [2, 1]],
                          'white': [[1, 1], [0, 2], [2, 2], [1, 3]]}, 'B',
                      [move('take', 'B', [1, 2]), move('wait', 'W', None),
                       move('elsewhere', 'B', [4, 0])])
        path = self.write_record('pass', data)
        manifest = self.manifest([self.entry('R', 'S', path)],
                                 [self.sample('S1', 'R', 1), self.sample('S2', 'R', 2),
                                  self.sample('S3', 'R', 3)], [], (0, 0))
        report = self.evaluate(manifest)
        self.assertEqual(self.sample_ids(report, 'train'), ['S1', 'S3'])
        exclusions = {item['sample_id']: item for item in report['exclusions']}
        self.assertEqual(exclusions['S2']['reason'], 'pass')
        self.assertEqual(exclusions['S2']['move_id'], 'wait')

    def test_answer_keeps_original_direction_while_key_is_canonical(self):
        # 两块同图的镜像摆法：关键局面相同，但原落子答案各按原方向保留。
        left = record(4, {'black': [[0, 1]], 'white': []}, 'B', [move('m', 'B', [0, 2])])
        right = record(4, {'black': [[3, 2]], 'white': []}, 'B', [move('m', 'B', [3, 1])])
        lp = self.write_record('left', left)
        rp = self.write_record('right', right)
        manifest = self.manifest(
            [self.entry('L', 'SL', lp), self.entry('R', 'SR', rp)],
            [self.sample('SL1', 'L', 1), self.sample('SR1', 'R', 1)], [], (0, 0))
        report = self.evaluate(manifest)
        by_id = {entry['sample_id']: entry
                 for entry in report['splits']['train']['samples']}
        self.assertEqual(by_id['SL1']['answer'], [0, 2])
        self.assertEqual(by_id['SR1']['answer'], [3, 1])
        self.assertEqual(by_id['SL1']['position_key']['board'],
                         by_id['SR1']['position_key']['board'])
        self.assertEqual(by_id['SL1']['group_id'], by_id['SR1']['group_id'])


class GroupingTests(DatasetCase):
    def _pair(self, left_setup, right_setup, left_player='B', right_player='B'):
        lp = self.write_record('left', record(4, left_setup, left_player,
                                              [move('m', left_player, [1, 1])]))
        rp = self.write_record('right', record(4, right_setup, right_player,
                                               [move('m', right_player, [1, 1])]))
        return self.manifest(
            [self.entry('L', 'SL', lp), self.entry('R', 'SR', rp)],
            [self.sample('SL1', 'L', 1), self.sample('SR1', 'R', 1)], [], (0, 0))

    def test_rotated_or_mirrored_positions_with_same_player_share_a_group(self):
        for transformed in ([[3, 2]], [[2, 3]]):
            manifest = self._pair({'black': [[0, 1]], 'white': []},
                                  {'black': transformed, 'white': []})
            report = self.evaluate(manifest)
            self.assertEqual(len(report['grouping']['groups']), 1)
            self.assertEqual(report['grouping']['groups'][0]['group_id'], 'SL')
            self.assertEqual(report['grouping']['groups'][0]['sample_ids'], ['SL1', 'SR1'])

    def test_same_board_but_other_player_is_not_joined_without_other_edges(self):
        manifest = self._pair({'black': [[0, 1]], 'white': []},
                              {'black': [[0, 1]], 'white': []},
                              left_player='B', right_player='W')
        report = self.evaluate(manifest)
        groups = report['grouping']['groups']
        self.assertEqual(len(groups), 2)
        self.assertEqual({group['group_id'] for group in groups}, {'SL', 'SR'})
        for group in groups:
            self.assertEqual(len(group['sample_ids']), 1)

    def test_relation_is_transitive_across_sources_and_keys(self):
        """A-C 同图、C-K 同来源时 A、C、K 必须同组，不能只合并直接相同的图。"""
        a = self.write_record('a', record(4, {'black': [[0, 1]]}, 'B', [move('a', 'B', [2, 2])]))
        # C 与 A 同图（镜像），同时与 K 同来源组。
        c = self.write_record('c', record(4, {'black': [[3, 2]]}, 'B', [move('c', 'B', [1, 1])]))
        k = self.write_record('k', record(4, {'black': [[0, 0]]}, 'B', [move('k', 'B', [3, 3])]))
        manifest = self.manifest(
            [self.entry('A', 'A', a), self.entry('C', 'C', c), self.entry('K', 'C', k)],
            [self.sample('a1', 'A', 1), self.sample('c1', 'C', 1), self.sample('k1', 'K', 1)],
            [], (0, 0))
        report = self.evaluate(manifest)
        self.assertEqual(len(report['grouping']['groups']), 1)
        group = report['grouping']['groups'][0]
        self.assertEqual(group['group_id'], 'A')
        self.assertEqual(group['source_groups'], ['A', 'C'])
        self.assertEqual(group['sample_ids'], ['a1', 'c1', 'k1'])


class HistoryTests(DatasetCase):
    def test_two_side_history_isolates_connected_group_and_removing_k_delivers(self):
        """验收场景：U 历史训练、V 历史盲评；K 把 A、C 连成一组后整组隔离。"""
        for name in ('manifest.json', 'manifest-without-k.json'):
            data = json.loads((FIXTURES / name).read_text(encoding='utf-8'))
            result = dataset.build_report(dataset.validate_manifest(data), str(FIXTURES))
            if name == 'manifest.json':
                self.assertEqual(result['status'], 'quota_unreachable')
                isolated = result['grouping']['isolated']
                self.assertEqual([entry['group_id'] for entry in isolated], ['A'])
                self.assertEqual(isolated[0]['sample_ids'], ['s-a', 's-c', 's-k1', 's-k2'])
                hits = {(key['kind'], key['split'], key['history']['release_id'])
                        for key in isolated[0]['matched_keys']}
                self.assertEqual(hits, {('position_key', 'train', 'rel-2026-01'),
                                        ('position_key', 'holdout', 'rel-2026-02')})
                self.assertIsNone(result['splits'])
                self.assertIsNone(result['publication'])
                self.assertEqual(result['diagnostic']['isolated_group_ids'], ['A'])
            else:
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(self.sample_ids(result, 'train'), ['s-a', 's-w'])
                self.assertEqual(self.sample_ids(result, 'holdout'), ['s-c'])
        # 去 K 后 A 回训练、C 回盲评：历史旧行本身没有作为本期样本加入。
        without = dataset.build_report(
            dataset.validate_manifest(json.loads(
                (FIXTURES / 'manifest-without-k.json').read_text(encoding='utf-8'))),
            str(FIXTURES))
        self.assertEqual(without['grouping']['isolated'], [])
        self.assertNotIn('h1', {entry['sample_id']
                                for split in ('train', 'holdout')
                                for entry in without['splits'][split]['samples']})

    def test_board_differs_but_source_group_hits_history(self):
        path = self.write_record('r', record(4, {'black': [[0, 0]]}, 'B',
                                             [move('m', 'B', [3, 3])]))
        history = [self.history_row('rel-1', 'h1', 'S', ['....'] * 4, 'B', 'holdout')]
        # 历史已把该组锁进盲评，故盲评目标含这一题。
        manifest = self.manifest([self.entry('R', 'S', path)],
                                 [self.sample('R1', 'R', 1)], history, (1, 0))
        report = self.evaluate(manifest)
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(report['grouping']['groups'][0]['constraint'], 'holdout')
        self.assertEqual(self.sample_ids(report, 'holdout'), ['R1'])
        self.assertEqual(report['splits']['holdout']['counts'], {'B': 1, 'W': 0})
        # 历史行只是约束，不进入本期练习。
        self.assertNotIn('h1', self.sample_ids(report, 'holdout'))

    def test_one_side_history_locks_group_and_its_count_feeds_quota(self):
        locked = self.write_record('locked', record(4, {'black': [[0, 0]]}, 'B',
                                                    [move('m', 'B', [3, 3])]))
        free_a = self.write_record('free_a', record(5, {'black': [[0, 1]]}, 'B',
                                                    [move('m', 'B', [3, 3])]))
        free_b = self.write_record('free_b', record(6, {'black': [[0, 2]]}, 'B',
                                                    [move('m', 'B', [3, 3])]))
        history = [self.history_row('rel-1', 'h1', 'LOCK', ['....'] * 4, 'B', 'holdout')]
        manifest = self.manifest(
            [self.entry('LOCK', 'LOCK', locked), self.entry('FA', 'FA', free_a),
             self.entry('FB', 'FB', free_b)],
            [self.sample('l1', 'LOCK', 1), self.sample('a1', 'FA', 1),
             self.sample('b1', 'FB', 1)], history, (2, 0))
        report = self.evaluate(manifest)
        self.assertEqual(report['status'], 'ready')
        # 已锁盲评的一题先占配额，再从组标识最小的未锁组补一题。
        self.assertEqual(self.sample_ids(report, 'holdout'), ['a1', 'l1'])
        self.assertEqual(self.sample_ids(report, 'train'), ['b1'])
        self.assertEqual(report['splits']['holdout']['counts'], {'B': 2, 'W': 0})

    def test_isolated_group_is_reported_but_batch_can_still_deliver(self):
        """隔离组不出现于两侧；其余组仍可满足配额时交付，并在分组里保留隔离证据。"""
        isolated = self.write_record('iso', record(4, {'black': [[0, 0]]}, 'B',
                                                   [move('m', 'B', [3, 3])]))
        ok = self.write_record('ok', record(5, {'black': [[0, 0]]}, 'B',
                                            [move('m', 'B', [3, 3])]))
        history = [
            self.history_row('rel-1', 'h-train', 'ISO', ['....'] * 4, 'B', 'train'),
            self.history_row('rel-2', 'h-holdout', 'ISO', ['....'] * 4, 'B', 'holdout'),
        ]
        manifest = self.manifest([self.entry('ISO', 'ISO', isolated), self.entry('OK', 'OK', ok)],
                                 [self.sample('i1', 'ISO', 1), self.sample('o1', 'OK', 1)],
                                 history, (1, 0))
        report = self.evaluate(manifest)
        self.assertEqual(report['status'], 'ready')
        self.assertEqual([entry['group_id'] for entry in report['grouping']['isolated']], ['ISO'])
        self.assertEqual(self.sample_ids(report, 'holdout'), ['o1'])
        self.assertEqual(self.sample_ids(report, 'train'), [])
        published = {row['sample_id'] for split in ('train', 'holdout')
                     for row in report['publication'][split]}
        self.assertEqual(published, {'o1'})


class QuotaTests(DatasetCase):
    def _five_groups(self):
        """五组独立题量：A=(2,1) B=(2,0) C=(0,2) D=(1,0) E=(1,2)，组标识按 A..E。"""
        # (size, black setup, white setup, 前四手的目标手号)
        layouts = {
            'A': (5, [[0, 1]], [], [1, 2, 3]),
            'B': (6, [[0, 1]], [], [1, 3]),
            'C': (7, [[0, 1]], [], [2, 4]),
            'D': (8, [[0, 1]], [], [1]),
            'E': (9, [[0, 1]], [], [1, 2, 4]),
        }
        records, samples = [], []
        for group in 'ABCDE':
            size, black, white, plies = layouts[group]
            seq = [('B', (2, 2)), ('W', (2, 3)), ('B', (3, 2)), ('W', (3, 3))]
            moves = [move(f'{group}{i}', color, list(point))
                     for i, (color, point) in enumerate(seq, start=1)]
            path = self.write_record(group, record(size, {'black': black, 'white': white},
                                                   'B', moves))
            records.append(self.entry(group, group, path))
            for ply in plies:
                samples.append(self.sample(f'{group}{ply}', group, ply))
        return records, samples

    def test_joint_colour_quota_prefers_lexicographic_holdout(self):
        records, samples = self._five_groups()
        report = self.evaluate(self.manifest(records, samples, [], (2, 2)))
        self.assertEqual(report['status'], 'ready')
        # A 逐项不超额（B2>目标单色不成立：A 自身黑先 2 已达目标，白先 1 未达），
        # 但不能只看单组；盲评须选 B+C 而非 D+E（D 黑先 1、E 白先 2 也凑得出 2/2）。
        holdout = {entry['group_id'] for entry in report['splits']['holdout']['samples']}
        self.assertEqual(holdout, {'B', 'C'})
        self.assertEqual(report['splits']['holdout']['counts'], {'B': 2, 'W': 2})

    def test_group_order_not_input_order_drives_choice(self):
        records, samples = self._five_groups()
        report = self.evaluate(self.manifest(records, samples, [], (2, 2)))
        # 组标识 A..E 与清单顺序无关：D+E 也满足配额，但字典序优先选 B+C。
        holdout_groups = {entry['group_id'] for entry in report['splits']['holdout']['samples']}
        self.assertEqual(holdout_groups, {'B', 'C'})
        self.assertNotIn('D', holdout_groups)
        self.assertNotIn('E', holdout_groups)

    def test_reordering_manifest_keeps_identical_report(self):
        records, samples = self._five_groups()
        first = self.evaluate(self.manifest(records, samples, [], (2, 2)))
        shuffled = self.evaluate(self.manifest(list(reversed(records)),
                                               list(reversed(samples)), [], (2, 2)))
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(shuffled, sort_keys=True))

    def test_forty_independent_groups_joint_quota(self):
        records, samples = self._forty_groups()
        ready = self.evaluate(self.manifest(records, samples, [], (50, 50)))
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(ready['splits']['holdout']['counts'], {'B': 50, 'W': 50})
        unreachable = self.evaluate(self.manifest(records, samples, [], (50, 49)))
        self.assertEqual(unreachable['status'], 'quota_unreachable')
        self.assertIsNone(unreachable['splits'])
        self.assertIsNone(unreachable['publication'])
        diagnostic = unreachable['diagnostic']
        self.assertEqual(diagnostic['remaining_targets'], {'B': 50, 'W': 49})
        self.assertEqual(len(diagnostic['free_groups']), 40)

    def _forty_groups(self):
        """20 组各 (3,2)、20 组各 (2,3)，全部图与来源互相独立。"""
        records, samples = [], []
        candidates = [(size, point) for size in range(3, 20)
                      for point in [(r, c) for r in range(size) for c in range(size)]]
        used_keys = set()
        chosen = {'B': [], 'W': []}
        for size, marker in candidates:
            if len(chosen['B']) >= 20 and len(chosen['W']) >= 20:
                break
            for first in ('B', 'W'):
                if len(chosen[first]) >= 20:
                    continue
                data = self._spread_record(size, marker, first)
                replay = process(data)
                if not replay['summary']['valid_record']:
                    continue
                boards = [dataset.canonical_rows(tuple(
                    [''.join('.' for _ in range(size))] * size))]
                from go_record.board import rows as board_rows
                from go_record.model import setup_board
                initial, _ = setup_board(data)
                boards = [board_rows(initial, size)]
                boards += [entry['board'] for entry in replay['trace']]
                keys = {dataset.position_key(size, data['moves'][i]['player'], boards[i])
                        for i in range(len(data['moves']))}
                if keys & used_keys:
                    continue
                used_keys |= keys
                chosen[first].append((size, marker, first, data))
                break
        self.assertEqual(len(chosen['B']), 20)
        self.assertEqual(len(chosen['W']), 20)
        index = 0
        for first in ('B', 'W'):
            for size, marker, _first, data in chosen[first]:
                group = f'G{index:02d}'
                index += 1
                path = self.write_record(group, data)
                records.append(self.entry(group, group, path))
                for ply in range(1, 6):
                    samples.append(self.sample(f'{group}-{ply}', group, ply))
        return records, samples

    @staticmethod
    def _spread_record(size, marker, first):
        points = [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3),
                  (3, 1), (3, 2), (3, 3), (4, 4), (4, 5), (5, 4), (5, 5),
                  (0, 0), (0, size - 1), (size - 1, 0), (size - 1, size - 1)]
        points = [point for point in points
                  if point != tuple(marker) and 0 <= point[0] < size and 0 <= point[1] < size]
        points = points[:5]
        colors = ['B', 'W', 'B', 'W', 'B'] if first == 'B' else ['W', 'B', 'W', 'B', 'W']
        moves = [move(f'm{i}', color, list(point))
                 for i, (color, point) in enumerate(zip(colors, points))]
        return record(size, {'black': [list(marker)], 'white': []}, first, moves)


class InputErrorTests(DatasetCase):
    def _valid_base(self):
        path = self.write_record('r', record(4, {'black': [[0, 0]]}, 'B',
                                             [move('m', 'B', [3, 3])]))
        return path

    def test_duplicate_and_unknown_identities_are_reported_with_location(self):
        path = self._valid_base()
        cases = []
        cases.append(('duplicate record', self.manifest(
            [self.entry('R', 'S', path), self.entry('R', 'T', path)],
            [self.sample('X', 'R', 1)], [])))
        cases.append(('unknown record reference', self.manifest(
            [self.entry('R', 'S', path)],
            [self.sample('X', 'MISSING', 1)], [])))
        cases.append(('duplicate sample', self.manifest(
            [self.entry('R', 'S', path)],
            [self.sample('X', 'R', 1), self.sample('X', 'R', 2)], [])))
        cases.append(('duplicate record/ply', self.manifest(
            [self.entry('R', 'S', path)],
            [self.sample('X', 'R', 1), self.sample('Y', 'R', 1)], [])))
        cases.append(('ply beyond moves', self.manifest(
            [self.entry('R', 'S', path)],
            [self.sample('X', 'R', 2)], [])))
        cases.append(('history release equals batch', self.manifest(
            [self.entry('R', 'S', path)], [self.sample('X', 'R', 1)],
            [self.history_row('batch-1', 'h', 'S', ['....'] * 4, 'B', 'train')])))
        cases.append(('history bad board', self.manifest(
            [self.entry('R', 'S', path)], [self.sample('X', 'R', 1)],
            [self.history_row('rel', 'h', 'S', ['...X'] * 4, 'B', 'train')])))
        cases.append(('too many source groups', self.manifest(
            [self.entry(f'R{i}', f'S{i}', path) for i in range(41)],
            [self.sample('X', 'R0', 1)], [])))
        many_history = [self.history_row('rel', f'h{i}', 'S', ['....'] * 4, 'B', 'train')
                        for i in range(8193)]
        cases.append(('history over limit', self.manifest(
            [self.entry('R', 'S', path)], [self.sample('X', 'R', 1)], many_history)))
        cases.append(('duplicate history release/sample', self.manifest(
            [self.entry('R', 'S', path)], [self.sample('X', 'R', 1)],
            [self.history_row('rel', 'h', 'S', ['....'] * 4, 'B', 'train'),
             self.history_row('rel', 'h', 'T', ['....'] * 4, 'B', 'holdout')])))
        for label, manifest in cases:
            with self.subTest(label=label):
                _, _, err = self.run_dataset(manifest, expect=2,
                                             output=os.path.join(self.dir, label + '.out'))
                self.assertIn('invalid_structure', err)

    def test_unreadable_and_invalid_record_are_input_errors(self):
        manifest = self.manifest([self.entry('R', 'S', 'absent.json')],
                                 [self.sample('X', 'R', 1)], [])
        _, _, err = self.run_dataset(manifest, expect=2)
        self.assertIn('record_unreadable', err)
        self.assertIn('R', err)
        bad = self.write_record('bad', {'size': 4})
        manifest = self.manifest([self.entry('R', 'S', bad)],
                                 [self.sample('X', 'R', 1)], [])
        _, _, err = self.run_dataset(manifest, expect=2)
        self.assertIn('invalid_record', err)
        self.assertIn('R', err)

    def test_limits_are_enforced(self):
        path = self._valid_base()
        many = [self.entry(f'R{i}', 'S', path) for i in range(257)]
        # 重复路径不重复记录 ID；限制按数组长度。
        _, _, err = self.run_dataset(self.manifest(many, [self.sample('X', 'R0', 1)], []),
                                     expect=2)
        self.assertIn('invalid_structure', err)
        _, _, err = self.run_dataset(self.manifest([self.entry('R', 'S', path)],
                                                   [self.sample('X', 'R', 1)], [],
                                                   quota=(513, 0)), expect=2)
        self.assertIn('invalid_structure', err)

    def test_existing_output_is_never_overwritten(self):
        path = self._valid_base()
        manifest = self.manifest([self.entry('R', 'S', path)],
                                 [self.sample('X', 'R', 1)], [])
        target = os.path.join(self.dir, 'existing.json')
        Path(target).write_text('keep-me', encoding='utf-8')
        _, _, err = self.run_dataset(manifest, expect=2, output=target, preexisting=True)
        self.assertIn('output_exists', err)
        self.assertEqual(Path(target).read_text(encoding='utf-8'), 'keep-me')

    def test_unreachable_quota_report_has_no_splits_or_publication(self):
        path = self.write_record('w', record(4, {'black': [[1, 1]]}, 'W',
                                             [move('m', 'W', [0, 0])]))
        manifest = self.manifest([self.entry('R', 'S', path)],
                                 [self.sample('X', 'R', 1)], [], (1, 0))
        report, _, out = self.run_dataset(manifest)
        self.assertEqual(report['status'], 'quota_unreachable')
        self.assertIsNone(report['splits'])
        self.assertIsNone(report['publication'])
        self.assertEqual(json.loads(out)['status'], 'quota_unreachable')


class CliAndExampleTests(DatasetCase):
    def test_shipped_example_is_reproducible(self):
        expected = json.loads((FIXTURES / 'expected-report.json').read_text(encoding='utf-8'))
        # 相对路径按清单所在目录解释：把清单复制到固定样例目录之外仍可复现。
        manifest = json.loads((FIXTURES / 'manifest-without-k.json').read_text(encoding='utf-8'))
        flat = os.path.join(self.dir, 'flat')
        os.makedirs(flat)
        manifest_path = os.path.join(flat, 'flat-manifest.json')
        copied = {}
        for entry in manifest['records']:
            target = os.path.join(flat, os.path.basename(entry['path']))
            Path(target).write_bytes((FIXTURES / entry['path']).read_bytes())
            copied[entry['record_id']] = os.path.basename(entry['path'])
        manifest['records'] = [dict(entry, path=copied[entry['record_id']])
                               for entry in manifest['records']]
        Path(manifest_path).write_text(json.dumps(manifest), encoding='utf-8')
        output = os.path.join(self.dir, 'flat-report.json')
        code, out, err = run_cli(['--source', manifest_path, '--output', output])
        self.assertEqual(code, 0, err)
        report = json.loads(Path(output).read_text(encoding='utf-8'))
        self.assertEqual(report, expected)
        self.assertEqual(json.loads(out)['status'], 'ready')

    def test_publication_rows_match_history_schema(self):
        report = dataset.build_report(
            dataset.validate_manifest(json.loads(
                (FIXTURES / 'manifest-without-k.json').read_text(encoding='utf-8'))),
            str(FIXTURES))
        keys = {'release_id', 'sample_id', 'source_group', 'size', 'board',
                'next_player', 'split'}
        rows = report['publication']['train'] + report['publication']['holdout']
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(set(row), keys)
            self.assertEqual(row['release_id'], report['batch_id'])
        selected = {entry['sample_id'] for split in ('train', 'holdout')
                    for entry in report['splits'][split]['samples']}
        self.assertEqual({row['sample_id'] for row in rows}, selected)


if __name__ == '__main__':
    unittest.main()
