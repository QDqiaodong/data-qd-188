# 离线分包接收与归档（go-record-archive）

赛场记录台分包补传，档案员先核对再封存。每个本地状态目录只管理一盘棋；全部流程离线，
仅用标准库。棋规（落子、提子、简单劫、双停）与面积模型完全沿用 v1
（见 [contract.md](contract.md)），不引入 SGF、联网、死活判定或其他计分规则。

```sh
go-record-archive init    --state DIR --spec spec.json
go-record-archive receive --state DIR --packet packet.json
go-record-archive view    --state DIR [--target N]
go-record-archive seal    --state DIR --target N --credential HEX
```

成功结果与业务状态以 JSON 打印到 stdout（`sort_keys`、两空格缩进）。格式错误、错盘、
版本冲突、ID 重复、不可接受的封存/凭据请求退出 2，诊断 JSON 打到 stderr：

```json
{"error": {"code": "version_conflict", "message": "...", "ply": 2, "move_id": "m2"}}
```

## init — 初始化（不覆盖已有状态）

spec.json 字段（不带 `moves`，带了就拒）：

| 字段 | 约束 |
| --- | --- |
| `game_id` | 标识符，同 v1 身份规则（`[A-Za-z0-9_:-]{1,64}`） |
| `total_moves` | 声明总手数，整数 2–2000，停手也算 |
| `size` / `setup` / `next_player` / `komi` | 同 v1 契约 |

已有状态目录 → 退出 2，`state_exists`。初始化输出 `{"initialized": true, "game_id", "total_moves"}`。

## receive — 接收分包（整包全收或全拒）

```json
{"game_id": "demo",
 "records": [{"ply": 3, "id": "m3", "player": "B", "point": [4, 4], "revision": 1}]}
```

- `records` 1–2000 条；同一包不得重复报同一 `ply`；`ply` 1..`total_moves`；
  `revision` 正整数 ≤ 2147483647；`id/player/point` 同 v1。
- 坐标结构合法但落在盘外是回放阶段的业务非法，**不是**弃包理由；该记录照常入库，
  可后续升版修复。
- 版本选取按手号不按到达顺序：低版本跳过（`stale`）；同版本同内容是重传
  （`retransmit`/`sealed`）；同版本不同内容是冲突，整包拒（`version_conflict`）；
  高版本只能替换未封存手，封存手即使内容相同也不接受升版（`sealed_ply_immutable`）。
- 内容比较只看 `id/player/point` 的值，不看 JSON 键顺序或空白。
- 已封存手的 ID 仍占用；未封存手的 ID 随有效版本更换而释放。整包合并后的有效棋步
  （含缺口后尚未回放的）必须全盘 ID 唯一（`duplicate_move_id`）；判定针对合并后的
  最终集合，同包交换两个未封存手的 ID 不会误报。
- 结构错误、错盘（`wrong_game`）、版本冲突、合并后 ID 重复 → 整包拒绝，已通过检查的
  其他条目也不留下。

输出：`{"accepted": [{"ply","id","revision","outcome":"new"|"upgrade"}],
"skipped": [{"ply","id","revision","reason":"retransmit"|"stale"|"sealed"}]}`。
全部是跳过时状态不变、不落盘。

## view — 查看状态 / 请求封存凭据

不带 `--target` 时输出：

| 字段 | 含义 |
| --- | --- |
| `sealed_ply` | 已封存到第几手 |
| `prefix_ply` | 当前最长连续合法前缀（绝对手号） |
| `blocker` | 首个阻断：`{"ply","reason":"missing"}` 或 `{"ply","reason","move_id"}`（reason 为原回放非法原因）；完整时为 `null` |
| `board` / `next_player` / `captures` / `double_pass` | 前缀末盘面、下一手颜色、累计提子、是否双停 |
| `area` | 按现有面积模型算的分数预览（`status` 为 `provisional`/`after_two_passes`）；除最终归档结果外只作预览 |
| `pending` | 所有已到达但未封存的有效棋步：`ply/id/player/point/revision/applied` |
| `sealed` | 已封存手保留的凭据：`ply/id/revision/fingerprint`（内容指纹） |
| `receipts` | 已签发的封存回执 |
| `final` | 最终归档结果；未归档为 `null` |

缺口后或首个非法步之后已到达的记录继续保留在 `pending`，但不进入盘面、提子或分数。
修正任一未封存手后，下游已到记录按新前缀重新判断，不沿用旧结论。

带 `--target N` 时尝试签发封存凭据。只有目标在当前封存点之后、该段全部连续合法、
且不把总手数与双停的矛盾锁死才签发：目标内已双停则双停必须恰好发生在声明末手；
目标就是末手则必须已双停。凭据输出为
`"credential": {"from", "to", "value"}`，只绑定本盘、本次核对起点、目标手号及这段
实际选中的棋步和版本；未来手的补传、旧包与等价重传不影响它，范围内一手升版（即使
盘面没变）立即使旧凭据失效。同一受核对范围在重开状态后得到相同凭据。
不合格时退出 2（`segment_incomplete` / `double_pass_mismatch` / `target_not_after_seal` 等）。

## seal — 凭目标手号和凭据封存

```sh
go-record-archive seal --state DIR --target 3 --credential <hex>
```

接收端以当前状态重新确认资格和凭据：不会因为当下也能封到同一手就封掉未经核对的新
版本（`credential_mismatch`）。封存成功后输出：

```json
{"receipt": {"game_id", "previous_sealed_ply", "sealed_ply", "credential", "receipt_id"},
 "final": null}
```

`receipt` 可保存。原样重试已成功的请求（同一 `target` + `credential`）返回原回执，
即使已经继续封存到更后面或已最终归档，也不得回滚。未经成功使用的过期或不匹配凭据
退出 2。

只有声明末手已合法双停且封到末手，才把该盘的分数和提子汇总作为最终归档结果
（`final`：`captures` + `area`，`status=after_two_passes`）对外返回；之后复看和重试
不能改变该结果。

## 断线 / 崩溃后的重试

- 状态是单个 `state.json`，封存点推进、封存段清理、回执登记、最终归档结果在**一次
  原子替换**中提交；异常中断并重开只能读到完整的变更前或变更后状态。收包与封存在
  同一把文件锁（`DIR/lock`）下完成校验与提交，互不穿插。
- `receive` 幂等：断线后重发同一包，已生效的记录按重传跳过。
- `seal` 幂等：断线后不确定是否封存成功，原样重发同一请求，成功过就返回原回执。
- 重开后随时可用 `view` 确认 `sealed_ply`、`pending`、`receipts` 再决定重发什么。
- 测试/演练可用环境变量 `GO_RECORD_ARCHIVE_FAULT=commit_before_replace` 或
  `commit_after_replace` 在提交边界注入受控中断。

## 封存后的存储

封存段的活动存储只保留：每手的 `id/revision`/内容指纹、回执，以及接续合法回放和出分
所需的边界状态（当前盘面、上一手之前的盘面用于简单劫、轮次、连续停手数、累计提子）。
不保留封存段的原始分包、逐手棋步内容或逐手盘面，也不靠外部原包重建；继续收包时凭
指纹与版本即可分清真正的重传、同版本改内容、跨封存线的更正和被占用的旧 ID。
不要求对磁盘做取证级擦除。

## 诊断码一览

`invalid_structure`（结构/类型/取值，含 `record_index`）、`wrong_game`、
`duplicate_ply_in_packet`、`version_conflict`、`sealed_ply_immutable`、
`duplicate_move_id`、`state_exists`、`state_missing`、`state_corrupt`、
`target_out_of_range`、`target_not_after_seal`、`segment_incomplete`（带 `blocker`）、
`double_pass_mismatch`、`credential_mismatch`。
