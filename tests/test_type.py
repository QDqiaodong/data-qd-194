import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from pixel_type.service import process

def sample():return json.loads((Path(__file__).resolve().parents[1]/'fixtures/input.json').read_text())

class TypeTests(unittest.TestCase):
    def test_kerned_wrapping_and_svg_share_positions(self):
        r=process(sample())
        self.assertEqual([p['line'] for p in r['placements']],[0,0,1,1,1,2])
        self.assertEqual([p['pen_x'] for p in r['placements']],[0,3,0,4,6,0])
        root=ET.fromstring(r['svg_text'])
        self.assertEqual(len(root.findall('.//{http://www.w3.org/2000/svg}rect')),48)
        self.assertEqual(r['summary'],dict(lines=3,placed_glyphs=6,drawn_pixels=48,unique_ink_pixels=48,overflow_glyphs=0))

    def test_exact_boundary_no_premature_wrap(self):
        d=sample();d['text']='AV';d['line_width']=7
        r=process(d)
        self.assertEqual(r['summary']['lines'],1);self.assertEqual(r['lines'][0]['advance'],7)

    def test_linebreak_retains_empty_lines_and_source_indices(self):
        d=sample();d['text']='A\n\nV\n'
        r=process(d)
        self.assertEqual(r['summary']['lines'],4)
        self.assertEqual([p['character_index'] for p in r['placements']],[0,3])
        self.assertEqual([p['line'] for p in r['placements']],[0,2])

    def test_ink_overhang_and_wide_glyph_are_reported(self):
        d=sample();d['text']='A';d['line_width']=2;d['glyphs'][0]['offset']=[-1,-1]
        r=process(d)
        self.assertEqual(r['overflow_character_indices'],[0]);self.assertEqual(r['ink_bbox']['min'],[-1,-1])
        self.assertEqual(r['summary']['lines'],1)

    def test_xml_special_character_escaped(self):
        d=sample();d['text']='<';d['glyphs']=[dict(char='<',advance=1,offset=[0,0],rows=['1'])];d['kerning']=[]
        r=process(d);root=ET.fromstring(r['svg_text'])
        self.assertEqual(root.find('{http://www.w3.org/2000/svg}title').text,'<')
        self.assertEqual(len(root.findall('.//{http://www.w3.org/2000/svg}rect')),1)

    def test_missing_glyph_bad_kern_and_ragged_bitmap(self):
        d=sample();d['text']='Z'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['kerning'][0]['adjust']=-4
        with self.assertRaises(ValueError):process(d)
        d=sample();d['glyphs'][0]['rows'][0]='1'
        with self.assertRaises(ValueError):process(d)

    def test_xml_forbidden_noncharacter_is_rejected(self):
        d=sample();d['text']='\ufffe'
        d['glyphs']=[dict(char='\ufffe',advance=1,offset=[0,0],rows=['1'])];d['kerning']=[]
        with self.assertRaises(ValueError):process(d)

if __name__=='__main__':unittest.main()
