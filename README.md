# 湖表两端快照的净变更导出与可重放补丁

本项目面向“结算仓只同步成本湖两版之间真正业务变化”的需求：比较两个**冻结快照的可见状态**，
而不是推送期间的提交日志。先插入后删除的临时行、纯压实重写、跨分区搬家都不会被算成业务变更。

## 核心语义

- 每个快照引用不可变的完整行文件、按文件生效的位置删除位图和字段投影目录（schema）。
- 行有跨文件稳定的 `row_id` 作为业务身份；文件路径与物理行位不是身份，只用于位图定位。
- 两端先**各自按自己的删除位图与自己的 schema 投影**还原可见状态，再按 `row_id` 关联：
  - 显式 `null` 与物理缺席按 schema 默认值区分（键存在即显式值，缺席才填默认值）；
  - 绝不用新 schema 的默认值覆盖旧端当时的实际值；
  - 绝不直接相减两个文件的删除位图；
  - 同一快照内同 `row_id` 出现多个不同可见终态即判损坏，不允许“选最新文件”。
- 结果分 `insert` / `delete` / `update`：update 的 before/after 在同一单元内成对出现、身份一致；
  无变化的重写、两端均不可见的中途行不输出。本迭代不做相等删除与部分 patch。

## 模块划分

| 模块 | 职责 |
| --- | --- |
| `diff_data/io.py` | 清单与 JSONL 读取、SHA256/字节长度绑定；支持登记的可选物理字段 |
| `diff_data/domain.py` | 表集合与直接引用检查 |
| `diff_data/snapshot.py` | 快照还原、投影、文件内容摘要、row_id 范围索引、损坏判定 |
| `diff_data/diff.py` | 跳读规划、有界外排、归并 join、净变更导出与断点续跑 |
| `diff_data/patchformat.py` | 补丁线上格式、单元/流摘要、原子写入器 |
| `diff_data/patch.py` | 独立补丁应用器：严格校验旧像并重放、与新端完整对账 |
| `diff_data/packbuilder.py` | 核对场景资料包构建器（纯压实、位图变化、改值、搬家、失联等） |

## 跳读与资源边界

只有同时满足以下三条，才把一对文件整区标记为已证明不变并跳读：

1. 文件内容摘要相等（摘要只含 `position`、`row_id` 与业务字段，不含注册表记录 ID 与容器名）；
2. 两端对该文件的删除位集相等——**文件相同但删除集不同不能略过**；
3. 两端公共字段的投影定义（类型与默认值）相等。

配不上对的文件全部逐行扫描；缺少统计时允许多读，不会漏变更。扫描行进有界外排：
单 run 缓冲达到预算即落盘，`heapq` 归并按 `row_id` 分组，同值重复 O(1) 折叠、异值冲突立即判损坏，
单键异常不会拖进无限内存。计量输出包含实际跳读区域、扫描文件、run 数、落盘字节与缓冲峰值。

## 补丁与断点

补丁为 JSONL 流：第 1 行 header（绑定两端快照摘要与投影签名、公共字段），
中间为按 `row_id` 稳定排序的不可拆分单元，末行 footer（单元计数与流摘要）。
断点文件绑定两端快照、投影、输出字节长度、流摘要与最后完整单元；
中断接续不重复、不遗漏，绑定或长度不符一律拒绝。应用器两阶段执行：
先完成全部旧像校验与新端完整对账，再原子发布重放产物；任何错误都不发布完整补丁。

## 命令行

```sh
# 资料完整性检查（兼容旧用法：python -m diff_data.cli <manifest>）
python -m diff_data.cli check data/normal.json

# 导出净变更补丁（--run-budget 控制外排内存边界）
python -m diff_data.cli diff data/normal.json old new --out /tmp/normal.patch --run-budget 1000

# 独立应用器重放并对账
python -m diff_data.cli apply data/normal.json /tmp/normal.patch --out /tmp/replayed.jsonl

# 生成七个核对场景资料包
python -m diff_data.packbuilder data/scenarios
```

## 测试

```sh
python -m unittest discover -s tests -v
python -m compileall -q diff_data tests
```

核对场景：纯压实（整区跳读、零变更）、删除位图变化（同文件不跳读）、同 ID 改值（含中途行不输出）、
跨分区搬家（全扫描零变更）、提交后失联（delete+insert）、schema 演化（只比公共字段）、
显式 null/物理缺席/默认值区分。另含损坏拒绝、补丁篡改、断点绑定与续跑、外排边界等 33 项测试。

容器内使用同样的检查入口：

```sh
docker build -t diff-data .
docker run --rm diff-data
```
