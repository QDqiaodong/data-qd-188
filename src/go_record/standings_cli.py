"""go-record-standings：离线赛会对账与晋级核定的本地命令行。

成功原子新建报告（不覆盖已有目标），stdout 打印简要 JSON；
输入错误与写入失败退出 2，诊断 JSON 打到 stderr，不落报告。
"""
import argparse
import json
import os
import sys

from . import standings
from .io import load, publish


def _emit_error(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='go-record-standings',
        description='离线赛会对账与晋级核定：按交接清单核对归档、枚举可行对应关系并核定晋级。')
    parser.add_argument('--source', required=True, help='交接清单 MANIFEST.json')
    parser.add_argument('--output', required=True, help='报告输出路径（原子新建，不覆盖已有目标）')
    args = parser.parse_args(argv)
    try:
        manifest = load(args.source)
        manifest_dir = os.path.dirname(os.path.abspath(args.source))
        report = standings.evaluate(manifest, manifest_dir)
        publish(args.output, report)
    except standings.Reject as error:
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
    summary = {'status': report['status']}
    if report['status'] == 'complete':
        summary['qualified'] = report['qualified']
        summary['witnesses'] = len(report['witnesses'])
    else:
        summary['awaiting_game_ids'] = report['awaiting_game_ids']
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
