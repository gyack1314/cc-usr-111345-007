"""本地检查、净变更导出与补丁重放的命令行入口。

兼容旧用法：
  python -m diff_data.cli <manifest>            # 只做资料完整性检查
新增子命令：
  python -m diff_data.cli check <manifest>
  python -m diff_data.cli diff <manifest> <old> <new> --out <patch.jsonl>
  python -m diff_data.cli apply <manifest> <patch.jsonl> --out <replayed.jsonl>
"""
import argparse
import json
import sys

from .io import load, DataError
from .domain import inspect_tables
from .diff import export_diff, DiffError
from .patch import apply_patch, PatchApplyError


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _cmd_check(args):
    _print(inspect_tables(load(args.manifest)))
    return 0


def _cmd_diff(args):
    metrics = export_diff(
        load(args.manifest), args.old_snapshot, args.new_snapshot,
        args.out, workdir=args.workdir, run_budget=args.run_budget,
        force=args.force, checkpoint_path=args.checkpoint)
    _print(metrics)
    return 0


def _cmd_apply(args):
    report = apply_patch(
        load(args.manifest), args.patch, args.out,
        expected_old=args.expect_old, expected_new=args.expect_new)
    _print(report)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description='湖表快照净变更导出与补丁重放')
    sub = parser.add_subparsers(dest='command')

    p_check = sub.add_parser('check', help='检查资料身份与完整性')
    p_check.add_argument('manifest')
    p_check.set_defaults(func=_cmd_check)

    p_diff = sub.add_parser('diff', help='比较两端冻结快照并导出净变更补丁')
    p_diff.add_argument('manifest')
    p_diff.add_argument('old_snapshot')
    p_diff.add_argument('new_snapshot')
    p_diff.add_argument('--out', required=True, help='补丁输出路径（JSONL）')
    p_diff.add_argument('--workdir', default=None, help='外排临时目录，缺省自动创建')
    p_diff.add_argument('--run-budget', type=int, default=1000,
                        help='单 run 内存缓冲行数，满即落盘，默认 1000')
    p_diff.add_argument('--checkpoint', default=None, help='断点文件路径，缺省为 <out>.ckpt')
    p_diff.add_argument('--force', action='store_true', help='覆盖无断点的既有输出')
    p_diff.set_defaults(func=_cmd_diff)

    p_apply = sub.add_parser('apply', help='严格校验旧像并重放补丁')
    p_apply.add_argument('manifest')
    p_apply.add_argument('patch')
    p_apply.add_argument('--out', required=True, help='重放状态输出路径（JSONL）')
    p_apply.add_argument('--expect-old', default=None)
    p_apply.add_argument('--expect-new', default=None)
    p_apply.set_defaults(func=_cmd_apply)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 兼容旧调用：单个清单参数等价于 check 子命令。
    if argv and not argv[0].startswith('-') and argv[0] not in ('check', 'diff', 'apply') \
            and len(argv) == 1:
        argv = ['check'] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, 'func', None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except (DataError, DiffError, PatchApplyError, ValueError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
