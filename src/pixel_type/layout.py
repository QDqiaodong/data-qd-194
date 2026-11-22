def layout(glyphs,kerning,text,width,line_height):
    lines=[{'index':0,'advance':0,'character_indices':[]}];placements=[]
    line=0;pen=0;previous=None
    for index,ch in enumerate(text):
        if ch=='\n':
            line+=1;lines.append({'index':line,'advance':0,'character_indices':[]});pen=0;previous=None;continue
        glyph=glyphs[ch];kern=kerning.get((previous,ch),0)
        if lines[line]['character_indices'] and pen+kern+glyph['advance']>width:
            line+=1;lines.append({'index':line,'advance':0,'character_indices':[]});pen=0;previous=None;kern=0
        x=pen+kern
        placements.append({'character_index':index,'char':ch,'line':line,'pen_x':x,'applied_kerning':kern,
                           'ink_origin':[x+glyph['offset'][0],line*line_height+glyph['offset'][1]],'advance':glyph['advance']})
        pen=x+glyph['advance'];previous=ch
        lines[line]['advance']=pen;lines[line]['character_indices'].append(index)
    return lines,placements
