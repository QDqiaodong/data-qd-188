"""验收 3：受限子进程跑完 384 盘合成批次与 40,000 个查询。

生成数据与读取报告核验在不受限的测试进程内完成（规格允许不计入）；受限的是
确实读入、辨认来源、求解并写报告的那一个子进程，设置 RLIMIT_AS=256 MiB、
RLIMIT_CPU=75 秒，不把实算转交其他进程。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'examples'


def k_value(g, j):
    return 1 + ((g // (16 ** (j % 3)) + 7 * j + (j // 3) * (g % 11)) % 16)


def game_rows(g):
    """独立行公式：返回该盘每行 (提子手, k, 到该行 k 之和)。"""
    total = 0
    rows = []
    for j in range(9):
        k = k_value(g, j)
        total += k
        rows.append((2 * total, k))
    return rows


def odds_in(low, high):
    if low > high:
        return 0
    return (high + 1) // 2 - low // 2


def expected(queries):
    """完全独立的行级闭式答案，盘数按“该盘至少一颗命中”统计。"""
    all_rows = [game_rows(g) for g in range(384)]
    results = []
    for query in queries:
        cf, ct = query['capture_from'], query['capture_to']
        lf, lt = query['life_from'], query['life_to']
        stones = 0
        games = 0
        for rows in all_rows:
            game_stones = 0
            hit = False
            for ply, k in rows:
                if cf <= ply <= ct:
                    count = odds_in(lf, min(lt, 2 * k - 1))
                    if count > 0:
                        hit = True
                        game_stones += count
            stones += game_stones
            if hit:
                games += 1
        results.append((stones, games))
    return results


class SyntheticAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='go-capture-syn-')
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def generate(self):
        sys.path.insert(0, str(EXAMPLES))
        try:
            from capture_synthetic import write_source
            return write_source(os.path.join(self.dir, 'source'))
        finally:
            sys.path.pop(0)

    def test_full_batch_under_resource_limits(self):
        manifest_path = self.generate()
        manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['games']), 384)
        self.assertEqual(len(manifest['queries']), 40000)
        output = os.path.join(self.dir, 'report.json')
        launcher = (
            "import sys;"
            f"sys.path.insert(0,{str(ROOT / 'src')!r});"
            "from go_record.capture_cli import main;"
            "raise SystemExit(main())")

        def limits():
            import resource
            resource.setrlimit(resource.RLIMIT_AS,
                               (256 * 1024 * 1024, 256 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (75, 75))

        proc = subprocess.run(
            [sys.executable, '-I', '-c', launcher,
             '--source', manifest_path, '--output', output],
            preexec_fn=limits, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.dir, timeout=200)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))
        summary = json.loads(proc.stdout.decode('utf-8'))
        self.assertEqual(summary['games'], 384)
        self.assertEqual(summary['queries'], 40000)
        self.assertEqual(summary['blocked_games'], [])
        report = json.loads(Path(output).read_text(encoding='utf-8'))
        # 报告按 query_id 排序输出（与清单条目顺序无关），ID 集合须完整。
        self.assertEqual({item['query_id'] for item in report['queries']},
                         {f'q{n}' for n in range(40000)})

        got = {item['query_id']: (item['stone_count'], item['game_count'])
               for item in report['queries']}
        want = expected(manifest['queries'])
        mismatches = [(query['query_id'], want[index], got[query['query_id']])
                      for index, query in enumerate(manifest['queries'])
                      if got[query['query_id']] != want[index]]
        self.assertEqual(mismatches[:5], [])

        for n in (0, 172, 173, 16608, 33216, 39999):
            self.assertEqual(got[f'q{n}'], want[n], f'q{n}')
        # 均匀分布抽样，覆盖各 capture/life 组合。
        for n in range(0, 40000, 997):
            self.assertEqual(got[f'q{n}'], want[n], f'q{n}')


if __name__ == '__main__':
    unittest.main()
