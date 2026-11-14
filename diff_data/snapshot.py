"""快照冻结可见状态的还原、投影与损坏检查。

快照引用不可变的完整行文件、按文件物理行位生效的删除位图以及字段投影目录。
本模块只回答一个问题：某个冻结快照当时“看得见”的逻辑行分别是什么，
不读取提交日志、不猜测文件新旧、不以路径或物理位置充当业务身份。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .io import DataError

# 本迭代支持的字段类型；显式 null 对任意类型都合法。
KNOWN_KINDS = {'int64', 'double', 'string', 'boolean'}


class SnapshotError(DataError):
    """快照引用、删除位图或可见终态损坏。"""


def canonical_json(obj):
    """跨进程稳定的规范 JSON 文本，用于摘要与断点绑定。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def canonical_digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _kind_matches(kind, value):
    if value is None:
        return True
    if kind == 'int64':
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == 'double':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == 'string':
        return isinstance(value, str)
    if kind == 'boolean':
        return isinstance(value, bool)
    return False


@dataclass(frozen=True)
class SchemaInfo:
    """一个快照自己的字段投影目录。"""
    id: str
    fields: tuple
    by_name: dict

    def projection_signature(self, common_names):
        """公共字段在本投影下的定义签名，用于证明两端投影等价。"""
        return tuple(
            (name, self.by_name[name]['kind'], canonical_json(self.by_name[name]['default']))
            for name in common_names
        )


@dataclass
class SnapshotView:
    """一个快照还原后的全部可见状态与索引。"""
    snapshot_id: str
    snapshot_row: dict
    snapshot_digest: str
    schema: SchemaInfo
    files: list
    delete_sets: dict
    physical: dict
    file_digests: dict
    ranges: dict
    # row_id -> 投影后的逻辑行（同值重复可见副本已折叠）
    visible: dict
    # row_id -> 可见来源 (file, position) 列表，用于跳读区域归属判断
    visible_sources: dict
    duplicate_visible: int

    def projected(self, row_id):
        return self.visible[row_id]


def load_schema(tables, schema_id):
    for row in tables['schema']:
        if row['id'] == schema_id:
            break
    else:
        raise SnapshotError(f'schema {schema_id} 不存在')
    if not isinstance(row['fields'], list) or not row['fields']:
        raise SnapshotError(f'schema {schema_id} 的字段目录为空或无效')
    fields = []
    seen_names, seen_ids = set(), set()
    for item in row['fields']:
        if not isinstance(item, dict) or set(item) != {'field_id', 'name', 'kind', 'default'}:
            raise SnapshotError(f'schema {schema_id} 存在无效字段定义')
        name, kind, field_id, default = item['name'], item['kind'], item['field_id'], item['default']
        if not isinstance(name, str) or not name or name in seen_names:
            raise SnapshotError(f'schema {schema_id} 字段名重复或无效: {name!r}')
        if not isinstance(field_id, int) or isinstance(field_id, bool) or field_id in seen_ids:
            raise SnapshotError(f'schema {schema_id} 字段ID重复或无效: {field_id!r}')
        if kind not in KNOWN_KINDS:
            raise SnapshotError(f'schema {schema_id} 字段 {name} 类型未知: {kind!r}')
        if not _kind_matches(kind, default):
            raise SnapshotError(f'schema {schema_id} 字段 {name} 默认值与类型不符')
        seen_names.add(name)
        seen_ids.add(field_id)
        fields.append({'field_id': field_id, 'name': name, 'kind': kind, 'default': default})
    return SchemaInfo(schema_id, tuple(fields), {f['name']: f for f in fields})


def _file_content_digest(rows):
    """文件内容摘要只绑定物理行业务内容，不含注册表记录 ID 与容器文件名。

    压实重写会分配新的注册表记录 ID，文件替换也可能只是换名重写，
    但只要位置、row_id 与各字段值一致，两个不可变文件的逻辑内容就相同。
    容器名是分区身份而非行内容，纳入摘要会让“纯重写”永远无法被证明不变。
    """
    h = hashlib.sha256()
    for row in sorted(rows, key=lambda r: r['position']):
        payload = {k: v for k, v in row.items() if k not in ('id', 'file')}
        h.update(canonical_json(payload).encode('utf-8'))
        h.update(b'\n')
    return h.hexdigest()


def build_view(tables, snapshot_id):
    """按快照自身的删除与投影语义还原冻结可见状态。"""
    snap = next((r for r in tables['snapshots'] if r['id'] == snapshot_id), None)
    if snap is None:
        raise SnapshotError(f'快照 {snapshot_id} 不存在')
    schema = load_schema(tables, snap['schema'])

    files = list(snap['files'])
    if len(set(files)) != len(files):
        raise SnapshotError(f'快照 {snapshot_id} 引用了重复文件')
    delete_rows = {r['id']: r for r in tables['deletes']}

    # 按文件归集物理行并校验位置语义：位置是该文件删除前的 0 起始物理行位。
    physical = {f: [] for f in files}
    for row in tables['file_rows']:
        if row['file'] in physical:
            physical[row['file']].append(row)
    delete_sets = {f: set() for f in files}
    for delete_id in snap['deletions']:
        drow = delete_rows.get(delete_id)
        if drow is None:
            raise SnapshotError(f'快照 {snapshot_id} 引用了不存在的删除位图 {delete_id}')
        if drow['file'] not in delete_sets:
            raise SnapshotError(
                f'删除位图 {delete_id} 目标文件 {drow["file"]} 不在快照 {snapshot_id} 中')
        target = delete_sets[drow['file']]
        for pos in drow['positions']:
            if not isinstance(pos, int) or isinstance(pos, bool) or pos < 0:
                raise SnapshotError(f'删除位图 {delete_id} 存在无效行位 {pos!r}')
            target.add(pos)  # 多位图并集，重复位置幂等

    file_digests, ranges = {}, {}
    visible, visible_sources, duplicate_visible = {}, {}, 0
    for fname in files:
        rows = sorted(physical[fname], key=lambda r: r['position'])
        positions = [r['position'] for r in rows]
        if len(set(positions)) != len(positions):
            raise SnapshotError(f'文件 {fname} 存在重复物理行位，资料损坏')
        if positions != list(range(len(rows))):
            raise SnapshotError(f'文件 {fname} 物理行位不连续，完整行文件损坏')
        deleted = delete_sets[fname]
        if deleted and max(deleted) >= len(rows):
            raise SnapshotError(f'文件 {fname} 的删除行位越过文件行数，资料损坏')
        file_digests[fname] = _file_content_digest(rows)
        if rows:
            ids = [r['row_id'] for r in rows]
            ranges[fname] = (min(ids), max(ids), len(rows))
        else:
            ranges[fname] = (None, None, 0)
        for row in rows:
            pos = row['position']
            if pos in deleted:
                continue
            image = _project_row(row, schema, fname, pos)
            rid = row['row_id']
            visible_sources.setdefault(rid, []).append((fname, pos))
            if rid in visible:
                # 同一快照同 row_id 的多个不同可见终态是损坏，不能选“最新文件”。
                if visible[rid] != image:
                    raise SnapshotError(
                        f'row_id {rid} 在快照 {snapshot_id} 中存在多个不同可见终态，资料损坏')
                duplicate_visible += 1
            else:
                visible[rid] = image

    return SnapshotView(
        snapshot_id=snapshot_id,
        snapshot_row=dict(snap),
        snapshot_digest=canonical_digest(canonical_json(snap)),
        schema=schema,
        files=files,
        delete_sets=delete_sets,
        physical=physical,
        file_digests=file_digests,
        ranges=ranges,
        visible=visible,
        visible_sources=visible_sources,
        duplicate_visible=duplicate_visible,
    )


def _project_row(row, schema: SchemaInfo, fname, pos):
    """按快照自己的 schema 投影：显式 null 保留，物理缺席填该 schema 默认值。"""
    image = {}
    for fdef in schema.fields:
        name = fdef['name']
        if name in row:
            value = row[name]  # 键存在即便是 null，也属于显式值
            if not _kind_matches(fdef['kind'], value):
                raise SnapshotError(
                    f'文件 {fname} 行位 {pos} 字段 {name} 与类型 {fdef["kind"]} 不符')
            image[name] = value
        else:
            image[name] = fdef['default']  # 物理缺席与显式 null 依默认值区分
    return image


def common_field_names(old_view: SnapshotView, new_view: SnapshotView):
    """两端可比较的公共字段，沿用旧端字段顺序；单边新增字段不构成变更。"""
    new_names = new_view.schema.by_name
    return [f['name'] for f in old_view.schema.fields if f['name'] in new_names]


def projections_equivalent(old_view: SnapshotView, new_view: SnapshotView, common_names):
    """公共字段的类型与默认值完全一致时，文件级跳读才具备投影正确性。"""
    return old_view.schema.projection_signature(common_names) == \
        new_view.schema.projection_signature(common_names)


def signature_equal(left, right):
    """投影签名的跨表示比较：tuple 与 JSON 往返后的 list 视为同一签名。"""
    return canonical_json(left) == canonical_json(right)
