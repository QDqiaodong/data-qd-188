from .board import adjacent

def area(board,size,komi):
    unseen={(r,c) for r in range(size) for c in range(size) if (r,c) not in board}
    regions=[];territory={'B':0,'W':0};neutral=0
    while unseen:
        start=min(unseen);unseen.remove(start)
        points={start};stack=[start];borders=set()
        while stack:
            for point in adjacent(stack.pop(),size):
                if point in board:borders.add(board[point])
                elif point in unseen:
                    unseen.remove(point);points.add(point);stack.append(point)
        owner=next(iter(borders)) if len(borders)==1 else None
        if owner:territory[owner]+=len(points)
        else:neutral+=len(points)
        regions.append({'id':len(regions)+1,'points':[list(p) for p in sorted(points)],'border_colors':sorted(borders),'owner':owner})
    stones={color:sum(c==color for c in board.values()) for color in ('B','W')}
    black=stones['B']+territory['B'];white=stones['W']+territory['W']+komi
    difference=black-white
    return {'regions':regions,'stones':stones,'territory':territory,'neutral_points':neutral,'black_area':str(black),'white_area_with_komi':str(white),
            'winner':'B' if difference>0 else 'W' if difference<0 else 'draw','margin':str(abs(difference))}
