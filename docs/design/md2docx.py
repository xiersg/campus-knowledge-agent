"""将 docs/design/*.md 转换为排版好的 .docx（A4、宋体/黑体、封面、目录域、页脚页码）。

用法: python md2docx.py 输入.md 输出.docx
"""
from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.enum.style import WD_STYLE_TYPE

BODY_FONT = "宋体"
HEAD_FONT = "黑体"
MONO_FONT = "Consolas"
DARK = RGBColor(0x1D, 0x4E, 0x89)
BLACK = RGBColor(0, 0, 0)
GRAY = RGBColor(0x59, 0x59, 0x59)
HEADER_BG = "D9E2F3"
MAX_IMG_W = 15.0  # cm
MAX_IMG_H = 20.0  # cm


def set_run(run, size=12, bold=False, italic=False, mono=False, color=None, font=None):
    f = font or (MONO_FONT if mono else BODY_FONT)
    run.font.name = f
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), f)
    rfonts.set(qn("w:hAnsi"), f)
    rfonts.set(qn("w:eastAsia"), f if mono else BODY_FONT)


def add_inline(par, text, size=12, base_bold=False):
    """解析 **加粗** 与 `行内代码`。"""
    for i, seg in enumerate(re.split(r"(\*\*.*?\*\*|`[^`]*`)", text)):
        if not seg:
            continue
        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
            r = par.add_run(seg[2:-2])
            set_run(r, size=size, bold=True)
        elif seg.startswith("`") and seg.endswith("`") and len(seg) > 2:
            r = par.add_run(seg[1:-1])
            set_run(r, size=size - 1.5, mono=True)
        else:
            r = par.add_run(seg)
            set_run(r, size=size, bold=base_bold)


def png_size(path: Path):
    data = path.read_bytes()
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def add_image(doc: Document, src: str, caption: str):
    img = Path(src)
    if not img.is_absolute():
        img = Path(doc._md_dir) / img
    w, h = png_size(img)
    width = MAX_IMG_W
    if h / w * width > MAX_IMG_H:
        width = MAX_IMG_H * w / h
    par = doc.add_paragraph()
    par.alignment = WD_ALIGN_PARAGRAPH.CENTER
    par.paragraph_format.space_before = Pt(6)
    run = par.add_run()
    run.add_picture(str(img), width=Cm(width))
    if caption:
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.paragraph_format.space_after = Pt(10)
        add_inline(cap, caption, size=10.5, base_bold=True)


def add_table(doc: Document, rows: list[list[str]]):
    if not rows:
        return
    ncol = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=ncol)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, row in enumerate(rows):
        for j in range(ncol):
            cell = table.cell(i, j)
            text = row[j] if j < len(row) else ""
            cell.text = ""
            par = cell.paragraphs[0]
            par.paragraph_format.space_before = Pt(1)
            par.paragraph_format.space_after = Pt(1)
            add_inline(par, text, size=10.5, base_bold=(i == 0))
            if i == 0:
                tcpr = cell._tc.get_or_add_tcPr()
                shd = tcpr.makeelement(qn("w:shd"), {qn("w:val"): "clear", qn("w:fill"): HEADER_BG})
                tcpr.append(shd)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def add_field(par, instr: str):
    r1 = par.add_run()
    fld = r1._element.makeelement(qn("w:fldChar"), {qn("w:fldCharType"): "begin", qn("w:dirty"): "true"})
    r1._element.append(fld)
    r2 = par.add_run()
    it = r2._element.makeelement(qn("w:instrText"), {qn("xml:space"): "preserve"})
    it.text = instr
    r2._element.append(it)
    r3 = par.add_run()
    sep = r3._element.makeelement(qn("w:fldChar"), {qn("w:fldCharType"): "separate"})
    r3._element.append(sep)
    r4 = par.add_run("（打开文档后在 Word 中 Ctrl+A → F9 更新目录页码）")
    set_run(r4, size=10.5, italic=True, color=GRAY)
    r5 = par.add_run()
    end = r5._element.makeelement(qn("w:fldChar"), {qn("w:fldCharType"): "end"})
    r5._element.append(end)


def setup_styles(doc: Document):
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(12)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(0)
    for name, size in (("Heading 1", 16), ("Heading 2", 14), ("Heading 3", 13), ("Heading 4", 12)):
        st = doc.styles[name]
        st.font.name = HEAD_FONT
        st.font.size = Pt(size)
        st.font.bold = True
        st.font.color.rgb = BLACK
        st.element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_FONT)
        st.paragraph_format.space_before = Pt(14 if name != "Heading 1" else 20)
        st.paragraph_format.space_after = Pt(8)
        st.paragraph_format.line_spacing = 1.3
    bullet = doc.styles["List Bullet"]
    bullet.font.name = BODY_FONT
    bullet.font.size = Pt(12)
    bullet.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    bullet.paragraph_format.line_spacing = 1.5


def setup_page(doc: Document, title: str):
    sec = doc.sections[0]
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.top_margin = Cm(2.54)
    sec.bottom_margin = Cm(2.54)
    sec.left_margin = Cm(2.8)
    sec.right_margin = Cm(2.8)
    hdr = sec.header.paragraphs[0]
    hdr.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    r = hdr.add_run(title)
    set_run(r, size=9, color=GRAY)
    ftr = sec.footer.paragraphs[0]
    ftr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_field(ftr, " PAGE ")


def cover(doc: Document, meta: dict):
    for _ in range(5):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(meta["title"])
    set_run(r, size=26, bold=True, font=HEAD_FONT)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(meta.get("project", ""))
    set_run(r, size=16, color=DARK)
    doc.add_paragraph()
    for key, label in (("team", "团队名称"), ("members", "团队成员"), ("version", "文档版本"), ("date", "编制日期")):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(f"{label}：{meta.get(key, '')}")
        set_run(r, size=14)
    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(f"参考标准：{meta.get('standard', '')}")
    set_run(r, size=12, color=GRAY)
    doc.add_page_break()


def revision_table(doc: Document):
    p = doc.add_paragraph()
    r = p.add_run("修订记录")
    set_run(r, size=16, bold=True, font=HEAD_FONT)
    add_table(doc, [["版本", "日期", "修订人", "说明"], ["v1.0", "2026-11-22", "杨富林、黎正阳", "首次发布（第二次团队作业交付）"]])
    doc.add_paragraph()
    p = doc.add_paragraph()
    r = p.add_run("目录")
    set_run(r, size=16, bold=True, font=HEAD_FONT)
    p = doc.add_paragraph()
    add_field(p, ' TOC \\o "1-3" \\h \\z \\u ')
    doc.add_page_break()


def convert(md_path: Path, out_path: Path):
    text = md_path.read_text(encoding="utf-8")
    meta = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        text = text[m.end():]

    doc = Document()
    doc._md_dir = md_path.parent
    doc.core_properties.title = meta.get("title", "")
    doc.core_properties.author = meta.get("team", "")
    setup_styles(doc)
    setup_page(doc, meta.get("title", ""))
    cover(doc, meta)
    revision_table(doc)

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped == "---":
            doc.add_page_break()
            i += 1
            continue
        hm = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if hm:
            level = len(hm.group(1))
            par = doc.add_paragraph(style=f"Heading {level}")
            add_inline(par, hm.group(2), size={1: 16, 2: 14, 3: 13, 4: 12}[level], base_bold=True)
            i += 1
            continue
        im = re.match(r"^!\[(.*?)\]\((.*?)\)$", stripped)
        if im:
            add_image(doc, im.group(2), im.group(1))
            i += 1
            continue
        if stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            add_table(doc, rows)
            continue
        if stripped.startswith("```"):
            i += 1
            code_lines = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            for cl in code_lines:
                par = doc.add_paragraph()
                par.paragraph_format.space_before = Pt(0)
                par.paragraph_format.space_after = Pt(0)
                par.paragraph_format.line_spacing = 1.15
                ppr = par._element.get_or_add_pPr()
                shd = ppr.makeelement(qn("w:shd"), {qn("w:val"): "clear", qn("w:fill"): "F2F2F2"})
                ppr.append(shd)
                r = par.add_run(cl if cl else " ")
                set_run(r, size=10, mono=True)
            continue
        if stripped.startswith(">"):
            par = doc.add_paragraph()
            par.paragraph_format.left_indent = Cm(0.74)
            add_inline(par, stripped.lstrip(">").strip(), size=11, base_bold=False)
            for r in par.runs:
                r.font.italic = True
                r.font.color.rgb = GRAY
            i += 1
            continue
        if re.match(r"^[-*]\s+", stripped):
            par = doc.add_paragraph(style="List Bullet")
            add_inline(par, re.sub(r"^[-*]\s+", "", stripped))
            i += 1
            continue
        if re.match(r"^\d+\.\s+", stripped):
            par = doc.add_paragraph()
            par.paragraph_format.left_indent = Cm(0.74)
            par.paragraph_format.first_line_indent = Cm(-0.74)
            add_inline(par, stripped)
            i += 1
            continue
        par = doc.add_paragraph()
        par.paragraph_format.first_line_indent = Pt(24)
        add_inline(par, stripped)
        i += 1

    doc.save(out_path)
    print(f"saved: {out_path}")


if __name__ == "__main__":
    convert(Path(sys.argv[1]), Path(sys.argv[2]))
