import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import go_record
from go_record import archive
from go_record.archive_cli import main as archive_main
from go_record.service import process

SPEC6 = {
    'game_id': 'ko-6', 'total_moves': 6, 'size': 5,
    'setup': {'black': [[0, 1], [1, 0], [2, 1]], 'white': [[1, 1], [0, 2], [2, 2], [1, 3]]},
    'next_player': 'B', 'komi': '0.5',
}
MOVES6 = [
    ('m1', 'B', [1, 2]), ('m2', 'W', None), ('m3', 'B', [4, 4]),
    ('m4', 'W', [1, 1]), ('m5', 'B', None), ('m6', 'W', None),
]


def rec(ply, move_id, player, point, revision=1):
    return {'ply': ply, 'id': move_id, 'player': player, 'point': point, 'revision': revision}


def packet(*records, game='ko-6'):
    return {'game_id': game, 'records': list(records)}


def full_record(moves=MOVES6):
    return {'size': 5, 'setup': SPEC6['setup'], 'next_player': 'B', 'komi': '0.5',
            'moves': [{'id': i, 'player': p, 'point': pt} for i, p, pt in moves]}


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = archive_main(argv)
    return code, out.getvalue(), err.getvalue()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='go-archive-')
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, 'game')

    def path(self, name, payload):
        p = os.path.join(self.dir, name)
        Path(p).write_text(json.dumps(payload), encoding='utf-8')
        return p

    def cli(self, argv, expect=0):
        code, out, err = run_cli(argv)
        self.assertEqual(code, expect, f'{argv}\n{err}')
        return (json.loads(out) if out.strip() else None), err

    def init(self, spec, state=None, expect=0):
        return self.cli(['init', '--state', state or self.state, '--spec',
                         self.path('spec.json', spec)], expect)

    def receive(self, pkt, state=None, expect=0):
        return self.cli(['receive', '--state', state or self.state, '--packet',
                         self.path('packet.json', pkt)], expect)

    def view(self, target=None, state=None, expect=0):
        argv = ['view', '--state', state or self.state]
        if target is not None:
            argv += ['--target', str(target)]
        return self.cli(argv, expect)

    def seal(self, target, credential, state=None, expect=0):
        return self.cli(['seal', '--state', state or self.state, '--target', str(target),
                         '--credential', credential], expect)

    def init6(self, state=None):
        self.init(SPEC6, state=state)
        # 初始化不得覆盖已有状态
        _, err = self.init(SPEC6, state=state, expect=2)
        self.assertIn('state_exists', err)

    # ---- 主场景：5 路盘、劫、分段封存、最终归档 ----

    def test_scenario_a_ko_game_end_to_end(self):
        self.init6()
        # 后半段先到：第 3、4 手
        report, _ = self.receive(packet(rec(3, 'm3', 'B', [4, 4]), rec(4, 'm4', 'W', [1, 1])))
        self.assertEqual([a['ply'] for a in report['accepted']], [3, 4])
        status, _ = self.view()
        self.assertEqual(status['prefix_ply'], 0)
        self.assertEqual(status['blocker'], {'ply': 1, 'reason': 'missing'})
        self.assertEqual([(p['ply'], p['applied']) for p in status['pending']], [(3, False), (4, False)])
        # 第 1 手补到：预览只到第 1 手，黑提一子，与整盘回放一致
        self.receive(packet(rec(1, 'm1', 'B', [1, 2])))
        status, _ = self.view()
        self.assertEqual(status['prefix_ply'], 1)
        self.assertEqual(status['captures'], {'B': 1, 'W': 0})
        self.assertEqual(status['blocker'], {'ply': 2, 'reason': 'missing'})
        r1 = process(full_record(MOVES6[:1]))
        self.assertEqual(status['board'], r1['board'])
        self.assertEqual(status['captures'], r1['captures'])
        # 核对并封存第 1 手；同一受核对范围重开后凭据相同
        status, _ = self.view(target=1)
        cred1 = status['credential']['value']
        self.assertEqual((status['credential']['from'], status['credential']['to']), (0, 1))
        status, _ = self.view(target=1)
        self.assertEqual(status['credential']['value'], cred1)
        out, _ = self.seal(1, cred1)
        receipt1 = out['receipt']
        self.assertEqual((receipt1['previous_sealed_ply'], receipt1['sealed_ply']), (0, 1))
        self.assertEqual(receipt1['credential'], cred1)
        self.assertIsNone(out['final'])
        # 重开后：第 2 手白立即回提 [1,1] 因简单劫阻断；已到的 3、4 手不得提前应用
        self.receive(packet(rec(2, 'm2', 'W', [1, 1])))
        status, _ = self.view()
        self.assertEqual(status['prefix_ply'], 1)
        self.assertEqual(status['blocker'], {'ply': 2, 'reason': 'simple_ko', 'move_id': 'm2'})
        self.assertEqual([(p['ply'], p['applied']) for p in status['pending']],
                         [(2, False), (3, False), (4, False)])
        r2 = process(full_record([('m1', 'B', [1, 2]), ('m2', 'W', [1, 1])]))
        self.assertEqual(r2['first_illegal']['reason'], 'simple_ko')
        # 第 2 手升版改成白停一手：3、4 手无需重传即进入合法前缀，黑白各提一子
        report, _ = self.receive(packet(rec(2, 'm2', 'W', None, revision=2)))
        self.assertEqual(report['accepted'][0]['outcome'], 'upgrade')
        status, _ = self.view()
        self.assertEqual(status['prefix_ply'], 4)
        self.assertEqual(status['captures'], {'B': 1, 'W': 1})
        self.assertEqual(status['blocker'], {'ply': 5, 'reason': 'missing'})
        self.assertEqual([(p['ply'], p['applied']) for p in status['pending']],
                         [(2, True), (3, True), (4, True)])
        # 第 5 手黑停一手后封存到第 5 手并重开
        self.receive(packet(rec(5, 'm5', 'B', None)))
        status, _ = self.view(target=5)
        cred5 = status['credential']['value']
        out, _ = self.seal(5, cred5)
        receipt5 = out['receipt']
        self.assertEqual((receipt5['previous_sealed_ply'], receipt5['sealed_ply']), (1, 5))
        self.assertIsNone(out['final'])
        # 重开后第 6 手白停一手结束该盘；封存前不得给最终归档结果
        self.receive(packet(rec(6, 'm6', 'W', None)))
        status, _ = self.view()
        self.assertEqual(status['prefix_ply'], 6)
        self.assertTrue(status['double_pass'])
        self.assertIsNone(status['blocker'])
        self.assertIsNone(status['final'])
        status, _ = self.view(target=6)
        cred6 = status['credential']['value']
        out, _ = self.seal(6, cred6)
        final = out['final']
        self.assertIsNotNone(final)
        # 最终归档与同一有效六手的原回放面积、提子一致
        r6 = process(full_record())
        self.assertTrue(r6['summary']['valid_record'])
        self.assertEqual(final['captures'], r6['captures'])
        self.assertEqual(final['area'], r6['area'])
        # 封存段明细不留在活动存储：无逐手内容/盘面，只留身份、版本、指纹
        raw = json.loads(Path(self.state, 'state.json').read_text(encoding='utf-8'))
        self.assertEqual(raw['pending'], {})
        for entry in raw['sealed'].values():
            self.assertEqual(sorted(entry), ['fingerprint', 'id', 'revision'])
        self.assertNotIn('moves', raw)
        # 最终归档后：成功的早期封存请求原样重试返回原回执，不得回滚
        out, _ = self.seal(1, cred1)
        self.assertEqual(out['receipt'], receipt1)
        out, _ = self.seal(5, cred5)
        self.assertEqual(out['receipt'], receipt5)
        # 复看不改变最终归档结果
        status, _ = self.view()
        self.assertEqual(status['final'], final)

    # ---- 凭据绑定范围 ----

    def test_credential_ignores_out_of_range_packets(self):
        self.init6()
        self.receive(packet(rec(1, 'm1', 'B', [1, 2]), rec(3, 'm3', 'B', [4, 4]),
                            rec(4, 'm4', 'W', [1, 1])))
        status, _ = self.view(target=1)
        cred = status['credential']['value']
        # 只涉及第 3、4 手的包（含升版与等价重传）不影响范围内凭据
        self.receive(packet(rec(3, 'm3b', 'B', [4, 4], revision=2), rec(4, 'm4', 'W', [1, 1])))
        # 旧包（低版本）也不影响
        report, _ = self.receive(packet(rec(3, 'm3', 'B', [4, 4], revision=1)))
        self.assertEqual(report['skipped'][0]['reason'], 'stale')
        out, _ = self.seal(1, cred)
        self.assertEqual(out['receipt']['sealed_ply'], 1)
        status, _ = self.view()
        self.assertEqual(status['sealed_ply'], 1)

    def test_in_range_upgrade_invalidates_credential(self):
        self.init6()
        self.receive(packet(rec(1, 'm1', 'B', [1, 2])))
        status, _ = self.view(target=1)
        cred = status['credential']['value']
        # 第 1 手用完全相同的棋步升版：旧凭据不能推进封存点
        self.receive(packet(rec(1, 'm1', 'B', [1, 2], revision=2)))
        _, err = self.seal(1, cred, expect=2)
        self.assertIn('credential_mismatch', err)
        status, _ = self.view()
        self.assertEqual(status['sealed_ply'], 0)
        # 重新核对后新凭据可用
        status, _ = self.view(target=1)
        cred2 = status['credential']['value']
        self.assertNotEqual(cred, cred2)
        out, _ = self.seal(1, cred2)
        self.assertEqual(out['receipt']['sealed_ply'], 1)

    # ---- 跨封存线的重传/更正与整包原子性 ----

    def test_sealed_retransmit_and_atomic_reject(self):
        self.init6()
        self.receive(packet(rec(1, 'm1', 'B', [1, 2]), rec(2, 'm2', 'W', [1, 1])))
        status, _ = self.view(target=1)
        self.seal(1, status['credential']['value'])
        # 同一包：第 1 手正确重传 + 修复第 2 手的更正 → 接受后者
        report, _ = self.receive(packet(rec(1, 'm1', 'B', [1, 2]), rec(2, 'm2', 'W', None, revision=2)))
        self.assertEqual([a['ply'] for a in report['accepted']], [2])
        self.assertEqual(report['skipped'], [{'ply': 1, 'id': 'm1', 'revision': 1, 'reason': 'sealed'}])
        status, _ = self.view()
        self.assertEqual(status['prefix_ply'], 2)
        # 同一包：第 1 手同版本不同内容 + 另一笔更正 → 整包拒绝，第 2 手不得被改
        _, err = self.receive(packet(rec(1, 'mX', 'B', [2, 2]), rec(2, 'm2', 'W', [0, 4], revision=3)),
                              expect=2)
        self.assertIn('version_conflict', err)
        status, _ = self.view()
        self.assertEqual(status['prefix_ply'], 2)
        ply2 = [p for p in status['pending'] if p['ply'] == 2][0]
        self.assertEqual((ply2['revision'], ply2['point']), (2, None))

    # ---- 版本与 ID 规则 ----

    def test_lower_revision_does_not_preempt(self):
        self.init6()
        self.receive(packet(rec(3, 'm3', 'B', [4, 4], revision=5)))
        report, _ = self.receive(packet(rec(3, 'other', 'B', [3, 3], revision=2)))
        self.assertEqual(report['accepted'], [])
        self.assertEqual(report['skipped'][0]['reason'], 'stale')
        status, _ = self.view()
        ply3 = [p for p in status['pending'] if p['ply'] == 3][0]
        self.assertEqual((ply3['id'], ply3['revision'], ply3['point']), ('m3', 5, [4, 4]))

    def test_sealed_ply_upgrade_rejected(self):
        self.init6()
        self.receive(packet(rec(1, 'm1', 'B', [1, 2])))
        status, _ = self.view(target=1)
        self.seal(1, status['credential']['value'])
        # 封存手即使内容相同也不接受升版
        for point in ([1, 2], [3, 3]):
            _, err = self.receive(packet(rec(1, 'm1', 'B', point, revision=2)), expect=2)
            self.assertIn('sealed_ply_immutable', err)
        status, _ = self.view()
        self.assertEqual(status['sealed_ply'], 1)

    def test_sealed_id_cannot_be_reused(self):
        self.init6()
        self.receive(packet(rec(1, 'm1', 'B', [1, 2])))
        status, _ = self.view(target=1)
        self.seal(1, status['credential']['value'])
        _, err = self.receive(packet(rec(2, 'm1', 'W', None)), expect=2)
        self.assertIn('duplicate_move_id', err)
        status, _ = self.view()
        self.assertEqual(status['pending'], [])

    def test_id_swap_within_packet(self):
        self.init6()
        self.receive(packet(rec(3, 'a', 'B', [4, 4]), rec(4, 'b', 'W', [1, 1])))
        # 同包交换两个未封存手的 ID：按合并后的有效集合判定，不得误报重复
        report, _ = self.receive(packet(rec(3, 'b', 'B', [4, 4], revision=2),
                                        rec(4, 'a', 'W', [1, 1], revision=2)))
        self.assertEqual(len(report['accepted']), 2)
        status, _ = self.view()
        self.assertEqual({p['ply']: p['id'] for p in status['pending']}, {3: 'b', 4: 'a'})

    # ---- 结构、错盘与业务非法 ----

    def test_wrong_game_and_structure_rejections(self):
        self.init6()
        _, err = self.receive(packet(rec(1, 'm1', 'B', [1, 2]), game='other'), expect=2)
        self.assertIn('wrong_game', err)
        _, err = self.receive(packet(rec(1, 'm1', 'B', [1, 2]), rec(1, 'm2', 'W', None)), expect=2)
        self.assertIn('duplicate_ply_in_packet', err)
        _, err = self.receive({'game_id': 'ko-6', 'records': []}, expect=2)
        self.assertIn('invalid_structure', err)
        _, err = self.receive(packet(rec(7, 'm7', 'B', None)), expect=2)
        self.assertIn('invalid_structure', err)
        _, err = self.receive(packet(rec(1, 'm1', 'B', [1, 2], revision=0)), expect=2)
        self.assertIn('invalid_structure', err)
        _, err = self.receive(packet(rec(1, 'm1', 'B', [1, 2], revision=2147483648)), expect=2)
        self.assertIn('invalid_structure', err)
        _, err = self.receive(packet(rec(1, 'm1', 'B', [101, 0])), expect=2)
        self.assertIn('invalid_structure', err)
        _, err = self.view(target=7, expect=2)
        self.assertIn('target_out_of_range', err)
        _, err = self.receive(packet(rec(1, 'm1', 'B', [1, 2])),
                              state=os.path.join(self.dir, 'nope'), expect=2)
        self.assertIn('state_missing', err)

    def test_business_illegal_kept_for_repair(self):
        self.init6()
        # 坐标结构合法但落在盘外：业务非法，不是弃包理由
        report, _ = self.receive(packet(rec(1, 'm1', 'B', [5, 5])))
        self.assertEqual(len(report['accepted']), 1)
        status, _ = self.view()
        self.assertEqual(status['blocker'], {'ply': 1, 'reason': 'out_of_bounds', 'move_id': 'm1'})
        _, err = self.view(target=1, expect=2)
        self.assertIn('segment_incomplete', err)
        # 升版修复后进入合法前缀
        self.receive(packet(rec(1, 'm1', 'B', [1, 2], revision=2)))
        status, _ = self.view(target=1)
        self.assertEqual(status['prefix_ply'], 1)
        self.assertEqual(status['credential']['to'], 1)

    def test_init_validation_and_state_guards(self):
        _, err = self.init(dict(SPEC6, total_moves=1), expect=2)
        self.assertIn('invalid_structure', err)
        _, err = self.init(dict(SPEC6, total_moves=2001), expect=2)
        self.assertIn('invalid_structure', err)
        _, err = self.init(dict(SPEC6, moves=[]), expect=2)
        self.assertIn('invalid_structure', err)
        _, err = self.init(dict(SPEC6, game_id='bad id'), expect=2)
        self.assertIn('invalid_structure', err)

    # ---- 双停与声明末手的一致性 ----

    def test_double_pass_mismatch_cannot_lock(self):
        # 双停发生在第 3 手，但声明总手数是 4：不得提前封死
        spec = dict(SPEC6, game_id='mm-4', total_moves=4)
        self.init(spec)
        self.receive(packet(rec(1, 'p1', 'B', [0, 0]), rec(2, 'p2', 'W', None),
                            rec(3, 'p3', 'B', None), rec(4, 'p4', 'W', [4, 4]), game='mm-4'))
        status, _ = self.view()
        self.assertEqual(status['prefix_ply'], 3)
        self.assertTrue(status['double_pass'])
        self.assertEqual(status['blocker'], {'ply': 4, 'reason': 'game_ended', 'move_id': 'p4'})
        _, err = self.view(target=3, expect=2)
        self.assertIn('double_pass_mismatch', err)
        _, err = self.view(target=4, expect=2)
        self.assertIn('segment_incomplete', err)
        # 目标是末手但未双停：同样不可封
        state2 = os.path.join(self.dir, 'game2')
        spec2 = {'game_id': 'mm-2', 'total_moves': 2, 'size': 5,
                 'setup': {'black': [], 'white': []}, 'next_player': 'B', 'komi': '0.5'}
        self.init(spec2, state=state2)
        self.receive(packet(rec(1, 'p1', 'B', [0, 0]), rec(2, 'p2', 'W', [1, 1]), game='mm-2'),
                     state=state2)
        _, err = self.view(target=2, state=state2, expect=2)
        self.assertIn('double_pass_mismatch', err)

    def test_two_ply_game_final_matches_process(self):
        spec = {'game_id': 'tiny', 'total_moves': 2, 'size': 5,
                'setup': {'black': [], 'white': []}, 'next_player': 'B', 'komi': '0.5'}
        self.init(spec)
        self.receive(packet(rec(1, 'p1', 'B', None), rec(2, 'p2', 'W', None), game='tiny'))
        status, _ = self.view(target=2)
        out, _ = self.seal(2, status['credential']['value'])
        final = out['final']
        self.assertIsNotNone(final)
        r = process({'size': 5, 'setup': {'black': [], 'white': []}, 'next_player': 'B',
                     'komi': '0.5', 'moves': [{'id': 'p1', 'player': 'B', 'point': None},
                                              {'id': 'p2', 'player': 'W', 'point': None}]})
        self.assertEqual(final['captures'], r['captures'])
        self.assertEqual(final['area'], r['area'])
        self.assertEqual(final['area']['status'], 'after_two_passes')

    # ---- 受控中断：提交边界与重开后的重传 ----

    def test_commit_boundary_faults(self):
        self.init6()
        self.receive(packet(rec(1, 'm1', 'B', [1, 2])))
        status, _ = self.view(target=1)
        cred = status['credential']['value']

        def crash_before(point):
            if point == 'commit_before_replace':
                raise RuntimeError('crash before commit')

        archive._FAULT_HOOK = crash_before
        try:
            with self.assertRaises(RuntimeError):
                archive.seal_locked(self.state, 1, cred)
        finally:
            archive._FAULT_HOOK = None
        # 中断在提交前：只能读到完整的封存前状态
        state = archive.load_state(self.state)
        self.assertEqual(state['sealed_ply'], 0)
        self.assertEqual(state['receipts'], {})
        # 重开后的重传与重试照常
        report, _ = self.receive(packet(rec(1, 'm1', 'B', [1, 2])))
        self.assertEqual(report['skipped'][0]['reason'], 'retransmit')
        out, _ = self.seal(1, cred)
        self.assertEqual(out['receipt']['sealed_ply'], 1)

        # 收包提交前崩溃：已校验条目不得留下
        archive._FAULT_HOOK = crash_before
        try:
            with self.assertRaises(RuntimeError):
                archive.receive_locked(self.state, packet(rec(2, 'm2', 'W', None)))
        finally:
            archive._FAULT_HOOK = None
        status, _ = self.view()
        self.assertEqual(status['pending'], [])
        self.receive(packet(rec(2, 'm2', 'W', None)))
        status, _ = self.view()
        self.assertEqual(status['prefix_ply'], 2)

        # 中断在原子替换之后：只能读到完整的封存后状态，回执已登记
        state2 = os.path.join(self.dir, 'game2')
        self.init6(state=state2)
        self.receive(packet(rec(1, 'm1', 'B', [1, 2])), state=state2)
        status, _ = self.view(target=1, state=state2)
        cred2 = status['credential']['value']

        def crash_after(point):
            if point == 'commit_after_replace':
                raise RuntimeError('crash after commit')

        archive._FAULT_HOOK = crash_after
        try:
            with self.assertRaises(RuntimeError):
                archive.seal_locked(state2, 1, cred2)
        finally:
            archive._FAULT_HOOK = None
        state = archive.load_state(state2)
        self.assertEqual(state['sealed_ply'], 1)
        receipt = state['receipts'][f'1:{cred2}']
        out, _ = self.seal(1, cred2, state=state2)
        self.assertEqual(out['receipt'], receipt)

    def test_subprocess_restart_and_fault_env(self):
        state = os.path.join(self.dir, 'proc-game')
        spec = self.path('spec.json', SPEC6)
        pkt1 = self.path('p1.json', packet(rec(1, 'm1', 'B', [1, 2])))
        package_root = str(Path(go_record.__file__).resolve().parents[1])

        def runp(argv, env_extra=None, expect=0):
            env = dict(os.environ)
            env.pop(archive.FAULT_ENV, None)
            env['PYTHONPATH'] = package_root
            if env_extra:
                env.update(env_extra)
            result = subprocess.run([sys.executable, '-m', 'go_record.archive_cli'] + argv,
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, expect, result.stderr)
            return result

        runp(['init', '--state', state, '--spec', spec])
        runp(['receive', '--state', state, '--packet', pkt1])
        out = runp(['view', '--state', state, '--target', '1'])
        cred = json.loads(out.stdout)['credential']['value']
        # 受控中断：提交替换完成后、返回前崩溃（非正常进程关闭）
        runp(['seal', '--state', state, '--target', '1', '--credential', cred],
             env_extra={archive.FAULT_ENV: 'commit_after_replace'}, expect=1)
        # 重开：封存点与回执已完整提交
        out = runp(['view', '--state', state])
        status = json.loads(out.stdout)
        self.assertEqual(status['sealed_ply'], 1)
        self.assertEqual(len(status['receipts']), 1)
        # 原样重试 → 原回执
        out = runp(['seal', '--state', state, '--target', '1', '--credential', cred])
        self.assertEqual(json.loads(out.stdout)['receipt'], status['receipts'][0])
        # 重开后的重传：识别为已封存重传
        out = runp(['receive', '--state', state, '--packet', pkt1])
        report = json.loads(out.stdout)
        self.assertEqual(report['accepted'], [])
        self.assertEqual(report['skipped'][0]['reason'], 'sealed')


if __name__ == '__main__':
    unittest.main()
