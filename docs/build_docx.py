import re
import sys
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Cm

SRC = sys.argv[1] if len(sys.argv) > 1 else "docs/AI_Logistics_Concept.md"
OUT = sys.argv[2] if len(sys.argv) > 2 else "docs/AI_Logistics_Concept.docx"
FONT = "TH Sarabun New"
BLUE = RGBColor(0x1F, 0x3A, 0x6E)


def set_font(style_or_run, size=None, bold=None, color=None):
    f = style_or_run.font
    f.name = FONT
    rpr = style_or_run.element.get_or_add_rPr() if hasattr(style_or_run.element, "get_or_add_rPr") else style_or_run.element.rPr
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(a), FONT)
    if size:
        f.size = Pt(size)
    if bold is not None:
        f.bold = bold
    if color is not None:
        f.color.rgb = color


def add_runs(par, text, size=None):
    for part in re.split(r"(\*\*[^*]+\*\*)", text):
        if not part:
            continue
        bold = part.startswith("**") and part.endswith("**")
        r = par.add_run(part[2:-2] if bold else part)
        set_font(r, size=size, bold=True if bold else None)


def shade(cell, hex_color):
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcpr.append(shd)


doc = Document()
sec = doc.sections[0]
sec.page_width, sec.page_height = Cm(21), Cm(29.7)
sec.left_margin = sec.right_margin = Cm(2.2)
sec.top_margin = sec.bottom_margin = Cm(2)

set_font(doc.styles["Normal"], size=16)
for name, size in (("Heading 1", 22), ("Heading 2", 19), ("Heading 3", 17)):
    set_font(doc.styles[name], size=size, bold=True, color=BLUE)
for name in ("List Bullet", "List Number"):
    set_font(doc.styles[name], size=16)

lines = open(SRC, encoding="utf-8").read().splitlines()
i = 0
first_title = True
while i < len(lines):
    line = lines[i].rstrip()
    if not line.strip() or line.strip() == "---":
        i += 1
        continue

    if line.startswith("# "):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(line[2:])
        set_font(r, size=26, bold=True, color=BLUE)
        p.paragraph_format.space_after = Pt(14)
    elif line.startswith("### "):
        doc.add_heading(line[4:], level=3)
    elif line.startswith("## "):
        doc.add_heading(line[3:], level=2)
    elif line.startswith("|"):
        rows = []
        while i < len(lines) and lines[i].startswith("|"):
            cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                rows.append(cells)
            i += 1
        table = doc.add_table(rows=len(rows), cols=len(rows[0]))
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for ri, row in enumerate(rows):
            for ci, text in enumerate(row):
                cell = table.cell(ri, ci)
                par = cell.paragraphs[0]
                if ri == 0:
                    r = par.add_run(text)
                    set_font(r, size=15, bold=True, color=RGBColor(255, 255, 255))
                    shade(cell, "1F3A6E")
                else:
                    add_runs(par, text, size=15)
                    if ri % 2 == 0:
                        shade(cell, "EEF2F9")
        doc.add_paragraph()
        continue
    elif re.match(r"^- ", line):
        p = doc.add_paragraph(style="List Bullet")
        add_runs(p, line[2:])
    elif re.match(r"^\d+\. ", line):
        p = doc.add_paragraph(style="List Number")
        add_runs(p, re.sub(r"^\d+\. ", "", line))
    elif line.startswith("("):
        p = doc.add_paragraph()
        r = p.add_run(line)
        set_font(r, size=14, color=RGBColor(0x66, 0x66, 0x66))
        r.italic = True
    else:
        p = doc.add_paragraph()
        add_runs(p, line)
    i += 1

# page numbers in footer
fp = sec.footer.paragraphs[0]
fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = fp.add_run()
set_font(run, size=14)
for t, txt in (("begin", None), (None, "PAGE"), ("end", None)):
    if t:
        el = OxmlElement("w:fldChar")
        el.set(qn("w:fldCharType"), t)
    else:
        el = OxmlElement("w:instrText")
        el.set(qn("xml:space"), "preserve")
        el.text = txt
    run._r.append(el)

doc.save(OUT)
print("saved", OUT)
