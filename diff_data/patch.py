"""独立补丁应用器：严格校验旧像后把旧端重放成新端。

两阶段执行：
  阶段一 只校验不发布——补丁外壳、两端快照绑定、每个单元的旧像、
         以及重放结果与新端冻结状态的完整对账，任何错误都不产生产物；
  阶段二 全部通过后，重放状态以临时文件原子发布。

应用器不导入净变更引擎，只相信注册表自证的两端快照与补丁自身的摘要。
"""
from __future__ import annotations

import json
import os
import tempfile

from .io import DataError
from .patchformat import read_patch, PatchFormatError
from .snapshot import build_view, common_field_names, signature_equal


class PatchApplyError(DataError):
    """补丁校验或重放失败；调用方不应信任任何已发布产物。"""


def _atomic_write_jsonl(path, records):
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.replayed-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            for rec in records:
                fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_patch(tables, patch_path, output_path, *, expected_old=None, expected_new=None):
    """校验并重放补丁，返回重放计量；失败时保证不发布任何完整产物。"""
    header, units, footer = read_patch(patch_path)

    old_id, new_id = header['old_snapshot'], header['new_snapshot']
    if expected_old is not None and expected_old != old_id:
        raise PatchApplyError('补丁旧端快照与调用方指定的旧端不一致')
    if expected_new is not None and expected_new != new_id:
        raise PatchApplyError('补丁新端快照与调用方指定的新端不一致')

    old_view = build_view(tables, old_id)
    new_view = build_view(tables, new_id)

    if old_view.snapshot_digest != header['old_snapshot_digest']:
        raise PatchApplyError('补丁绑定的旧端快照摘要与注册表实际快照不符，拒绝重放')
    if new_view.snapshot_digest != header['new_snapshot_digest']:
        raise PatchApplyError('补丁绑定的新端快照摘要与注册表实际快照不符，拒绝重放')

    common_fields = common_field_names(old_view, new_view)
    if common_fields != list(header['common_fields']):
        raise PatchApplyError('补丁公共字段目录与两端 schema 实际公共字段不符')
    if not signature_equal(old_view.schema.projection_signature(common_fields),
                           header['old_projection']):
        raise PatchApplyError('补丁记录的旧端投影签名与实际 schema 不符')
    if not signature_equal(new_view.schema.projection_signature(common_fields),
                           header['new_projection']):
        raise PatchApplyError('补丁记录的新端投影签名与实际 schema 不符')

    # ---- 阶段一：严格校验每个旧像，先在内存重放，任何错误都不触达输出文件 ----
    replayed = dict(old_view.visible)
    touched = {}
    checks = {'insert_checked': 0, 'delete_checked': 0, 'update_checked': 0}
    for unit in units:
        rid = unit['row_id']
        if rid in touched:
            raise PatchApplyError(f'row_id {rid} 在补丁中出现多次，补丁不合法')
        op = unit['op']
        if op == 'insert':
            if rid in old_view.visible:
                raise PatchApplyError(f'insert 单元的旧端实际存在 row_id {rid}，旧像校验失败')
            if unit['after'] != new_view.visible.get(rid):
                raise PatchApplyError(f'insert 单元的 after 像与新端冻结状态不符: {rid}')
            replayed[rid] = unit['after']
            checks['insert_checked'] += 1
        elif op == 'delete':
            if unit['before'] != old_view.visible.get(rid):
                raise PatchApplyError(f'delete 单元的 before 像与旧端冻结状态不符: {rid}')
            if rid in new_view.visible:
                raise PatchApplyError(f'delete 单元的 row_id {rid} 在新端仍然可见，拒绝重放')
            replayed.pop(rid, None)
            checks['delete_checked'] += 1
        else:  # update
            if unit['before'] != old_view.visible.get(rid):
                raise PatchApplyError(f'update 单元的 before 像与旧端冻结状态不符: {rid}')
            if unit['after'] != new_view.visible.get(rid):
                raise PatchApplyError(f'update 单元的 after 像与新端冻结状态不符: {rid}')
            if unit['before'] == unit['after']:
                raise PatchApplyError(f'update 单元两端像完全相同，补丁不合法: {rid}')
            replayed[rid] = unit['after']
            checks['update_checked'] += 1
        touched[rid] = op

    # 未被补丁触及的行，其公共字段必须在两端逐字段一致，否则补丁不完整。
    # 单边新增的投影字段属于 schema 演化，不构成补丁内容，不参与这里的完整性判断。
    def common_equal(left_image, right_image):
        return right_image is not None and \
            all(left_image[name] == right_image[name] for name in common_fields)

    for rid, image in old_view.visible.items():
        if rid not in touched and not common_equal(image, new_view.visible.get(rid)):
            raise PatchApplyError(
                f'row_id {rid} 公共字段两端逻辑值不同但补丁未覆盖，拒绝发布不完整补丁')

    # 完整对账：身份集合必须逐行相等；重放像的公共字段必须等于新端冻结状态，
    # 新端单边字段按新端 schema 的投影值补齐后也必须一致，一个 row_id 都不能差。
    if set(replayed) != set(new_view.visible):
        missing = sorted(set(new_view.visible) - set(replayed))
        extra = sorted(set(replayed) - set(new_view.visible))
        raise PatchApplyError(
            f'重放结果与新端身份集合不符，缺失 {missing[:5]} 多余 {extra[:5]}')
    old_only_fields = [f['name'] for f in old_view.schema.fields
                       if f['name'] not in new_view.schema.by_name]
    new_only_fields = [f['name'] for f in new_view.schema.fields
                       if f['name'] not in old_view.schema.by_name]
    for rid, expected in new_view.visible.items():
        actual = replayed[rid]
        for name in common_fields:
            if actual[name] != expected[name]:
                raise PatchApplyError(f'重放结果在 row_id {rid} 字段 {name} 上与新端冻结状态不符')
        for name in new_only_fields:
            # 单边字段补丁不承载，重放时按新端投影目录的实际可见值补齐。
            actual[name] = expected[name]
        for name in old_only_fields:
            actual.pop(name, None)
        if actual != expected:
            raise PatchApplyError(f'重放结果在 row_id {rid} 上与新端冻结状态不符')

    # ---- 阶段二：全部校验通过后原子发布 ----
    if os.path.exists(output_path):
        raise PatchApplyError(f'重放产物已存在，拒绝覆盖: {output_path}')
    records = [
        {'row_id': rid, 'image': replayed[rid]}
        for rid in sorted(replayed)
    ]
    _atomic_write_jsonl(output_path, records)

    return {
        'old_snapshot': old_id,
        'new_snapshot': new_id,
        'patch_units': len(units),
        'footer_counts': footer['counts'],
        'checks': checks,
        'replayed_visible_rows': len(records),
        'output_path': os.path.abspath(output_path),
        'reconciled': True,
    }
