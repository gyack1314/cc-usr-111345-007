"""净变更补丁的线上格式、单元摘要与断点绑定。

补丁是按 row_id 稳定排序的 JSONL 流：
  第 1 行 header，中间每行一个不可拆分单元，最后 1 行 footer。
before/after 同在一个单元内，任何重放器都不会拿到半条更新。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile

from .io import DataError
from .snapshot import canonical_digest, canonical_json

HEADER_LINE = 0
FOOTER_LINE = -1

OPS = ('insert', 'delete', 'update')


class PatchFormatError(DataError):
    """补丁结构、断点或绑定信息不合法。"""


def _digest_parts(parts):
    h = hashlib.sha256()
    for part in parts:
        h.update(canonical_json(part).encode('utf-8'))
        h.update(b'\n')
    return h.hexdigest()


def unit_digest(unit):
    """单个 before/after 单元的摘要，断点与 footer 共用同一算法。"""
    return _digest_parts([unit['op'], unit['row_id'], unit['before'], unit['after']])


def make_header(*, old_snapshot, new_snapshot, old_snapshot_digest, new_snapshot_digest,
                old_projection, new_projection, common_fields):
    return {
        'kind': 'header',
        'format': 1,
        'old_snapshot': old_snapshot,
        'new_snapshot': new_snapshot,
        'old_snapshot_digest': old_snapshot_digest,
        'new_snapshot_digest': new_snapshot_digest,
        'old_projection': old_projection,
        'new_projection': new_projection,
        'common_fields': list(common_fields),
    }


def make_unit(op, row_id, before, after):
    if op not in OPS:
        raise PatchFormatError(f'未知变更类型 {op!r}')
    if op == 'insert' and (before is not None or after is None):
        raise PatchFormatError('insert 单元必须且只能携带 after 像')
    if op == 'delete' and (before is None or after is not None):
        raise PatchFormatError('delete 单元必须且只能携带 before 像')
    if op == 'update' and (before is None or after is None):
        raise PatchFormatError('update 单元必须成对携带 before/after 像')
    unit = {'op': op, 'row_id': row_id, 'before': before, 'after': after}
    unit['unit_digest'] = unit_digest(unit)
    return unit


def make_footer(*, count_by_op, unit_count, stream_digest):
    return {
        'kind': 'footer',
        'unit_count': unit_count,
        'counts': {op: count_by_op.get(op, 0) for op in OPS},
        'stream_digest': stream_digest,
    }


def encode_line(obj) -> bytes:
    return (canonical_json(obj) + '\n').encode('utf-8')


def decode_line(raw, line_number):
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise PatchFormatError(f'补丁第{line_number}行不是有效JSON') from exc
    if not isinstance(obj, dict):
        raise PatchFormatError(f'补丁第{line_number}行结构无效')
    return obj


class StreamHasher:
    """随单元推进的流式摘要，footer 记录最终值，断点记录截至值。"""

    def __init__(self):
        self._h = hashlib.sha256()

    def add(self, unit):
        self._h.update(canonical_json(unit['unit_digest']).encode('utf-8'))
        self._h.update(b'\n')

    def value(self):
        return self._h.hexdigest()


class AtomicPatchWriter:
    """先写临时文件再原子发布；错误或资料损坏时不会留下看似完整的补丁。"""

    def __init__(self, path):
        self.path = path
        self._dir = os.path.dirname(os.path.abspath(path)) or '.'
        self._tmp = tempfile.NamedTemporaryFile(
            mode='wb', dir=self._dir, prefix='.patch-', suffix='.tmp', delete=False)
        self.closed = False

    def write(self, raw: bytes):
        self._tmp.write(raw)

    def commit(self):
        self._tmp.flush()
        os.fsync(self._tmp.fileno())
        self._tmp.close()
        os.replace(self._tmp.name, self.path)
        self.closed = True

    def abort(self):
        if self.closed:
            return
        self._tmp.close()
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass
        self.closed = True


def read_patch(path):
    """逐行读取已发布补丁，校验外壳结构并产出 (header, units, footer)。"""
    with open(path, 'rb') as fh:
        raw_lines = fh.read().splitlines()
    if len(raw_lines) < 2:
        raise PatchFormatError('补丁至少包含 header 与 footer')
    header = decode_line(raw_lines[0], 1)
    if header.get('kind') != 'header' or header.get('format') != 1:
        raise PatchFormatError('补丁 header 无效')
    footer = decode_line(raw_lines[-1], len(raw_lines))
    if footer.get('kind') != 'footer':
        raise PatchFormatError('补丁 footer 无效')
    units = []
    prev_id = None
    stream = StreamHasher()
    for index, raw in enumerate(raw_lines[1:-1], start=2):
        unit = decode_line(raw, index)
        if unit.get('kind') == 'footer':
            raise PatchFormatError(f'footer 必须位于补丁末尾（第{index}行）')
        if unit.get('op') not in OPS or not isinstance(unit.get('row_id'), str) or not unit['row_id']:
            raise PatchFormatError(f'第{index}行单元结构无效')
        if prev_id is not None and unit['row_id'] <= prev_id:
            raise PatchFormatError(f'第{index}行未按 row_id 稳定排序')
        prev_id = unit['row_id']
        stated = unit.pop('unit_digest', None)
        if stated != unit_digest(unit):
            raise PatchFormatError(f'第{index}行单元摘要不一致，补丁已损坏')
        unit['unit_digest'] = stated
        stream.add(unit)
        units.append(unit)
    if stream.value() != footer.get('stream_digest'):
        raise PatchFormatError('补丁流摘要与 footer 不一致，补丁已损坏')
    if len(units) != footer.get('unit_count'):
        raise PatchFormatError('补丁单元数与 footer 不一致')
    counts = footer.get('counts')
    if not isinstance(counts, dict) or any(counts.get(op, 0) < 0 for op in OPS):
        raise PatchFormatError('补丁 footer 计数无效')
    actual = {op: 0 for op in OPS}
    for unit in units:
        actual[unit['op']] += 1
    if any(actual[op] != counts.get(op, 0) for op in OPS):
        raise PatchFormatError('补丁分类型计数与实际单元不符')
    return header, units, footer
