# 离线“下一手”练习集分区计划（go-record-dataset）

教研组要从已有棋谱里挑“下一手”练习，建训练集与盲评集。同一局面常被旋转、镜像或
转载到不同讲义系列；训练见过的局面不能换个方向当作新的盲评题，同一来源系列也不能
两边各发一部分。上期已发布的练习即使本期不再收录，也不能当作从未发布。

本命令读取一批 `go-record` 原始输入 JSON 和管理员提供的已发布历史清单，只输出
供数据管理员复核的分区计划（`ready` 或 `quota_unreachable`）。它**不向任何外部系统
发布数据**，不修改棋谱、归档或历史，只原子新建本地报告。棋规完全沿用 v1
（见 [contract.md](contract.md)），仅用标准库。

```sh
go-record-dataset --source MANIFEST.json --output REPORT.json
```

成功退出 0 并原子新建报告（已有目标拒绝覆盖），stdout 打印简要 JSON；输入错误退出
2，诊断 JSON 打到 stderr，不落正式报告：

```json
{"error": {"code": "record_unreadable", "message": "...", "record_id": "R7", "path": "records/r7.json"}}
```

## 清单字段

清单是单个 JSON 对象，字段严格限定，重复键、多余键、NaN 等均拒绝；清单和其中引用
的棋谱都按仓库既有严格 JSON 规则读取。

| 字段 | 约束 |
| --- | --- |
| `batch_id` | 本次批次标识符，同 v1 身份规则（`[A-Za-z0-9_:-]{1,64}`） |
| `records` | 1–256 项，见下 |
| `samples` | 1–4096 项，见下 |
| `history` | 0–8192 项，管理员提供的**实际已发布**样本，见下 |
| `holdout_quota` | `{"B": 0..512, "W": 0..512}`，盲评所需的黑先、白先练习数 |

每条 `records`：

| 字段 | 约束 |
| --- | --- |
| `record_id` | 全清单唯一标识符 |
| `source_group` | 来源系列标识符；可复用，整批至多 40 个不同系列 |
| `path` | 相对路径，按清单所在目录解释，指向现有 `go-record` 原始输入 JSON（不是回放报告或归档） |

身份以清单标识为准，不从文件名推断。`samples` 每项为 `sample_id / record_id / ply`：
`sample_id` 唯一，`record_id` 必须已声明，同一 `(record_id, ply)` 组合不得重复，`ply`
从 1 开始且不超过该记录 `moves` 长度。

`history` 每项为 `release_id / sample_id / source_group / size / board / next_player /
split`：`(release_id, sample_id)` 唯一，`release_id` 不得等于本次 `batch_id`，盘宽 3–19，
`board` 为等宽行字符串（仅 `.`、`B`、`W`），行棋方为 `B`/`W`，分区为 `train`/`holdout`。
历史行不要求有原始棋谱，也**不会**被当作本期的新样本。

## 取样：取目标手之前的局面

对每个样本，特征取目标手**之前**的棋盘和此刻的行棋方，答案取该手在原方向的落子点，
并保留原步 ID：

- `ply = 1` 用记录的初始设置；`ply > 1` 用此前合法前缀回放出的盘面。
- 合法回放沿用 `service.process`：轮流、提子、禁自杀、简单劫（落子后盘面等于前一手
  之前盘面）、停手与双停。目标手必须是该回放判出的合法落子。
- **合法停手不作题**，记为排除（`reason: "pass"`）。
- **首个非法步及之后一律排除**，报告触发阻断的手号、步 ID 与原因（`blocking_move`：
  `ply / move_id / reason`），不允许跳过坏手再取样。非法步之前的合法取样点仍有效。
- 棋谱不可读、JSON 结构无效或盘面初始设置非法是**输入错误**（`record_unreadable` /
  `invalid_record`），不是业务排除。

排除项不进入任何分组或配额计算，但会列在报告的 `exclusions` 中供复核。

## 不能拆开的练习：局面键与并组

**局面键**由盘宽、行棋方和棋盘组成。棋盘在正方形的八种旋转/镜像中取字典序最小的行
字符串序列；**不交换黑白**，也不把答案、贴目、提子数或劫历史放入键。行棋方不同即
不同键，因此不同棋规上下文下的同图会被保守隔离，而改由另一方行棋的同图不会仅凭
棋盘相同并组。

对本期**有效**样本：只要**同来源系列**或**同局面键**相同，就必须在同一组；关系可
传递（A 与 C 同图、C 与 K 同来源 ⇒ A、C、K 同组），不能只在单个系列内去重或只合并
直接相同的图。组标识取组内最小的 `source_group`。同组内相同图的不同有效样本仍是
不同练习，按样本数计入配额；报告中的原落子点保持原方向，不随规范局面键被变换。

## 历史约束：曾发布的系列与局面

历史行用**同一口径**计算局面键（盘宽、行棋方、规范棋盘），给曾发布的来源系列和局面
键各自留下分区约束：

- 组内任何成员命中一个历史键（来源系列相同或局面键相同），约束就对**整组**生效。
- 只命中 `train` 的组只能进训练；只命中 `holdout` 的组只能进盲评。
- **同一组命中两侧**（既有训练约束又有盲评约束）时整组隔离，不得为了凑配额拆组、
  选择一侧或只排除直接命中的练习。
- 历史行本身不参与本期分组，也不是本期连接边；历史旧行不计入盲评配额。

报告的 `grouping.isolated` 指出被隔离组的 `matched_keys`（命中的键、其分区）以及可
反查的历史行身份（`release_id` / `sample_id` / `source_group` / `split`），管理员据此
判断为什么整组不能用。

## 配额与选择

在其余可用组中，按历史约束固定已有分区，其余整组选择 `train` 或 `holdout`。盲评配额
须**精确同时满足**黑先与白先两色，且计入历史约束已押在盲评侧的**本期**练习（不计历史
旧行）；不得重复使用样本或用另一颜色补齐。

若多个未锁组分配方案可行，按**未锁组组标识从小到大**比较，第一个分配不同的组以进入
盲评（`holdout`）的方案优先；输入列表顺序不参与打破平局。

- 两项精确配额同时可满足 → `ready`，输出两侧样本、各色数量、每个有效样本的原记录/
  手号/原方向局面及答案、规范局面键、所属组，以及取样排除与隔离原因。
- 不能同时满足 → `quota_unreachable`，`splits` 与 `publication` 均为 `null`，并给出
  诊断：有效样本数、组数、未锁组题量、隔离组、已锁盲评计数与目标。**不输出**可被
  误认成部分发布的训练或盲评清单。

## 报告字段

```json
{
  "schema": "go-record-dataset/1",
  "batch_id": "batch-2026-11",
  "status": "ready | quota_unreachable",
  "targets": {"B": 1, "W": 0},
  "exclusions": [{"sample_id", "record_id", "ply", "reason", "move_id"?, "blocking_move"?}],
  "grouping": {
    "groups": [{"group_id", "source_groups", "sample_ids", "counts",
                "constraint": "train|holdout|isolated|null", "position_keys"}],
    "isolated": [{"group_id", "source_groups", "sample_ids", "counts", "matched_keys"}]
  },
  "splits": {
    "train":   {"sample_count", "counts": {"B", "W"}, "samples": [SAMPLE]},
    "holdout": {"sample_count", "counts": {"B", "W"}, "samples": [SAMPLE]}
  },
  "publication": {"train": [ROW], "holdout": [ROW]},
  "diagnostic": {"valid_sample_count", "group_count", "free_groups",
                 "isolated_group_ids", "locked_holdout_counts",
                 "locked_holdout_sample_count", "locked_train_sample_count",
                 "remaining_targets"}
}
```

`SAMPLE`：`sample_id / record_id / source_group / ply / move_id / next_player / size /
board / answer / position_key {size, next_player, board} / group_id`。
`ROW` 与 `history` 行字段兼容：`release_id`（取本次 `batch_id`）、`sample_id`、
`source_group`、`size`、`board`、`next_player`、`split`，只列两侧实际选中的样本。
数据管理员**真的发布后**才把这些行带入下期；不可行时 `publication` 为 `null`，没有
新增发布记录。报告集合顺序稳定：重排清单中不含顺序语义的列表，结果与选择不变。

## 诊断码一览

退出 2，诊断 JSON 打到 stderr，不落报告：

| code | 含义 |
| --- | --- |
| `invalid_structure` | 清单结构/类型/取值错误，含 `records_index` / `samples_index` / `history_index` 及 `record_id` / `sample_id` 等定位 |
| `record_unreadable` | 引用的棋谱文件不可读或不是严格 JSON（含 `record_id`、`path`） |
| `invalid_record` | 棋谱结构无效、初始设置非法（含 `record_id`、`path`） |
| `output_exists` | 输出目标已存在（原子新建，不覆盖） |
| `io_error` | 源文件或输出路径读写失败 |

`quota_unreachable` 不是错误：命令退出 0 并正常写出诊断报告。

## 可复现示例

[`fixtures/dataset/`](../fixtures/dataset) 提供一组合成样例（作者自制，见
[来源说明](../fixtures/SOURCES.md)）：

- `manifest.json`：本期 A、C 各有黑先题，分别以规范图命中历史训练 U、历史盲评 V；
  来源系列 K 有两题分别命中 U、V，把 A、C 连成一组后整组隔离，故不可交付。
- `manifest-without-k.json`：只去掉 K 的两题、保留同样历史。A 回到训练、C 回到盲评，
  本批可交付，报告见 [`expected-report.json`](../fixtures/dataset/expected-report.json)。

```sh
go-record-dataset --source fixtures/dataset/manifest.json --output run/isolated.json
# stdout: {"batch_id":"batch-2026-11","status":"quota_unreachable", ...}

go-record-dataset --source fixtures/dataset/manifest-without-k.json --output run/ready.json
# stdout: {"batch_id":"batch-2026-11","status":"ready","holdout":{"B":1,"W":0},...}
```

第一条输出 `quota_unreachable`：`grouping.isolated` 的 A 组同时命中
`rel-2026-01`（train）与 `rel-2026-02`（holdout）。第二条输出 `ready`：`train` 为
`s-a`、`s-w`，`holdout` 为 `s-c`，`publication` 中 `release_id` 均为 `batch-2026-11`。
输出的演示路径已生成过则不能复用，复跑请换新文件名。

## 边界

不向外部系统发布、不联网、不解析 SGF、不做死活裁定；不修改棋谱、归档或历史；不把
历史旧行当作本期练习，也不从文件名推断身份。`go-record`、`go-record-archive` 与
`go-record-standings` 的接口和行为不变。
