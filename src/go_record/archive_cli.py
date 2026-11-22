"""go-record-archive：离线分包接收、核对与封存的本地命令行。每个状态目录一盘。"""
import argparse
import json
import sys

from . import archive
from .io import load


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='go-record-archive',
        description='离线分包接收与归档：每个状态目录管理一盘棋；棋规与面积模型同 go-record。')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('init', help='初始化一盘（已有状态拒绝覆盖）')
    p.add_argument('--state', required=True, help='状态目录')
    p.add_argument('--spec', required=True,
                   help='初始化 JSON：game_id/total_moves/size/setup/next_player/komi（不带 moves）')

    p = sub.add_parser('receive', help='接收一个分包（整包全收或全拒）')
    p.add_argument('--state', required=True, help='状态目录')
    p.add_argument('--packet', required=True, help='分包 JSON：game_id + records[]')

    p = sub.add_parser('view', help='查看状态；--target N 请求核对至第 N 手并签发封存凭据')
    p.add_argument('--state', required=True, help='状态目录')
    p.add_argument('--target', type=int, default=None, help='希望核对至第几手（可选）')

    p = sub.add_parser('seal', help='凭目标手号与凭据封存')
    p.add_argument('--state', required=True, help='状态目录')
    p.add_argument('--target', type=int, required=True, help='封存至第几手')
    p.add_argument('--credential', required=True, help='view --target 签发的凭据')

    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            state = archive.create_state(args.state, load(args.spec))
            _emit({'initialized': True, 'game_id': state['game_id'], 'total_moves': state['total_moves']})
        elif args.command == 'receive':
            _emit(archive.receive_locked(args.state, load(args.packet)))
        elif args.command == 'view':
            _emit(archive.view(archive.load_state(args.state), target=args.target))
        elif args.command == 'seal':
            receipt, state = archive.seal_locked(args.state, args.target, args.credential)
            _emit({'receipt': receipt, 'final': state['final']})
    except archive.Reject as error:
        print(json.dumps(error.diagnostic(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        diagnostic = {'error': {'code': 'invalid_structure', 'message': str(error)}}
        print(json.dumps(diagnostic, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
