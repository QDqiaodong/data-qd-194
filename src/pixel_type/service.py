from .layout import layout
from .model import validate
from .render import render

def process(data):
    glyphs,kerning,text=validate(data)
    lines,placements=layout(glyphs,kerning,text,data['line_width'],data['line_height'])
    bbox,overflow,pixels,svg=render(glyphs,placements,lines,data['line_width'],data['line_height'],text)
    return {'schema_version':1,'lines':lines,'placements':placements,'ink_bbox':bbox,'overflow_character_indices':overflow,'svg_text':svg,
            'summary':{'lines':len(lines),'placed_glyphs':len(placements),'drawn_pixels':len(pixels),'unique_ink_pixels':len(set(pixels)),'overflow_glyphs':len(overflow)}}
