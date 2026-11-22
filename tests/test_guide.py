import contextlib
import io
import json
import os
import resource
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from go_record import guide
from go_record.guide_cli import main as guide_main

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'guide'

BLACK_SETUP = [[0, 1], [1, 0], [2, 1]]
WHITE_SETUP = [[1, 1], [0, 2], [2, 2], [1, 3]]
# 黑 [1,2] 提掉白 [1,1] 后的盘面，行棋方为白
AFTER_TAKE_BLACK = [[0, 1], [1, 0], [2, 1], [1, 2]]
AFTER_TAKE_WHITE = [[0, 2], [2, 2], [1, 3]]


def node(node_id, parent_id, slot, player, point):
    return {'node_id': node_id, 'parent_id': parent_id, 'slot': slot,
            'player': player, 'point': point}


def tree(size, black, white, next_player, nodes, komi='0'):
    return {'size': size,
            'setup': {'black': [list(p) for p in black],
                      'white': [list(p) for p in white]},
            'next_player': next_player, 'komi': komi,
            'nodes': list(nodes)}


def ko_tree_a():
    return tree(5, BLACK_SETUP, WHITE_SETUP, 'B', [
        node('take', 'root', 'take', 'B', [1, 2]),
        node('retake', 'take', 'retake', 'W', [1, 1]),
        node('draft', 'retake', 'draft', 'B', [4, 4]),
        node('wait', 'take', 'wait', 'W', None),
    ])


def ko_tree_b():
    return tree(5, AFTER_TAKE_BLACK, AFTER_TAKE_WHITE, 'W', [
        node('retake2', 'root', 'retake', 'W', [1, 1]),
        node('wait2', 'root', 'wait', 'W', None),
    ])


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = guide_main(argv)
    return code, out.getvalue(), err.getvalue()


class GuideCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='go-guide-')
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.counter = 0

    def write(self, name, payload):
        path = os.path.join(self.dir, name)
        Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return path

    def manifest(self, entries, name='manifest.json'):
        return self.write(name, {'trees': entries})

    def entry(self, tree_id, path):
        return {'tree_id': tree_id, 'path': path}

    def build(self, manifest_name='manifest.json'):
        source = os.path.join(self.dir, manifest_name)
        return guide.build_package(guide.load(source), self.dir)

    def build_payload(self, manifest_name='manifest.json'):
        return guide.as_payload(self.build(manifest_name))

    def run_guide(self, manifest_path=None, output=None, expect=0):
        if manifest_path is None:
            manifest_path = os.path.join(self.dir, 'manifest.json')
        if output is None:
            self.counter += 1
            output = os.path.join(self.dir, f'package-{self.counter}.json')
        code, out, err = run_cli(['--source', manifest_path, '--output', output])
        self.assertEqual(code, expect, err)
        return output, out, err

    # ---- 包内引用与按钮查询辅助 ----

    @staticmethod
    def index(package):
        diagrams = {d['diagram_id']: d for d in package['diagrams']}
        views = {v['view_id']: v for v in package['views']}
        return diagrams, views

    @staticmethod
    def button(view, slot):
        matches = [b for b in view['buttons'] if b['slot'] == slot]
        assert len(matches) == 1
        return matches[0]

    def bookmark(self, package, tree_id, node_id):
        matches = [b for b in package['bookmarks']
                   if b['tree_id'] == tree_id and b['node_id'] == node_id]
        self.assertEqual(len(matches), 1)
        return matches[0]

    # ---- 验收 1：5 路盘两个白先图，同显示图不同 view ----

    def test_ko_two_white_first_diagrams(self):
        pa = self.write('a.json', ko_tree_a())
        pb = self.write('b.json', ko_tree_b())
        self.manifest([self.entry('tA', 'a.json'), self.entry('tB', 'b.json')])
        package = self.build_payload()
        diagrams, views = self.index(package)

        bm_a_take = self.bookmark(package, 'tA', 'take')
        bm_b_root = self.bookmark(package, 'tB', 'root')
        va, vb = views[bm_a_take['view_id']], views[bm_b_root['view_id']]
        # 共用显示图（同盘宽、同盘面、同行棋方、同结束态），但不共用 view
        self.assertEqual(va['diagram_id'], vb['diagram_id'])
        self.assertNotEqual(va['view_id'], vb['view_id'])
        shared_diagram = diagrams[va['diagram_id']]
        self.assertEqual(shared_diagram['board'],
                         ['.BW..', 'B.BW.', '.BW..', '.....', '.....'])
        self.assertEqual(shared_diagram['next_player'], 'W')
        self.assertFalse(shared_diagram['ended'])

        # 第一图：回提因简单劫禁用，等待合法
        retake_a = self.button(va, 'retake')
        self.assertEqual({k: retake_a[k] for k in ('player', 'point', 'reason')},
                         {'player': 'W', 'point': [1, 1], 'reason': 'simple_ko'})
        self.assertNotIn('target_view_id', retake_a)
        wait_a = self.button(va, 'wait')
        self.assertIsNone(wait_a['point'])
        self.assertEqual(wait_a['captures'], [])

        # 第二图：回提合法并实际提掉 [1,2]
        retake_b = self.button(vb, 'retake')
        self.assertEqual(retake_b['captures'], [[1, 2]])
        self.assertNotIn('reason', retake_b)
        wait_b = self.button(vb, 'wait')

        # 两个 wait 进入同一个 view（无按钮的白停手后黑先图）
        self.assertEqual(wait_a['target_view_id'], wait_b['target_view_id'])

        # 回提目标图：白 [1,1] 落回并提走黑 [1,2]（白 (1,3) 仍在），行棋方为黑
        target = views[retake_b['target_view_id']]
        self.assertEqual(diagrams[target['diagram_id']]['board'],
                         ['.BW..', 'BW.W.', '.BW..', '.....', '.....'])
        self.assertEqual(diagrams[target['diagram_id']]['next_player'], 'B')

        # 黑先提子按钮的实际提子坐标
        a_root = views[self.bookmark(package, 'tA', 'root')['view_id']]
        self.assertEqual(self.button(a_root, 'take')['captures'], [[1, 1]])

        # 被挡草稿：书签指回本树自己的回提节点
        bm_retake = self.bookmark(package, 'tA', 'retake')
        self.assertEqual(bm_retake['status'], 'blocked')
        self.assertEqual(bm_retake['first_illegal_node'], 'retake')
        self.assertEqual(bm_retake['reason'], 'simple_ko')
        self.assertEqual(bm_retake['parent_view_id'], bm_a_take['view_id'])
        bm_draft = self.bookmark(package, 'tA', 'draft')
        self.assertEqual(bm_draft['status'], 'blocked')
        self.assertEqual(bm_draft['first_illegal_node'], 'retake')
        self.assertEqual(bm_draft['parent_view_id'], bm_a_take['view_id'])
        # 第二棵树没有被挡节点
        self.assertEqual(self.bookmark(package, 'tB', 'retake2')['status'], 'playable')

        # 所有引用都可解析，view 目标不形成播放循环以外的悬空引用
        self.assert_references_closed(package)

    def test_removing_disabled_branch_allows_view_sharing(self):
        trimmed_a = tree(5, BLACK_SETUP, WHITE_SETUP, 'B', [
            node('take', 'root', 'take', 'B', [1, 2]),
            node('wait', 'take', 'wait', 'W', None),
        ])
        trimmed_b = tree(5, AFTER_TAKE_BLACK, AFTER_TAKE_WHITE, 'W', [
            node('wait2', 'root', 'wait', 'W', None),
        ])
        self.write('a2.json', trimmed_a)
        self.write('b2.json', trimmed_b)
        self.manifest([self.entry('tA', 'a2.json'), self.entry('tB', 'b2.json')],
                      name='m2.json')
        package = self.build_payload('m2.json')
        va = self.bookmark(package, 'tA', 'take')['view_id']
        vb = self.bookmark(package, 'tB', 'root')['view_id']
        self.assertEqual(va, vb)  # 现在必须共用 view
        # 两个源书签仍分别存在（含各自的根书签）
        self.assertEqual(len(package['bookmarks']), 5)
        trees_in_bookmarks = {(b['tree_id'], b['node_id']) for b in package['bookmarks']}
        self.assertEqual(trees_in_bookmarks, {
            ('tA', 'root'), ('tA', 'take'), ('tA', 'wait'),
            ('tB', 'root'), ('tB', 'wait2')})

    # ---- 验收 2：深层差异阻止误合；双停 game_ended；禁用按钮共用 ----

    def test_deep_difference_blocks_view_merge(self):
        # 两条导览：同显示图、同首个合法按钮，差异只在更深处（一条末图多一个按钮）。
        # go2 分支两侧完全同构、同盘面、同行棋方，必须共用；go 分支深处不同，顶层不合并。
        common = tree(5, [], [], 'B', [
            node('m1', 'root', 'go', 'B', [0, 0]),
            node('m2a', 'm1', 'go', 'W', [1, 1]),
            node('m2b', 'root', 'go2', 'B', [2, 2]),
        ])
        deep_extra = tree(5, [], [], 'B', [
            node('n1', 'root', 'go', 'B', [0, 0]),
            node('n2a', 'n1', 'go', 'W', [1, 1]),
            node('n3', 'n2a', 'extra', 'B', [3, 3]),
            node('n2b', 'root', 'go2', 'B', [2, 2]),
        ])
        self.write('c.json', common)
        self.write('d.json', deep_extra)
        self.manifest([self.entry('tC', 'c.json'), self.entry('tD', 'd.json')])
        package = self.build_payload()
        _, views = self.index(package)
        top_c = views[self.bookmark(package, 'tC', 'root')['view_id']]
        top_d = views[self.bookmark(package, 'tD', 'root')['view_id']]
        self.assertNotEqual(top_c['view_id'], top_d['view_id'])
        # 首个按钮的 slot/player/point 完全相同，目标 view 却不同
        bc, bd = self.button(top_c, 'go'), self.button(top_d, 'go')
        self.assertEqual((bc['slot'], bc['player'], bc['point']),
                         (bd['slot'], bd['player'], bd['point']))
        self.assertNotEqual(bc['target_view_id'], bd['target_view_id'])
        # 但两个 go2 分支末图（无按钮且盘面相同）仍可共用
        self.assertEqual(self.button(top_c, 'go2')['target_view_id'],
                         self.button(top_d, 'go2')['target_view_id'])
    def test_double_pass_ends_game_and_sibling_branch_independent(self):
        data = tree(5, [], [], 'B', [
            node('p1', 'root', 'pass1', 'B', None),
            node('p2', 'p1', 'pass2', 'W', None),
            node('late', 'p2', 'late', 'B', [0, 0]),       # 双停后：game_ended
            node('side', 'p1', 'side', 'W', [1, 1]),       # 同父落子分支：独立合法
            node('after_side', 'side', 'next', 'B', [2, 2]),
        ])
        self.write('e.json', data)
        self.manifest([self.entry('tE', 'e.json')])
        package = self.build_payload()
        diagrams, views = self.index(package)
        after_two = views[self.bookmark(package, 'tE', 'p2')['view_id']]
        self.assertTrue(diagrams[after_two['diagram_id']]['ended'])
        late = self.button(after_two, 'late')
        self.assertEqual(late['reason'], 'game_ended')
        self.assertNotIn('target_view_id', late)
        # late 下没有更深节点，但其书签仍登记为 blocked
        self.assertEqual(self.bookmark(package, 'tE', 'late')['status'], 'blocked')
        # 同父的落子分支独立判定，不受双停影响
        one_pass = views[self.bookmark(package, 'tE', 'p1')['view_id']]
        side = self.button(one_pass, 'side')
        self.assertNotIn('reason', side)
        side_view = views[side['target_view_id']]
        self.assertFalse(diagrams[side_view['diagram_id']]['ended'])
        self.bookmark(package, 'tE', 'after_side')  # 可达书签存在

    def test_shared_disabled_button_keeps_distinct_source_bookmarks(self):
        # 两棵树可见行为完全相同；同一禁用按钮下挂不同 ID、不同深度的草稿。
        # 初始设置让白落 (1,0) 即自杀：三个邻点全黑，另放一颗独立白子。
        def make(draft_chain):
            nodes = [node('bad', 'root', 'bad', 'W', [1, 0])]
            parent = 'bad'
            for depth, draft_id in enumerate(draft_chain):
                nodes.append(node(draft_id, parent, f'd{depth}', 'B',
                                  [2 + depth, 2 + depth]))
                parent = draft_id
            return tree(5, [[0, 0], [2, 0], [1, 1]], [[1, 2]], 'W', nodes)

        self.write('f.json', make(['x1']))
        self.write('g.json', make(['y1', 'y2', 'y3']))
        self.manifest([self.entry('tF', 'f.json'), self.entry('tG', 'g.json')])
        package = self.build_payload()
        _, views = self.index(package)
        vf = views[self.bookmark(package, 'tF', 'root')['view_id']]
        vg = views[self.bookmark(package, 'tG', 'root')['view_id']]
        self.assertEqual(vf['view_id'], vg['view_id'])  # 禁用菜单共用
        self.assertEqual(self.button(vf, 'bad')['reason'], 'suicide')
        # 深链书签各自指向本树非法节点
        bf = self.bookmark(package, 'tF', 'x1')
        bg = self.bookmark(package, 'tG', 'y3')
        self.assertEqual((bf['first_illegal_node'], bf['reason']), ('bad', 'suicide'))
        self.assertEqual((bg['first_illegal_node'], bg['reason']), ('bad', 'suicide'))
        self.assertEqual(bf['parent_view_id'], vf['view_id'])
        self.assertEqual(bg['parent_view_id'], vg['view_id'])
        self.assertNotEqual(bf['node_id'], bg['node_id'])

    # ---- 结构校验：无论可达与否都要检查 ----

    def assert_reject(self, entries, code='invalid_structure', **files):
        for name, payload in files.items():
            filename = name if name.endswith('.json') else f'{name}.json'
            self.write(filename, payload)
        self.manifest(entries)
        with self.assertRaises(guide.Reject) as ctx:
            self.build()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception.diagnostic()

    def test_missing_parent_and_cycle(self):
        missing = tree(5, [], [], 'B', [node('m', 'ghost', 's', 'B', [0, 0])])
        diag = self.assert_reject([self.entry('t1', 'miss.json')], miss=missing)
        self.assertEqual(diag['error']['tree_id'], 't1')
        self.assertEqual(diag['error']['node_id'], 'm')

        cycle = tree(5, [], [], 'B', [
            node('a', 'root', 'a', 'B', [0, 0]),
            node('b', 'a', 'b', 'W', [0, 1]),
            node('c', 'b', 'c', 'B', [0, 2]),
        ])
        cycle['nodes'][0]['parent_id'] = 'c'  # a->c->b->a
        diag = self.assert_reject([self.entry('t2', 'cycle.json')], **{'cycle': cycle})
        self.assertEqual(diag['error']['tree_id'], 't2')
        self.assertIn('cycle', diag['error']['message'])

        # 与根分离的环：即使从根走不到，结构校验仍须拒绝
        detached = tree(5, [], [], 'B', [
            node('rootward', 'root', 's1', 'B', [0, 0]),
            node('x', 'y', 's2', 'W', [0, 1]),
            node('y', 'x', 's3', 'B', [0, 2]),
        ])
        diag = self.assert_reject([self.entry('t2b', 'detached.json')],
                                  **{'detached': detached})
        self.assertEqual(diag['error']['tree_id'], 't2b')
        self.assertIn('cycle', diag['error']['message'])

    def test_duplicate_sibling_slot_and_reserved_root(self):
        dup = tree(5, [], [], 'B', [
            node('a', 'root', 'same', 'B', [0, 0]),
            node('b', 'root', 'same', 'W', [0, 1]),
            node('c', 'b', 'same', 'B', [0, 2]),  # 不同父可复用
        ])
        diag = self.assert_reject([self.entry('t3', 'dup.json')], dup=dup)
        self.assertEqual(diag['error']['node_id'], 'b')
        reserved = tree(5, [], [], 'B', [node('root', 'root', 's', 'B', [0, 0])])
        self.assert_reject([self.entry('t4', 'res.json')], res=reserved)

    def test_downstream_structure_error_not_pardoned_by_upstream_illegal(self):
        # 上游一手非法（自杀），下游同时存在缺父结构错误：结构错误仍须失败
        bad = tree(5, [[0, 0], [2, 0], [1, 1]], [[1, 2]], 'W', [
            node('illegal', 'root', 'bad', 'W', [1, 0]),
            node('broken', 'nowhere', 'x', 'B', [3, 3]),
        ])
        diag = self.assert_reject([self.entry('t5', 'bad.json')], bad=bad)
        self.assertEqual(diag['error']['node_id'], 'broken')

    def test_depth_and_batch_limits(self):
        nodes = [node('n0', 'root', 's', 'B', None)]
        for k in range(1, 2001):
            player = 'W' if k % 2 else 'B'
            nodes.append(node(f'n{k}', f'n{k - 1}', 's', player, None))
        deep = tree(5, [], [], 'B', nodes)  # 2001 手
        diag = self.assert_reject([self.entry('t6', 'deep.json')], deep=deep)
        self.assertEqual(diag['error']['node_id'], 'n2000')
        self.assertIn('2000', diag['error']['message'])

        # 恰好 2000 手合法
        ok = tree(5, [], [], 'B', nodes[:-1])
        self.write('ok.json', ok)
        self.manifest([self.entry('t7', 'ok.json')], name='mok.json')
        package = self.build('mok.json')
        self.assertEqual(len(package['bookmarks']), 2001)

        # 整批上限：26 棵各 2,000 节点的浅层树（根下挂两片 1,000 深的停手链）
        def wide_tree(prefix):
            nodes = []
            for branch in ('a', 'b'):
                top = node(f'{prefix}{branch}0', 'root', f'{branch}0', 'B', None)
                nodes.append(top)
                for k in range(1, 1000):
                    nodes.append(node(
                        f'{prefix}{branch}{k}', f'{prefix}{branch}{k - 1}',
                        f'{branch}{k}', 'B' if k % 2 == 0 else 'W', None))
            return tree(5, [], [], 'B', nodes)
        for i in range(26):
            self.write(f'h{i}.json', wide_tree(f't{i}'))
        self.manifest([self.entry(f'h{i}', f'h{i}.json') for i in range(26)],
                      name='mh.json')
        with self.assertRaises(guide.Reject) as ctx:
            self.build('mh.json')
        self.assertEqual(ctx.exception.code, 'invalid_structure')
        self.assertEqual(ctx.exception.details['node_count'], 52_000)
        self.assertEqual(ctx.exception.details['limit'], 50_000)

    def test_manifest_identity_errors(self):
        self.write('x.json', tree(5, [], [], 'B', []))
        cases = [
            {'trees': []},
            {'trees': [self.entry('t1', 'x.json'), self.entry('t1', 'x.json')]},
            {'trees': [{'tree_id': 'bad id!', 'path': 'x.json'}]},
            {'trees': [{'tree_id': 't1', 'path': ''}]},
            {'trees': [{'tree_id': 't1'}]},
            {'trees': 'not-a-list'},
            {'other': 1},
        ]
        for index, manifest in enumerate(cases):
            path = self.write(f'mm-{index}.json', manifest)
            with self.assertRaises(guide.Reject) as ctx:
                guide.build_package(guide.load(path), self.dir)
            self.assertEqual(ctx.exception.code, 'invalid_structure')
        # 257 棵树
        many = {'trees': [self.entry(f't{i:03}', 'x.json') for i in range(257)]}
        path = self.write('mm-many.json', many)
        with self.assertRaises(guide.Reject):
            guide.build_package(guide.load(path), self.dir)

    def test_out_of_bounds_is_rules_rejection_not_structure(self):
        # 形式合法但盘外：禁用按钮，不整包失败
        data = tree(5, [], [], 'B', [
            node('outside', 'root', 'off', 'B', [5, 0]),
            node('draft', 'outside', 'd', 'W', [0, 0]),
            node('fine', 'root', 'on', 'B', [0, 0]),
        ])
        self.write('oob.json', data)
        self.manifest([self.entry('t8', 'oob.json')])
        package = self.build_payload()
        _, views = self.index(package)
        root = views[self.bookmark(package, 't8', 'root')['view_id']]
        off = self.button(root, 'off')
        self.assertEqual(off['reason'], 'out_of_bounds')
        self.assertNotIn('target_view_id', off)
        # 缺席的按钮与禁用按钮不同：fine 是另一条独立合法分支（轮到黑）
        self.assertNotIn('reason', self.button(root, 'on'))
        bm = self.bookmark(package, 't8', 'draft')
        self.assertEqual((bm['status'], bm['first_illegal_node'], bm['reason']),
                         ('blocked', 'outside', 'out_of_bounds'))
        # 坐标形式不合法仍是结构错误
        malformed = tree(5, [], [], 'B', [node('bad', 'root', 's', 'B', [1])])
        self.assert_reject([self.entry('t9', 'mal.json')], mal=malformed)

    def test_wrong_turn_occupied_and_capture_contents(self):
        # 黑 (0,1) 落子：白 (0,2) 仅余 (0,1) 一口气（(0,3) 与 (1,2) 皆黑），
        # 先提白再检查己方气，落子合法，实际提子 [[0,2]]
        data = tree(5, [[0, 0], [0, 3], [1, 2]], [[0, 2]], 'B', [
            node('wrong', 'root', 'wrong', 'W', [1, 0]),
            node('occupy', 'root', 'occupy', 'B', [0, 0]),
            node('capture', 'root', 'cap', 'B', [0, 1]),
        ])
        self.write('rules.json', data)
        self.manifest([self.entry('tR', 'rules.json')])
        package = self.build_payload()
        _, views = self.index(package)
        root = views[self.bookmark(package, 'tR', 'root')['view_id']]
        self.assertEqual(self.button(root, 'wrong')['reason'], 'wrong_turn')
        self.assertEqual(self.button(root, 'occupy')['reason'], 'occupied')
        cap = self.button(root, 'cap')
        self.assertEqual(cap['captures'], [[0, 2]])
        target = views[cap['target_view_id']]
        self.assertEqual(self.index(package)[0][target['diagram_id']]['board'][0],
                         'BB.B.')

    def test_suicide_reason_and_branch_isolation(self):
        # 白落 (1,0) 即自杀：三个邻点全黑；另放一颗独立白子保证设置合法
        data = tree(5, [[0, 0], [2, 0], [1, 1]], [[1, 2]], 'W', [
            node('kill', 'root', 'suicide', 'W', [1, 0]),
            node('other', 'root', 'pass', 'W', None),
        ])
        self.write('sui.json', data)
        self.manifest([self.entry('tS', 'sui.json')])
        package = self.build_payload()
        _, views = self.index(package)
        root = views[self.bookmark(package, 'tS', 'root')['view_id']]
        self.assertEqual(self.button(root, 'suicide')['reason'], 'suicide')
        self.assertNotIn('reason', self.button(root, 'pass'))

    # ---- 重排稳定性 ----

    def test_reorder_stability(self):
        a = ko_tree_a()
        b = ko_tree_b()
        reordered_a = json.loads(json.dumps(a))
        reordered_a['nodes'] = list(reversed(a['nodes']))
        reordered_a['setup']['black'] = list(reversed(a['setup']['black']))
        reordered_a['setup']['white'] = list(reversed(a['setup']['white']))
        reordered_b = json.loads(json.dumps(b))
        reordered_b['nodes'] = list(reversed(b['nodes']))

        self.write('a1.json', a)
        self.write('b1.json', b)
        self.manifest([self.entry('tA', 'a1.json'), self.entry('tB', 'b1.json')],
                      name='m1.json')
        p1 = self.build_payload('m1.json')
        self.write('a2.json', reordered_a)
        self.write('b2.json', reordered_b)
        self.manifest([self.entry('tB', 'b2.json'), self.entry('tA', 'a2.json')],
                      name='m2.json')
        p2 = self.build_payload('m2.json')
        self.assertEqual(
            json.dumps(p1, ensure_ascii=False, sort_keys=True),
            json.dumps(p2, ensure_ascii=False, sort_keys=True))

    # ---- 引用闭合、点击路径与独立播放 ----

    def assert_references_closed(self, package):
        diagrams, views = self.index(package)
        for view in package['views']:
            self.assertIn(view['diagram_id'], diagrams)
            diagram = diagrams[view['diagram_id']]
            self.assertEqual(len(diagram['board']), diagram['size'])
            self.assertTrue(all(len(row) == diagram['size'] for row in diagram['board']))
            slots = [b['slot'] for b in view['buttons']]
            self.assertEqual(len(slots), len(set(slots)))
            for button in view['buttons']:
                if 'target_view_id' in button:
                    self.assertNotIn('reason', button)
                    self.assertIn(button['target_view_id'], views)
                else:
                    self.assertIn('reason', button)
        for bookmark in package['bookmarks']:
            if bookmark['status'] == 'playable':
                self.assertIn(bookmark['view_id'], views)
            else:
                self.assertIn(bookmark['parent_view_id'], views)

    def test_click_path_walk_from_bookmark(self):
        self.write('a.json', ko_tree_a())
        self.write('b.json', ko_tree_b())
        self.manifest([self.entry('tA', 'a.json'), self.entry('tB', 'b.json')])
        path = self.run_guide()[0]
        package = json.loads(Path(path).read_text(encoding='utf-8'))
        diagrams, views = self.index(package)

        # 从 tA 根书签沿 take -> wait 点击，核对每步提子与盘面
        current = views[self.bookmark(package, 'tA', 'root')['view_id']]
        take = self.button(current, 'take')
        self.assertEqual(take['captures'], [[1, 1]])
        current = views[take['target_view_id']]
        self.assertEqual(diagrams[current['diagram_id']]['next_player'], 'W')
        # 禁用按钮不可点：没有目标
        self.assertNotIn('target_view_id', self.button(current, 'retake'))
        wait = self.button(current, 'wait')
        current = views[wait['target_view_id']]
        self.assertEqual(current['buttons'], [])  # wait 下无后续

        # 从 tB 根书签回提，核对提子坐标
        current_b = views[self.bookmark(package, 'tB', 'root')['view_id']]
        retake = self.button(current_b, 'retake')
        self.assertEqual(retake['captures'], [[1, 2]])
        target = views[retake['target_view_id']]
        self.assertEqual(diagrams[target['diagram_id']]['next_player'], 'B')

    # ---- CLI、原子新建与输出保护 ----

    def test_cli_atomic_create_and_existing_target(self):
        self.write('a.json', tree(5, [], [], 'B', []))
        self.manifest([self.entry('tA', 'a.json')])
        output, out, _ = self.run_guide()
        summary = json.loads(out)
        self.assertEqual(summary['bookmarks'], 1)
        # 已有普通文件：拒绝覆盖
        before = Path(output).read_bytes()
        _, _, err = self.run_guide(output=output, expect=2)
        self.assertIn('output_exists', err)
        self.assertEqual(Path(output).read_bytes(), before)
        # 已有符号链接：同样拒绝
        link = os.path.join(self.dir, 'link.json')
        os.symlink(output, link)
        _, _, err = self.run_guide(output=link, expect=2)
        self.assertIn('output_exists', err)

    def test_input_error_leaves_no_output(self):
        bad = os.path.join(self.dir, 'missing-tree.json')
        self.manifest([self.entry('t1', 'missing-tree.json')])
        output = os.path.join(self.dir, 'never.json')
        code, _, err = run_cli(['--source', os.path.join(self.dir, 'manifest.json'),
                                '--output', output])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(output))
        diag = json.loads(err)
        self.assertEqual(diag['error']['code'], 'tree_unreadable')
        self.assertEqual(diag['error']['tree_id'], 't1')

    def test_absolute_tree_path(self):
        abs_path = self.write('abs.json', tree(5, [], [], 'B', []))
        self.manifest([self.entry('tA', abs_path)])
        self.run_guide()

    def test_root_only_tree_and_empty_buttons_view(self):
        self.write('r.json', tree(9, [], [], 'B', []))
        self.manifest([self.entry('tR', 'r.json')])
        package = self.build_payload()
        self.assertEqual(len(package['diagrams']), 1)
        self.assertEqual(len(package['views']), 1)
        self.assertEqual(package['views'][0]['buttons'], [])
        self.assertEqual(self.bookmark(package, 'tR', 'root')['status'], 'playable')

    def test_fixture_manifest_matches_documented_example(self):
        manifest = guide.load(str(FIXTURES / 'manifest.json'))
        package = guide.build_package(manifest, str(FIXTURES))
        payload = guide.as_payload(package)
        # 与文档中的小样例要点一致
        views = {v['view_id']: v for v in payload['views']}
        bm = {(b['tree_id'], b['node_id']): b for b in payload['bookmarks']}
        va = views[bm[('tree-a', 'take')]['view_id']]
        vb = views[bm[('tree-b', 'root')]['view_id']]
        self.assertEqual(va['diagram_id'], vb['diagram_id'])
        self.assertNotEqual(va['view_id'], vb['view_id'])
        self.assertEqual(next(b for b in va['buttons'] if b['slot'] == 'retake')
                         ['reason'], 'simple_ko')


# ---------- 受限批处理：100 棵 19 路树，256 MiB / 90 s 单进程 ----------

def all_points(size=19):
    return [(r, c) for r in range(size) for c in range(size)]


def generate_stress_batch(base_dir):
    """按验收口径生成 100 棵树的清单与树文件，返回清单路径。"""
    base = Path(base_dir)
    trees_dir = base / 'trees'
    trees_dir.mkdir(parents=True, exist_ok=True)
    points = all_points()
    eligible = [p for p in points if p[0] in (7, 8)]
    combos = []
    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            combos.append((eligible[i], eligible[j]))
            if len(combos) == 100:
                break
        if len(combos) == 100:
            break
    black_line = points[:120]
    white_line = list(reversed(points[-120:]))
    entries = []
    for index, (p, q) in enumerate(combos):
        nodes = []
        for k in range(240):
            if k % 2 == 0:
                player, move_point = 'B', black_line[k // 2]
            else:
                player, move_point = 'W', white_line[k // 2]
            parent = 'root' if k == 0 else f'n{k - 1}'
            nodes.append(node(f'n{k}', parent, 'main', player, list(move_point)))
            nodes.append(node(f'l{k}', parent, 'leaf', player, [9, 9]))
        data = tree(19, [list(p), list(q)], [], 'B', nodes, komi='0')
        tree_path = trees_dir / f'tree-{index:03}.json'
        tree_path.write_text(json.dumps(data), encoding='utf-8')
        entries.append({'tree_id': f'stress-{index:03}',
                        'path': f'trees/tree-{index:03}.json'})
    manifest_path = base / 'manifest.json'
    manifest_path.write_text(json.dumps({'trees': entries}), encoding='utf-8')
    return str(manifest_path)


class StressCase(unittest.TestCase):
    def test_limited_batch_under_rlimits(self):
        work = Path(self.id().replace('.', '-')) if False else None
        with tempfile.TemporaryDirectory(prefix='go-guide-stress-') as tmp:
            base = Path(tmp)
            manifest = generate_stress_batch(base)
            output = base / 'package.json'
            env = dict(os.environ)
            package_root = str(Path(guide.__file__).resolve().parents[1])
            env['PYTHONPATH'] = package_root
            # 预限制脚本：在解释器启动后立刻套上 256 MiB 地址空间与 90 s CPU 配额
            preexec = (
                "import resource, sys;"
                "resource.setrlimit(resource.RLIMIT_AS,(268435456,268435456));"
                "resource.setrlimit(resource.RLIMIT_CPU,(90,90));"
                "from go_record.guide_cli import main;"
                "sys.exit(main(['--source',sys.argv[1],'--output',sys.argv[2]]))"
            )
            result = subprocess.run(
                [sys.executable, '-c', preexec, manifest, str(output)],
                capture_output=True, text=True, env=env, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            # 子进程峰值驻留内存也必须在 256 MiB 以内（Linux 下 ru_maxrss 单位 KiB）
            child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            self.assertLess(child_usage.ru_maxrss, 256 * 1024)
            package = json.loads(output.read_text(encoding='utf-8'))

        self.assertEqual(len(package['diagrams']), 48_100)
        self.assertEqual(len(package['views']), 48_100)
        self.assertEqual(len(package['bookmarks']), 48_100)
        playable = [b for b in package['bookmarks'] if b['status'] == 'playable']
        self.assertEqual(len(playable), 48_100)

        diagrams = {d['diagram_id']: d for d in package['diagrams']}
        views = {v['view_id']: v for v in package['views']}
        bm = {(b['tree_id'], b['node_id']): b for b in package['bookmarks']}

        # 盘面/导航信息没有被省略：每张显示图都是完整 19 路行字符串
        for diagram in package['diagrams']:
            self.assertEqual(diagram['size'], 19)
            self.assertEqual(len(diagram['board']), 19)
            self.assertTrue(all(len(row) == 19 for row in diagram['board']))

        # 沿第一棵树主线点 240 次，再沿每个叶分支点一次
        first = 'stress-000'
        current = views[bm[(first, 'root')]['view_id']]
        for k in range(240):
            diagram = diagrams[current['diagram_id']]
            main_btn = next(b for b in current['buttons'] if b['slot'] == 'main')
            leaf_btn = next(b for b in current['buttons'] if b['slot'] == 'leaf')
            self.assertEqual(main_btn['captures'], [])
            self.assertEqual(leaf_btn['captures'], [])
            expected_player = 'B' if k % 2 == 0 else 'W'
            self.assertEqual(diagram['next_player'], expected_player)
            # 叶分支书签可解析并指向同一父图菜单下的目标
            leaf_bookmark = bm[(first, f'l{k}')]
            self.assertEqual(views[leaf_bookmark['view_id']]['diagram_id'],
                             views[leaf_btn['target_view_id']]['diagram_id'])
            current = views[main_btn['target_view_id']]
        self.assertEqual(current['buttons'], [])  # 末图无按钮

        # 抽查中段叶分支：叶图无按钮且盘面比主线图多一颗 (9,9)
        mid_view = views[bm[(first, 'n119')]['view_id']]
        leaf_target = views[next(b for b in mid_view['buttons']
                                 if b['slot'] == 'leaf')['target_view_id']]
        main_board = diagrams[mid_view['diagram_id']]['board']
        leaf_board = diagrams[leaf_target['diagram_id']]['board']
        self.assertEqual(leaf_board[9][9], 'B')
        self.assertEqual(main_board[9][9], '.')
        diffs = [(r, c) for r in range(19) for c in range(19)
                 if main_board[r][c] != leaf_board[r][c]]
        self.assertEqual(diffs, [(9, 9)])


if __name__ == '__main__':
    unittest.main()
