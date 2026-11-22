"""go-record-guide：把编辑变化树编制成可独立播放的共享离线导航图。

成功原子新建导览包（不覆盖已有目标或链接），stdout 打印简要 JSON；
输入错误与写入失败退出 2，诊断 JSON 打到 stderr，不落正式输出。
棋规禁用不是整包失败，只在包内体现为禁用按钮与 blocked 书签。
"""
import argparse
import json
import os
import sys

from . import guide
from .io import load, publish_stream


def _emit_error(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='go-record-guide',
        description='离线导览包：把作者编辑树编译成共享 diagrams/views/bookmarks 导航图。')
    parser.add_argument('--source', required=True, help='编辑树清单 MANIFEST.json')
    parser.add_argument('--output', required=True, help='导览包输出路径（原子新建，不覆盖已有目标）')
    args = parser.parse_args(argv)
    try:
        manifest = load(args.source)
        manifest_dir = os.path.dirname(os.path.abspath(args.source))
        package = guide.build_package(manifest, manifest_dir)
        publish_stream(args.output, guide.as_payload(package))
    except guide.Reject as error:
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
    playable = sum(1 for item in package['bookmarks'] if item['status'] == 'playable')
    blocked = len(package['bookmarks']) - playable
    summary = {'diagrams': len(package['diagrams']), 'views': len(package['views']),
               'bookmarks': len(package['bookmarks']),
               'playable_bookmarks': playable, 'blocked_bookmarks': blocked}
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
