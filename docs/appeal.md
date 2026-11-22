# 离线终局死子申诉案卷（go-record-appeal）

赛会有些棋盘在双方停手后仍留着未实际提走的死子。档案员已按原盘面封存棋谱，
裁判复核可以改判正式胜负，但不得改写原档或回执。`go-record-appeal` 在可信本地
环境为每盘棋建立一份独立案卷：开案取证、两名裁判按轮次投票、一致后结案固化
不可改写的裁定、必要时重开下一轮，并在归档移入冷库后仍能复查历次裁定。

工具**不自行判断死活**，不改变落子、劫、贴目或面积规则；棋块按开案原盘面上的
同色四邻连通定义（`board.group`），结案时先从开案盘面同时移去全部确认棋块，再用
既有面积模型（`regions.area`）重新算整盘。移除不是走棋或提子，不额外加俘虏分。
裁判 ID 只用于归属意见，不要求登录、签名或权限系统。全程离线，仅用标准库。

```sh
go-record-appeal open   --case DIR --spec open.json
go-record-appeal vote   --case DIR --referee R1 --expected-version N --groups '[[r,c],...]'
go-record-appeal close  --case DIR --expected-version N
go-record-appeal reopen --case DIR --expected-version N
go-record-appeal view   --case DIR
go-record-appeal history --case DIR [--round K]
go-record-appeal recompute --case DIR --round K
```

成功结果与业务状态以 JSON 打印到 stdout（`sort_keys`、两空格缩进）。错误退出 2，
诊断 JSON 打到 stderr，可定位（含盘号、轮次、坐标等），不留下正式的部分输出，
也不覆盖已有案卷。

## open — 开案取证（不覆盖已有案卷）

open.json 字段严格限定：

| 字段 | 约束 |
| --- | --- |
| `case_ref` | 案卷标识符，同 v1 身份规则（`[A-Za-z0-9_:-]{1,64}`） |
| `game_id` | 盘号，必须与归档状态内的盘号一致 |
| `archive_dir` | 已有 `go-record-archive` 状态目录（开案时读取一次） |
| `referees` | 恰好两名**不同**的裁判 ID |

只接受合法双停、封到声明末手的归档：核对盘号、末手封存位置（`sealed_ply ==
total_moves`）、唯一末手回执及终局状态（`final.area.status=after_two_passes`、
胜负值有效），并用归档边界盘面**独立复算一次面积**与 `final.area` 逐项核对。
任一不符退出 2（`archive_evidence_invalid` / `game_id_mismatch` /
`archive_unreadable`），不产生案卷。

开案即固定该盘的末手回执 ID、原方向终局盘面、盘宽、贴目和原面积胜负，写入单个
`case.json`。此后录入、裁定、重开和历史核查都**不再读取原归档**：不复制整份归档、
不要求已被清理的棋步、不回写原归档。目标目录已有 `case.json` 时拒绝（`case_exists`）。

## vote — 提交或替换意见

```sh
go-record-appeal vote --case DIR --referee R1 --expected-version 1 --groups '[[2,2]]'
```

- `--groups` 是 JSON 数组，每项为**开案原盘面**上某块内任一棋子的坐标 `[row,col]`；
  两个不同代表坐标只要落在同一同色四邻连通块就代表相同棋块。
- 空点（`point_not_stone`）、盘外（`point_out_of_board`）、一份意见重复标同一块
  （`duplicate_group_in_opinion`）均拒绝**整次提交**，原票不变。
- 空数组 `[]` 是一份已投的空意见（主张不移除任何棋块），与缺票不同。
- 每名登记裁判每轮一份意见，结案前可替换；重提**相同**意见也算一次成功写入，
  版本同样推进一版。
- 非登记裁判（`unknown_referee`）、已结案轮次（`case_closed`）、预期版本不符
  （`version_conflict`）均拒绝且不产生变化。

案卷从第 1 轮、版本 1、未裁定开始。投票、结案、重开都必须携带预期版本；每次成功
推进一版，版本不符拒绝。

## close — 结案固化裁定

只有本轮两名裁判都已提交、且标的是**完全相同的棋块集合**才能结案；不能用两票的
交集、并集或上一轮的意见。缺票（`votes_incomplete`）或分歧（`votes_divergent`）
拒绝。一致但未结案不算正式结果。

结案动作：先从开案盘面同时移去全部确认棋块，再用现有面积模型重算整盘（原空域可能
合并），固化一份不可改写的裁定记录，包含：

- 盘号 `game_id` 与末手回执 `final_receipt_id`；
- 轮次 `round`、两位裁判 `referees`；
- 确定的棋块颜色和全部成员 `groups[].color/stones`，以及两位裁判各自的代表坐标；
- 裁定前后盘面 `board_before/board_after`、盘宽 `size`、贴目 `komi`；
- 裁定前后面积与胜负 `area_before/area_after`、`winner_before/winner_after`；
- 内容自证身份 `ruling_id`（对上述证据的规范哈希，不含可变状态）。

已结案轮次不能再改票或再次结案。

## reopen — 重开下一轮

只准重开已结案案卷（未结案重开报 `case_open`）。重开建立下一轮：轮次加一、版本
推进一版、票数清空，两人均须以**最初开案盘面**为依据重投。前一轮裁定只留作历史，
重开即不再代表当前胜负。

## view / history / recompute — 状态与历史核查

`view` 区分四种业务阶段（`phase`）：

| phase | 含义 |
| --- | --- |
| `awaiting_votes` | 缺票（至少一名裁判未提交） |
| `divergent` | 票齐但棋块集合分歧 |
| `agreed` | 已一致待结案（**无正式结果**） |
| `closed` | 已结案，`current_ruling_round/current_winner` 可用 |

`history` 不带 `--round` 时列各轮摘要（轮次、裁定 ID、末手回执、前后胜负）；
带轮次时交付该轮**足以独立复算**的完整证据，不借最新投票或最新胜负补足。
`recompute` 仅凭一份历史裁定重建前后盘面与面积胜负，归档已移走也能工作；
轮次不存在报 `round_not_found`。

## 持久化与并发

- 案卷是单个 `case.json`：每次变更的前置检查、版本推进、当前状态与历史裁定可见性
  在**一次原子替换**（临时文件 + fsync + `os.replace` + 目录 fsync）中提交；写到一半
  被强制终止后重开工具，只能看到整个操作发生前或之后的状态。
- 同一把文件锁（`DIR/lock`）下完成校验与提交；两个携带同一预期版本的写入不可能都
  成功，恰有一个成功，另一个得 `version_conflict`。不要求多份案卷之间的全局快照。
- 测试/演练可用环境变量 `GO_RECORD_APPEAL_FAULT=commit_before_replace` 或
  `commit_after_replace` 在提交边界注入受控中断。

## 诊断码一览

`invalid_structure`（结构/类型/取值）、`case_exists`、`case_missing`、`case_corrupt`、
`archive_unreadable`、`archive_evidence_invalid`、`game_id_mismatch`、
`unknown_referee`、`point_out_of_board`、`point_not_stone`、
`duplicate_group_in_opinion`、`version_conflict`、`votes_incomplete`、
`votes_divergent`、`case_closed`、`case_open`、`round_not_found`、
`ruling_inconsistent`。

## case.json 数据契约

案卷目录只含 `case.json`（正式状态）与 `lock`（文件锁，可重建）。`case.json` 为
严格 JSON（重复键、NaN 拒绝），顶层字段：

| 字段 | 含义 |
| --- | --- |
| `schema` | 固定 `go-record-appeal/1` |
| `case_ref` / `game_id` | 案卷标识与盘号 |
| `size` / `komi` | 开案固定的盘宽与贴目（贴目为小数字符串） |
| `original_board` | 原方向终局盘面，字符串行（`B`/`W`/`.`），所有轮次的唯一依据 |
| `final_receipt_id` / `final_sealed_ply` | 固定的末手封存回执 ID 与末手位置 |
| `original_area` | 开案时复算并核对过的原面积胜负（含 regions 完整明细） |
| `referees` | 两名不同裁判 ID（排序保留输入） |
| `round` / `version` / `status` | 当前轮次（从 1）、单调版本（从 1，每次成功写入 +1）、`open`/`closed` |
| `votes` | 当前轮各裁判意见：`groups[].representative/color/stones` 与 `written_version`；重开清空 |
| `rounds` | 历次不可改写裁定的追加列表，轮次编号严格 1..N |

裁定记录（`rounds[]` 与 `history --round` 同构）字段：`case_ref`、`game_id`、
`final_receipt_id`、`round`、`referees`、`groups`（`color`、全部 `stones`、两人的
`representatives`）、`board_before`、`board_after`、`size`、`komi`、`area_before`、
`area_after`、`winner_before`、`winner_after`、`ruling_id`。`ruling_id` 是对除自身
外全部证据字段的规范 SHA-256，任何字节改写都会在 `verify_state`/`recompute` 时暴露。
`recompute` 只读取单条裁定记录，不依赖原归档或最新状态。
