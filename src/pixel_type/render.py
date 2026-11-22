from html import escape

def render(glyphs,placements,lines,line_width,line_height,text):
    pixels=[];overflow=[]
    for placement in placements:
        x,y=placement['ink_origin'];glyph=glyphs[placement['char']]
        local=[(x+col,y+row) for row,bits in enumerate(glyph['rows']) for col,bit in enumerate(bits) if bit=='1']
        pixels.extend(local)
        top=placement['line']*line_height
        if placement['pen_x']<0 or placement['pen_x']+placement['advance']>line_width or any(px<0 or px>=line_width or py<top or py>=top+line_height for px,py in local):
            overflow.append(placement['character_index'])
    bbox={'min':[min(x for x,y in pixels),min(y for x,y in pixels)],'max_exclusive':[max(x for x,y in pixels)+1,max(y for x,y in pixels)+1]} if pixels else None
    left=min(0,bbox['min'][0]) if bbox else 0;top=min(0,bbox['min'][1]) if bbox else 0
    right=max(line_width,bbox['max_exclusive'][0]) if bbox else line_width
    bottom=max(len(lines)*line_height,bbox['max_exclusive'][1]) if bbox else len(lines)*line_height
    rects=''.join(f'<rect x="{x}" y="{y}" width="1" height="1"/>' for x,y in pixels)
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{left} {top} {right-left} {bottom-top}" shape-rendering="crispEdges"><title>{escape(text)}</title><g fill="black">{rects}</g></svg>'
    return bbox,overflow,pixels,svg
