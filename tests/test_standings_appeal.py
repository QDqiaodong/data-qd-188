import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from go_record import appeal, archive
from go_record.standings_cli import main as standings_main

# 3 路盘黑 [0,0]、白 [2,2]、贴目 0，黑白各停一手：原面积和棋
DRAW_MOVES = [('m1', 'B', [0, 0]), ('m2', 'W', [2, 2]), ('m3', 'B', None), ('m4', 'W', None)]


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = standings_main(argv)
    return code, out.getvalue(), err.getvalue()


class StandingsAppealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='go-standings-appeal-')
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.out_counter = 0

    # ---- 工具 ----

    def make_archive(self, game_id, moves=DRAW_MOVES, seal=True):
        state_dir = os.path.join(self.dir, 'games', game_id)
        spec = {'game_id': game_id, 'total_moves': len(moves), 'size': 3,
                'setup': {'black': [], 'white': []}, 'next_player': 'B', 'komi': '0'}
        archive.create_state(state_dir, spec)
        archive.receive_locked(state_dir, {'game_id': game_id, 'records': [
            {'ply': i + 1, 'id': mv[0], 'player': mv[1], 'point': mv[2], 'revision': 1}
            for i, mv in enumerate(moves)]})
        if seal:
            state = archive.load_state(state_dir)
            credential = archive.credential_for(state, len(moves))
            _receipt, new_state = archive.seal_locked(state_dir, len(moves), credential)
            self.assertIsNotNone(new_state['final'])
        return state_dir

    def open_case(self, game_id, referees=('R1', 'R2'), case_ref=None):
        case_dir = os.path.join(self.dir, 'cases', game_id)
        appeal.open_case(case_dir, {
            'case_ref': case_ref or f'C-{game_id}', 'game_id': game_id,
            'archive_dir': os.path.join(self.dir, 'games', game_id),
            'referees': list(referees)})
        return case_dir

    def vote(self, case_dir, referee, version, groups):
        appeal.vote_locked(case_dir, referee, version, groups)

    def close(self, case_dir, version):
        return appeal.close_locked(case_dir, version)

    def reopen(self, case_dir, version):
        return appeal.reopen_locked(case_dir, version)

    def manifest(self, game_id, with_appeal=False, case_rel=None, players=('A', 'B')):
        entry = {'game_id': game_id, 'state_dir': os.path.join('games', game_id)}
        if with_appeal:
            entry['appeal_dir'] = case_rel or os.path.join('cases', game_id)
        return {
            'players': list(players), 'qualify_count': 1, 'lot_order': list(players),
            'tables': [{'table_id': 'T1', 'round': 1,
                        'black': players[0], 'white': players[1],
                        'candidates': [game_id]}],
            'archives': [entry]}

    def write_manifest(self, data, name='manifest.json'):
        path = os.path.join(self.dir, name)
        Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return path

    def run_standings(self, manifest_path, expect=0):
        self.out_counter += 1
        output = os.path.join(self.dir, f'report-{self.out_counter}.json')
        code, out, err = run_cli(['--source', manifest_path, '--output', output])
        self.assertEqual(code, expect, err)
        if expect == 0:
            return json.loads(Path(output).read_text(encoding='utf-8')), output
        self.assertFalse(os.path.exists(output), '失败不得留下半份报告')
        return None, err

    # ---- 主场景：和棋抽签 → 黑胜晋级 → awaiting_ruling → 白胜晋级 ----

    def test_appeal_lifecycle_drives_qualification(self):
        self.make_archive('G3')
        case_dir = self.open_case('G3')
        data = self.manifest('G3', with_appeal=False)
        path = self.write_manifest(data)
        # 不挂案卷：和棋按抽签序，A 晋级，输出结构与原行为一致
        report, _ = self.run_standings(path)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['qualified'], ['A'])
        assignment = report['witnesses'][0]['assignment'][0]
        self.assertEqual(set(assignment), {'table_id', 'game_id', 'receipt_id'})

        # 挂上案卷，第一轮两人标白块结案 → 黑胜，A 晋级，见证引用裁定轮次
        data_app = self.manifest('G3', with_appeal=True)
        path_app = self.write_manifest(data_app, 'manifest-appeal.json')
        self.vote(case_dir, 'R1', 1, [[2, 2]])
        self.vote(case_dir, 'R2', 2, [[2, 2]])
        record1 = self.close(case_dir, 3)
        self.assertEqual(record1['winner_after'], 'B')
        report, _ = self.run_standings(path_app)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['qualified'], ['A'])
        witness = report['witnesses'][0]['assignment'][0]
        self.assertEqual(witness['appeal_round'], 1)
        self.assertEqual(witness['receipt_id'], record1['final_receipt_id'])

        # 重开：awaiting_ruling，含待裁盘号、轮次与各台实际可用候选
        self.reopen(case_dir, 4)
        report, _ = self.run_standings(path_app)
        self.assertEqual(report['status'], 'awaiting_ruling')
        self.assertEqual(report['awaiting_ruling'],
                         [{'game_id': 'G3', 'round': 2, 'table_ids': ['T1']}])
        self.assertEqual(report['awaiting_game_ids'], [])
        self.assertIsNone(report['players'])
        self.assertIsNone(report['qualified'])
        self.assertIsNone(report['witnesses'])
        self.assertEqual(report['tables'][0]['usable_candidates'], ['G3'])

        # 第二轮两人标黑块结案 → 白胜，B 晋级，见证引用第二轮
        self.vote(case_dir, 'R1', 5, [[0, 0]])
        self.vote(case_dir, 'R2', 6, [[0, 0]])
        record2 = self.close(case_dir, 7)
        self.assertEqual(record2['winner_after'], 'W')
        report, _ = self.run_standings(path_app)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['qualified'], ['B'])
        witness = report['witnesses'][0]['assignment'][0]
        self.assertEqual(witness['appeal_round'], 2)
        self.assertEqual(witness['receipt_id'], record2['final_receipt_id'])

    def test_agreed_but_unclosed_is_not_an_official_result(self):
        self.make_archive('G3')
        case_dir = self.open_case('G3')
        self.vote(case_dir, 'R1', 1, [[2, 2]])
        self.vote(case_dir, 'R2', 2, [[2, 2]])  # 一致但未结案
        path = self.write_manifest(self.manifest('G3', with_appeal=True))
        report, _ = self.run_standings(path)
        self.assertEqual(report['status'], 'awaiting_ruling')
        self.assertEqual(report['awaiting_ruling'][0]['round'], 1)
        self.assertIsNone(report['qualified'])

    def test_divergent_and_single_vote_are_awaiting_ruling(self):
        self.make_archive('G3')
        case_dir = self.open_case('G3')
        path = self.write_manifest(self.manifest('G3', with_appeal=True))
        self.vote(case_dir, 'R1', 1, [[0, 0]])
        report, _ = self.run_standings(path)
        self.assertEqual(report['status'], 'awaiting_ruling')
        # 票齐但分歧同样拦名单
        self.vote(case_dir, 'R2', 2, [[2, 2]])
        report, _ = self.run_standings(path)
        self.assertEqual(report['status'], 'awaiting_ruling')

    # ---- 案卷与归档证据核对 ----

    def test_appeal_input_errors_are_located(self):
        self.make_archive('G3')
        case_dir = self.open_case('G3')
        base = self.manifest('G3', with_appeal=True)

        def expect_error(mutator, code, name):
            data = json.loads(json.dumps(base))
            mutator(data)
            _, err = self.run_standings(self.write_manifest(data, name), expect=2)
            self.assertIn(code, err)
            return err

        # 案卷不可读
        expect_error(lambda d: d['archives'][0].__setitem__(
            'appeal_dir', os.path.join('cases', 'MISSING')),
            'appeal_unreadable', 'm1.json')
        # 案卷盘号不符
        other_case = os.path.join(self.dir, 'cases', 'OTHER')
        appeal.open_case(other_case, {
            'case_ref': 'C-OTHER', 'game_id': 'G3',
            'archive_dir': os.path.join(self.dir, 'games', 'G3'),
            'referees': ['R1', 'R2']})
        state = appeal.load_case(other_case)
        state['game_id'] = 'OTHER'
        Path(other_case, 'case.json').write_text(json.dumps(state), encoding='utf-8')
        expect_error(lambda d: d['archives'][0].__setitem__(
            'appeal_dir', os.path.join('cases', 'OTHER')),
            'appeal_game_id_mismatch', 'm2.json')
        # 末手回执不符
        forged = os.path.join(self.dir, 'cases', 'FORGED')
        shutil.copytree(case_dir, forged)
        bad = appeal.load_case(forged)
        bad['final_receipt_id'] = 'deadbeef'
        Path(forged, 'case.json').write_text(json.dumps(bad), encoding='utf-8')
        expect_error(lambda d: d['archives'][0].__setitem__(
            'appeal_dir', os.path.join('cases', 'FORGED')),
            'appeal_evidence_mismatch', 'm3.json')
        # 开案盘面不符
        forged2 = os.path.join(self.dir, 'cases', 'FORGED2')
        shutil.copytree(case_dir, forged2)
        bad2 = appeal.load_case(forged2)
        bad2['original_board'] = ['...', '...', '...']
        Path(forged2, 'case.json').write_text(json.dumps(bad2), encoding='utf-8')
        expect_error(lambda d: d['archives'][0].__setitem__(
            'appeal_dir', os.path.join('cases', 'FORGED2')),
            'appeal_evidence_mismatch', 'm4.json')
        # 盘宽/贴目不符
        forged3 = os.path.join(self.dir, 'cases', 'FORGED3')
        shutil.copytree(case_dir, forged3)
        bad3 = appeal.load_case(forged3)
        bad3['komi'] = '7.5'
        Path(forged3, 'case.json').write_text(json.dumps(bad3), encoding='utf-8')
        expect_error(lambda d: d['archives'][0].__setitem__(
            'appeal_dir', os.path.join('cases', 'FORGED3')),
            'appeal_evidence_mismatch', 'm5.json')

    def test_current_status_inconsistent_with_ruling_is_error(self):
        self.make_archive('G3')
        case_dir = self.open_case('G3')
        self.vote(case_dir, 'R1', 1, [])
        self.vote(case_dir, 'R2', 2, [])
        self.close(case_dir, 3)
        # 篡改：状态仍 open 但挂着首轮裁定（状态与裁定不一致）
        bad = appeal.load_case(case_dir)
        bad['status'] = 'open'
        Path(case_dir, 'case.json').write_text(json.dumps(bad), encoding='utf-8')
        path = self.write_manifest(self.manifest('G3', with_appeal=True), 'bad.json')
        _, err = self.run_standings(path, expect=2)
        self.assertIn('appeal_state_inconsistent', err)

    def test_appeal_references_archive_without_final_evidence(self):
        # 归档只收到双停、未封存：挂案卷是可定位输入错误，不能改用旧比分
        self.make_archive('G3')
        self.make_archive('PEND', seal=False)
        case_dir = os.path.join(self.dir, 'cases', 'PEND')
        # 无法正常开案（无终局证据）；直接构造一个指向它的案卷引用
        os.makedirs(case_dir, exist_ok=True)
        good = appeal.load_case(self.open_case('G3'))
        good['case_ref'] = 'C-PEND'
        good['game_id'] = 'PEND'
        Path(case_dir, 'case.json').write_text(json.dumps(good), encoding='utf-8')
        data = self.manifest('PEND', with_appeal=True)
        _, err = self.run_standings(self.write_manifest(data, 'pend.json'), expect=2)
        self.assertIn('appeal_archive_not_final', err)

    def test_awaiting_final_takes_precedence_and_mixed_statuses(self):
        # G1 未封存（无案卷）→ awaiting_final；G3 案卷未结案 → 不改变 awaiting_final 结论
        self.make_archive('G1', seal=False)
        self.make_archive('G3')
        case_dir = self.open_case('G3')
        self.vote(case_dir, 'R1', 1, [[0, 0]])
        data = {
            'players': ['A', 'B', 'C', 'D'], 'qualify_count': 1,
            'lot_order': ['A', 'B', 'C', 'D'],
            'tables': [
                {'table_id': 'T1', 'round': 1, 'black': 'A', 'white': 'B',
                 'candidates': ['G1']},
                {'table_id': 'T2', 'round': 1, 'black': 'C', 'white': 'D',
                 'candidates': ['G3']}],
            'archives': [
                {'game_id': 'G1', 'state_dir': os.path.join('games', 'G1')},
                {'game_id': 'G3', 'state_dir': os.path.join('games', 'G3'),
                 'appeal_dir': os.path.join('cases', 'G3')}]}
        report, _ = self.run_standings(self.write_manifest(data, 'mixed.json'))
        self.assertEqual(report['status'], 'awaiting_final')
        self.assertEqual(report['awaiting_game_ids'], ['G1'])
        # 封存 G1 后，仅剩案卷未结案 → awaiting_ruling
        state = archive.load_state(os.path.join(self.dir, 'games', 'G1'))
        credential = archive.credential_for(state, state['total_moves'])
        archive.seal_locked(os.path.join(self.dir, 'games', 'G1'),
                            state['total_moves'], credential)
        report, _ = self.run_standings(self.write_manifest(data, 'mixed2.json'))
        self.assertEqual(report['status'], 'awaiting_ruling')
        self.assertEqual(report['awaiting_ruling'],
                         [{'game_id': 'G3', 'round': 1, 'table_ids': ['T2']}])

    def test_no_complete_assignment_not_masked_by_awaiting_ruling(self):
        # 无完整对应关系必须先报错，不能被 awaiting_ruling 掩盖
        self.make_archive('X')
        self.make_archive('Y')
        case_dir = self.open_case('X')
        self.vote(case_dir, 'R1', 1, [[0, 0]])  # 未结案
        data = {
            'players': ['A', 'B', 'C', 'D'], 'qualify_count': 1,
            'lot_order': ['A', 'B', 'C', 'D'],
            'tables': [
                {'table_id': 'T1', 'round': 1, 'black': 'A', 'white': 'B',
                 'candidates': ['X']},
                {'table_id': 'T2', 'round': 1, 'black': 'C', 'white': 'D',
                 'candidates': ['X']}],
            'archives': [
                {'game_id': 'X', 'state_dir': os.path.join('games', 'X'),
                 'appeal_dir': os.path.join('cases', 'X')},
                {'game_id': 'Y', 'state_dir': os.path.join('games', 'Y')}]}
        _, err = self.run_standings(self.write_manifest(data, 'nomatch.json'), expect=2)
        self.assertIn('no_complete_assignment', err)

    def test_appeal_dirs_are_read_only_and_archive_moved(self):
        self.make_archive('G3')
        case_dir = self.open_case('G3')
        self.vote(case_dir, 'R1', 1, [[2, 2]])
        self.vote(case_dir, 'R2', 2, [[2, 2]])
        self.close(case_dir, 3)
        case_bytes = Path(case_dir, 'case.json').read_bytes()
        path = self.write_manifest(self.manifest('G3', with_appeal=True))
        self.run_standings(path)
        # 只读：案卷字节不变
        self.assertEqual(Path(case_dir, 'case.json').read_bytes(), case_bytes)
        # 归档移到冷库：standings 无法核对归档，给可定位错误而不是静默用旧比分
        cold = os.path.join(self.dir, 'cold')
        os.makedirs(cold, exist_ok=True)
        shutil.move(os.path.join(self.dir, 'games', 'G3'), os.path.join(cold, 'G3'))
        _, err = self.run_standings(self.write_manifest(
            self.manifest('G3', with_appeal=True), 'cold.json'), expect=2)
        self.assertIn('archive_unreadable', err)
        # 但案卷自身的两份证据仍可独立复算（这里只有一轮，再开一轮验证多轮）
        self.reopen(case_dir, 4)
        self.vote(case_dir, 'R1', 5, [[0, 0]])
        self.vote(case_dir, 'R2', 6, [[0, 0]])
        self.close(case_dir, 7)
        for round_number, expected in ((1, 'B'), (2, 'W')):
            record = appeal.history(appeal.load_case(case_dir), round_number)
            self.assertEqual(appeal.recompute_ruling(record)['winner_after'], expected)

    def test_relative_appeal_dir_resolved_from_manifest_dir(self):
        self.make_archive('G3')
        case_dir = self.open_case('G3')
        self.vote(case_dir, 'R1', 1, [[2, 2]])
        self.vote(case_dir, 'R2', 2, [[2, 2]])
        self.close(case_dir, 3)
        nested = os.path.join(self.dir, 'nested')
        os.makedirs(nested, exist_ok=True)
        # 相对路径按清单所在目录解释；归档与案卷都在 nested 的上一层
        data = self.manifest('G3', with_appeal=True,
                             case_rel=os.path.join('..', 'cases', 'G3'))
        data['archives'][0]['state_dir'] = os.path.join('..', 'games', 'G3')
        path = self.write_manifest(data, os.path.join('nested', 'manifest.json'))
        report, _ = self.run_standings(path)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['qualified'], ['A'])

    def test_output_not_overwritten_and_duplicate_json_key(self):
        self.make_archive('G3')
        self.open_case('G3')
        path = self.write_manifest(self.manifest('G3', with_appeal=True))
        report1, out1 = self.run_standings(path)
        before = Path(out1).read_bytes()
        code, _out, err = run_cli(['--source', path, '--output', out1])
        self.assertEqual(code, 2)
        self.assertIn('output_exists', err)
        self.assertEqual(Path(out1).read_bytes(), before)
        dup = os.path.join(self.dir, 'dup.json')
        Path(dup).write_text(
            '{"players":["A","B"],"players":["A","B"],"qualify_count":1,'
            '"lot_order":["A","B"],"tables":[],"archives":[]}', encoding='utf-8')
        code, _out, err = run_cli(['--source', dup, '--output',
                                   os.path.join(self.dir, 'x.json')])
        self.assertEqual(code, 2)
        self.assertIn('duplicate JSON key', err)


if __name__ == '__main__':
    unittest.main()
