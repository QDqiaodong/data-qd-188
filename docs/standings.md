# 离线赛会对账与晋级核定（go-record-standings）

棋赛结束后，编排表上的轮次、台号和黑白选手没有争议，但几份离线记录器漏记了台号。
本命令按档案员的交接清单核对一批 `go-record-archive` 状态目录：枚举所有说得通的
台次↔盘号对应关系，在每套关系下独立核定名次，给出哪些选手必然晋级、必然无缘、
以及必须等待人工确认的结论。全程只读归档目录，不修改任何状态；不根据文件名、
输入顺序或棋盘异同猜测身份。棋规与面积模型完全沿用 v1
（见 [contract.md](contract.md)），不引入新的胜负规则。

```sh
go-record-standings --source MANIFEST.json --output REPORT.json
```

成功时原子新建报告（已有目标拒绝覆盖），stdout 打印简要 JSON，退出 0；
输入错误与写入失败退出 2，诊断 JSON 打到 stderr，不落报告：

```json
{"error": {"code": "no_complete_assignment", "message": "...", "tables": ["T1", "T2"], "candidates": ["X"]}}
```

## 清单字段

清单是单个 JSON 对象，字段严格限定（重复键、多余键、NaN 等均拒绝）：

| 字段 | 约束 |
| --- | --- |
| `players` | 2–64 个唯一选手 ID（`[A-Za-z0-9_:-]{1,64}`） |
| `qualify_count` | 整数，1 至选手数减一 |
| `lot_order` | 赛前抽签优先序：包含全部选手且无重复，靠前者优先 |
| `tables` | 1–256 台 |
| `archives` | 归档清单，条数必须等于台数 |

每台：

| 字段 | 约束 |
| --- | --- |
| `table_id` | 全清单唯一标识符 |
| `round` | 整数 1–16；轮次允许不连续，缺席不给分也不虚构对手 |
| `black` / `white` | 两个不同的已登记选手；每名选手每轮至多上场一次 |
| `candidates` | 非空、无重复的候选 `game_id` 列表，必须都在 `archives` 中 |

每份归档：`game_id`（全清单唯一）与 `state_dir`（相对目录按清单所在目录解释）。
候选超过一份的台次最多 10 个，这是本次人工交接筛查的范围。

归档条目允许增加可选的 `appeal_dir`（相对路径同样按清单所在目录解释），指向一份
`go-record-appeal` 终局死子申诉案卷。不填时维持原输出和行为，报告字节级不变；
填写后按 [终局死子申诉案卷](appeal.md) 接入复核。

## 申诉案卷接入

配置 `appeal_dir` 后以**只读**方式打开案卷，并用归档核对案卷的开案证据：盘号、
末手回执 ID、末手封存位置、开案盘面、盘宽与贴目必须逐项一致；同时确认案卷当前
状态与它所固化的裁定互相一致（`verify_state`：轮次连续、裁定 ID 与内容相符、
裁定可由记录的证据独立复算）。以下均为可定位的输入错误（退出 2），**不能改用
旧比分**：

| code | 含义 |
| --- | --- |
| `appeal_unreadable` | 案卷不可读或模式无法识别（含 `game_id`、`appeal_dir`） |
| `appeal_game_id_mismatch` | 案卷内盘号与清单声明不符 |
| `appeal_evidence_mismatch` | 末手回执、封存位置、开案盘面、盘宽或贴目与归档不符 |
| `appeal_state_inconsistent` | 当前状态与对应裁定不一致，或裁定无法独立复算 |
| `appeal_archive_not_final` | 引用的归档尚无终局证据（未封到声明末手） |

一次读取同一案卷时，状态、轮次和胜负来自同一份已提交状态；不要求多份案卷同时
取一个全局快照，也不修改案卷或归档。

- 案卷**未结案**（缺票、票齐分歧、一致待结案）不产出正式胜负：若不存在其他未最终
  封存的归档，报告状态为 `awaiting_ruling`，列出待裁盘号、当前轮次、涉及台号与
  各台实际可用候选，不给积分、名次、晋级结论。
- 案卷**已结案**时只采用当前轮裁定的 `winner_after`（`B`/`W`/`draw`），原归档
  `final.area` 不再作为该盘成绩；见证的台次→盘号条目同时给出原末手回执
  `receipt_id` 与采用的裁定轮次 `appeal_round`，可反查案卷 `history --round`。
- 重开后的案卷不再让上一轮裁定代表当前胜负；历史轮次仍可经案卷独立复算。

原结构检查、归档终局证据检查与完整对应关系检查（Hall/完美匹配）仍先于一切状态
结论执行：无完整对应关系时照常报 `no_complete_assignment`，不会被
`awaiting_ruling` 掩盖。

## 只认已经完成的归档

逐份读取 `state_dir` 下现有 `go-record-archive` 状态，核对声明盘号与实际盘号一致。
只使用封到声明末手的 `final.area.winner`（`B`、`W` 或 `draw`），并要求：

- `final.sealed_ply` 与当前封存点都等于声明总手数（一致的末手封存位置）；
- 回执中恰有一条本盘、封存到末手的回执，其 `receipt_id` 作为证据；
- 胜负值有效且面积状态为 `after_two_passes`。

尚未最终封存的正常状态（`final` 为 `null`）识别为未完成，但不得用当前盘面、
双停预览或部分封存的面积代替结果。归档不可读、格式错误、盘号不符，或所谓最终
结果缺少一致的末手封存位置、回执证据或有效胜负值，均属输入错误。不要求从已
清理的原始棋步重建或验证归档。

## 对应关系与可用候选

一个可行对应关系必须让每台恰好采用一份自己的候选归档，且每份归档在全清单中
恰好用一次。不能因单台有候选就认为全批可对上，也不能把各台各自可用的候选任意
拼接。没有任何完整对应关系时按输入错误处理（`no_complete_assignment`，诊断给出
一组台多候选少的定位信息）；这项检查不因有盘尚未封存而跳过。

报告的每台 `usable_candidates` 列出原候选中实际出现在至少一个完整对应关系里的
盘号，便于档案员优先追查有效线索；同胜负的盘号不合并。

## 名次与晋级口径

归档中的黑白胜负归给该台编排表上的对应颜色选手：胜 2、和 1、负 0。每套可行
对应关系都独立计算一次最终积分；同分时比较该选手每次上场所遇对手的**本套**
最终积分之和（同一对手遇到两次算两次），仍同分按抽签优先序。积分和对手分均
按降序，因此每套关系都有从 1 开始的唯一名次，前 `qualify_count` 名晋级。
对手分不取自另一套关系，也不把各人独立的积分上下界拼成一张不可能的榜。

对每名选手给出所有对应关系下可达的 `best_rank` / `worst_rank`，以及三类之一：

| `category` | 含义 |
| --- | --- |
| `certain` | 确定晋级（最差名次也不出晋级线） |
| `undetermined` | 可能晋级但未定（需人工确认对应关系） |
| `out` | 确定无缘（最好名次也够不到晋级线） |

`qualified` 只在所有对应关系下晋级成员完全相同才对外给出（按选手 ID 排序，
不要求名次也不变）；否则明确为 `null`。

每个最好、最差名次均引用一份可复核的见证（`best_witness` / `worst_witness` 为
`witnesses` 下标）：同一个完整的台次→盘号→末手回执 ID 对应关系，和按它算出的
全体积分、对手分和名次。允许共享同一见证，不要求列举所有对应关系。若有多份
等价见证可任选，但只打乱清单或候选列表等不表示优先级的输入顺序时，报告及见证
的选择保持字节级稳定；`lot_order` 本身不被重排。

## 报告字段

```json
{
  "schema": "go-record-standings/1",
  "status": "complete | awaiting_final | awaiting_ruling",
  "qualify_count": 1,
  "awaiting_game_ids": [],
  "awaiting_ruling": [{"game_id": "...", "round": 2, "table_ids": ["T1"]}],
  "tables": [{"table_id", "round", "black", "white", "usable_candidates": ["..."]}],
  "players": [{"player_id", "category", "best_rank", "best_witness",
               "worst_rank", "worst_witness"}],
  "qualified": ["..."] | null,
  "witnesses": [{"id", "assignment": [{"table_id", "game_id", "receipt_id",
                                        "appeal_round"?: 1}],
                 "standings": [{"rank", "player_id", "score", "opponent_score"}]}]
}
```

若存在完整对应关系但还有盘未最终封存，出 `awaiting_final` 报告：列出未完成的
`awaiting_game_ids` 和各台 `usable_candidates`，`players` / `qualified` /
`witnesses` 均为 `null`，不给分、不列晋级结论。没有未最终封存的归档、但有案卷未
结案时，出 `awaiting_ruling` 报告：`awaiting_ruling` 逐项给出待裁盘号、轮次与涉及
台号，其余结论字段同样为 `null`。全部归档封存且全部案卷结案后才出 `complete`。
`appeal_round` 只在该盘配置了案卷时出现，无案卷清单的报告字段保持原样。

## 诊断码一览

退出 2，诊断 JSON 打到 stderr，不落报告：

| code | 含义 |
| --- | --- |
| `invalid_structure` | 清单结构/类型/取值错误（含 `table_index`、`table_id`、`round`、`player_id` 等定位信息） |
| `archive_unreadable` | 归档不可读或格式错误（含 `game_id`、`state_dir`） |
| `game_id_mismatch` | 状态目录里的实际盘号与清单声明不符 |
| `final_inconsistent` | 所谓最终结果缺少一致的末手封存位置、回执证据或有效胜负值 |
| `appeal_unreadable` | 申诉案卷不可读或模式无法识别（含 `game_id`、`appeal_dir`） |
| `appeal_game_id_mismatch` | 案卷内盘号与清单声明不符 |
| `appeal_evidence_mismatch` | 案卷开案证据（末手回执/封存位置/盘面/盘宽/贴目）与归档不符 |
| `appeal_state_inconsistent` | 案卷当前状态与对应裁定不一致或裁定不可复算 |
| `appeal_archive_not_final` | 案卷引用的归档尚无终局证据 |
| `no_complete_assignment` | 全批无法一一对应（给出违约的台号集合与候选集合） |
| `output_exists` | 输出目标已存在（原子新建，不覆盖） |
| `io_error` | 源文件或输出路径读写失败 |

## 可复现示例

一轮两台：T1（A 黑、B 白）、T2（C 黑、D 白），两台候选都是 X 或 Y；
X 是黑胜归档，Y 是白胜归档；只晋级一人，抽签序 A、C、D、B。

```sh
# 两盘演示棋（3 路盘，komi 0）：X 黑胜、Y 白胜
go-record-archive init --state games/X --spec spec-x.json      # spec-x: total_moves 3
go-record-archive receive --state games/X --packet packet-x.json  # B[0,0]、W停、B停
go-record-archive seal --state games/X --target 3 --credential <view签发的hex>
go-record-archive init --state games/Y --spec spec-y.json      # spec-y: total_moves 4
go-record-archive receive --state games/Y --packet packet-y.json  # B停、W[0,0]、B停、W停
go-record-archive seal --state games/Y --target 4 --credential <view签发的hex>

go-record-standings --source manifest.json --output report.json
```

manifest.json：

```json
{"players":["A","B","C","D"],"qualify_count":1,"lot_order":["A","C","D","B"],
 "tables":[{"table_id":"T1","round":1,"black":"A","white":"B","candidates":["X","Y"]},
           {"table_id":"T2","round":1,"black":"C","white":"D","candidates":["X","Y"]}],
 "archives":[{"game_id":"X","state_dir":"games/X"},{"game_id":"Y","state_dir":"games/Y"}]}
```

stdout：`{"qualified": null, "status": "complete", "witnesses": 2}`。两套对应关系
（T1=X/T2=Y 与 T1=Y/T2=X）晋级者分别是 A 和 C，故 `qualified` 为 `null`；
A、C 的最好名次都是 1（各有一套见证），B、D `category` 为 `out`（同一个 Y 不能
把 D 推到第一：A 同分且抽签序优先）。删去 T1 对 Y 的候选后，T2 只能用 Y，
`qualified` 确定为 `["A"]`。

## 边界

不解析 SGF、不联网、不做死活裁定；不从已清理的原始棋步重建归档；不修改任何
归档目录或申诉案卷。`go-record` 与 `go-record-archive` 的接口和行为不变；
不配置 `appeal_dir` 的清单输出与历史版本字节级一致。
