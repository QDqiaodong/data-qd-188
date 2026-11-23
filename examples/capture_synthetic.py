"""合成验收数据生成器（规格复现用；读取与核验不计入受限进程）。

384 盘 19 路、白先、贴目 "0"。黑初始占全部偶数行及奇数行的第 0 列；九条奇数行
j=0..8 的初始白占第 k+1..17 列，k 依赖盘序 g 与 j。依 j 顺序：白在第 1..k 列落子，
前 k-1 次白落后黑停手，最后一次白落后黑在第 18 列落子并提掉该行第 1..17 列全部
白子。第 j 行的提子在手号 2*(到该行各 k 之和)，其中第 t 列（1..k）为 played、
寿命 2(k-t)+1；第 k+1..17 列为 setup。
"""
import json
import os
import sys

def k_value(g, j):
    return 1 + ((g // (16 ** (j % 3)) + 7 * j + (j // 3) * (g % 11)) % 16)

def build_game(g):
    size = 19
    black = [[r, c] for r in range(19) for c in range(19) if r % 2 == 0 or c == 0]
    white = []
    ks = [k_value(g, j) for j in range(9)]
    for j, k in enumerate(ks):
        row = 2 * j + 1
        for c in range(k + 1, 18):
            white.append([row, c])
    moves = []
    counter = 0
    for j, k in enumerate(ks):
        row = 2 * j + 1
        for t in range(1, k + 1):
            counter += 1
            moves.append({'id': f'm{counter}', 'player': 'W', 'point': [row, t]})
            if t < k:
                counter += 1
                moves.append({'id': f'm{counter}', 'player': 'B', 'point': None})
        counter += 1
        moves.append({'id': f'm{counter}', 'player': 'B', 'point': [row, 18]})
    return {'size': size, 'setup': {'black': black, 'white': white},
            'next_player': 'W', 'komi': '0', 'moves': moves}

def write_source(directory):
    os.makedirs(directory, exist_ok=True)
    games = []
    for g in range(384):
        name = f'g{g}.json'
        with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
            json.dump(build_game(g), f)
        games.append({'game_id': f'g{g}', 'path': name})
    queries = []
    for n in range(40000):
        cf = 1 + n % 173
        ct = cf + 1 + ((n // 173) % 96)
        lf = 1 + n // (173 * 96)
        lt = 7 + n % 25
        queries.append({'query_id': f'q{n}', 'capture_from': cf, 'capture_to': ct,
                        'origin': 'played', 'life_from': lf, 'life_to': lt})
    manifest = {'games': games, 'queries': queries, 'lookups': []}
    path = os.path.join(directory, 'manifest.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f)
    return path

if __name__ == '__main__':
    print(write_source(sys.argv[1]))
