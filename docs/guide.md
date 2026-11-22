# 离线导览包（go-record-guide）

把多位作者各自编辑的变化树，编译成观众可离线独立播放的共享导航图。它**不登记对局、
不归档棋步、也不判定胜负**：不产生面积、贴目或累计提子，只负责把每条分支判给它自己
的父图、按 v1 棋规（见 [contract.md](contract.md)）判出按钮结果，并把相同可见行为的
位置最大程度共用为同一份导航记录。

```sh
go-record-guide --source MANIFEST.json --output PACKAGE.json
```

成功退出 0 并**原子新建**导览包（已有目标文件或符号链接一律拒绝覆盖），stdout 打印
简要计数；输入错误退出 2，诊断 JSON 打到 stderr，不落正式输出。**棋规禁用不是整包
失败**：禁用按钮与 blocked 书签是正常产物。仅用标准库。

## 清单与树文件

清单是单个严格 JSON 对象，只允许一个字段：

| 字段 | 约束 |
| --- | --- |
| `trees` | 1–256 项，每项含 `tree_id` 与 `path`；`tree_id` 全清单唯一，身份规则同 v1（`[A-Za-z0-9_:-]{1,64}`） |

`path` 相对清单所在目录解释，也可以是绝对路径。每个树文件与原棋谱同义地携带
`size / setup / next_player / komi`，但用 `nodes` 代替 `moves`：

| 节点字段 | 约束 |
| --- | --- |
| `node_id` | 树内唯一的 v1 标识符；保留 ID `root` 不得用于节点 |
| `parent_id` | 父节点 ID；根图保留 ID 为 `root`，根盘面就是初始设置，根不出现在 `nodes` 内 |
| `slot` | 按钮标识，同一父节点下唯一；不同父节点可复用同一标识 |
| `player` | `B` / `W`，该按钮显示的行棋方 |
| `point` | `[row, col]`（0 起坐标）或 `null`（停一手） |

JSON 与标识沿用项目严格规则：重复键、多余键、NaN/Infinity、单文件超过 8 MB 一律拒绝；
不依赖 `nodes` 的列出顺序。结构约束在任何棋规判断**之前**全量检查，无论节点之后是否
能走到：

- 节点 ID 树内唯一、按钮标识同父唯一、所有父链必须到达本树根（缺父、成环都是输入错误）。
- 每条根到节点路径至多 2000 手；全批非根节点总计至多 50,000；允许只有根的树。
- 初始设置与 v1 相同（坐标在盘内、黑白不重叠、每个初始连通块有气）。
- 落子坐标若**形式合法但在盘外**（如 5 路盘的 `[5,0]`），是棋规拒绝（按钮原因
  `out_of_bounds`），不是结构错误；坐标形式本身不合法（如只有一个分量）才是结构错误。

## 逐分支独立判棋

每条根到节点的真实路径独立回放 v1 规则：轮流落子、棋盘范围与占用、先提无气对方棋块、
再检查己方气（禁自杀）、简单劫（落子后盘面等于前一手之前的盘面）、停一手与双停。
兄弟分支互不污染——各自有独立的棋子、劫历史与停手计数。

- 一手合法：按钮记录排序后的实际提子坐标与目标 view。
- 一手非法：只禁用当前父图的这个按钮，记录原因，**不产生下一图**；此手以下整棵草稿
  不可达，不会重新判棋或转接到别处。其他分支照常可播放。
- 原因取值与 v1 一致：`wrong_turn`、`out_of_bounds`、`occupied`、`suicide`、
  `simple_ko`、`game_ended`（双停后仍配置的按钮）。

## 显示图与 view 的共用口径

包内三部分通过 ID 互相关联，播放器只读本包即可完成书签跳转与分支导航：

```json
{
  "schema": "go-record-guide/1",
  "diagrams": [
    {"diagram_id": "d1", "size": 5,
     "board": [".BW..", "B.BW.", ".BW..", ".....", "....."],
     "next_player": "W", "ended": false}
  ],
  "views": [
    {"view_id": "v1", "diagram_id": "d1",
     "buttons": [
       {"slot": "retake", "player": "W", "point": [1, 1], "reason": "simple_ko"},
       {"slot": "wait", "player": "W", "point": null,
        "captures": [], "target_view_id": "v2"}
     ]}
  ],
  "bookmarks": [
    {"tree_id": "tree-a", "node_id": "take", "status": "playable", "view_id": "v1"},
    {"tree_id": "tree-a", "node_id": "retake", "status": "blocked",
     "first_illegal_node": "retake", "reason": "simple_ko", "parent_view_id": "v1"}
  ]
}
```

### diagrams（显示图）

识别一个显示图只用四项：原方向的盘宽 `size`、盘面 `board`（等长 `.BW` 行字符串，
不旋转、不换色）、行棋方 `next_player`、是否已双停结束 `ended`。相同显示图在
`diagrams` 中只存一份。贴目、面积与不可见的劫历史不属于显示图。

### views（按钮菜单）

一条 view 引用一张显示图和该位置的整套**直接按钮**。每个按钮带 `slot/player/point`：

- 合法按钮：`captures`（排序后的 `[row,col]` 提子坐标，无提子为 `[]`）与
  `target_view_id`；没有 `reason`。
- 禁用按钮：`reason`；没有 `target_view_id` 与 `captures`。
- **展示时不存在的按钮与展示为禁用的按钮是两回事**：按钮集合（按 slot）不同即不同 view。

两个位置只有在**显示图相同、所有按钮的可见行为相同、且每个合法按钮进入的后续导览
也相同（一直递归到所有分支走完）**时才共用一条 view。共用通过后序 intern 实现：
view ID 在第一次遇到该完整行为时分配，之后全批复用，因此共享之后不会出现播放循环、
不会改变任何按钮结果，也不会引入原分支没有的走法。

不参与共用判定的内容：作者节点 ID、父节点是谁、未被任何按钮体现的劫历史；一个禁用
按钮下挂什么草稿也不影响其父 view 的共用。不同 view 可以引用同一张显示图。

### bookmarks（源书签）

为每个源 `(tree_id, node_id)` 保留独立书签，包含每棵树的根（`node_id: "root"`），
按 `(tree_id, node_id)` 排序，根排在该树最前：

- 能走到的位置：`status: "playable"`，指向相应 `view_id`。
- 被挡在非法分支上的位置：`status: "blocked"`，不伪造可播放目标，给出本树路径上
  **第一条非法节点**的 `first_illegal_node`、`reason`，以及其父图的 `parent_view_id`，
  供作者回到编辑树定位。即使两个禁用按钮在包中共用同一条菜单记录，书签仍各自保留
  自己的源节点 ID，不会混用。

## 确定性与输出保护

- 只重排树清单、节点表或初始棋子列表，包的内容、引用 ID 与集合顺序完全不变；
  换一批输入不要求 ID 保持稳定。所有顺序敏感的比较都以 slot、坐标或 ID 的确定排序进行。
- 成功输出原子新建：先写同目录临时文件并 fsync，再硬链接到目标；目标文件或符号链接
  已存在则拒绝，不覆盖、不修改。输入错误时不留下正式输出。
- 大输出按缩进 JSON 分块流式写入，避免在内存中同时持有完整字符串。

## 诊断码

退出 2，诊断 JSON 打到 stderr，不落报告：

| code | 含义 |
| --- | --- |
| `invalid_structure` | 清单或树文件的结构/类型/身份/树关系错误，附 `trees_index` 或 `tree_id`（及 `node_id`）定位；整批节点超限附 `node_count/limit` |
| `tree_unreadable` | 引用的树文件不可读或不是严格 JSON（含 `tree_id`、`path`） |
| `output_exists` | 输出目标已存在（原子新建，不覆盖） |
| `io_error` | 源文件或输出路径读写失败 |

## 可复现小样例

[`fixtures/guide/`](../fixtures/guide) 是 5 路盘的双树样例（作者自制合成数据，见
[来源说明](../fixtures/SOURCES.md)）：

- `trees/tree-a.json`：黑先，黑 `[1,2]` 提掉白 `[1,1]` 后来到白先图；该图配置
  `retake`（白 `[1,1]`，因简单劫禁用，其下草稿不可达）与 `wait`（白停一手，无后续）。
- `trees/tree-b.json`：直接以提子后的棋盘为初始设置、白先；`retake` 合法并实际提掉
  `[1,2]`，`wait` 同样停一手无后续。

两树的白先图共用一张显示图却不能共用 view；两个 `wait` 进入同一个 view。

```sh
go-record-guide --source fixtures/guide/manifest.json --output run/guide-demo.json
# stdout: {"blocked_bookmarks":2,"bookmarks":8,"diagrams":3,"playable_bookmarks":6,"views":5}
```

删掉两棵树各自的 `retake` 及其后草稿后，两个白先图只剩相同的 `wait` 菜单，必须共用
同一 view，而两个源书签仍分别存在。自动化测试 `tests/test_guide.py` 同时覆盖：
深层差异阻止误合并、双停 `game_ended` 与同父分支独立判定、禁用菜单共用而深链书签
各自定位、重排稳定性、缺父/分离环/重复兄弟 slot/上游非法不赦免下游结构错误、
输出目标保护，以及 100 棵 19 路树在 256 MiB 地址空间与 90 秒 CPU 单进程配额内的
受限批处理（48,100 个可播放书签、48,100 张显示图、48,100 个 view，并沿主线与叶分支
实点核对）。

## 边界

不解析 SGF、不联网、不做死活或赛事裁定；不登记对局、不归档棋步、不判定胜负与贴目。
`go-record`、`go-record-archive`、`go-record-standings` 与 `go-record-dataset` 的
接口和行为不变，`service.process(data)` 契约保持原样。
