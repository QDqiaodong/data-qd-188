#!/usr/bin/env python3
"""可运行小样例：3 路盘死子申诉两轮裁定，并接入一轮一台的晋级核定。

场景（贴目 "0"）：黑 [0,0]、白 [2,2]，黑白各停一手后最终封存，原面积和棋。
  1. 两人各标一方 → 分歧，结不了案；
  2. 两人改标白块、尚未结案 → 没有正式胜负；
  3. 第一轮结案 → 移除白块，黑面积 9、白面积 0，黑胜；
  4. 重开 → 第一轮裁定只作历史，当前 awaiting_ruling；
  5. 第二轮两人标黑块结案 → 白胜；
  6. 归档移入冷库后，仅凭案卷两份裁定独立复算各自胜负；
  7. 同一盘放进一轮一台、只晋级一人的清单：不挂案卷按和棋抽签；挂案卷后
     在第一轮结案、重开、第二轮结案三个时点分别观察到黑方晋级、
     awaiting_ruling、白方晋级，见证引用对应裁定轮次。

只用标准库；在仓库根目录运行：PYTHONPATH=src python3 examples/appeal_demo.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from go_record import appeal, archive, standings

MOVES = [('m1', 'B', [0, 0]), ('m2', 'W', [2, 2]), ('m3', 'B', None), ('m4', 'W', None)]
GAME = 'demo-3x3'
REFEREES = ['referee-alpha', 'referee-beta']


def seal_archive(games_dir):
    state_dir = os.path.join(games_dir, GAME)
    spec = {'game_id': GAME, 'total_moves': 4, 'size': 3,
            'setup': {'black': [], 'white': []}, 'next_player': 'B', 'komi': '0'}
    archive.create_state(state_dir, spec)
    archive.receive_locked(state_dir, {'game_id': GAME, 'records': [
        {'ply': i + 1, 'id': mv[0], 'player': mv[1], 'point': mv[2], 'revision': 1}
        for i, mv in enumerate(MOVES)]})
    state = archive.load_state(state_dir)
    credential = archive.credential_for(state, 4)
    receipt, new_state = archive.seal_locked(state_dir, 4, credential)
    return state_dir, receipt, new_state


def open_fresh(cases_dir, archive_dir, suffix):
    case_dir = os.path.join(cases_dir, f'{GAME}-{suffix}')
    appeal.open_case(case_dir, {'case_ref': f'case-{suffix}', 'game_id': GAME,
                                'archive_dir': archive_dir, 'referees': REFEREES})
    return case_dir


def manifest(root, archive_rel, case_dir=None):
    entry = {'game_id': GAME, 'state_dir': archive_rel}
    if case_dir is not None:
        entry['appeal_dir'] = os.path.relpath(case_dir, root)
    return {
        'players': ['player-an', 'player-bai'], 'qualify_count': 1,
        'lot_order': ['player-an', 'player-bai'],
        'tables': [{'table_id': 'T1', 'round': 1, 'black': 'player-an',
                    'white': 'player-bai', 'candidates': [GAME]}],
        'archives': [entry]}


def evaluate(root, data):
    path = os.path.join(root, f'manifest-{os.getpid()}-{len(os.listdir(root))}.json')
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return standings.evaluate(json.loads(Path(path).read_text()), root)


def main():
    root = tempfile.mkdtemp(prefix='go-appeal-demo-')
    try:
        games_dir = os.path.join(root, 'games')
        cases_dir = os.path.join(root, 'cases')
        archive_dir, receipt, arc = seal_archive(games_dir)
        archive_rel = os.path.relpath(archive_dir, root)
        print('== 开案固定证据 ==')
        print('末手回执:', receipt['receipt_id'][:16], '... 原面积胜负:',
              arc['final']['area']['winner'])

        case_dir = open_fresh(cases_dir, archive_dir, 'main')
        print('\n== 1. 各标一方：分歧，结不了案 ==')
        appeal.vote_locked(case_dir, REFEREES[0], 1, [[0, 0]])
        appeal.vote_locked(case_dir, REFEREES[1], 2, [[2, 2]])
        try:
            appeal.close_locked(case_dir, 3)
        except appeal.Reject as error:
            print('结案拒绝:', error.code, '| phase =',
                  appeal.view_state(appeal.load_case(case_dir))['phase'])

        print('\n== 2. 改标白块、一致但未结案：无正式胜负 ==')
        appeal.vote_locked(case_dir, REFEREES[0], 3, [[2, 2]])
        view = appeal.view_state(appeal.load_case(case_dir))
        print('phase =', view['phase'], '| current_winner =', view['current_winner'])

        print('\n== 3. 第一轮结案：黑胜（黑面积 9、白面积 0）==')
        first = appeal.close_locked(case_dir, 4)
        print('winner:', first['winner_before'], '->', first['winner_after'],
              '| area:', first['area_after']['black_area'],
              first['area_after']['white_area_with_komi'])
        report = evaluate(root, manifest(root, archive_rel, case_dir))
        print('晋级核定:', report['status'], 'qualified =', report['qualified'],
              '| 见证 =', report['witnesses'][0]['assignment'][0])

        print('\n== 4. 重开：历史仍可查，当前 awaiting_ruling ==')
        appeal.reopen_locked(case_dir, 5)
        report = evaluate(root, manifest(root, archive_rel, case_dir))
        print('晋级核定:', report['status'], report['awaiting_ruling'])
        round1 = appeal.history(appeal.load_case(case_dir), 1)
        print('第一轮历史仍在:', round1['winner_after'], '/ ruling',
              round1['ruling_id'][:12], '...')

        print('\n== 5. 第二轮标黑块结案：白胜 ==')
        appeal.vote_locked(case_dir, REFEREES[0], 6, [[0, 0]])
        appeal.vote_locked(case_dir, REFEREES[1], 7, [[0, 0]])
        second = appeal.close_locked(case_dir, 8)
        print('winner:', second['winner_before'], '->', second['winner_after'])
        report = evaluate(root, manifest(root, archive_rel, case_dir))
        print('晋级核定:', report['status'], 'qualified =', report['qualified'],
              '| 见证 =', report['witnesses'][0]['assignment'][0])

        print('\n== 6. 归档移入冷库：仅凭案卷复算两轮 ==')
        cold = os.path.join(root, 'cold-storage')
        os.makedirs(cold, exist_ok=True)
        shutil.move(archive_dir, os.path.join(cold, GAME))
        for round_number in (1, 2):
            record = appeal.history(appeal.load_case(case_dir), round_number)
            result = appeal.recompute_ruling(record)
            print(f'第 {round_number} 轮独立复算:', result['winner_after'],
                  result['area_after']['black_area'],
                  result['area_after']['white_area_with_komi'])
        shutil.move(os.path.join(cold, GAME), archive_dir)

        print('\n== 7. 三种接入时点与无案卷基线 ==')
        plain = evaluate(root, manifest(root, archive_rel))
        print('无案卷（和棋抽签）:', plain['status'], 'qualified =', plain['qualified'],
              '| 见证 =', plain['witnesses'][0]['assignment'][0])
        r1_case = open_fresh(cases_dir, archive_dir, 'r1')
        appeal.vote_locked(r1_case, REFEREES[0], 1, [[2, 2]])
        appeal.vote_locked(r1_case, REFEREES[1], 2, [[2, 2]])
        appeal.close_locked(r1_case, 3)
        report = evaluate(root, manifest(root, archive_rel, r1_case))
        print('第一轮结案:', report['status'], 'qualified =', report['qualified'],
              '| 见证 =', report['witnesses'][0]['assignment'][0])
        open_case = open_fresh(cases_dir, archive_dir, 'pending')
        appeal.vote_locked(open_case, REFEREES[0], 1, [[2, 2]])
        appeal.vote_locked(open_case, REFEREES[1], 2, [[2, 2]])  # 一致未结案
        report = evaluate(root, manifest(root, archive_rel, open_case))
        print('一致未结案:', report['status'], report['awaiting_ruling'])
        report = evaluate(root, manifest(root, archive_rel, case_dir))
        print('第二轮结案:', report['status'], 'qualified =', report['qualified'],
              '| 见证 =', report['witnesses'][0]['assignment'][0])

        print('\n== CLI 独立复算（子进程）==')
        env = dict(os.environ,
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        cli = subprocess.run([sys.executable, '-m', 'go_record.appeal_cli', 'recompute',
                              '--case', case_dir, '--round', '1'],
                             check=True, env=env, capture_output=True, text=True)
        print('recompute 第 1 轮:', json.loads(cli.stdout)['winner_after'])
        print('\n样例目录（可手工复查）:', root)
    finally:
        if os.environ.get('APPEAL_DEMO_CLEAN'):
            shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    main()
