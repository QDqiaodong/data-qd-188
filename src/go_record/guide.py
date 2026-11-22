"""离线导览包（go-record-guide）：把作者各自编辑的变化树编译成可独立播放的共享导航图。

输入清单含 1..256 棵编辑树；每棵树以 size/setup/next_player/komi 描述根盘面，
用 nodes 代替 moves：node_id/parent_id/slot/player/point，根图保留 ID "root"。

处理分两阶段，互不混淆：

1. 全量结构校验（先于一切棋规判断）：身份、按钮标识、父链可达本树根、无环、
   深度上限、兄弟 slot 唯一、整批节点上限。无论节点之后能否走到，都要通过校验。
2. 逐分支独立棋规判定，沿用 v1 规则（轮次、落子、提子、禁自杀、简单劫、双停，
   见 board.py 与 service.py）：兄弟分支互不污染棋子、劫历史与停手计数。棋规禁用
   只禁用当前父图的那个按钮并给出原因，不使整包失败；其下整棵草稿不可达。

输出 diagrams/views/bookmarks：相同显示图（盘宽、盘面、行棋方、结束态）只存一份；
view 按完整可见行为最大共用——相同的显示图、完全一致的按钮集合，且每个合法按钮
进入的全部后续导览也相同。作者节点 ID、父链身份与不可见的劫历史不参与共用。
仅用标准库，不登记对局、不归档棋步、不判定胜负。
"""
import os
import sys

from .board import identity, place
from .checks import array, fields, ident
from .io import load
from .model import coordinate, setup_board

SCHEMA = 'go-record-guide/1'
MAX_TREES = 256
MAX_NODES_BATCH = 50_000
MAX_DEPTH = 2000
ROOT_ID = 'root'
PLAYERS = ('B', 'W')

# 每条根到节点路径至多 2000 手，递归 DFS 需要明显高于该深度的栈余量。
sys.setrecursionlimit(10_000)


class Reject(Exception):
    """输入错误：CLI 退出 2，diagnostic() 给出可定位信息，不落正式输出。"""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def diagnostic(self):
        return {'error': {'code': self.code, 'message': str(self), **self.details}}


def _reject(code, message, **details):
    raise Reject(code, message, **details)


def _tree_error(tree_id, error, node_id=None):
    details = {'tree_id': tree_id}
    if node_id is not None:
        details['node_id'] = node_id
    _reject('invalid_structure', f'tree {tree_id}: {error}', **details)


# ---------- 清单与树文件校验 ----------

def validate_manifest(manifest):
    """严格校验清单；返回按 tree_id 排序的条目（顺序不影响结果，仅为诊断确定）。"""
    try:
        fields(manifest, ('trees',))
        raw_entries = array(manifest['trees'], 1, MAX_TREES)
    except ValueError as error:
        _reject('invalid_structure', f'manifest: {error}')
    entries = []
    tree_ids = set()
    for index, entry in enumerate(raw_entries):
        try:
            fields(entry, ('tree_id', 'path'))
            ident(entry['tree_id'])
            if not isinstance(entry['path'], str) or not entry['path']:
                raise ValueError('path: non-empty string required')
        except ValueError as error:
            _reject('invalid_structure', f'trees[{index}]: {error}', trees_index=index)
        if entry['tree_id'] in tree_ids:
            _reject('invalid_structure',
                    f'trees[{index}]: duplicate tree_id {entry["tree_id"]}',
                    trees_index=index, tree_id=entry['tree_id'])
        tree_ids.add(entry['tree_id'])
        entries.append({'tree_id': entry['tree_id'], 'path': entry['path']})
    entries.sort(key=lambda item: item['tree_id'])
    return entries


def _resolve_path(entry, manifest_dir):
    path = entry['path']
    if os.path.isabs(path):
        return path
    return os.path.join(manifest_dir, path)


def load_tree_file(entry, manifest_dir):
    """读取单棵树文件；不可读或不是严格 JSON 是输入错误。"""
    path = _resolve_path(entry, manifest_dir)
    try:
        return load(path)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        _reject('tree_unreadable',
                f'tree {entry["tree_id"]}: cannot read {entry["path"]}: {error}',
                tree_id=entry['tree_id'], path=entry['path'])


def validate_tree(entry, data):
    """校验单棵树的结构、身份与树关系；棋规不在此阶段判断。"""
    tree_id = entry['tree_id']
    try:
        fields(data, ('size', 'setup', 'next_player', 'komi', 'nodes'))
        board, komi = setup_board(data)
        size = data['size']
        raw_nodes = array(data['nodes'], 0, MAX_NODES_BATCH)
    except ValueError as error:
        _tree_error(tree_id, error)

    by_id = {}
    for node in raw_nodes:
        try:
            fields(node, ('node_id', 'parent_id', 'slot', 'player', 'point'))
            node_id = ident(node['node_id'])
            parent_id = ident(node['parent_id'])
            slot = ident(node['slot'])
            if node['player'] not in PLAYERS:
                raise ValueError('player: B/W required')
            point = None if node['point'] is None else coordinate(node['point'])
        except ValueError as error:
            _tree_error(tree_id, error)
        if node_id == ROOT_ID:
            _tree_error(tree_id, f'node_id {ROOT_ID!r} is reserved for the root diagram',
                        node_id=node_id)
        if node_id in by_id:
            _tree_error(tree_id, f'duplicate node_id {node_id}', node_id=node_id)
        by_id[node_id] = {'node_id': node_id, 'parent_id': parent_id, 'slot': slot,
                          'player': node['player'], 'point': point}

    # 父节点必须存在（或为本树根）；同时检查同父下 slot 唯一。
    siblings = {}
    for node_id in sorted(by_id):
        node = by_id[node_id]
        if node['parent_id'] != ROOT_ID and node['parent_id'] not in by_id:
            _tree_error(tree_id,
                        f'node {node_id}: parent {node["parent_id"]} is missing',
                        node_id=node_id)
        bucket = siblings.setdefault(node['parent_id'], {})
        if node['slot'] in bucket:
            _tree_error(tree_id,
                        f'duplicate slot {node["slot"]!r} under parent '
                        f'{node["parent_id"]}: nodes {bucket[node["slot"]]} and {node_id}',
                        node_id=node_id)
        bucket[node['slot']] = node_id

    # 沿父链求深度：必须全部到达本树根，不能成环，路径至多 2000 手。
    depth = {ROOT_ID: 0}
    visiting = set()
    for start in sorted(by_id):
        if start in depth:
            continue
        chain = []
        current = start
        while current not in depth:
            if current in visiting:
                _tree_error(tree_id, f'parent chain forms a cycle at node {current}',
                            node_id=current)
            visiting.add(current)
            chain.append(current)
            current = by_id[current]['parent_id']
        level = depth[current]
        for node_id in reversed(chain):
            level += 1
            if level > MAX_DEPTH:
                _tree_error(tree_id,
                            f'node {node_id}: path depth {level} exceeds {MAX_DEPTH} moves',
                            node_id=node_id)
            depth[node_id] = level
            visiting.discard(node_id)

    children = {}
    for node_id, node in by_id.items():
        children.setdefault(node['parent_id'], []).append(node)
    for bucket in children.values():
        bucket.sort(key=lambda item: item['slot'])

    return {'tree_id': tree_id, 'size': size, 'board': board,
            'next_player': data['next_player'], 'komi': komi,
            'nodes': by_id, 'children': children, 'depth': depth}


# ---------- 棋规判定（逐分支独立，镜像 service.process 的 v1 规则） ----------

def _evaluate_button(frame, node, size):
    """返回 (candidate_board, sorted_captures, reason)；reason 非空表示禁用。"""
    board, player, passes, _last_id, _ko_ref = frame
    if passes == 2:
        return None, [], 'game_ended'
    if node['player'] != player:
        return None, [], 'wrong_turn'
    point = node['point']
    if point is None:
        return board, [], None
    candidate, captured, reason = place(board, point, player, size)
    if reason is not None:
        return None, [], reason
    if frame[4] is not None and identity(candidate) == frame[4]:
        return None, [], 'simple_ko'
    return candidate, captured, None


def _diagram_cells(board, size):
    """盘面的紧凑字节键（行优先，B/W/.）；只依赖棋子位置，与输入列表顺序无关。"""
    cells = bytearray(b'.' * (size * size))
    for (row, col), color in board.items():
        cells[row * size + col] = ord(color)
    return bytes(cells)


# ---------- 编译共享导航图 ----------

class _Compiler:
    def __init__(self):
        self.diagrams = []          # 编制顺序即 d1,d2,...，重排输入下确定
        self.diagram_ids = {}       # 显示图键 -> diagram_id
        self.views = []             # 编制顺序即 v1,v2,...
        self.view_ids = {}          # 完整可见行为签名 -> view_id

    def _intern_diagram(self, size, cells, player, ended):
        key = (size, cells, player, ended)
        diagram_id = self.diagram_ids.get(key)
        if diagram_id is None:
            diagram_id = f'd{len(self.diagrams) + 1}'
            self.diagram_ids[key] = diagram_id
            self.diagrams.append({'diagram_id': diagram_id, 'size': size,
                                  'cells': cells, 'next_player': player, 'ended': ended})
        return diagram_id

    def walk(self, tree, frame, view_of, blocked):
        """后序 DFS：先定全部子 view，再 intern 当前 view，返回当前节点的 view_id。"""
        children = tree['children']
        size = tree['size']

        def visit(node_id, frame):
            board, player, passes, last_id, ko_ref = frame
            ended = passes == 2
            diagram_id = self._intern_diagram(size, _diagram_cells(board, size),
                                              player, ended)
            signature_buttons = []
            output_buttons = []
            for child in children.get(node_id, ()):
                candidate, captured, reason = _evaluate_button(frame, child, size)
                if reason is not None:
                    # 禁用：只禁用当前父图的这个按钮，其下草稿整棵不可达。
                    blocked[child['node_id']] = (reason, node_id)
                    signature_buttons.append(
                        (1, child['slot'], child['player'], child['point'], reason))
                    output_buttons.append(
                        {'slot': child['slot'], 'player': child['player'],
                         'point': None if child['point'] is None else list(child['point']),
                         'reason': reason})
                    continue
                next_passes = passes + 1 if child['point'] is None else 0
                next_player = 'W' if player == 'B' else 'B'
                child_frame = (candidate, next_player, next_passes,
                               identity(candidate), last_id)
                target_view_id = visit(child['node_id'], child_frame)
                signature_buttons.append(
                    (0, child['slot'], child['player'], child['point'],
                     tuple(captured), target_view_id))
                output_buttons.append(
                    {'slot': child['slot'], 'player': child['player'],
                     'point': None if child['point'] is None else list(child['point']),
                     'captures': [list(point) for point in captured],
                     'target_view_id': target_view_id})
            signature = (diagram_id, tuple(signature_buttons))
            view_id = self.view_ids.get(signature)
            if view_id is None:
                view_id = f'v{len(self.views) + 1}'
                self.view_ids[signature] = view_id
                self.views.append({'view_id': view_id, 'diagram_id': diagram_id,
                                   'buttons': output_buttons})
            view_of[node_id] = view_id
            return view_id

        return visit(ROOT_ID, frame)


def _blocked_info(tree, node_id, blocked, cache, view_of):
    """返回该不可达节点路径上第一条非法节点的 (非法节点ID, 原因, 父图 view_id)。"""
    if node_id in cache:
        return cache[node_id]
    chain = []
    current = node_id
    while current not in blocked:
        chain.append(current)
        current = tree['nodes'][current]['parent_id']
    reason, parent_id = blocked[current]
    info = (current, reason, view_of[parent_id])
    cache[node_id] = info
    for ancestor in chain:
        cache[ancestor] = info
    return info


def compile_package(entries, manifest_dir):
    """校验全部树并编译共享导览包；棋规禁用通过 blocked 书签体现，不抛错。"""
    validated = []
    total_nodes = 0
    for entry in entries:
        tree = validate_tree(entry, load_tree_file(entry, manifest_dir))
        total_nodes += len(tree['nodes'])
        if total_nodes > MAX_NODES_BATCH:
            _reject('invalid_structure',
                    f'batch has {total_nodes} non-root nodes; limit is {MAX_NODES_BATCH}',
                    node_count=total_nodes, limit=MAX_NODES_BATCH)
        validated.append(tree)

    compiler = _Compiler()
    bookmarks = []
    for tree in validated:
        tree_id = tree['tree_id']
        root_frame = (tree['board'], tree['next_player'], 0,
                      identity(tree['board']), None)
        view_of = {}
        blocked = {}
        compiler.walk(tree, root_frame, view_of, blocked)

        cache = {}
        bookmark_ids = [ROOT_ID] + sorted(tree['nodes'])
        for node_id in bookmark_ids:
            if node_id in view_of:
                bookmarks.append(
                    {'tree_id': tree_id, 'node_id': node_id, 'status': 'playable',
                     'view_id': view_of[node_id]})
            else:
                illegal_id, reason, parent_view_id = _blocked_info(
                    tree, node_id, blocked, cache, view_of)
                bookmarks.append(
                    {'tree_id': tree_id, 'node_id': node_id, 'status': 'blocked',
                     'first_illegal_node': illegal_id, 'reason': reason,
                     'parent_view_id': parent_view_id})

    bookmarks.sort(key=lambda item: (item['tree_id'],
                                     0 if item['node_id'] == ROOT_ID else 1,
                                     item['node_id']))
    return {'schema': SCHEMA, 'diagrams': compiler.diagrams,
            'views': compiler.views, 'bookmarks': bookmarks}


def build_package(manifest, manifest_dir):
    """清单 dict -> 导览包内部结构（cells 为紧凑字节，输出前再转行字符串）。"""
    return compile_package(validate_manifest(manifest), manifest_dir)


def as_payload(package):
    """把内部紧凑盘面转成契约文档中的 JSON 形状（行字符串）。"""
    diagrams = []
    for diagram in package['diagrams']:
        size = diagram['size']
        cells = diagram['cells']
        board = [cells[r * size:(r + 1) * size].decode('ascii')
                 for r in range(size)]
        diagrams.append({'diagram_id': diagram['diagram_id'], 'size': size,
                         'board': board, 'next_player': diagram['next_player'],
                         'ended': diagram['ended']})
    return {'schema': package['schema'], 'diagrams': diagrams,
            'views': package['views'], 'bookmarks': package['bookmarks']}
