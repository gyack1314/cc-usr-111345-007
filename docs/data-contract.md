# 业务资料约定

file_rows.position为文件内0起始物理行位，row_id为跨文件稳定业务身份，file_rows.id只是资料记录ID。
snapshot的deletions引用删除位图ID，位图按其所属文件的“删除前原行行位”生效，不以目录中全部文件决定可见性。

每行id在该表内唯一，文件顺序不代表业务事件顺序。清单schema=1，各表登记文件名、字节长度、SHA256、行数及完整字段集合，
未知版本或内容变化应重新登记。清单可选登记`optional`字段名集合：列入的字段允许在JSONL中物理缺席，
由快照还原层按该快照schema的字段默认值补齐；显式null（键存在、值为null）与物理缺席是两种不同来源，
投影后可能同值，但语义不混同。业务身份、金额、版本及测量数值均为本地资料，不代表任何现实机构的经营数据。

- snapshots：id、files、deletions、schema。
- file_rows：id、file、position、row_id、cost（cost可登记为optional）。
- deletes：id、file、positions（多位图并集，重复行位幂等；越过文件行数即损坏）。
- schema：id、fields；每个field含field_id、name、kind、default，field_id与name均不可重复。

## 快照还原与净变更口径

1. 每个快照先按自己的deletions过滤物理行，再按自己的schema投影：键存在取显式值（含null），缺席填默认值。
2. 同一快照内同一row_id出现多个不同可见终态是资料损坏，必须报错，不能选择“最新文件”；同值重复副本折叠计数。
3. 两端快照各自还原后按row_id归并关联：仅旧端可见→delete，仅新端可见→insert，两端可见且公共字段逻辑值变化→update，
   公共字段无变化（含纯重写、搬家、单边schema新字段）不输出；两端均不可见的中途行不出现。
4. 文件内容摘要只绑定position、row_id与业务字段，不含file_rows.id与容器文件名；文件级跳读还要求删除位集相等、
   公共字段投影签名相等。删除位图绝不允许跨文件直接相减。
5. schema演化时只比较两端公共字段；新端单边字段不构成补丁变化，旧端实值不被新schema默认值覆盖。

normal与boundary是相互独立的资料包，不能直接拼接重复id。data/scenarios下的七个场景包同理各自独立。
引用字段按本项目domain.py的REFERENCES登记。读取检查只检查完整性、身份及这些直接引用，
快照可见性、损坏判定与净变更由snapshot.py、diff.py与patch.py负责。
