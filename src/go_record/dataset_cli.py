"""go-record-dataset：离线编制“下一手”训练集/盲评集分区计划的本地命令行。

成功原子新建报告（不覆盖已有目标），stdout 打印简要 JSON；输入错误与写入失败
退出 2，诊断 JSON 打到 stderr，不落报告。不向外部系统发布任何数据。
"""
import argparse
import json
import os
import sys

from . import dataset
from .io import load, publish


def _emit_error(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='go-record-dataset',
        description='离线编制下一手训练集与盲评集：按来源系列与规范局面去重、'
                    '遵守历史发布约束并精确满足盲评配额，输出供人工复核的分区计划。')
    parser.add_argument('--source', required=True, help='编制清单 MANIFEST.json')
    parser.add_argument('--output', required=True, help='报告输出路径（原子新建，不覆盖已有目标）')
    args = parser.parse_args(argv)
    try:
        manifest = load(args.source)
        manifest_dir = os.path.dirname(os.path.abspath(args.source))
        report = dataset.build_report(dataset.validate_manifest(manifest), manifest_dir)
        publish(args.output, report)
    except dataset.Reject as error:
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
    summary = {'status': report['status'], 'batch_id': report['batch_id'],
               'targets': report['targets']}
    if report['status'] == 'ready':
        summary['train'] = report['splits']['train']['counts']
        summary['holdout'] = report['splits']['holdout']['counts']
    else:
        summary['diagnostic'] = report['diagnostic']
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
