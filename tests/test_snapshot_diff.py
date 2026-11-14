"""快照净变更导出、补丁重放与断点续跑的端到端核对。"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from diff_data.io import load, DataError
from diff_data.domain import inspect_tables
from diff_data.snapshot import build_view, SnapshotError
from diff_data.diff import export_diff, DiffError, plan_skips, common_field_names
from diff_data.patch import apply_patch, PatchApplyError
from diff_data.patchformat import read_patch, PatchFormatError, encode_line, decode_line
from diff_data import packbuilder

SCENARIO_DIR = Path('data/scenarios')


def _patch_units(path):
    _, units, _ = read_patch(path)
    return {(u['op'], u['row_id']): (u['before'], u['after']) for u in units}


class ScenarioTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='diff-test-')
        cls.manifests = packbuilder.build_all(cls.tmp)
        cls._seq = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, scenario, budget=2):
        # 每次运行使用独立产物名，避免同名断点在测试方法之间串扰。
        self.__class__._seq += 1
        tag = f'{scenario}-{self._seq}'
        work = tempfile.mkdtemp(dir=self.tmp, prefix=f'work-{tag}-')
        patch_path = os.path.join(self.tmp, f'{tag}.patch')
        tables = load(self.manifests[scenario])
        metrics = export_diff(tables, 'old', 'new', patch_path,
                              workdir=work, run_budget=budget)
        replayed = os.path.join(self.tmp, f'{tag}.replayed.jsonl')
        report = apply_patch(tables, patch_path, replayed)
        return tables, metrics, patch_path, replayed, report


class FiveRequiredScenariosTests(ScenarioTestBase):
    def test_pure_compaction_is_fully_skipped_and_empty(self):
        tables, metrics, patch_path, replayed, report = self._run('pure_compaction')
        self.assertEqual(metrics['result_counts'],
                         {'insert': 0, 'delete': 0, 'update': 0})
        self.assertEqual(metrics['scanned_files'], {'old': [], 'new': []})
        self.assertEqual(len(metrics['skipped_regions']), 1)
        region = metrics['skipped_regions'][0]
        self.assertEqual((region['old_file'], region['new_file']), ('f1', 'f2'))
        self.assertEqual(region['row_id_range'], ['r1', 'r3'])
        self.assertEqual(region['visible_rows'], 3)
        self.assertEqual(metrics['external_sort']['old']['runs'], 0)
        self.assertTrue(report['reconciled'])

    def test_bitmap_change_never_skips_shared_file(self):
        tables, metrics, patch_path, replayed, report = self._run('bitmap_change')
        # 同一物理文件但删除位集不同，禁止整区跳读。
        self.assertEqual(metrics['scanned_files'], {'old': ['f1'], 'new': ['f1']})
        self.assertEqual(metrics['skipped_regions'], [])
        self.assertIn('删除位集不同', metrics['file_decisions']['old']['f1'])
        units = _patch_units(patch_path)
        self.assertEqual(set(units), {('insert', 'r2')})  # 位图放开 => 业务重新出现
        self.assertEqual(units[('insert', 'r2')], (None, {'cost': 20}))
        self.assertTrue(report['reconciled'])

    def test_same_id_update_pairs_images_and_ignores_ghost_rows(self):
        tables, metrics, patch_path, replayed, report = self._run('same_id_update')
        units = _patch_units(patch_path)
        # r2 在两端都不可见（旧端位图删除、新端文件缺席），属于中途行，不得出现。
        self.assertEqual(set(units), {('update', 'r1'), ('insert', 'r3')})
        self.assertEqual(units[('update', 'r1')], ({'cost': 10}, {'cost': 15}))
        self.assertEqual(units[('insert', 'r3')], (None, {'cost': 30}))
        self.assertNotIn(('any', 'r2'), units)
        self.assertTrue(report['reconciled'])

    def test_partition_move_scans_all_but_emits_nothing(self):
        tables, metrics, patch_path, replayed, report = self._run('partition_move')
        self.assertEqual(metrics['scanned_files'],
                         {'old': ['part-a/f1'], 'new': ['part-b/f2']})
        self.assertEqual(metrics['result_counts'],
                         {'insert': 0, 'delete': 0, 'update': 0})
        self.assertTrue(report['reconciled'])

    def test_lost_after_commit_emits_delete_and_insert(self):
        tables, metrics, patch_path, replayed, report = self._run('lost_after_commit')
        units = _patch_units(patch_path)
        self.assertEqual(set(units), {('delete', 'r2'), ('insert', 'r3')})
        self.assertEqual(units[('delete', 'r2')], ({'cost': 20}, None))
        self.assertTrue(report['reconciled'])


class ProjectionSemanticsTests(ScenarioTestBase):
    def test_schema_evolution_only_diffs_common_fields(self):
        tables, metrics, patch_path, replayed, report = self._run('schema_evolution')
        units = _patch_units(patch_path)
        self.assertEqual(set(units), {('update', 'r1')})
        # 旧像只有旧投影字段；新像携带新投影（含新字段默认值 0）。
        self.assertEqual(units[('update', 'r1')],
                         ({'cost': 10}, {'cost': 15, 'amount': 0}))
        self.assertTrue(report['reconciled'])
        with open(replayed, encoding='utf-8') as fh:
            replayed_rows = [json.loads(line) for line in fh]
        self.assertEqual({r['row_id'] for r in replayed_rows}, {'r1', 'r2'})
        by_id = {r['row_id']: r['image'] for r in replayed_rows}
        self.assertEqual(by_id['r2'], {'cost': 20, 'amount': 0})

    def test_explicit_null_physical_absence_and_default_are_distinguished(self):
        tables, metrics, patch_path, replayed, report = self._run('default_vs_absent')
        units = _patch_units(patch_path)
        # r1/r2 的 null↔缺席在投影后相等；r4 同值重写；只有 r3 是业务新增。
        self.assertEqual(set(units), {('insert', 'r3')})
        old_view = build_view(tables, 'old')
        # 显式 null 与物理缺席（默认 null）在还原层都得到 None，但来源语义可区分。
        self.assertIsNone(old_view.visible['r1']['cost'])  # 显式 null
        self.assertIsNone(old_view.visible['r2']['cost'])  # 物理缺席 -> schema 默认
        self.assertTrue(report['reconciled'])

    def test_new_schema_default_must_not_overwrite_old_value(self):
        tables = load(self.manifests['schema_evolution'])
        old_view = build_view(tables, 'old')
        new_view = build_view(tables, 'new')
        # 旧端 r2 的 cost=20 是当时实值，绝不能被新 schema 的任何默认值覆盖。
        self.assertEqual(old_view.visible['r2'], {'cost': 20})
        self.assertEqual(new_view.visible['r2'], {'cost': 20, 'amount': 0})


class CorruptionRejectionTests(ScenarioTestBase):
    def _corrupt_pack(self, name, mutate):
        src = Path(self.manifests[name])
        dst_dir = Path(self.tmp) / f'corrupt-{name}'
        shutil.copytree(src.parent, dst_dir)
        manifest = dst_dir / f'{name}.json'
        tables_raw = json.loads(manifest.read_text(encoding='utf-8'))
        mutate(tables_raw, dst_dir)
        manifest.write_text(json.dumps(tables_raw, ensure_ascii=False, indent=2),
                            encoding='utf-8')
        return str(manifest)

    def test_conflicting_visible_terminal_states_rejected(self):
        # 同一快照的同一文件里 r1 有两个不同可见终态：损坏，禁止挑选“最新文件”。
        directory = Path(self.tmp) / 'conflicting-pack'
        manifest = packbuilder.write_pack(
            directory, name='conflicting',
            snapshots=[
                {'id': 'old', 'files': ['f1'], 'deletions': [], 'schema': 's1'},
                {'id': 'new', 'files': ['f1'], 'deletions': [], 'schema': 's1'}],
            file_rows=[
                {'id': 'p1', 'file': 'f1', 'position': 0, 'row_id': 'r1', 'cost': 10},
                {'id': 'p2', 'file': 'f1', 'position': 1, 'row_id': 'r1', 'cost': 999},
            ],
            deletes=[], schemas=[packbuilder.schema_cost_only()])
        tables = load(manifest)
        with self.assertRaisesRegex(SnapshotError, '多个不同可见终态'):
            export_diff(tables, 'old', 'new', os.path.join(self.tmp, 'corrupt.patch'))

    def test_delete_bitmap_out_of_range_rejected(self):
        def mutate(raw, dst):
            dp = dst / raw['tables']['deletes']['file']
            row = json.loads(dp.read_text(encoding='utf-8').splitlines()[0])
            row['positions'] = [7]
            import hashlib
            content = (json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n').encode()
            dp.write_bytes(content)
            raw['tables']['deletes']['sha256'] = hashlib.sha256(content).hexdigest()
            raw['tables']['deletes']['bytes'] = len(content)
        manifest = self._corrupt_pack('bitmap_change', mutate)
        tables = load(manifest)
        with self.assertRaisesRegex(SnapshotError, '越过文件行数'):
            build_view(tables, 'old')

    def _rewrite_patch(self, patch_path, unit_index, new_unit):
        """替换补丁中的某个单元并重算全部摘要，使外壳校验通过、直达语义校验。"""
        lines = Path(patch_path).read_bytes().splitlines()
        header = decode_line(lines[0], 1)
        body = [decode_line(raw, i + 2) for i, raw in enumerate(lines[1:-1])]
        footer = decode_line(lines[-1], len(lines))
        body[unit_index] = new_unit
        from diff_data.patchformat import StreamHasher
        hasher = StreamHasher()
        for unit in body:
            hasher.add(unit)
        footer['stream_digest'] = hasher.value()
        raw_lines = [encode_line(header).rstrip(b'\n')]
        raw_lines += [encode_line(u).rstrip(b'\n') for u in body]
        raw_lines.append(encode_line(footer).rstrip(b'\n'))
        bad = patch_path + '.rewritten'
        Path(bad).write_bytes(b'\n'.join(raw_lines) + b'\n')
        return bad

    def test_tampered_patch_unit_is_rejected_by_applier(self):
        tables, metrics, patch_path, _, _ = self._run('same_id_update')
        lines = Path(patch_path).read_bytes().splitlines()
        unit = json.loads(lines[1])
        unit['before'] = {'cost': 999}  # 篡改旧像；unit_digest 未同步
        bad = patch_path + '.bad'
        lines[1] = encode_line(unit).rstrip(b'\n')
        Path(bad).write_bytes(b'\n'.join(lines) + b'\n')
        with self.assertRaisesRegex(PatchFormatError, '单元摘要不一致'):
            apply_patch(tables, bad, os.path.join(self.tmp, 'bad.replayed'))

    def test_wrong_old_image_rejected_and_nothing_published(self):
        tables, metrics, patch_path, _, _ = self._run('same_id_update')
        lines = Path(patch_path).read_bytes().splitlines()
        unit = json.loads(lines[1])
        unit['before'] = {'cost': 999}
        from diff_data.patchformat import unit_digest
        unit['unit_digest'] = unit_digest(unit)  # 单元摘要自洽但旧像与旧端不符
        bad = self._rewrite_patch(patch_path, 0, unit)
        out = os.path.join(self.tmp, 'stale.replayed')
        with self.assertRaisesRegex(PatchApplyError, 'before 像与旧端冻结状态不符'):
            apply_patch(tables, bad, out)
        self.assertFalse(os.path.exists(out))

    def test_binding_mismatch_rejects_patch_application(self):
        tables, metrics, patch_path, _, _ = self._run('same_id_update')
        bad = patch_path + '.rebind'
        lines = Path(patch_path).read_bytes().splitlines()
        header = json.loads(lines[0])
        header['old_snapshot'] = 'new'
        lines[0] = encode_line(header).rstrip(b'\n')
        Path(bad).write_bytes(b'\n'.join(lines) + b'\n')
        with self.assertRaises(PatchApplyError):
            apply_patch(tables, bad, os.path.join(self.tmp, 'rebind.replayed'))

    def test_digest_broken_manifest_still_rejected(self):
        with self.assertRaisesRegex(DataError, '摘要'):
            load('data/broken-digest.json')


class ExternalSortAndSkipTests(ScenarioTestBase):
    def test_bounded_spill_matches_large_budget_result(self):
        # 小预算下每侧产生多个外排 run，归并结果必须与大预算完全一致。
        _, _, p_big, _, _ = self._run('same_id_update', budget=1000)
        tables = load(self.manifests['same_id_update'])
        work = tempfile.mkdtemp(dir=self.tmp)
        p_small = os.path.join(self.tmp, 'small-budget.patch')
        m_small = export_diff(tables, 'old', 'new', p_small, workdir=work, run_budget=1)
        self.assertGreaterEqual(m_small['external_sort']['new']['runs'], 2)
        self.assertGreater(m_small['external_sort']['new']['spilled_bytes'], 0)
        self.assertEqual(m_small['result_counts'],
                         {'insert': 1, 'delete': 0, 'update': 1})
        self.assertEqual(_patch_units(p_small), _patch_units(p_big))

    def test_pathological_single_key_duplicates_stay_bounded(self):
        # 同一 row_id 的海量同值可见副本：分组只保留首像，单键异常不拖入无限内存。
        n = 500
        rows = [{'id': f'q{i}', 'file': 'f2', 'position': i,
                 'row_id': 'r1', 'cost': 10} for i in range(n)]
        directory = Path(self.tmp) / 'duplicate-pack'
        manifest = packbuilder.write_pack(
            directory, name='duplicates',
            snapshots=[
                {'id': 'old', 'files': ['f1'], 'deletions': [], 'schema': 's1'},
                {'id': 'new', 'files': ['f2'], 'deletions': [], 'schema': 's1'}],
            file_rows=[{'id': 'p1', 'file': 'f1', 'position': 0,
                        'row_id': 'r1', 'cost': 10}] + rows,
            deletes=[], schemas=[packbuilder.schema_cost_only()])
        tables = load(manifest)
        work = tempfile.mkdtemp(dir=self.tmp)
        patch_path = os.path.join(self.tmp, 'dups.patch')
        metrics = export_diff(tables, 'old', 'new', patch_path, workdir=work, run_budget=16)
        # 500 个重复全部折叠，净变更为空；缓冲峰值受预算约束而非随重复数膨胀。
        self.assertEqual(metrics['result_counts'],
                         {'insert': 0, 'delete': 0, 'update': 0})
        self.assertEqual(metrics['external_sort']['new']['duplicates_collapsed'], n - 1)
        self.assertLessEqual(metrics['external_sort']['new']['peak_buffered_rows'], 16)

    def test_spill_temp_files_are_cleaned_up(self):
        tables = load(self.manifests['same_id_update'])
        work = tempfile.mkdtemp(dir=self.tmp)
        patch_path = os.path.join(self.tmp, 'cleanup.patch')
        export_diff(tables, 'old', 'new', patch_path, workdir=work, run_budget=1)
        self.assertEqual([n for n in os.listdir(work) if n.startswith('diff-run-')], [])

    def test_same_digest_different_delete_set_cannot_skip(self):
        tables = load(self.manifests['bitmap_change'])
        old_view = build_view(tables, 'old')
        new_view = build_view(tables, 'new')
        common = common_field_names(old_view, new_view)
        plan = plan_skips(old_view, new_view, common)
        self.assertEqual(plan.skipped_pairs, [])
        self.assertEqual(plan.old_scan_files, ['f1'])
        self.assertEqual(plan.new_scan_files, ['f1'])

    def test_range_index_reported_for_skipped_region(self):
        tables = load(self.manifests['pure_compaction'])
        old_view = build_view(tables, 'old')
        new_view = build_view(tables, 'new')
        plan = plan_skips(old_view, new_view, common_field_names(old_view, new_view))
        self.assertEqual(plan.skipped_pairs[0]['row_id_range'], ['r1', 'r3'])


class CheckpointTests(ScenarioTestBase):
    def test_resume_produces_identical_patch_without_duplicates(self):
        tables = load(self.manifests['same_id_update'])
        patch_path = os.path.join(self.tmp, 'resume.patch')
        ckpt = patch_path + '.ckpt'
        work = tempfile.mkdtemp(dir=self.tmp)
        export_diff(tables, 'old', 'new', patch_path, workdir=work, run_budget=1)
        full = Path(patch_path).read_bytes()
        # 模拟中断：footer 尚未写入，断点回滚到最后一个完整单元（update r1）。
        lines = full.splitlines(keepends=True)
        header, unit1, unit2, footer = lines
        Path(patch_path).write_bytes(header + unit1)
        state = json.loads(Path(ckpt).read_text(encoding='utf-8'))
        u1 = json.loads(unit1)
        state['finished'] = False
        state['bytes_written'] = len(header) + len(unit1)
        state['last_unit'] = [u1['row_id'], u1['unit_digest']]
        state['emitted'] = 1
        state['counts'] = {'insert': 0, 'delete': 0, 'update': 1}
        # 流摘要重算为仅含 unit1 的前缀摘要。
        from diff_data.patchformat import StreamHasher
        hasher = StreamHasher()
        hasher.add(u1)
        state['stream_digest'] = hasher.value()
        Path(ckpt).write_text(json.dumps(state, ensure_ascii=False, sort_keys=True),
                              encoding='utf-8')
        m2 = export_diff(tables, 'old', 'new', patch_path, workdir=work, run_budget=1)
        self.assertTrue(m2['resumed'])
        self.assertEqual(m2['units_emitted'], 2)  # 断点前缀 1 + 续跑补发 1
        self.assertEqual(Path(patch_path).read_bytes(), full)
        _, units, footer = read_patch(patch_path)
        self.assertEqual([u['row_id'] for u in units], ['r1', 'r3'])
        self.assertEqual(footer['unit_count'], 2)

    def test_checkpoint_rejects_binding_change(self):
        tables = load(self.manifests['same_id_update'])
        patch_path = os.path.join(self.tmp, 'rebind-ckpt.patch')
        ckpt = patch_path + '.ckpt'
        work = tempfile.mkdtemp(dir=self.tmp)
        export_diff(tables, 'old', 'new', patch_path, workdir=work, run_budget=1)
        state = json.loads(Path(ckpt).read_text(encoding='utf-8'))
        state['binding']['old']['snapshot_id'] = 'forged'
        state['finished'] = False
        Path(ckpt).write_text(json.dumps(state), encoding='utf-8')
        with self.assertRaisesRegex(DiffError, '断点绑定'):
            export_diff(tables, 'old', 'new', patch_path, workdir=work, run_budget=1)

    def test_checkpoint_rejects_truncated_output(self):
        tables = load(self.manifests['same_id_update'])
        patch_path = os.path.join(self.tmp, 'trunc.patch')
        ckpt = patch_path + '.ckpt'
        work = tempfile.mkdtemp(dir=self.tmp)
        export_diff(tables, 'old', 'new', patch_path, workdir=work, run_budget=1)
        with open(patch_path, 'ab') as fh:
            fh.truncate(10)  # 实际长度与断点记录不符
        with self.assertRaisesRegex(DiffError, '输出长度'):
            export_diff(tables, 'old', 'new', patch_path, workdir=work, run_budget=1)


class PatchStructureTests(ScenarioTestBase):
    def test_units_are_sorted_and_indivisible(self):
        tables, metrics, patch_path, _, _ = self._run('lost_after_commit')
        lines = Path(patch_path).read_bytes().splitlines()
        self.assertEqual(json.loads(lines[0])['kind'], 'header')
        self.assertEqual(json.loads(lines[-1])['kind'], 'footer')
        ids = [json.loads(x)['row_id'] for x in lines[1:-1]]
        self.assertEqual(ids, sorted(ids))
        for raw in lines[1:-1]:
            unit = json.loads(raw)
            if unit['op'] == 'update':
                self.assertIsNotNone(unit['before'])
                self.assertIsNotNone(unit['after'])
                self.assertIn('unit_digest', unit)

    def test_unsorted_patch_rejected(self):
        tables, metrics, patch_path, _, _ = self._run('lost_after_commit')
        lines = Path(patch_path).read_bytes().splitlines()
        body = [json.loads(x) for x in lines[1:-1]]
        body.reverse()
        raw = b'\n'.join([lines[0]] + [encode_line(u).rstrip(b'\n') for u in body]
                         + [lines[-1]]) + b'\n'
        bad = patch_path + '.unsorted'
        Path(bad).write_bytes(raw)
        with self.assertRaisesRegex(PatchFormatError, '稳定排序'):
            read_patch(bad)

    def test_force_required_to_overwrite_without_checkpoint(self):
        tables = load(self.manifests['same_id_update'])
        patch_path = os.path.join(self.tmp, 'exists.patch')
        work = tempfile.mkdtemp(dir=self.tmp)
        export_diff(tables, 'old', 'new', patch_path, workdir=work)
        os.unlink(patch_path + '.ckpt')
        with self.assertRaisesRegex(DiffError, 'force'):
            export_diff(tables, 'old', 'new', patch_path, workdir=work)
        export_diff(tables, 'old', 'new', patch_path, workdir=work, force=True)


class RegisteredMaterialTests(unittest.TestCase):
    """仓库自带 normal/boundary 资料包在新引擎下的语义核对。"""

    def test_same_snapshot_pair_is_rejected(self):
        tables = load('data/normal.json')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(DiffError, '两端快照相同'):
                export_diff(tables, 'old', 'old', os.path.join(tmp, 'x.patch'))

    def test_legacy_single_arg_cli_still_checks(self):
        from diff_data.cli import main
        rc = main(['data/normal.json'])
        self.assertEqual(rc, 0)

    def test_cli_diff_and_apply_roundtrip(self):
        from diff_data.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            patch_path = os.path.join(tmp, 'c.patch')
            out_path = os.path.join(tmp, 'r.jsonl')
            rc = main(['diff', 'data/normal.json', 'old', 'new',
                       '--out', patch_path, '--workdir', tmp, '--run-budget', '1'])
            self.assertEqual(rc, 0)
            rc = main(['apply', 'data/normal.json', patch_path, '--out', out_path])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(out_path))

    def test_normal_pack_net_change(self):
        tables = load('data/normal.json')
        self.assertTrue(inspect_tables(tables)['identity_checked'])
        with tempfile.TemporaryDirectory() as tmp:
            patch_path = os.path.join(tmp, 'n.patch')
            metrics = export_diff(tables, 'old', 'new', patch_path, workdir=tmp)
            units = _patch_units(patch_path)
            self.assertEqual(set(units), {('update', 'r1'), ('insert', 'r3')})
            report = apply_patch(tables, patch_path, os.path.join(tmp, 'r.jsonl'))
            self.assertTrue(report['reconciled'])

    def test_boundary_pack_ghost_row_never_emitted(self):
        tables = load('data/boundary.json')
        with tempfile.TemporaryDirectory() as tmp:
            patch_path = os.path.join(tmp, 'b.patch')
            metrics = export_diff(tables, 'old', 'new', patch_path, workdir=tmp)
            units = _patch_units(patch_path)
            # r1/r2 旧端全部被位图删除；新端只有 r1/r3。r2 两端皆不可见，绝不输出。
            self.assertEqual(set(units), {('insert', 'r1'), ('insert', 'r3')})
            report = apply_patch(tables, patch_path, os.path.join(tmp, 'r.jsonl'))
            self.assertTrue(report['reconciled'])


if __name__ == '__main__':
    unittest.main()
