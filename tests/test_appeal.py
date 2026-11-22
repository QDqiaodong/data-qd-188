import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import go_record
from go_record import appeal, archive
from go_record.appeal_cli import main as appeal_main

# 3 路盘黑 [0,0]、白 [2,2]、贴目 0，黑白各停一手：原面积和棋
G3_MOVES = [('m1', 'B', [0, 0]), ('m2', 'W', [2, 2]), ('m3', 'B', None), ('m4', 'W', None)]


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = appeal_main(argv)
    return code, out.getvalue(), err.getvalue()


class AppealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='go-appeal-')
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.game_id = 'G3'
        self.archive_dir = os.path.join(self.dir, 'games', self.game_id)
        self.case_dir = os.path.join(self.dir, 'cases', self.game_id)

    # ---- 工具 ----

    def write_json(self, name, payload):
        path = os.path.join(self.dir, name)
        Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return path

    def make_sealed_archive(self, game_id=None, moves=G3_MOVES, size=3, komi='0',
                            setup=None):
        game_id = game_id or self.game_id
        state_dir = os.path.join(self.dir, 'games', game_id)
        spec = {'game_id': game_id, 'total_moves': len(moves), 'size': size,
                'setup': setup or {'black': [], 'white': []},
                'next_player': 'B', 'komi': komi}
        archive.create_state(state_dir, spec)
        archive.receive_locked(state_dir, {'game_id': game_id, 'records': [
            {'ply': i + 1, 'id': mv[0], 'player': mv[1], 'point': mv[2], 'revision': 1}
            for i, mv in enumerate(moves)]})
        state = archive.load_state(state_dir)
        credential = archive.credential_for(state, len(moves))
        receipt, new_state = archive.seal_locked(state_dir, len(moves), credential)
        self.assertIsNotNone(new_state['final'])
        return state_dir, receipt, new_state

    def open(self, referees=('R1', 'R2'), game_id=None, archive_dir=None, case_ref='C1',
             case_dir=None, expect=0):
        game_id = game_id or self.game_id
        spec = {'case_ref': case_ref, 'game_id': game_id,
                'archive_dir': archive_dir or self.archive_dir, 'referees': list(referees)}
        path = self.write_json(f'open-{case_ref}.json', spec)
        code, out, err = run_cli(['open', '--case', case_dir or self.case_dir, '--spec', path])
        self.assertEqual(code, expect, err)
        return (json.loads(out) if out.strip() else None), err

    def cli(self, argv, expect=0):
        code, out, err = run_cli(argv)
        self.assertEqual(code, expect, f'{argv}\n{err}')
        return (json.loads(out) if out.strip() else None), err

    def cli_fail(self, argv, code_name):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = appeal_main(argv)
        self.assertEqual(code, 2)
        diagnostic = json.loads(err.getvalue())
        self.assertEqual(diagnostic['error']['code'], code_name)
        return diagnostic

    def vote(self, referee, version, groups, expect=0):
        return self.cli(['vote', '--case', self.case_dir, '--referee', referee,
                         '--expected-version', str(version), '--groups', json.dumps(groups)],
                        expect=expect)

    def close(self, version, expect=0):
        return self.cli(['close', '--case', self.case_dir,
                         '--expected-version', str(version)], expect=expect)

    def reopen(self, version, expect=0):
        return self.cli(['reopen', '--case', self.case_dir,
                         '--expected-version', str(version)], expect=expect)

    def view(self):
        out, _ = self.cli(['view', '--case', self.case_dir])
        return out

    def history(self, round_number=None, expect=0):
        argv = ['history', '--case', self.case_dir]
        if round_number is not None:
            argv += ['--round', str(round_number)]
        return self.cli(argv, expect=expect)

    # ---- 开案取证 ----

    def test_open_fixes_evidence_and_refuses_overwrite(self):
        state_dir, receipt, arc = self.make_sealed_archive()
        out, _ = self.open()
        self.assertTrue(out['opened'])
        self.assertEqual(out['final_receipt_id'], receipt['receipt_id'])
        self.assertEqual(out['original_winner'], 'draw')
        case = appeal.load_case(self.case_dir)
        self.assertEqual((case['round'], case['version'], case['status']), (1, 1, 'open'))
        self.assertEqual(case['final_receipt_id'], receipt['receipt_id'])
        self.assertEqual(case['final_sealed_ply'], 4)
        self.assertEqual(case['original_board'], arc['boundary']['board'])
        self.assertEqual(case['size'], 3)
        self.assertEqual(case['komi'], '0')
        self.assertEqual(case['original_area'], arc['final']['area'])
        self.assertEqual(case['referees'], ['R1', 'R2'])
        self.assertEqual(case['votes'], {})
        self.assertEqual(case['rounds'], [])
        # 不覆盖已有案卷
        _, err = self.open(case_ref='C1', expect=2)
        self.assertIn('case_exists', err)

    def test_open_requires_final_double_pass_archive(self):
        # 只收包到双停、未封到末手：没有终局证据，拒绝开案
        state_dir = os.path.join(self.dir, 'games', 'PEND')
        spec = {'game_id': 'PEND', 'total_moves': 4, 'size': 3,
                'setup': {'black': [], 'white': []}, 'next_player': 'B', 'komi': '0'}
        archive.create_state(state_dir, spec)
        archive.receive_locked(state_dir, {'game_id': 'PEND', 'records': [
            {'ply': i + 1, 'id': mv[0], 'player': mv[1], 'point': mv[2], 'revision': 1}
            for i, mv in enumerate(G3_MOVES)]})
        _, err = self.open(game_id='PEND', archive_dir=state_dir,
                           case_dir=os.path.join(self.dir, 'cases', 'PEND'), expect=2)
        self.assertIn('archive_evidence_invalid', err)

    def test_open_rejects_wrong_game_missing_archive_and_bad_referees(self):
        self.make_sealed_archive()
        _, err = self.open(game_id='OTHER', expect=2)
        self.assertIn('game_id_mismatch', err)
        _, err = self.open(archive_dir=os.path.join(self.dir, 'games', 'NOPE'), expect=2)
        self.assertIn('archive_unreadable', err)
        _, err = self.open(referees=('R1', 'R1'), expect=2)
        self.assertIn('invalid_structure', err)
        _, err = self.open(referees=('R1',), expect=2)
        self.assertIn('invalid_structure', err)

    def test_open_recomputes_area_and_detects_tampered_final(self):
        self.make_sealed_archive()
        forged_dir = os.path.join(self.dir, 'games', 'FORGED')
        shutil.copytree(self.archive_dir, forged_dir)
        state_path = os.path.join(forged_dir, 'state.json')
        forged = json.loads(Path(state_path).read_text(encoding='utf-8'))
        forged['final']['area']['winner'] = 'B'
        Path(state_path).write_text(json.dumps(forged), encoding='utf-8')
        _, err = self.open(game_id=self.game_id, archive_dir=forged_dir,
                           case_dir=os.path.join(self.dir, 'cases', 'FORGED'), expect=2)
        self.assertIn('archive_evidence_invalid', err)
        # 被篡改的归档不产生案卷
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'cases', 'FORGED', 'case.json')))

    # ---- 主场景：分歧、一致未结案、结案、重开 ----

    def test_full_two_round_scenario_and_cold_storage(self):
        self.make_sealed_archive()
        self.open()
        # 各标一方：票齐但分歧，结不了案
        self.vote('R1', 1, [[0, 0]])
        self.vote('R2', 2, [[2, 2]])
        view = self.view()
        self.assertEqual(view['phase'], 'divergent')
        self.assertIsNone(view['current_winner'])
        _, err = self.close(3, expect=2)
        self.assertIn('votes_divergent', err)
        # 改为都标白块；尚未结案时仍没有正式胜负
        self.vote('R1', 3, [[2, 2]])
        view = self.view()
        self.assertEqual(view['phase'], 'agreed')
        self.assertEqual(view['status'], 'open')
        self.assertIsNone(view['current_winner'])
        self.assertIsNone(view['current_ruling_round'])
        # 结案：黑面积 9、白面积 0
        out, _ = self.close(4)
        self.assertTrue(out['closed'])
        self.assertEqual(out['winner_before'], 'draw')
        self.assertEqual(out['winner_after'], 'B')
        self.assertEqual(out['area_after']['black_area'], '9')
        self.assertEqual(out['area_after']['white_area_with_komi'], '0')
        view = self.view()
        self.assertEqual(view['phase'], 'closed')
        self.assertEqual(view['current_winner'], 'B')
        self.assertEqual(view['current_ruling_round'], 1)
        # 已结案不能再改票或结案
        diag = self.cli_fail(['vote', '--case', self.case_dir, '--referee', 'R1',
                              '--expected-version', '5', '--groups', '[]'], 'case_closed')
        self.assertEqual(diag['error']['round'], 1)
        self.cli_fail(['close', '--case', self.case_dir, '--expected-version', '5'],
                      'case_closed')
        # 重开：第一轮仍可查且是黑胜，但当前不能使用它
        out, _ = self.reopen(5)
        self.assertEqual((out['round'], out['phase']), (2, 'awaiting_votes'))
        view = self.view()
        self.assertEqual((view['round'], view['status'], view['phase']), (2, 'open', 'awaiting_votes'))
        self.assertIsNone(view['current_winner'])
        round1 = self.history(1)[0]
        self.assertEqual(round1['winner_after'], 'B')
        self.assertEqual(round1['round'], 1)
        # 第二轮两人一致标黑块，结果改为白胜
        self.vote('R1', 6, [[0, 0]])
        self.vote('R2', 7, [[0, 0]])
        out, _ = self.close(8)
        self.assertEqual(out['winner_after'], 'W')
        self.assertEqual(out['area_after']['black_area'], '0')
        self.assertEqual(out['area_after']['white_area_with_komi'], '9')
        view = self.view()
        self.assertEqual((view['round'], view['current_ruling_round'],
                          view['current_winner']), (2, 2, 'W'))
        # 历史目录与两份裁定
        index, _ = self.history()
        self.assertEqual([r['round'] for r in index['rounds']], [1, 2])
        self.assertEqual([r['winner_after'] for r in index['rounds']], ['B', 'W'])
        for round_number, expected in ((1, 'B'), (2, 'W')):
            record = self.history(round_number)[0]
            # 裁定内容自证身份，且固定盘号、末手回执、轮次、两位裁判、棋块颜色成员、
            # 前后盘面、盘宽、贴目与面积胜负
            self.assertEqual(record['game_id'], self.game_id)
            self.assertEqual(record['final_receipt_id'], view['final_receipt_id'])
            self.assertEqual(record['referees'], ['R1', 'R2'])
            self.assertEqual(record['size'], 3)
            self.assertEqual(record['komi'], '0')
            self.assertEqual(record['board_before'], ['B..', '...', '..W'])
            self.assertTrue(record['groups'])
            recomputed, _ = self.cli(['recompute', '--case', self.case_dir,
                                      '--round', str(round_number)])
            self.assertEqual(recomputed['winner_after'], expected)
        # 归档移到冷库后，案卷仍能独立复算两份裁定，且不回写原归档
        cold_dir = os.path.join(self.dir, 'cold-storage')
        os.makedirs(cold_dir, exist_ok=True)
        shutil.move(self.archive_dir, os.path.join(cold_dir, self.game_id))
        self.assertFalse(os.path.isdir(self.archive_dir))
        for round_number, expected in ((1, 'B'), (2, 'W')):
            record = appeal.history(appeal.load_case(self.case_dir), round_number)
            recomputed = appeal.recompute_ruling(record)
            self.assertEqual(recomputed['winner_after'], expected)
        self.assertEqual(appeal.view_state(appeal.load_case(self.case_dir))['current_winner'],
                         'W')

    def test_empty_opinion_is_a_vote_not_a_missing_vote(self):
        self.make_sealed_archive()
        self.open()
        # 空集合是已投意见：缺票阶段仍可区分 submitted
        self.vote('R1', 1, [])
        view = self.view()
        self.assertEqual(view['phase'], 'awaiting_votes')
        r1 = next(v for v in view['votes'] if v['referee'] == 'R1')
        r2 = next(v for v in view['votes'] if v['referee'] == 'R2')
        self.assertTrue(r1['submitted'])
        self.assertFalse(r2['submitted'])
        # 缺票不能结案
        self.cli_fail(['close', '--case', self.case_dir, '--expected-version', '2'],
                      'votes_incomplete')
        # 两人都投空意见：一致，可结案；不移除任何棋块，胜负与原面积相同
        self.vote('R2', 2, [])
        view = self.view()
        self.assertEqual(view['phase'], 'agreed')
        out, _ = self.close(3)
        self.assertEqual(out['winner_after'], 'draw')
        self.assertEqual(out['area_after'], out['area_before'])
        record = self.history(1)[0]
        self.assertEqual(record['groups'], [])

    def test_resubmit_same_opinion_advances_version(self):
        self.make_sealed_archive()
        self.open()
        self.vote('R1', 1, [[0, 0]])
        out, _ = self.vote('R1', 2, [[0, 0]])  # 重提相同意见也算一次成功写入
        self.assertEqual(out['version'], 3)
        case = appeal.load_case(self.case_dir)
        self.assertEqual(case['version'], 3)
        self.assertEqual(case['votes']['R1']['written_version'], 3)

    def test_version_conflict_changes_nothing(self):
        self.make_sealed_archive()
        self.open()
        self.vote('R1', 1, [[0, 0]])
        before = Path(self.case_dir, 'case.json').read_bytes()
        # 错误预期版本：拒绝且案卷字节不变
        diag = self.cli_fail(['vote', '--case', self.case_dir, '--referee', 'R2',
                              '--expected-version', '99', '--groups', '[[2,2]]'],
                             'version_conflict')
        self.assertEqual((diag['error']['expected_version'], diag['error']['actual_version']),
                         (99, 2))
        self.assertEqual(Path(self.case_dir, 'case.json').read_bytes(), before)
        self.cli_fail(['close', '--case', self.case_dir, '--expected-version', '99'],
                      'version_conflict')
        self.assertEqual(Path(self.case_dir, 'case.json').read_bytes(), before)

    # ---- 棋块代表坐标规则 ----

    def _ring_archive(self):
        # 7 路盘：白棋环（12 子）围住 2x2 内域，内有黑子两子连通块。
        ring = [(2, 2), (2, 3), (2, 4), (2, 5),
                (3, 2), (3, 5), (4, 2), (4, 5),
                (5, 2), (5, 3), (5, 4), (5, 5)]
        setup = {'black': [[3, 3], [3, 4]], 'white': [list(p) for p in ring]}
        moves = [('p1', 'B', None), ('p2', 'W', None)]
        state_dir, receipt, arc = self.make_sealed_archive(
            game_id='RING', moves=moves, size=7, setup=setup)
        self.archive_dir = state_dir
        return state_dir, receipt, arc

    def test_same_group_different_representatives_close(self):
        self.archive_dir, _receipt, _ = self._ring_archive()
        self.case_dir = os.path.join(self.dir, 'cases', 'RING')
        self.open(game_id='RING', case_dir=self.case_dir)
        # 两名裁判点同一块的不同坐标：代表相同棋块，可以结案
        self.vote('R1', 1, [[3, 3]])
        self.vote('R2', 2, [[3, 4]])
        view = self.view()
        self.assertEqual(view['phase'], 'agreed')
        out, _ = self.close(3)
        record = self.history(1)[0]
        self.assertEqual(len(record['groups']), 1)
        self.assertEqual(record['groups'][0]['color'], 'B')
        self.assertEqual(sorted(record['groups'][0]['stones']), [[3, 3], [3, 4]])
        self.assertEqual(sorted(record['groups'][0]['representatives']), [[3, 3], [3, 4]])
        # 空域重新归属：内域 4 点全归白，不能只按子数改分
        self.assertEqual(out['area_after']['black_area'], '0')
        self.assertEqual(out['area_after']['white_area_with_komi'], '49')
        self.assertEqual(out['area_before']['black_area'], '2')
        self.assertEqual(out['area_before']['white_area_with_komi'], '45')

    def test_duplicate_representative_rejected_and_vote_kept(self):
        self._ring_archive()
        self.case_dir = os.path.join(self.dir, 'cases', 'RING')
        self.open(game_id='RING', case_dir=self.case_dir)
        self.vote('R1', 1, [[3, 3]])
        # 一份意见用两个坐标重复标同块：整次退回，原票不变
        diag = self.cli_fail(['vote', '--case', self.case_dir, '--referee', 'R2',
                              '--expected-version', '2',
                              '--groups', json.dumps([[3, 3], [3, 4]])],
                             'duplicate_group_in_opinion')
        self.assertEqual(diag['error']['point'], [3, 4])
        case = appeal.load_case(self.case_dir)
        self.assertEqual(case['version'], 2)
        self.assertNotIn('R2', case['votes'])
        self.assertEqual(case['votes']['R1']['groups'][0]['representative'], [3, 3])
        # 空点与盘外同样拒绝
        self.cli_fail(['vote', '--case', self.case_dir, '--referee', 'R2',
                       '--expected-version', '2', '--groups', '[[4,4]]'], 'point_not_stone')
        self.cli_fail(['vote', '--case', self.case_dir, '--referee', 'R2',
                       '--expected-version', '2', '--groups', '[[7,0]]'], 'point_out_of_board')

    def test_unknown_referee_and_closed_round_guards(self):
        self.make_sealed_archive()
        self.open()
        self.cli_fail(['vote', '--case', self.case_dir, '--referee', 'NOBODY',
                       '--expected-version', '1', '--groups', '[]'], 'unknown_referee')
        # 未结案不能重开
        self.cli_fail(['reopen', '--case', self.case_dir, '--expected-version', '1'],
                      'case_open')
        self.vote('R1', 1, [])
        self.vote('R2', 2, [])
        self.close(3)
        # 重开后拿旧版本改票被拒且旧裁定不变
        self.reopen(4)
        self.cli_fail(['vote', '--case', self.case_dir, '--referee', 'R1',
                       '--expected-version', '4', '--groups', '[[0,0]]'],
                      'version_conflict')
        record = self.history(1)[0]
        self.assertEqual(record['winner_after'], 'draw')
        self.assertEqual(appeal.load_case(self.case_dir)['round'], 2)

    def test_history_unknown_round(self):
        self.make_sealed_archive()
        self.open()
        diag = self.cli_fail(['history', '--case', self.case_dir, '--round', '9'],
                             'round_not_found')
        self.assertEqual(diag['error']['closed_rounds'], [])

    # ---- 提交边界：故障注入与并发 ----

    def test_close_commit_boundary_faults(self):
        # 中断在替换前：只能看到旧版本、原两票且无正式裁定
        self.make_sealed_archive()
        self.open()
        appeal.vote_locked(self.case_dir, 'R1', 1, [[2, 2]])
        appeal.vote_locked(self.case_dir, 'R2', 2, [[2, 2]])

        def crash_before(point):
            if point == 'commit_before_replace':
                raise RuntimeError('crash before replace')

        appeal._FAULT_HOOK = crash_before
        try:
            with self.assertRaises(RuntimeError):
                appeal.close_locked(self.case_dir, 3)
        finally:
            appeal._FAULT_HOOK = None
        state = appeal.load_case(self.case_dir)
        self.assertEqual((state['version'], state['status']), (3, 'open'))
        self.assertEqual(set(state['votes']), {'R1', 'R2'})
        self.assertEqual(state['rounds'], [])
        self.assertEqual(appeal.view_state(state)['phase'], 'agreed')
        # 重开工具后正常结案
        record = appeal.close_locked(self.case_dir, 3)
        self.assertEqual(record['winner_after'], 'B')

        # 中断在替换后：新版本、已结案且首轮完整可查
        case2 = os.path.join(self.dir, 'cases', 'G3B')
        self.open(case_dir=case2, case_ref='C2')
        appeal.vote_locked(case2, 'R1', 1, [[2, 2]])
        appeal.vote_locked(case2, 'R2', 2, [[2, 2]])

        def crash_after(point):
            if point == 'commit_after_replace':
                raise RuntimeError('crash after replace')

        appeal._FAULT_HOOK = crash_after
        try:
            with self.assertRaises(RuntimeError):
                appeal.close_locked(case2, 3)
        finally:
            appeal._FAULT_HOOK = None
        state = appeal.load_case(case2)
        self.assertEqual((state['version'], state['status']), (4, 'closed'))
        self.assertEqual(len(state['rounds']), 1)
        record = appeal.history(state, 1)
        self.assertEqual(record['winner_after'], 'B')
        appeal.verify_state(state)

    def test_concurrent_same_version_writes_only_one_succeeds(self):
        self.make_sealed_archive()
        self.open()
        barrier = threading.Barrier(2)
        results = []

        def writer(referee):
            barrier.wait()
            try:
                appeal.vote_locked(self.case_dir, referee, 1,
                                   [[0, 0]] if referee == 'R1' else [[2, 2]])
                results.append((referee, 'ok'))
            except appeal.Reject as error:
                results.append((referee, error.code))

        threads = [threading.Thread(target=writer, args=(r,)) for r in ('R1', 'R2')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(code for _ref, code in results),
                         ['ok', 'version_conflict'])
        state = appeal.load_case(self.case_dir)
        self.assertEqual(state['version'], 2)
        self.assertEqual(len(state['votes']), 1)

    def test_subprocess_restart_with_fault_env(self):
        self.make_sealed_archive()
        spec_path = self.write_json('open.json', {
            'case_ref': 'C1', 'game_id': self.game_id, 'archive_dir': self.archive_dir,
            'referees': ['R1', 'R2']})
        package_root = str(Path(go_record.__file__).resolve().parents[1])
        env = dict(os.environ)
        env['PYTHONPATH'] = package_root

        def runp(argv, expect=0, env_extra=None):
            merged = dict(env)
            if env_extra:
                merged.update(env_extra)
            result = subprocess.run([sys.executable, '-m', 'go_record.appeal_cli'] + argv,
                                    capture_output=True, text=True, env=merged, cwd=self.dir)
            self.assertEqual(result.returncode, expect, result.stderr)
            return result

        runp(['open', '--case', 'cases/G3', '--spec', spec_path])
        runp(['vote', '--case', 'cases/G3', '--referee', 'R1',
              '--expected-version', '1', '--groups', '[[2,2]]'])
        runp(['vote', '--case', 'cases/G3', '--referee', 'R2',
              '--expected-version', '2', '--groups', '[[2,2]]'])
        # 替换后崩溃：重开只能看到已结案状态
        runp(['close', '--case', 'cases/G3', '--expected-version', '3'], expect=1,
             env_extra={appeal.FAULT_ENV: 'commit_after_replace'})
        out = runp(['view', '--case', 'cases/G3'])
        status = json.loads(out.stdout)
        self.assertEqual((status['status'], status['phase'], status['current_ruling_round']),
                         ('closed', 'closed', 1))
        out = runp(['history', '--case', 'cases/G3', '--round', '1'])
        record = json.loads(out.stdout)
        self.assertEqual(record['winner_after'], 'B')
        # 旧版本改票被拒（先撞版本检查），旧裁定不变
        result = subprocess.run(
            [sys.executable, '-m', 'go_record.appeal_cli', 'vote', '--case',
             'cases/G3', '--referee', 'R1', '--expected-version', '3', '--groups', '[]'],
            capture_output=True, text=True, env=env, cwd=self.dir)
        self.assertEqual(result.returncode, 2)
        self.assertIn('version_conflict', result.stderr)
        # 即使带上当前版本，已结案状态仍拒绝改票
        result = subprocess.run(
            [sys.executable, '-m', 'go_record.appeal_cli', 'vote', '--case',
             'cases/G3', '--referee', 'R1', '--expected-version', '4', '--groups', '[]'],
            capture_output=True, text=True, env=env, cwd=self.dir)
        self.assertEqual(result.returncode, 2)
        self.assertIn('case_closed', result.stderr)
        record = json.loads(runp(['history', '--case', 'cases/G3', '--round', '1']).stdout)
        self.assertEqual(record['winner_after'], 'B')


if __name__ == '__main__':
    unittest.main()
