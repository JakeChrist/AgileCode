"""Pure-Python syntax spans. No Qt objects are constructed by worker threads."""
from pygments import lex
from pygments.lexers import get_lexer_for_filename,get_lexer_by_name,TextLexer
from pygments.styles import get_style_by_name


def spans(text,filename,dark,language=None):
    try:lexer=get_lexer_by_name(language) if language else get_lexer_for_filename(filename)
    except Exception:lexer=TextLexer()
    style=get_style_by_name("monokai" if dark else "friendly")
    lines={};line=0;column=0
    for token,content in lex(text,lexer):
        format_=style.style_for_token(token)
        parts=content.split("\n")
        for i,part in enumerate(parts):
            width=len(part.encode("utf-16-le"))//2
            if width:lines.setdefault(line,[]).append((column,width,format_))
            column+=width
            if i<len(parts)-1:line+=1;column=0
    return lines
