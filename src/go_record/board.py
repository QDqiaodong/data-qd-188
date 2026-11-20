def adjacent(point, size):
    row, col = point
    return [(r,c) for r,c in ((row-1,col),(row+1,col),(row,col-1),(row,col+1)) if 0<=r<size and 0<=c<size]

def group(board, point, size):
    color=board[point]
    stones={point}; liberties=set(); stack=[point]
    while stack:
        current=stack.pop()
        for other in adjacent(current,size):
            if other not in board:
                liberties.add(other)
            elif board[other]==color and other not in stones:
                stones.add(other);stack.append(other)
    return stones,liberties

def identity(board):
    return tuple(sorted(board.items()))

def rows(board,size):
    return [''.join(board.get((r,c),'.') for c in range(size)) for r in range(size)]

def place(board,point,color,size):
    if not (0<=point[0]<size and 0<=point[1]<size):
        return None, [], 'out_of_bounds'
    if point in board:
        return None, [], 'occupied'
    result=dict(board);result[point]=color
    captured=set()
    for neighbor in adjacent(point,size):
        if neighbor in result and result[neighbor]!=color:
            stones,liberties=group(result,neighbor,size)
            if not liberties:
                captured.update(stones)
                for stone in stones: del result[stone]
    if not group(result,point,size)[1]:
        return None, [], 'suicide'
    return result,sorted(captured),None
