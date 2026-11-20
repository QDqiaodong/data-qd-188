from .board import identity, place, rows
from .model import validate
from .regions import area

def process(data):
    board,komi=validate(data)
    history=[identity(board)];next_player=data['next_player'];passes=0;trace=[];illegal=None;captures={'B':0,'W':0}
    for index,move in enumerate(data['moves']):
        reason=None;captured=[];candidate=board
        if passes==2:reason='game_ended'
        elif move['player']!=next_player:reason='wrong_turn'
        elif move['point'] is not None:
            candidate,captured,reason=place(board,tuple(move['point']),next_player,data['size'])
            if reason is None and len(history)>=2 and identity(candidate)==history[-2]:reason='simple_ko'
        if reason:
            illegal={'move_id':move['id'],'index':index,'reason':reason};break
        board=candidate
        passes=passes+1 if move['point'] is None else 0
        captures[next_player]+=len(captured)
        history.append(identity(board))
        trace.append({'move_id':move['id'],'player':next_player,'point':move['point'],'captured':[list(p) for p in captured], 'board':rows(board,data['size'])})
        next_player='W' if next_player=='B' else 'B'
    score=area(board,data['size'],komi)
    score['status']='after_two_passes' if passes==2 else 'provisional'
    return {'schema_version':1,'trace':trace,'first_illegal':illegal,'unprocessed_move_ids':[m['id'] for m in data['moves'][len(trace):]],'board':rows(board,data['size']),
            'next_player':next_player,'captures':captures,'area':score,
            'summary':{'accepted_moves':len(trace),'valid_record':illegal is None,'ended':passes==2,'captured_stones':sum(captures.values()),'board_stones':len(board)}}
