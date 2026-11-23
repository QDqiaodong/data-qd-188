# data-qd-188 · 围棋棋谱合法回放与盘面区域

按顺序落子、提子、查气与简单劫，保留首个非法步之前的合法盘面；盘面区域给出明确模型下的面积分和中立区域。

board.py 处理棋块气与提子，service.py 管理轮次/简单劫及合法前缀，regions.py 计算空域与面积。 完整字段、单位、身份与失败约定见 [数据契约](docs/contract.md)。样例是作者自制合成数据，许可见 [来源说明](fixtures/SOURCES.md)。

另含离线分包接收与归档：archive.py 按手号合并分包版本、核对后凭据封存、原子提交与最终归档，archive_cli.py 提供 `go-record-archive` 子命令（init/receive/view/seal），每个状态目录管理一盘。参数、JSON 字段与断线重试见 [归档流程](docs/archive.md)。

赛后对账与晋级核定：standings.py 按交接清单核对台次↔盘号的一一对应、枚举可行对应关系并独立核定名次，standings_cli.py 提供 `go-record-standings --source MANIFEST.json --output REPORT.json`。清单字段、晋级口径、见证与诊断码见 [对账与晋级](docs/standings.md)。

离线终局死子申诉案卷：appeal.py 每份案卷管理一盘，从已最终封存的归档开案并固定末手回执、原方向终局盘面、盘宽、贴目与原面积胜负；两名裁判按轮次以棋块代表坐标投票，一致后结案，先移除确认棋块再用既有面积模型重算整盘，固化不可改写裁定并可逐轮独立复算，归档移入冷库后仍可复查。appeal_cli.py 提供 `go-record-appeal`（open/vote/close/reopen/view/history/recompute）。standings 清单的 archives 条目可选 `appeal_dir` 接入：未结案拦成 `awaiting_ruling`，已结案只采用当前轮裁定胜负，见证同时反查末手回执与裁定轮次。字段、版本与持久化边界见 [终局死子申诉案卷](docs/appeal.md)。

离线“下一手”练习集分区：dataset.py 按规范局面键（八向旋转/镜像、不换黑白、含行棋方）与来源系列保守并组，套用已发布历史的分区约束，再按组标识优先联合满足黑先/白先盲评配额，dataset_cli.py 提供 `go-record-dataset --source MANIFEST.json --output REPORT.json`。只输出供人工复核的分区计划，不对外发布；字段、隔离口径与诊断码见 [下一手分区](docs/dataset.md)。

离线导览包：guide.py 把多位作者编辑的变化树按 v1 棋规逐分支独立判定，编制成共享的 diagrams/views/bookmarks 导航图——相同显示图只存一份，完整可见行为（含全部后续导览）相同的位置才共用 view，棋规禁用只禁用当前按钮并在书签中定位首个非法节点，guide_cli.py 提供 `go-record-guide --source MANIFEST.json --output PACKAGE.json`。字段、共用口径与诊断码见 [离线导览包](docs/guide.md)。

被提棋子来源研究：capture.py 一次读入多份原始棋谱和一批任意区间查询，在 v1 合法前缀上逐手回放，为每颗棋子实例记住来源（`setup` 或落下的 `played` 手号/步 ID），逐子产出提子事实并一次性回答全部查询的 `stone_count`（棋子实例数）与 `game_count`（至少命中一颗的不同盘号数），另支持少量逐子反查；capture_cli.py 提供 `go-record-capture-study --source MANIFEST.json --output REPORT.json`。只交付本次报告、不保留运行间状态。字段、寿命口径与规模验收见 [被提棋子来源研究](docs/capture.md)。

## 安装、测试与演示

Python >=3.11，本批实际使用本机 CPython3.12.14。运行与测试仅用标准库；构建工具精确锁定。每仓独立 .venv，公共包缓存可共享；不共享其他项目的site-packages。

下面命令已在本机已有uv、Python与公共缓存条件下离线执行通过。其他机器如未准备缓存，需先准备构建包；不保证首次安装可以离线完成。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/go-record --source fixtures/input.json --output run/report-initial.json
```

实际135项测试通过（含申诉案卷与晋级接入 26 项、被提棋子来源研究 28 项）；CLI演示生成JSON，摘要为：

```json
{
  "accepted_moves": 3,
  "board_stones": 5,
  "captured_stones": 1,
  "ended": true,
  "valid_record": true
}
```

输出是原子新建；演示已生成过的同名路径不能重用，复跑请换一个新文件名。非法输入与文件写入错误退出2；业务诊断的表达方式见契约。已经实际核验重复JSON键拒绝、旧结果字节不变、已有符号链接不覆盖。

归档流程演示（状态目录可复用，逐命令可重试）：

```sh
.venv/bin/go-record-archive init --state run/game --spec run/spec.json
.venv/bin/go-record-archive receive --state run/game --packet run/packet.json
.venv/bin/go-record-archive view --state run/game --target 3
.venv/bin/go-record-archive seal --state run/game --target 3 --credential <view签发的hex>
```

下一手分区演示（读 [样例清单](fixtures/dataset/manifest-without-k.json) 生成可复核计划，
输出原子新建，复跑请换新路径）：

```sh
.venv/bin/go-record-dataset --source fixtures/dataset/manifest.json --output run/dataset-isolated.json
.venv/bin/go-record-dataset --source fixtures/dataset/manifest-without-k.json --output run/dataset-ready.json
```

第一条因历史训练/盲评两侧约束把整组隔离而输出 `quota_unreachable`；第二条输出 `ready`，
与 [预期报告](fixtures/dataset/expected-report.json) 一致。详见 [下一手分区](docs/dataset.md)。

终局死子申诉演示（建案卷、两轮裁定、冷库复算与晋级接入；不写仓库目录）：

```sh
PYTHONPATH=src python3 examples/appeal_demo.py
```

被提棋子来源研究演示（读 [样例清单](fixtures/capture/manifest.json) 生成与
[预期报告](fixtures/capture/expected-report.json) 一致的报告，输出原子新建，复跑请换新路径）：

```sh
.venv/bin/go-record-capture-study --source fixtures/capture/manifest.json --output run/capture.json
```

合成规模验收（384 盘 19 路、40,000 查询，单进程 256 MiB / 75 秒 CPU）见
`tests/test_capture_synthetic.py`；生成器为 `examples/capture_synthetic.py`。

## 当前边界

只有simple ko与无自杀模型，无SGF、超级劫、死活AI。申诉案卷不自行判断死活：棋块按
开案原盘面同色四邻连通定义，裁判意见决定移除哪些块，工具只负责核对、移除与按既有
面积模型复算；落子、劫、贴目与面积规则不变。实际后续需求可从棋谱片段复用和人工
死子裁定沿既有身份和结果集成；变化树到共享导航图的离线导览已由 go-record-guide
提供，当前未实现其他扩展，也不保证固定数量的未来任务。
