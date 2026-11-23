"""go-record-capture-study：离线逐子溯源被提棋子来源的本地命令行。

成功原子新建报告（不覆盖已有目标或链接），stdout 打印简要 JSON；输入错误与
写入失败退出 2，诊断 JSON 打到 stderr，不落正式报告。只读输入棋谱，不保留
任何运行间状态。
"""
import argparse
import json
import os
import sys

from . import capture
from .io import load, publish


def _emit_error(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='go-record-capture-study',
        description='离线批量溯源被提棋子来源：一次读入多份棋谱与一批任意区间查询，'
                    '按来源类别和寿命汇总，并支持少量逐子反查。')
    parser.add_argument('--source', required=True, help='分析清单 MANIFEST.json')
    parser.add_argument('--output', required=True, help='报告输出路径（原子新建，不覆盖已有目标）')
    args = parser.parse_args(argv)
    try:
        manifest = capture.validate_manifest(load(args.source))
        manifest_dir = os.path.dirname(os.path.abspath(args.source))
        report = capture.build_report(manifest, manifest_dir)
        publish(args.output, report)
    except capture.Reject as error:
        _emit_error(error.diagnostic())
        return 2
    except FileExistsError as error:
        _emit_error({'error': {'code': 'output_exists', 'message': str(error)}})
        return 2
    except OSError as error:
        _emit_error({'error': {'code': 'io_error', 'message': str(error)}})
        return 2
    except (ValueError, UnicodeError, RecursionError) as error:
        _emit_error({'error': {'code': 'invalid_structure', 'message': str(error)}})
        return 2
    summary = {
        'schema': report['schema'],
        'games': len(report['games']),
        'queries': len(report['queries']),
        'lookups': len(report['lookups']),
        'blocked_games': [game['game_id'] for game in report['games']
                          if game['first_illegal'] is not None],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
