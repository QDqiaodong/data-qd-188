import json
from pathlib import Path
import unittest
from go_record.service import process

def sample():return json.loads((Path(__file__).resolve().parents[1]/'fixtures/input.json').read_text())
def move(id,player,point):return dict(id=id,player=player,point=point)

class RecordTests(unittest.TestCase):
    def test_capture_before_suicide_and_two_passes(self):
        r=process(sample())
        self.assertEqual(r['summary'],dict(accepted_moves=3,valid_record=True,ended=True,captured_stones=1,board_stones=5))
        self.assertEqual(r['trace'][0]['captured'],[[1,1]])
        self.assertEqual(r['area']['territory'],{'B':2,'W':0})
        self.assertEqual(r['area']['neutral_points'],18)
        self.assertEqual(r['area']['margin'],'9/2')

    def test_occupied_and_wrong_turn_stop_at_prefix(self):
        for first,reason in [(move('bad','B',[0,1]),'occupied'),(move('bad','W',None),'wrong_turn')]:
            d=sample();d['moves']=[first,move('ignored','W',None)]
            r=process(d)
            self.assertEqual(r['first_illegal']['reason'],reason)
            self.assertEqual(r['summary']['accepted_moves'],0)
            self.assertEqual(r['unprocessed_move_ids'],['bad','ignored'])

    def test_suicide_and_out_of_bounds(self):
        d=sample();d['setup']={'black':[[0,1],[1,0],[1,2],[2,1]],'white':[]};d['next_player']='W'
        d['moves']=[move('bad','W',[1,1])]
        self.assertEqual(process(d)['first_illegal']['reason'],'suicide')
        d['moves']=[move('outside','W',[5,0])]
        self.assertEqual(process(d)['first_illegal']['reason'],'out_of_bounds')

    def test_simple_ko_and_pass_breaks_ban(self):
        d=sample();d['setup']={'black':[[0,1],[1,0],[2,1]],'white':[[1,1],[0,2],[2,2],[1,3]]}
        d['moves']=[move('take','B',[1,2]),move('retake','W',[1,1])]
        r=process(d)
        self.assertEqual(r['first_illegal']['reason'],'simple_ko')
        self.assertEqual(r['captures']['B'],1)
        d['moves']=[move('take','B',[1,2]),move('wait','W',None),move('elsewhere','B',[4,4]),move('retake','W',[1,1])]
        self.assertTrue(process(d)['summary']['valid_record'])

    def test_area_conserves_points_and_empty_board_neutral(self):
        d=sample();d['setup']={'black':[],'white':[]};d['moves']=[];d['komi']='0'
        r=process(d)
        self.assertEqual(r['area']['neutral_points'],25)
        self.assertEqual(r['area']['winner'],'draw')
        a=process(sample())['area']
        self.assertEqual(sum(a['stones'].values())+sum(a['territory'].values())+a['neutral_points'],25)

    def test_invalid_setup_and_moves_after_end(self):
        d=sample();d['setup']['white'].append([0,1])
        with self.assertRaises(ValueError):process(d)
        d=sample();d['moves'].append(move('extra','W',[3,3]))
        self.assertEqual(process(d)['first_illegal']['reason'],'game_ended')

if __name__=='__main__':unittest.main()
