"""Strict JSON input and atomic create-only output publication."""
import json
import os
from pathlib import Path
import tempfile


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f'non-finite JSON constant: {value}')


def load(path):
    with open(path, 'rb') as source:
        data = source.read(8_000_001)
    if len(data) > 8_000_000:
        raise ValueError('input exceeds 8 MB')
    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=reject_constant)


def _scalar(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=False, allow_nan=False)


def _indented(value, level=0):
    """Yield indented JSON text chunks compatible with json.dumps(indent=2).

    流式产出，避免大型包在内存里同时持有对象与完整字符串。键顺序按容器给定顺序；
    本项目所有输出容器均已按确定顺序构造。
    """
    pad = '  ' * level
    child_pad = '  ' * (level + 1)
    if isinstance(value, dict):
        if not value:
            yield '{}'
            return
        yield '{\n'
        items = list(value.items())
        for index, (key, item) in enumerate(items):
            yield child_pad + _scalar(key) + ': '
            yield from _indented(item, level + 1)
            if index < len(items) - 1:
                yield ','
            yield '\n'
        yield pad + '}'
    elif isinstance(value, list):
        if not value:
            yield '[]'
            return
        yield '[\n'
        for index, item in enumerate(value):
            yield child_pad
            yield from _indented(item, level + 1)
            if index < len(value) - 1:
                yield ','
            yield '\n'
        yield pad + ']'
    else:
        yield _scalar(value)


def publish(path, result):
    payload = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.report-', delete=False) as f:
            temporary = Path(f.name)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish_stream(path, result):
    """与 publish 相同的原子新建语义，但分块写入以控制大输出的内存占用。

    缩进风格与 json.dumps(indent=2) 一致；已有目标文件或链接一律不覆盖。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.report-', delete=False) as f:
            temporary = Path(f.name)
            for chunk in _indented(result):
                f.write(chunk.encode('utf-8'))
            f.write(b'\n')
            f.flush()
            os.fsync(f.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
