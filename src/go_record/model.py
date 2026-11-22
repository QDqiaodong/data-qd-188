from .board import group
from .checks import array, decimal, fields, ident, integer, unique

def coordinate(value):
    array(value,2,2)
    return tuple(integer(x,-100,100) for x in value)

def setup_board(data):
    size=integer(data['size'],3,19)
    if data['next_player'] not in ('B','W'):raise ValueError('B/W required')
    komi=decimal(data['komi'])
    if abs(komi)>100:raise ValueError('komi outside bounded model')
    setup=fields(data['setup'],('black','white'))
    board={}
    for key,color in [('black','B'),('white','W')]:
        for value in array(setup[key],0,size*size):
            point=coordinate(value)
            if not all(0<=x<size for x in point) or point in board:raise ValueError('invalid setup coordinate')
            board[point]=color
    seen=set()
    for point in board:
        if point not in seen:
            stones,liberties=group(board,point,size)
            if not liberties:raise ValueError('setup contains a group without liberties')
            seen.update(stones)
    return board,komi

def validate(data):
    fields(data,('size','setup','next_player','komi','moves'))
    board,komi=setup_board(data)
    for move in array(data['moves'],0,2000):
        fields(move,('id','player','point'));ident(move['id'])
        if move['player'] not in ('B','W'):raise ValueError('B/W required')
        if move['point'] is not None:coordinate(move['point'])
    unique([m['id'] for m in data['moves']])
    return board,komi
