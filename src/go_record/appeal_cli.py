"""go-record-appeal：离线终局死子申诉案卷的本地命令行。每个案卷目录管理一盘棋。

子命令：open（开案取证）/ vote（录入或替换意见）/ close（结案固化裁定）/
reopen（重开下一轮）/ view（看当前状态）/ history（查历史裁定）/
recompute（仅凭历史裁定独立复算）。

成功结果与业务状态以 JSON 打印到 stdout；错误退出 2，诊断 JSON 打到 stderr，
不留下正式的部分输出，也不覆盖已有案卷。
"""
import argparse
import json
import sys

from . import appeal
from .io import load


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


def _load_groups(text):
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise appeal.Reject('invalid_structure', f'--groups must be a JSON array: {error}')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='go-record-appeal',
        description='离线终局死子申诉案卷：开案、投票、结案、重开、看当前与查历史；'
                    '不自行判断死活，不改变落子、劫、贴目或面积规则。')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('open', help='从一份已最终封存的归档开案（不覆盖已有案卷）')
    p.add_argument('--case', required=True, dest='case_dir', help='新案卷目录')
    p.add_argument('--spec', required=True,
                   help='开案 JSON：case_ref/game_id/archive_dir/referees（两名不同裁判）')

    p = sub.add_parser('vote', help='提交或替换本轮意见（一组待移除棋块的代表坐标）')
    p.add_argument('--case', required=True, dest='case_dir')
    p.add_argument('--referee', required=True, help='归属意见的裁判 ID')
    p.add_argument('--expected-version', required=True, type=int, help='预期案卷版本')
    p.add_argument('--groups', required=True,
                   help='JSON 数组，每项为块内任一棋子坐标 [row,col]；空数组是空意见')

    p = sub.add_parser('close', help='两人意见棋块集合完全相同时结案，固化不可改写裁定')
    p.add_argument('--case', required=True, dest='case_dir')
    p.add_argument('--expected-version', required=True, type=int)

    p = sub.add_parser('reopen', help='只准重开已结案案卷，建立下一轮，两人均须重投')
    p.add_argument('--case', required=True, dest='case_dir')
    p.add_argument('--expected-version', required=True, type=int)

    p = sub.add_parser('view', help='查看当前状态（缺票/分歧/一致待结案/已结案）')
    p.add_argument('--case', required=True, dest='case_dir')

    p = sub.add_parser('history', help='查历史裁定；不带 --round 列目录，带轮次交付完整证据')
    p.add_argument('--case', required=True, dest='case_dir')
    p.add_argument('--round', dest='round_number', type=int, default=None)

    p = sub.add_parser('recompute', help='仅凭指定轮次的历史裁定独立复算前后盘面与胜负')
    p.add_argument('--case', required=True, dest='case_dir')
    p.add_argument('--round', dest='round_number', type=int, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == 'open':
            state = appeal.open_case(args.case_dir, load(args.spec))
            _emit({'opened': True, 'case_ref': state['case_ref'], 'game_id': state['game_id'],
                   'round': state['round'], 'version': state['version'],
                   'final_receipt_id': state['final_receipt_id'],
                   'original_winner': state['original_area']['winner']})
        elif args.command == 'vote':
            report = appeal.vote_locked(args.case_dir, args.referee,
                                        args.expected_version, _load_groups(args.groups))
            _emit({'voted': True, **report})
        elif args.command == 'close':
            record = appeal.close_locked(args.case_dir, args.expected_version)
            _emit({'closed': True, 'round': record['round'],
                   'ruling_id': record['ruling_id'],
                   'final_receipt_id': record['final_receipt_id'],
                   'winner_before': record['winner_before'],
                   'winner_after': record['winner_after'],
                   'area_before': record['area_before'],
                   'area_after': record['area_after']})
        elif args.command == 'reopen':
            report = appeal.reopen_locked(args.case_dir, args.expected_version)
            _emit({'reopened': True, **report})
        elif args.command == 'view':
            _emit(appeal.view_state(appeal.load_case(args.case_dir)))
        elif args.command == 'history':
            _emit(appeal.history(appeal.load_case(args.case_dir), args.round_number))
        elif args.command == 'recompute':
            state = appeal.load_case(args.case_dir)
            record = appeal.history(state, args.round_number)
            _emit({'round': record['round'], 'ruling_id': record['ruling_id'],
                   **appeal.recompute_ruling(record)})
    except appeal.Reject as error:
        print(json.dumps(error.diagnostic(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        diagnostic = {'error': {'code': 'invalid_structure', 'message': str(error)}}
        print(json.dumps(diagnostic, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
