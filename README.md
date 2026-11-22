# data-qd-188 · 围棋棋谱合法回放与盘面区域

按顺序落子、提子、查气与简单劫，保留首个非法步之前的合法盘面；盘面区域给出明确模型下的面积分和中立区域。

board.py 处理棋块气与提子，service.py 管理轮次/简单劫及合法前缀，regions.py 计算空域与面积。 完整字段、单位、身份与失败约定见 [数据契约](docs/contract.md)。样例是作者自制合成数据，许可见 [来源说明](fixtures/SOURCES.md)。

另含离线分包接收与归档：archive.py 按手号合并分包版本、核对后凭据封存、原子提交与最终归档，archive_cli.py 提供 `go-record-archive` 子命令（init/receive/view/seal），每个状态目录管理一盘。参数、JSON 字段与断线重试见 [归档流程](docs/archive.md)。

赛后对账与晋级核定：standings.py 按交接清单核对台次↔盘号的一一对应、枚举可行对应关系并独立核定名次，standings_cli.py 提供 `go-record-standings --source MANIFEST.json --output REPORT.json`。清单字段、晋级口径、见证与诊断码见 [对账与晋级](docs/standings.md)。

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

实际37项测试通过；CLI演示生成JSON，摘要为：

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

## 当前边界

只有simple ko与无自杀模型，无SGF、超级劫、死活AI或赛事裁定。 实际后续需求可从变化树导入、棋谱片段复用和人工死子裁定沿既有身份和结果集成；当前未实现这些扩展，也不保证固定数量的未来任务。
