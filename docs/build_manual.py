"""Build the shipped PDF from USER_MANUAL.md using ReportLab (optional extra).

Run from any directory: python docs/build_manual.py
No external fonts, images, web calls or proprietary document tools are required.
"""
from __future__ import annotations
import html
import re
import textwrap
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle,getSampleStyleSheet
from reportlab.platypus import BaseDocTemplate,PageTemplate,Frame,Paragraph,Spacer,PageBreak,Table,TableStyle,Preformatted,KeepTogether
from reportlab.platypus.tableofcontents import TableOfContents

HERE=Path(__file__).resolve().parent
WIDTH,HEIGHT=letter
INK=colors.HexColor('#233b4b');MUTED=colors.HexColor('#607589');ACCENT=colors.HexColor('#276a58');LINE=colors.HexColor('#dae3e9')
styles=getSampleStyleSheet()
styles.add(ParagraphStyle(name='BodyManual',fontName='Helvetica',fontSize=10.1,leading=14.6,textColor=INK,spaceAfter=8))
styles.add(ParagraphStyle(name='Chapter',fontName='Helvetica-Bold',fontSize=22,leading=27,textColor=INK,spaceBefore=0,spaceAfter=16,keepWithNext=True))
styles.add(ParagraphStyle(name='SectionManual',fontName='Helvetica-Bold',fontSize=13,leading=17,textColor=ACCENT,spaceBefore=14,spaceAfter=8,keepWithNext=True))
styles.add(ParagraphStyle(name='TableManual',fontName='Helvetica',fontSize=8.5,leading=12,textColor=INK,spaceAfter=0))
styles.add(ParagraphStyle(name='TableHeaderManual',fontName='Helvetica-Bold',fontSize=8.6,leading=11,textColor=colors.white,spaceAfter=0))
styles.add(ParagraphStyle(name='CodeManual',fontName='Courier',fontSize=8,leading=11,textColor=INK,backColor=colors.HexColor('#f0f4f6'),borderPadding=10,spaceBefore=6,spaceAfter=13))
styles.add(ParagraphStyle(name='CoverManual',fontName='Helvetica-Bold',fontSize=46,leading=51,textColor=INK,spaceAfter=18))
styles.add(ParagraphStyle(name='CoverSubtitle',fontName='Helvetica',fontSize=19,leading=26,textColor=INK,spaceAfter=16))
styles.add(ParagraphStyle(name='SmallManual',fontName='Helvetica',fontSize=9,leading=13,textColor=MUTED,spaceAfter=8))
styles.add(ParagraphStyle(name='TOCManual',fontName='Helvetica',fontSize=9.7,leading=13,leftIndent=0,firstLineIndent=0,textColor=INK,spaceBefore=0,spaceAfter=0))

def clean(text):
    return text.replace('\u2011','-').replace('\u2013','-').replace('\u2014',' - ').replace('\u2192',' -> ').replace('\u2260','!=').replace('\u2026','...').replace('\u00a0',' ')

def inline(text):
    text=html.escape(clean(text))
    text=re.sub(r'`([^`]+)`',r'<font name="Courier">\1</font>',text)
    text=re.sub(r'\*\*([^*]+)\*\*',r'<b>\1</b>',text)
    return text

class ManualDoc(BaseDocTemplate):
    def __init__(self,path):
        super().__init__(str(path),pagesize=letter,leftMargin=48,rightMargin=48,topMargin=61,bottomMargin=51,
            title='AgileCode User Manual 0.1.0',author='AgileCode',subject='Installation, operation, recovery and release qualification')
        self.chapter_index=0
        frame=Frame(48,51,WIDTH-96,HEIGHT-112,id='body',leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)
        self.addPageTemplates(PageTemplate(id='manual',frames=[frame],onPage=self.decorate))
    def decorate(self,canvas,doc):
        canvas.saveState()
        if doc.page>1:
            canvas.setFont('Helvetica-Bold',8);canvas.setFillColor(ACCENT);canvas.drawString(48,HEIGHT-32,'AGILECODE / USER MANUAL')
            canvas.setFont('Helvetica',8);canvas.setFillColor(MUTED);canvas.drawRightString(WIDTH-48,HEIGHT-32,'SOURCE RELEASE 0.1.0')
            canvas.setStrokeColor(LINE);canvas.line(48,HEIGHT-41,WIDTH-48,HEIGHT-41)
        canvas.setStrokeColor(LINE);canvas.line(48,37,WIDTH-48,37)
        canvas.setFont('Helvetica',8);canvas.setFillColor(MUTED);canvas.drawString(48,23,'23 September 2026  |  Design baseline v1.1')
        canvas.drawRightString(WIDTH-48,23,str(doc.page))
        canvas.restoreState()
    def afterFlowable(self,flow):
        if isinstance(flow,Paragraph) and flow.style.name=='Chapter':
            title=flow.getPlainText();key='chapter-'+str(self.seq.nextf('chapter'))
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title,key,0,False)
            if title != 'Contents':
                self.notify('TOCEntry',(0,title,self.page,key))

def parse_markdown(text):
    lines=text.splitlines();i=0;out=[]
    while i<len(lines):
        line=lines[i]
        if not line.strip():i+=1;continue
        if line.startswith('# '):
            out.extend([PageBreak(),Paragraph(inline(line[2:]),styles['Chapter'])]);i+=1;continue
        if line.startswith('## '):
            out.append(Paragraph(inline(line[3:]),styles['SectionManual']));i+=1;continue
        if line.startswith('```'):
            i+=1;block=[]
            while i<len(lines) and not lines[i].startswith('```'):
                value=clean(lines[i])
                # Readable wrapping of particularly long command/path lines.
                block.extend(textwrap.wrap(value,105,replace_whitespace=False,drop_whitespace=False,subsequent_indent='    ') or [''])
                i+=1
            out.append(Preformatted('\n'.join(block),styles['CodeManual']));i+=1;continue
        if line.startswith('|'):
            rows=[]
            while i<len(lines) and lines[i].startswith('|'):
                cells=[v.strip() for v in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-+:?',v.replace(' ','')) for v in cells):rows.append(cells)
                i+=1
            count=len(rows[0]);weights={2:[.29,.71],3:[.26,.29,.45],4:[.19,.27,.27,.27]}.get(count,[1/count]*count)
            data=[[Paragraph(inline(c),styles['TableHeaderManual' if ri==0 else 'TableManual']) for c in row] for ri,row in enumerate(rows)]
            table=Table(data,colWidths=[(WIDTH-96)*w for w in weights],repeatRows=1,hAlign='LEFT')
            table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),INK),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#f0f4f6'),colors.white]),('LINEBELOW',(0,0),(-1,0),.5,INK),('LINEBELOW',(0,1),(-1,-1),.35,LINE)]))
            out.extend([table,Spacer(1,12)]);continue
        parts=[line];i+=1
        while i<len(lines) and lines[i].strip() and not lines[i].startswith(('# ','## ','```','|')):
            parts.append(lines[i]);i+=1
        out.append(Paragraph(inline(' '.join(parts)),styles['BodyManual']))
    return out

def build():
    path=HERE/'AgileCode_User_Manual_v0.1.0.pdf'
    story=[Spacer(1,45),Paragraph('AGENTIC DEVELOPMENT / LOCAL WORKBENCH',styles['SmallManual']),Spacer(1,25),
        Paragraph('AgileCode',styles['CoverManual']),Paragraph('User and operator manual',styles['CoverSubtitle']),
        Paragraph('Install. Configure. Execute. Inspect. Recover.',styles['SectionManual']),Spacer(1,25),
        Paragraph('Source release <b>0.1.0</b><br/>Implementation baseline <b>Design specification v1.1</b><br/>Issued <b>23 September 2026</b>',styles['BodyManual']),Spacer(1,35),
        Paragraph('A headless Python engine with a replaceable PySide6/QML desktop interface, persistent ticket worktrees, and an enforced eight-step development process.',styles['CoverSubtitle']),Spacer(1,26),
        Paragraph('<b>Qualification note</b><br/>Core behavior is exercised by the included automated tests. GUI runtime, live local-model behavior and large-repository performance require local validation. The manual identifies these limits explicitly.',styles['BodyManual']),
        PageBreak(),Paragraph('Contents',styles['Chapter'])]
    toc=TableOfContents(tableStyle=TableStyle([('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]));toc.levelStyles=[styles['TOCManual']];story.extend([toc,Spacer(1,16)])
    story+=parse_markdown((HERE/'USER_MANUAL.md').read_text())
    ManualDoc(path).multiBuild(story)
    print(path)

if __name__=='__main__':build()
