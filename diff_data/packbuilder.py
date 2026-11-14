"""演示与核对用快照资料包的构建器。

把场景声明（快照、行文件、删除位图、schema）写成与线上契约一致的
JSONL 与摘要清单。仅用于本地生成核对资料，不参与运行时读取路径。
"""
from __future__ import annotations

import hashlib
import json
import os

from .io import DataError

TABLE_FIELDS = {
    'snapshots': ['id', 'files', 'deletions', 'schema'],
    'file_rows': ['id', 'file', 'position', 'row_id', 'cost'],
    'deletes': ['id', 'file', 'positions'],
    'schema': ['id', 'fields'],
}
# cost 允许物理缺席，用于核对“物理缺席依 schema 默认值”与显式 null 的区分。
TABLE_OPTIONAL = {'file_rows': ['cost']}


def _line(row):
    return (json.dumps(row, sort_keys=True, ensure_ascii=False,
                       separators=(',', ':')) + '\n').encode('utf-8')


def write_pack(directory, *, name, snapshots, file_rows, deletes, schemas):
    """在 directory 下写一个独立资料包（manifest + 每表 JSONL）。"""
    os.makedirs(directory, exist_ok=True)
    payloads = {
        'snapshots': snapshots,
        'file_rows': file_rows,
        'deletes': deletes,
        'schema': schemas,
    }
    tables = {}
    for table, rows in payloads.items():
        filename = f'{name}-{table}.jsonl'
        content = b''.join(_line(row) for row in rows)
        with open(os.path.join(directory, filename), 'wb') as fh:
            fh.write(content)
        entry = {
            'file': filename,
            'sha256': hashlib.sha256(content).hexdigest(),
            'bytes': len(content),
            'rows': len(rows),
            'fields': TABLE_FIELDS[table],
        }
        if TABLE_OPTIONAL.get(table):
            entry['optional'] = TABLE_OPTIONAL[table]
        tables[table] = entry
    manifest = {'schema': 1, 'tables': tables}
    manifest_path = os.path.join(directory, f'{name}.json')
    with open(manifest_path, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')
    return manifest_path


# ---------------------------------------------------------------------------
# 五个核对场景的固定声明。每个场景都独立成包，避免跨包重复 id。
# ---------------------------------------------------------------------------

def schema_cost_only(sid='s1', default=None):
    return {'id': sid, 'fields': [
        {'field_id': 1, 'name': 'cost', 'kind': 'int64', 'default': default}]}


def build_pure_compaction(directory):
    """纯压实：f2 是 f1 的全量重写，内容摘要相同、无删除、零业务变化。"""
    rows_a = [
        {'id': 'p1', 'file': 'f1', 'position': 0, 'row_id': 'r1', 'cost': 10},
        {'id': 'p2', 'file': 'f1', 'position': 1, 'row_id': 'r2', 'cost': 20},
        {'id': 'p3', 'file': 'f1', 'position': 2, 'row_id': 'r3', 'cost': 30},
    ]
    rows_b = [
        {'id': 'q1', 'file': 'f2', 'position': 0, 'row_id': 'r1', 'cost': 10},
        {'id': 'q2', 'file': 'f2', 'position': 1, 'row_id': 'r2', 'cost': 20},
        {'id': 'q3', 'file': 'f2', 'position': 2, 'row_id': 'r3', 'cost': 30},
    ]
    return write_pack(directory, name='pure_compaction',
                      snapshots=[
                          {'id': 'old', 'files': ['f1'], 'deletions': [], 'schema': 's1'},
                          {'id': 'new', 'files': ['f2'], 'deletions': [], 'schema': 's1'}],
                      file_rows=rows_a + rows_b, deletes=[],
                      schemas=[schema_cost_only()])


def build_bitmap_change(directory):
    """删除位图变化：同一物理文件在新快照取消对 r2 的删除（业务上重新出现）。"""
    rows = [
        {'id': 'p1', 'file': 'f1', 'position': 0, 'row_id': 'r1', 'cost': 10},
        {'id': 'p2', 'file': 'f1', 'position': 1, 'row_id': 'r2', 'cost': 20},
        {'id': 'p3', 'file': 'f1', 'position': 2, 'row_id': 'r3', 'cost': 30},
    ]
    return write_pack(directory, name='bitmap_change',
                      snapshots=[
                          {'id': 'old', 'files': ['f1'], 'deletions': ['d1'], 'schema': 's1'},
                          {'id': 'new', 'files': ['f1'], 'deletions': [], 'schema': 's1'}],
                      file_rows=rows,
                      deletes=[{'id': 'd1', 'file': 'f1', 'positions': [1]}],
                      schemas=[schema_cost_only()])


def build_same_id_update(directory):
    """同 ID 改值：r1 的 cost 变化，r2 走删除位图消失，r3 为业务新增。"""
    rows = [
        {'id': 'p1', 'file': 'f1', 'position': 0, 'row_id': 'r1', 'cost': 10},
        {'id': 'p2', 'file': 'f1', 'position': 1, 'row_id': 'r2', 'cost': 20},
        {'id': 'p3', 'file': 'f2', 'position': 0, 'row_id': 'r1', 'cost': 15},
        {'id': 'p4', 'file': 'f2', 'position': 1, 'row_id': 'r3', 'cost': 30},
    ]
    return write_pack(directory, name='same_id_update',
                      snapshots=[
                          {'id': 'old', 'files': ['f1'], 'deletions': ['d1'], 'schema': 's1'},
                          {'id': 'new', 'files': ['f2'], 'deletions': [], 'schema': 's1'}],
                      file_rows=rows,
                      deletes=[{'id': 'd1', 'file': 'f1', 'positions': [1]}],
                      schemas=[schema_cost_only()])


def build_partition_move(directory):
    """跨分区搬家：r2 从分区 A 搬到分区 B 且物理位置颠倒，逻辑值不变。

    路径和行位置都不是业务身份，也不存在可配对的同摘要文件，
    因此两端文件全部进入扫描，但净变更必须为空。
    """
    rows = [
        {'id': 'p1', 'file': 'part-a/f1', 'position': 0, 'row_id': 'r1', 'cost': 10},
        {'id': 'p2', 'file': 'part-a/f1', 'position': 1, 'row_id': 'r2', 'cost': 20},
        {'id': 'p3', 'file': 'part-b/f2', 'position': 0, 'row_id': 'r2', 'cost': 20},
        {'id': 'p4', 'file': 'part-b/f2', 'position': 1, 'row_id': 'r1', 'cost': 10},
    ]
    return write_pack(directory, name='partition_move',
                      snapshots=[
                          {'id': 'old', 'files': ['part-a/f1'], 'deletions': [], 'schema': 's1'},
                          {'id': 'new', 'files': ['part-b/f2'], 'deletions': [], 'schema': 's1'}],
                      file_rows=rows, deletes=[],
                      schemas=[schema_cost_only()])


def build_lost_after_commit(directory):
    """提交后失联：r2 在旧端可见，新端文件里完全不存在，应输出 delete。"""
    rows = [
        {'id': 'p1', 'file': 'f1', 'position': 0, 'row_id': 'r1', 'cost': 10},
        {'id': 'p2', 'file': 'f1', 'position': 1, 'row_id': 'r2', 'cost': 20},
        {'id': 'p3', 'file': 'f2', 'position': 0, 'row_id': 'r1', 'cost': 10},
        {'id': 'p4', 'file': 'f2', 'position': 1, 'row_id': 'r3', 'cost': 30},
    ]
    return write_pack(directory, name='lost_after_commit',
                      snapshots=[
                          {'id': 'old', 'files': ['f1'], 'deletions': [], 'schema': 's1'},
                          {'id': 'new', 'files': ['f2'], 'deletions': [], 'schema': 's1'}],
                      file_rows=rows, deletes=[],
                      schemas=[schema_cost_only()])


def build_schema_evolution(directory):
    """schema 演化：新端增加带默认值的 amount；公共字段 cost 的变化才构成更新。"""
    schemas = [
        schema_cost_only('s1'),
        {'id': 's2', 'fields': [
            {'field_id': 1, 'name': 'cost', 'kind': 'int64', 'default': None},
            {'field_id': 2, 'name': 'amount', 'kind': 'int64', 'default': 0}]},
    ]
    rows = [
        {'id': 'p1', 'file': 'f1', 'position': 0, 'row_id': 'r1', 'cost': 10},
        {'id': 'p2', 'file': 'f1', 'position': 1, 'row_id': 'r2', 'cost': 20},
        {'id': 'p3', 'file': 'f2', 'position': 0, 'row_id': 'r1', 'cost': 15},
        {'id': 'p4', 'file': 'f2', 'position': 1, 'row_id': 'r2', 'cost': 20},
    ]
    return write_pack(directory, name='schema_evolution',
                      snapshots=[
                          {'id': 'old', 'files': ['f1'], 'deletions': [], 'schema': 's1'},
                          {'id': 'new', 'files': ['f2'], 'deletions': [], 'schema': 's2'}],
                      file_rows=rows, deletes=[], schemas=schemas)


def build_default_vs_absent(directory):
    """物理缺席、显式 null 与默认值三者的区分核对。

    r1 旧端显式 null、新端物理缺席（默认 null）：投影后相等，不是变更；
    r2 旧端物理缺席（默认 null）、新端显式 null：同样不是变更；
    r3 旧端物理缺席、新端给值：业务新增；r4 同值重写：不是变更。
    """
    rows = [
        {'id': 'p1', 'file': 'f1', 'position': 0, 'row_id': 'r1', 'cost': None},
        {'id': 'p2', 'file': 'f1', 'position': 1, 'row_id': 'r2'},
        {'id': 'p3', 'file': 'f1', 'position': 2, 'row_id': 'r4', 'cost': 40},
        {'id': 'p4', 'file': 'f2', 'position': 0, 'row_id': 'r1'},
        {'id': 'p5', 'file': 'f2', 'position': 1, 'row_id': 'r2', 'cost': None},
        {'id': 'p6', 'file': 'f2', 'position': 2, 'row_id': 'r3', 'cost': 30},
        {'id': 'p7', 'file': 'f2', 'position': 3, 'row_id': 'r4', 'cost': 40},
    ]
    return write_pack(directory, name='default_vs_absent',
                      snapshots=[
                          {'id': 'old', 'files': ['f1'], 'deletions': [], 'schema': 's1'},
                          {'id': 'new', 'files': ['f2'], 'deletions': [], 'schema': 's1'}],
                      file_rows=rows, deletes=[],
                      schemas=[schema_cost_only()])


SCENARIOS = {
    'pure_compaction': build_pure_compaction,
    'bitmap_change': build_bitmap_change,
    'same_id_update': build_same_id_update,
    'partition_move': build_partition_move,
    'lost_after_commit': build_lost_after_commit,
    'schema_evolution': build_schema_evolution,
    'default_vs_absent': build_default_vs_absent,
}


def build_all(base_dir):
    paths = {}
    for name, builder in SCENARIOS.items():
        paths[name] = builder(os.path.join(base_dir, name))
    return paths


if __name__ == '__main__':
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else 'data/scenarios'
    try:
        for name, path in build_all(target).items():
            print(f'{name}: {path}')
    except (OSError, DataError) as exc:
        raise SystemExit(f'资料包构建失败: {exc}')
