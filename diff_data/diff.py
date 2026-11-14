"""两端冻结快照的净变更导出引擎。

比较顺序固定为：
  1. 两端各自按“自己的删除位图 + 自己的 schema 投影”还原可见状态；
  2. 用共享文件摘要、删除位集、投影签名和 row_id 范围索引证明整区不变并跳读；
  3. 其余可见行进入有界外排（缓冲满即落盘，归并时按 row_id 流式分组）；
  4. 按 row_id 归并关联，只输出投影后逻辑值真正变化的单元。

绝不直接相减两个文件的删除位图，也绝不用新 schema 的默认值改写旧端实值。
"""
from __future__ import annotations

import heapq
import json
import os
import tempfile
from dataclasses import dataclass, field

from .patchformat import (
    AtomicPatchWriter, StreamHasher, PatchFormatError, make_footer, make_header,
    make_unit, encode_line, decode_line, unit_digest,
)
from .snapshot import (
    SnapshotError, build_view, common_field_names, projections_equivalent,
    canonical_json,
)

# 单条 run 在内存中缓冲的记录数上限；超出即外排落盘，异常键也不会撑爆内存。
DEFAULT_RUN_BUDGET = 1000


class DiffError(SnapshotError):
    """净变更导出过程中的资料或断点错误。"""


@dataclass
class SkipPlan:
    projection_equivalent: bool
    common_fields: list
    skipped_pairs: list                      # [{old_file,new_file,row_id_range,visible_rows,reason}]
    old_scan_files: list
    new_scan_files: list
    skipped_row_ids_old: set
    skipped_row_ids_new: set
    old_file_reasons: dict
    new_file_reasons: dict


def plan_skips(old_view, new_view, common_fields):
    """只有内容摘要、删除位集、投影语义三者同时一致，整区跳读才成立。

    文件相同但删除集不同（或投影不同）时绝不能略过——摘要只证明物理内容，
    证明不了逻辑可见状态。配对按 1:1 贪心进行，配不上的文件全部进入扫描。
    """
    equivalent = projections_equivalent(old_view, new_view, common_fields)
    old_buckets, new_buckets = {}, {}
    if equivalent:
        for view, buckets in ((old_view, old_buckets), (new_view, new_buckets)):
            for fname in view.files:
                key = (view.file_digests[fname], frozenset(view.delete_sets[fname]))
                buckets.setdefault(key, []).append(fname)

    skipped_pairs, old_used, new_used = [], set(), set()
    if equivalent:
        for key, old_files in old_buckets.items():
            mates = new_buckets.get(key, [])
            for old_file, new_file in zip(old_files, mates):
                old_used.add(old_file)
                new_used.add(new_file)
                lo_hi = old_view.ranges[old_file][:2]
                visible_rows = sum(
                    1 for pos, _ in enumerate(old_view.physical[old_file])
                    if pos not in old_view.delete_sets[old_file])
                skipped_pairs.append({
                    'old_file': old_file,
                    'new_file': new_file,
                    'row_id_range': list(lo_hi),
                    'visible_rows': visible_rows,
                    'reason': '内容摘要、删除位集与公共字段投影全部一致',
                })

    def reason(view, other, fname, used):
        if not equivalent:
            return '两端公共字段投影定义不同，文件级跳读不成立'
        if fname in used:
            return '已整区跳读'
        digest = view.file_digests[fname]
        same_digest = [f for f in other.files if other.file_digests[f] == digest]
        if not same_digest:
            return '对端无相同内容摘要文件（重写或搬家均需逐行还原比较）'
        if frozenset(view.delete_sets[fname]) != frozenset(other.delete_sets[same_digest[0]]):
            return '文件内容相同但删除位集不同，不能略过'
        return '对端候选已被其他文件 1:1 占用，转为逐行扫描'

    old_reasons = {f: reason(old_view, new_view, f, old_used) for f in old_view.files}
    new_reasons = {f: reason(new_view, old_view, f, new_used) for f in new_view.files}

    skipped_old, skipped_new = set(), set()
    for pair in skipped_pairs:
        for row in old_view.physical[pair['old_file']]:
            if row['position'] not in old_view.delete_sets[pair['old_file']]:
                skipped_old.add(row['row_id'])
        for row in new_view.physical[pair['new_file']]:
            if row['position'] not in new_view.delete_sets[pair['new_file']]:
                skipped_new.add(row['row_id'])

    return SkipPlan(
        projection_equivalent=equivalent,
        common_fields=list(common_fields),
        skipped_pairs=skipped_pairs,
        old_scan_files=[f for f in old_view.files if f not in old_used],
        new_scan_files=[f for f in new_view.files if f not in new_used],
        skipped_row_ids_old=skipped_old,
        skipped_row_ids_new=skipped_new,
        old_file_reasons=old_reasons,
        new_file_reasons=new_reasons,
    )


class _RunSpiller:
    """有界缓冲：满了就把已排序 run 落到临时目录，内存占用与总数据量脱钩。"""

    def __init__(self, workdir, budget):
        self.workdir = workdir
        self.budget = budget
        self._buffer = []
        self.run_paths = []
        self.peak_buffered = 0
        self.rows_spilled = 0
        self.bytes_spilled = 0
        self.rows_scanned = 0
        self.duplicates_collapsed = 0

    def add(self, record):
        self._buffer.append(record)
        self.rows_scanned += 1
        if len(self._buffer) > self.peak_buffered:
            self.peak_buffered = len(self._buffer)
        if len(self._buffer) >= self.budget:
            self.flush()

    def flush(self):
        if not self._buffer:
            return
        self._buffer.sort(key=lambda r: r['row_id'])
        fd, path = tempfile.mkstemp(prefix='diff-run-', suffix='.jsonl', dir=self.workdir)
        with os.fdopen(fd, 'wb') as fh:
            for rec in self._buffer:
                line = encode_line(rec)
                fh.write(line)
                self.bytes_spilled += len(line)
                self.rows_spilled += 1
        self.run_paths.append(path)
        self._buffer = []

    def grouped_stream(self):
        """归并所有 run，按 row_id 分组；同值重复折叠，异值冲突立即判损坏。

        分组只保留首像做流式比较，单个 row_id 即使出现海量重复也 O(1) 内存，
        单键异常不会把进程拖进无限内存。
        """
        self.flush()
        streams = [self._read_run(path) for path in self.run_paths]
        if not streams:
            return iter(())
        merged = heapq.merge(*streams, key=lambda r: r['row_id'])

        def groups():
            current_id, image = None, None
            for rec in merged:
                rid = rec['row_id']
                if rid != current_id:
                    if current_id is not None:
                        yield current_id, image
                    current_id, image = rid, rec['image']
                elif rec['image'] != image:
                    raise DiffError(
                        f'row_id {rid} 存在多个不同可见终态，资料损坏；不能选择“最新文件”')
                else:
                    self.duplicates_collapsed += 1
            if current_id is not None:
                yield current_id, image

        return groups()

    @staticmethod
    def _read_run(path):
        with open(path, 'rb') as fh:
            for line in fh:
                yield json.loads(line)

    def cleanup(self):
        for path in self.run_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.run_paths = []


def _scan_side(view, scan_files, spiller):
    """只逐行还原扫描区文件；跳读区已被摘要与位集证明不变，不再进入外排。"""
    for fname in scan_files:
        deleted = view.delete_sets[fname]
        for row in sorted(view.physical[fname], key=lambda r: r['position']):
            if row['position'] in deleted:
                continue
            spiller.add({
                'row_id': row['row_id'],
                'image': view.visible[row['row_id']],
                'file': fname,
                'position': row['position'],
            })


def _join_streams(left, right):
    """两个按 row_id 排序的分组流做归并关联，路径与位置完全不参与身份判断。"""
    lo = next(left, None)
    ro = next(right, None)
    while lo is not None or ro is not None:
        if ro is None or (lo is not None and lo[0] < ro[0]):
            yield 'only_old', lo, None
            lo = next(left, None)
        elif lo is None or ro[0] < lo[0]:
            yield 'only_new', None, ro
            ro = next(right, None)
        else:
            yield 'both', lo, ro
            lo = next(left, None)
            ro = next(right, None)


def _binding(view, common_fields):
    return {
        'snapshot_id': view.snapshot_id,
        'snapshot_digest': view.snapshot_digest,
        'schema_id': view.schema.id,
        'projection': view.schema.projection_signature(common_fields),
    }


def _validate_checkpoint(ckpt, header_binding, output_path):
    binding = ckpt.get('binding') or {}
    # 断点来自 JSON 往返，tuple/list 等表示差异按规范文本归一化后再绑定。
    if canonical_json(binding.get('old')) != canonical_json(header_binding['old']) \
            or canonical_json(binding.get('new')) != canonical_json(header_binding['new']):
        raise DiffError('断点绑定的快照或投影与本次请求不一致，拒绝接续')
    if not os.path.exists(output_path):
        raise DiffError('断点存在但补丁输出缺失，无法安全接续')
    if os.path.getsize(output_path) != ckpt.get('bytes_written'):
        raise DiffError('断点记录的输出长度与实际文件不符，拒绝接续')
    if ckpt.get('finished'):
        raise DiffError('补丁已完整发布；如需重导请显式移除旧产物')


def _resume_state(output_path, ckpt):
    """重读已有输出验证最后完整单元与流摘要，返回 (最后row_id, 流摘要, 计数)。"""
    hasher = StreamHasher()
    last_unit, count, prev_id = None, 0, None
    with open(output_path, 'rb') as fh:
        lines = fh.read().splitlines()
    if len(lines) < 1:
        raise DiffError('断点输出为空，无法接续')
    if decode_line(lines[0], 1).get('kind') != 'header':
        raise DiffError('断点输出缺少 header，无法接续')
    for index, raw in enumerate(lines[1:], start=2):
        unit = decode_line(raw, index)
        if unit.get('kind') == 'footer':
            raise DiffError('断点输出已含 footer 但断点未标记完成，产物矛盾')
        stated = unit.pop('unit_digest', None)
        if stated != unit_digest(unit):
            raise DiffError(f'已输出单元第{index}行摘要不一致，拒绝接续')
        unit['unit_digest'] = stated
        if prev_id is not None and unit['row_id'] <= prev_id:
            raise DiffError('已输出单元未保持 row_id 稳定排序，拒绝接续')
        prev_id = unit['row_id']
        hasher.add(unit)
        last_unit, count = unit, count + 1
    if hasher.value() != ckpt.get('stream_digest'):
        raise DiffError('断点流摘要与已输出内容不符，拒绝接续')
    if last_unit is None or [last_unit['row_id'], last_unit['unit_digest']] != \
            list(ckpt.get('last_unit', (None, None))):
        raise DiffError('断点记录的最后完整单元与实际输出不符，拒绝接续')
    return last_unit['row_id'], hasher, count


def export_diff(tables, old_snapshot_id, new_snapshot_id, output_path, *,
                workdir=None, run_budget=DEFAULT_RUN_BUDGET, force=False,
                checkpoint_path=None):
    """导出净变更补丁，返回资源计量；支持绑定两端快照与投影的断点续跑。"""
    if run_budget < 1:
        raise DiffError('run 缓冲预算必须为正整数')
    checkpoint_path = checkpoint_path or (output_path + '.ckpt')
    old_view = build_view(tables, old_snapshot_id)
    new_view = build_view(tables, new_snapshot_id)
    if old_snapshot_id == new_snapshot_id:
        raise DiffError('两端快照相同，不存在净变更区间')
    common_fields = common_field_names(old_view, new_view)
    if not common_fields:
        raise DiffError('两端 schema 没有公共字段，无法按业务身份比较')
    plan = plan_skips(old_view, new_view, common_fields)

    header = make_header(
        old_snapshot=old_snapshot_id,
        new_snapshot=new_snapshot_id,
        old_snapshot_digest=old_view.snapshot_digest,
        new_snapshot_digest=new_view.snapshot_digest,
        old_projection=list(old_view.schema.projection_signature(common_fields)),
        new_projection=list(new_view.schema.projection_signature(common_fields)),
        common_fields=common_fields,
    )
    binding = {'old': _binding(old_view, common_fields),
               'new': _binding(new_view, common_fields)}

    workdir = workdir or tempfile.mkdtemp(prefix='diff-work-')
    os.makedirs(workdir, exist_ok=True)
    old_spiller = _RunSpiller(workdir, run_budget)
    new_spiller = _RunSpiller(workdir, run_budget)

    resume_row_id, hasher, emitted = None, StreamHasher(), 0
    # emitted 始终表示已发布补丁中的单元总数（含断点前已存在的单元）。
    counts = {op: 0 for op in ('insert', 'delete', 'update')}
    ckpt_exists = os.path.exists(checkpoint_path)
    if ckpt_exists:
        try:
            ckpt = json.loads(open(checkpoint_path, encoding='utf-8').read())
            _validate_checkpoint(ckpt, binding, output_path)
            resume_row_id, hasher, emitted = _resume_state(output_path, ckpt)
            for op in counts:
                counts[op] = ckpt.get('counts', {}).get(op, 0)
        except (OSError, ValueError, PatchFormatError) as exc:
            raise DiffError(f'断点不可用: {exc}') from exc
    elif os.path.exists(output_path):
        if not force:
            raise DiffError('输出补丁已存在且无断点；确认覆盖请使用 force')
        os.unlink(output_path)

    out = open(output_path, 'ab' if ckpt_exists else 'wb')
    bytes_written = os.path.getsize(output_path) if ckpt_exists else 0
    if not ckpt_exists:
        raw = encode_line(header)
        out.write(raw)
        bytes_written += len(raw)

    def write_checkpoint(last_unit_obj, finished):
        payload = {
            'binding': binding,
            'bytes_written': bytes_written,
            'stream_digest': hasher.value(),
            'last_unit': None if last_unit_obj is None
            else [last_unit_obj['row_id'], last_unit_obj['unit_digest']],
            'counts': counts,
            'emitted': emitted,
            'finished': finished,
        }
        fd, tmp = tempfile.mkstemp(prefix='.ckpt-', dir=os.path.dirname(
            os.path.abspath(checkpoint_path)) or '.')
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, checkpoint_path)

    try:
        _scan_side(old_view, plan.old_scan_files, old_spiller)
        _scan_side(new_view, plan.new_scan_files, new_spiller)
        left = old_spiller.grouped_stream()
        right = new_spiller.grouped_stream()

        def changed_on_common(lo_image, ro_image):
            return any(lo_image[name] != ro_image[name] for name in common_fields)

        last_unit_obj = None
        for kind, lo, ro in _join_streams(left, right):
            if kind == 'only_old':
                rid, image = lo
                unit = make_unit('delete', rid, image, None)
            elif kind == 'only_new':
                rid, image = ro
                unit = make_unit('insert', rid, None, image)
            else:
                rid, lo_image = lo
                _, ro_image = ro
                if not changed_on_common(lo_image, ro_image):
                    continue  # 无变化重写不输出
                unit = make_unit('update', rid, lo_image, ro_image)
            if resume_row_id is not None and rid <= resume_row_id:
                continue  # 断点前的完整单元不重复发布
            raw = encode_line(unit)
            out.write(raw)
            out.flush()
            bytes_written += len(raw)
            hasher.add(unit)
            counts[unit['op']] += 1
            emitted += 1
            last_unit_obj = unit
            write_checkpoint(last_unit_obj, finished=False)

        footer = make_footer(
            count_by_op=counts, unit_count=emitted, stream_digest=hasher.value())
        raw = encode_line(footer)
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
        bytes_written += len(raw)
        write_checkpoint(last_unit_obj, finished=True)
    except BaseException:
        out.close()
        if not ckpt_exists:
            for p in (output_path, checkpoint_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        raise
    else:
        out.close()

    metrics = _build_metrics(
        old_view, new_view, plan, old_spiller, new_spiller, counts, emitted,
        bytes_written, resumed=ckpt_exists, workdir=workdir)
    old_spiller.cleanup()
    new_spiller.cleanup()
    return metrics


def _build_metrics(old_view, new_view, plan, old_spiller, new_spiller, counts,
                   emitted, bytes_written, resumed, workdir):
    return {
        'old_snapshot': old_view.snapshot_id,
        'new_snapshot': new_view.snapshot_id,
        'projection_equivalent': plan.projection_equivalent,
        'common_fields': plan.common_fields,
        'skipped_regions': plan.skipped_pairs,
        'skipped_visible_rows': {
            'old': len(plan.skipped_row_ids_old),
            'new': len(plan.skipped_row_ids_new),
        },
        'scanned_files': {
            'old': plan.old_scan_files,
            'new': plan.new_scan_files,
        },
        'file_decisions': {
            'old': plan.old_file_reasons,
            'new': plan.new_file_reasons,
        },
        'external_sort': {
            'run_budget': old_spiller.budget,
            'old': {'runs': len(old_spiller.run_paths),
                    'rows_scanned': old_spiller.rows_scanned,
                    'spilled_bytes': old_spiller.bytes_spilled,
                    'peak_buffered_rows': old_spiller.peak_buffered,
                    'duplicates_collapsed': old_spiller.duplicates_collapsed},
            'new': {'runs': len(new_spiller.run_paths),
                    'rows_scanned': new_spiller.rows_scanned,
                    'spilled_bytes': new_spiller.bytes_spilled,
                    'peak_buffered_rows': new_spiller.peak_buffered,
                    'duplicates_collapsed': new_spiller.duplicates_collapsed},
            'workdir': workdir,
        },
        'result_counts': counts,
        'units_emitted': emitted,
        'output_bytes': bytes_written,
        'resumed': resumed,
    }
