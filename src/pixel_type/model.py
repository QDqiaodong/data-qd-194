from .checks import array,fields,integer,unique

def character(value):
    if not isinstance(value,str) or len(value)!=1 or ord(value)<32 or 0xD800<=ord(value)<=0xDFFF or ord(value) in (127,0xFFFE,0xFFFF):raise ValueError('one XML-compatible printable Unicode scalar required')
    return value

def validate(data):
    fields(data,('glyphs','kerning','text','line_width','line_height'))
    integer(data['line_width'],1,10000);integer(data['line_height'],1,1000)
    text=data['text']
    if not isinstance(text,str) or not 1<=len(text)<=2000:raise ValueError('1..2000 text codepoints required')
    for ch in text:
        if ch!='\n':character(ch)
    glyphs={}
    for glyph in array(data['glyphs'],1,256):
        fields(glyph,('char','advance','offset','rows'));ch=character(glyph['char'])
        if ch in glyphs:raise ValueError('duplicate glyph')
        integer(glyph['advance'],1,128)
        for v in array(glyph['offset'],2,2):integer(v,-128,128)
        rows=array(glyph['rows'],0,32);width=None
        for row in rows:
            if not isinstance(row,str) or not 1<=len(row)<=32 or any(c not in '01' for c in row):raise ValueError('bitmap row must be 1..32 bits')
            if width is not None and width!=len(row):raise ValueError('bitmap rows must share width')
            width=len(row)
        glyphs[ch]=glyph
    if any(ch!='\n' and ch not in glyphs for ch in text):raise ValueError('missing glyph; no implicit fallback')
    kerning={}
    for pair in array(data['kerning'],0,2000):
        fields(pair,('left','right','adjust'));left=character(pair['left']);right=character(pair['right'])
        if left not in glyphs or right not in glyphs or (left,right) in kerning:raise ValueError('unknown or duplicate kern pair')
        adjust=integer(pair['adjust'],-127,127)
        if glyphs[right]['advance']+adjust<=0:raise ValueError('kerned pen advance must remain positive')
        kerning[left,right]=adjust
    pixels={ch:sum(row.count('1') for row in g['rows']) for ch,g in glyphs.items()}
    if sum(pixels.get(ch,0) for ch in text)>200000:raise ValueError('render pixel budget exceeded')
    return glyphs,kerning,text
