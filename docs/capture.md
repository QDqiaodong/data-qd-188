# 被提棋子来源研究（go-record-capture-study）

教研组要比较一批棋谱里被提棋子的来源：多少来自开局摆子（setup），多少是对局中刚
落下或已经活了很久的棋子（played）；有的盘一手就提掉好几颗，有的盘分几次提子。除了
颗数，分析员还要知道一个查询窗口实际覆盖多少盘。本命令一次读入多份 `go-record` 原始
输入 JSON 和一批任意区间的查询，同时支持少量逐子反查，只输出本次的分析报告。

- **离线、无状态**：不联网、不改输入、不保留运行间状态；分析员每天可换一批棋谱重跑。
- **只交付本次报告**：成功只原子新建一份 JSON 报告（不覆盖已有文件或链接），失败不
  留下正式报告。只读输入棋谱。
- **只沿用 v1 棋规与输入契约**（见 [contract.md](contract.md)）：落子、提子、禁自杀、
  简单劫、停手、双停，均不改原工具对非法前缀或双停的判定。只读本批 `go-record`
  原始棋谱 JSON，不解析 SGF。

```sh
go-record-capture-study --source MANIFEST.json --output REPORT.json
```

成功退出 0，stdout 打印简要 JSON；输入错误或写入失败退出 2，诊断 JSON 打到 stderr，
不产生正式输出。原 `go-record` 及其 `process(data)` 对原输入的行为不变。

## 清单字段

清单是单个严格 JSON 对象，字段限定为 `games`、`queries`、`lookups` 三项数组；重复键、
多余键、NaN 等一律拒绝。清单和引用棋谱都按仓库既有严格 JSON 规则读取。

| 字段 | 约束 |
| --- | --- |
| `games` | 1–384 项，见下 |
| `queries` | 1–40,000 项，见下 |
| `lookups` | 0–128 项，见下 |

每项 `games`：`game_id`（全批唯一标识符）、`path`（指向现有 `go-record` 原始棋谱 JSON；
绝对路径原样使用，相对路径按清单所在目录解释）。身份以 `game_id` 为准，**不取决**于
文件名、盘面或内容是否相同：两条不同盘号引用同一文件仍是两盘，分别计数。

每项 `queries`：

| 字段 | 约束 |
| --- | --- |
| `query_id` | 全批唯一标识符 |
| `capture_from` / `capture_to` | 1–2000 的整数，闭区间且不倒序 |
| `origin` | `setup` 或 `played` |
| `life_from` / `life_to` | 仅 `played` 必填：1–2000 的整数，闭区间且不倒序；`setup` 不允许出现 |

不同 `query_id` 可以问同一个窗口。窗口超出某盘合法手数不报错，该盘只贡献实际匹配的
事实。

每项 `lookups`：`lookup_id`（全批唯一）、`game_id`（必须引用本批已有盘号）、
`capture_ply`（1–2000）、`point`（该盘原方向的 0 基合法盘内坐标，对应**被提棋子**所在
位置，不是提子一方的落点）。

整批原始 `moves` 合计不超过 240,000 条（停手、规则非法步及未处理后缀都占输入限额）。
文件不存在、结构错误、盘号引用不明或边界非法使整批失败（退出 2），诊断能定位到相应
盘、查询、反查项或总量限制。

## 来源、寿命与事实

同一盘里，在同一坐标先后出现的两颗子是**不同实例**。回放在合法前缀上进行：

- 在 v1 合法棋步提掉某颗子时产生一条**事实**，身份是 `(game_id, capture_ply, point)`。
  事实指出提子手 ID、提子方颜色、被提颜色与坐标，以及这颗子来源：`setup`，或哪一手
  `played`（给出实际来源手号与步 ID）。
- 手号均从 1 开始，**停手照常占号**。`played` 的寿命是提子手号减来源手号；`setup`
  没有寿命，**不把开局当第 0 手**。
- 一次提掉一个棋块时**逐子**追溯，每颗子各成一条事实；一次提多个棋块同样逐子成条。
- 棋子被提后再在该点落子，是**新实例，不继承原来源**。
- 规则非法步及之后的棋步**不产生事实**，即使孤立看后续某手似乎能提子。
- 合法但没有双停的盘同样分析。

## 查询口径

对每个 query，在**所有盘**中选取提子手号、来源类别、以及（仅 `played`）寿命**都**落在
该查询范围内的事实：

- `stone_count`：匹配的**棋子实例**数（事实条数）。
- `game_count`：至少有一个匹配实例的**不同 `game_id`** 数，不是匹配的手数、组数或
  分段盘数之和。同一盘在多个提子手和寿命命中时只计一次；两个不同盘号即使棋谱完全
  一样也分别计数。空结果两项均为 0。

逐子反查返回精确的那条事实，若没有则返回 `null`；盘存在但手号超出合法前缀时也为
`null`。报告**不导出全批逐子事实**，只按查询和反查汇总。

全部查询由**一次求解**回答，不逐查询遍历全批逐子事实：`stone_count` 用按提子手离线
扫描 + 寿命 Fenwick 树（`played`）与排序二分（`setup`）；`game_count` 以位切片计数器
按盘去重累加。报告按各自 ID 确定性输出；重排无顺序语义的清单条目不改报告（原始棋步
顺序仍有语义）。

## 报告字段

```json
{
  "schema": "go-record-capture-study/1",
  "games": [{"game_id", "path", "legal_plies", "first_illegal"}],
  "queries": [{"query_id", "origin", "capture_from", "capture_to",
               "life_from", "life_to", "stone_count", "game_count"}],
  "lookups": [{"lookup_id", "game_id", "capture_ply", "point", "fact"}]
}
```

`first_illegal` 为 `null` 或 `{"ply", "move_id", "reason"}`（首个非法步；该步及之后
不分析）。`legal_plies` 是合法前缀的手数。`fact` 为 `null` 或：

```json
{"capture_ply", "capture_move_id", "capture_player", "captured_color", "point",
 "origin", "source_ply", "source_move_id", "life"}
```

`setup` 事实的 `source_ply` / `source_move_id` / `life` 均为 `null`；`played` 事实三者
都有值。

## 诊断码

退出 2，诊断 JSON 打到 stderr，不落报告：

| code | 含义 |
| --- | --- |
| `invalid_structure` | 清单结构/类型/取值错误，含 `games_index` / `queries_index` / `lookups_index` 及 `game_id` / `query_id` / `lookup_id` 等定位 |
| `game_unreadable` | 引用的棋谱不可读或不是严格 JSON（含 `game_id`、`path`） |
| `invalid_game` | 棋谱结构无效、初始设置非法（含 `game_id`、`path`） |
| `move_limit` | 整批原始 `moves` 合计超限（含 `total_moves`、`limit`） |
| `output_exists` | 输出目标已存在（原子新建，不覆盖） |
| `io_error` | 源文件或输出路径读写失败 |

棋谱存在规则非法步**不是**整批输入错误：该盘只分析首个非法步之前的合法前缀，并在
`games[].first_illegal` 给出阻断的手号、步 ID 和原因。

## 可复现示例

[`fixtures/capture/`](../fixtures/capture) 提供作者自制的合成样例（见
[来源说明](../fixtures/SOURCES.md)）：

```sh
go-record-capture-study --source fixtures/capture/manifest.json --output run/capture.json
```

两盘都 5 路：黑初始占满第 0、2 行并占 [1,0]，白先、贴目 "0"。G 初始白在 [1,2]、[1,3]，
白第 1 手落 [1,1]，黑第 2 手落 [1,4]；H 的初始白额外含 [1,1]，第 1 手改停手，其他相同。
两盘第 2 手各提 [1,1]、[1,2]、[1,3] 三子，提子坐标与末盘面完全相同。清单里：

- `origin=setup`、提子手 `[2,2]` → `(5 颗, 2 盘)`；
- `played`、提子手 `[2,2]`、寿命 `[1,1]` → `(1 颗, 1 盘)`（G 的第 1 手子）；
- `played`、提子手 `[2,2]`、寿命 `[2,2]` → `(0, 0)`。

反查 G 第 2 手 [1,1] 得到实际第 1 手（`source_move_id` 为 `g1`，`life` 为 1）；反查
H 第 2 手 [1,1] 得到 `setup`；反查 G 第 2 手 [1,2] 仍为 `setup`。

### 合成规模验收

[`examples/capture_synthetic.py`](../examples/capture_synthetic.py) 按规格生成 384 盘
19 路合成棋谱与 40,000 个 `played` 窗口；生成与核验不计入受限进程：

```sh
python3 examples/capture_synthetic.py run/synthetic-source
go-record-capture-study --source run/synthetic-source/manifest.json --output run/synthetic.json
```

受限验收（单进程 256 MiB 地址空间、75 秒 CPU）见
[`tests/test_capture_synthetic.py`](../tests/test_capture_synthetic.py)：对子进程设置
`RLIMIT_AS` 与 `RLIMIT_CPU`，一次读入、辨认来源、求解并写报告，不把实算转给其他进程；
随后用独立行级闭式公式核验全部 40,000 个结果（含 n = 0、172、173、16608、33216、
39999 及均匀抽样），盘数按“该盘至少一颗命中”判断，而非用区间内非空行数代替。

## 边界

只读本批 `go-record` 原始棋谱，不写归档、不改棋谱；不联网、不做死活裁定、不解析 SGF；
不保留运行间状态。`go-record`、`go-record-archive`、`go-record-standings`、
`go-record-appeal`、`go-record-dataset` 与 `go-record-guide` 的接口和行为不变。
