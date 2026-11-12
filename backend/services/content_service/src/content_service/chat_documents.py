"""Create genuine private DOCX/PDF artifacts from the assistant's Markdown text."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4
from xml.sax.saxutils import escape

MAX_DOCUMENT_CHARACTERS = 100000
FONT_PATH = Path(__file__).with_name("chat_fonts") / "NotoSansSC-Regular.ttf"
PDF_FONT = "ContentChatNotoSansSC"
_font_lock = RLock()


class ChatDocumentError(ValueError):
    """A safe error suitable for returning to a chat user."""


@dataclass(frozen=True)
class DocumentBlock:
    kind: str
    text: str = ""
    level: int = 0
    rows: tuple[tuple[str, ...], ...] = ()


def _cells(line: str) -> tuple[str, ...]:
    return tuple(
        cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
    )


def parse_document_markdown(content: str) -> list[DocumentBlock]:
    """Parse a bounded document subset; HTML and links never fetch external data."""
    if not isinstance(content, str) or not content.strip():
        raise ChatDocumentError("文档正文不能为空")
    if len(content) > MAX_DOCUMENT_CHARACTERS:
        raise ChatDocumentError("文档正文超过 10 万字，请拆分后生成")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in content):
        raise ChatDocumentError("文档正文包含不可显示的控制字符")
    lines = content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.startswith("```"):
            code = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            blocks.append(DocumentBlock("code", "\n".join(code)))
            index += 1
            continue
        if index + 1 < len(lines) and "|" in line:
            separator = _cells(lines[index + 1])
            if separator and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
                rows = [_cells(line)]
                index += 2
                while index < len(lines) and "|" in lines[index] and lines[index].strip():
                    rows.append(_cells(lines[index]))
                    index += 1
                columns = len(rows[0])
                if columns > 8 or len(rows) > 1000 or any(len(row) != columns for row in rows):
                    raise ChatDocumentError("表格需保持每行列数一致，最多 8 列、1000 行")
                blocks.append(DocumentBlock("table", rows=tuple(rows)))
                continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        bullet = re.match(r"^[-*+]\s+(.+)$", line)
        ordered = re.match(r"^(\d+)[.)]\s+(.+)$", line)
        if heading:
            blocks.append(DocumentBlock("heading", heading[2], level=len(heading[1])))
        elif bullet:
            blocks.append(DocumentBlock("bullet", bullet[1]))
        elif ordered:
            # Preserve the model's explicit numbering instead of sharing Word's global list counter.
            blocks.append(DocumentBlock("ordered", ordered[2], level=int(ordered[1])))
        elif re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", line):
            blocks.append(DocumentBlock("break"))
        elif line.startswith("> "):
            blocks.append(DocumentBlock("quote", line[2:]))
        else:
            blocks.append(DocumentBlock("paragraph", line))
        index += 1
    if len(blocks) > 5000:
        raise ChatDocumentError("文档段落过多，请拆分后生成")
    return blocks


def _inline_runs(text: str):
    pattern = r"(\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]+`|\*[^*\n]+\*|\[[^]\n]+\]\([^)\n]+\))"
    for piece in re.split(pattern, text):
        if not piece:
            continue
        if piece.startswith(("**", "__")) and piece.endswith(piece[:2]):
            yield piece[2:-2], "bold"
        elif piece.startswith("*") and piece.endswith("*"):
            yield piece[1:-1], "italic"
        elif piece.startswith("`") and piece.endswith("`"):
            yield piece[1:-1], "code"
        elif piece.startswith("[") and "](" in piece:
            label, url = piece[1:-1].split("](", 1)
            yield f"{label} ({url})", "plain"
        else:
            yield piece, "plain"


def _docx(output: Path, title: str, blocks: list[DocumentBlock]) -> None:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt, RGBColor

    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.top_margin = section.bottom_margin = Mm(20)
    section.left_margin = section.right_margin = Mm(20)
    for name, size in (
        ("Normal", 11),
        ("Title", 22),
        ("Heading 1", 16),
        ("Heading 2", 14),
        ("Heading 3", 12),
        ("List Bullet", 11),
        ("List Number", 11),
    ):
        style = document.styles[name]
        style.font.name = "Microsoft YaHei"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0x1D, 0x24, 0x29)
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "Microsoft YaHei")
        style.paragraph_format.space_after = Pt(7)
        style.paragraph_format.line_spacing = 1.35
    document.core_properties.title = title
    document.core_properties.author = "AI 个性化内容智能体"
    document.add_paragraph(title, "Title")

    def fill(paragraph, text):
        for value, emphasis in _inline_runs(text):
            run = paragraph.add_run(value)
            run.bold = emphasis == "bold"
            run.italic = emphasis == "italic"
            if emphasis == "code":
                run.font.name = "Consolas"

    for block in blocks:
        if block.kind == "table":
            table = document.add_table(rows=1, cols=len(block.rows[0]))
            table.style = "Table Grid"
            table.autofit = False
            for cell in table.columns:
                cell.width = Mm(170 / len(block.rows[0]))
            repeat = OxmlElement("w:tblHeader")
            table.rows[0]._tr.get_or_add_trPr().append(repeat)
            for row_index, row in enumerate(block.rows):
                cells = table.rows[0].cells if row_index == 0 else table.add_row().cells
                for cell, text in zip(cells, row, strict=True):
                    cell.width = Mm(170 / len(row))
                    fill(cell.paragraphs[0], text)
                    if row_index == 0:
                        shading = OxmlElement("w:shd")
                        shading.set(qn("w:fill"), "EAF3F1")
                        cell._tc.get_or_add_tcPr().append(shading)
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
            document.add_paragraph()
        elif block.kind == "heading":
            fill(document.add_paragraph(style=f"Heading {min(block.level, 3)}"), block.text)
        elif block.kind == "bullet":
            fill(document.add_paragraph(style="List Bullet"), block.text)
        elif block.kind == "ordered":
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Mm(5)
            fill(paragraph, f"{block.level}. {block.text}")
        elif block.kind == "break":
            document.add_paragraph()
        else:
            paragraph = document.add_paragraph()
            if block.kind in {"quote", "code"}:
                paragraph.paragraph_format.left_indent = Mm(5)
            fill(paragraph, block.text)
    footer = section.footer.paragraphs[0]
    footer.alignment = 2
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    document.save(output)


def _pdf(output: Path, title: str, blocks: list[DocumentBlock]) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        raise ChatDocumentError("PDF 组件尚未安装，请联系管理员升级文档组件") from None
    if not FONT_PATH.is_file():
        raise ChatDocumentError("PDF 中文字体文件缺失，请联系管理员恢复文档组件")
    with _font_lock:
        if PDF_FONT not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(PDF_FONT, str(FONT_PATH)))
            pdfmetrics.registerFontFamily(
                PDF_FONT, normal=PDF_FONT, bold=PDF_FONT, italic=PDF_FONT, boldItalic=PDF_FONT
            )
    styles = {
        "body": ParagraphStyle(
            "chat-body",
            fontName=PDF_FONT,
            fontSize=11,
            leading=18,
            spaceAfter=8,
            wordWrap="CJK",
            alignment=TA_LEFT,
        ),
        "title": ParagraphStyle(
            "chat-title",
            fontName=PDF_FONT,
            fontSize=22,
            leading=31,
            spaceAfter=18,
            wordWrap="CJK",
            keepWithNext=True,
        ),
        "cell": ParagraphStyle(
            "chat-cell", fontName=PDF_FONT, fontSize=9, leading=14, wordWrap="CJK", spaceAfter=0
        ),
    }
    for level, size in ((1, 16), (2, 14), (3, 12)):
        styles[f"heading-{level}"] = ParagraphStyle(
            f"chat-heading-{level}",
            parent=styles["body"],
            fontSize=size,
            leading=size * 1.5,
            spaceBefore=12,
            spaceAfter=7,
            keepWithNext=True,
        )

    def inline(text):
        return "".join(
            f"<b>{escape(value)}</b>"
            if emphasis == "bold"
            else f"<i>{escape(value)}</i>"
            if emphasis == "italic"
            else escape(value)
            for value, emphasis in _inline_runs(text)
        ).replace("\n", "<br/>")

    story = [Paragraph(escape(title), styles["title"])]
    for block in blocks:
        if block.kind == "table":
            rows = [[Paragraph(inline(text), styles["cell"]) for text in row] for row in block.rows]
            table = Table(
                rows,
                colWidths=[(A4[0] - 112) / len(rows[0])] * len(rows[0]),
                repeatRows=1,
                hAlign="LEFT",
                splitByRow=1,
                splitInRow=1,
            )
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF3F1")),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C7C2")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 7),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            story.extend([table, Spacer(1, 10)])
        elif block.kind == "break":
            story.append(Spacer(1, 10))
        else:
            style = (
                styles[f"heading-{min(block.level, 3)}"]
                if block.kind == "heading"
                else styles["body"]
            )
            prefix = (
                "• "
                if block.kind == "bullet"
                else f"{block.level}. "
                if block.kind == "ordered"
                else ""
            )
            story.append(Paragraph(inline(prefix + block.text), style))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(PDF_FONT, 9)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        canvas.drawRightString(A4[0] - 56, 30, str(document.page))
        canvas.restoreState()

    document = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=56,
        rightMargin=56,
        topMargin=56,
        bottomMargin=56,
        title=title,
        author="AI 个性化内容智能体",
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)


def create_chat_document(
    output_dir: Path,
    *,
    content: str,
    title: str = "AI 对话文档",
    format: Literal["doc", "docx", "pdf"],
    filename: str | None = None,
) -> dict:
    """Write one complete document atomically; every name is generated within output_dir."""
    if format not in {"doc", "docx", "pdf"}:
        raise ChatDocumentError("文档格式仅支持 DOC、DOCX 和 PDF")
    if (
        not isinstance(title, str)
        or not title.strip()
        or len(title) > 160
        or any(ord(char) < 32 for char in title)
    ):
        raise ChatDocumentError("文档标题需为 1 到 160 个可显示字符")
    title = title.strip()
    blocks = parse_document_markdown(content)
    if blocks and blocks[0].kind == "heading" and blocks[0].text == title:
        blocks = blocks[1:]
    stem = re.split(r"[/\\]", filename or title)[-1]
    stem = re.sub(r"\.(?:docx?|pdf)$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[^\w\u4e00-\u9fff .()-]+", "_", stem).strip(" .")[:80] or "AI对话文档"
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / f"{stem}-{uuid4().hex[:12]}.{format}"
    descriptor, temporary = tempfile.mkstemp(
        prefix=".chat-document-", suffix=f".{format}", dir=directory
    )
    os.close(descriptor)
    try:
        if format == "doc":
            from .chat_office import OfficeConversionError, convert_office

            try:
                with tempfile.TemporaryDirectory(prefix="chat-doc-source-") as source_dir:
                    source_docx = Path(source_dir) / "document.docx"
                    _docx(source_docx, title, blocks)
                    convert_office(source_docx, Path(temporary), target="doc")
            except OfficeConversionError as exc:
                raise ChatDocumentError(str(exc)) from None
        else:
            creator = _docx if format == "docx" else _pdf
            creator(Path(temporary), title, blocks)
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return {
            "path": destination,
            "name": destination.name,
            "mime_type": {
                "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "doc": "application/msword",
                "pdf": "application/pdf",
            }[format],
            "size": destination.stat().st_size,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "format": format,
        }
    except ChatDocumentError:
        raise
    except Exception:
        raise ChatDocumentError("文档生成失败，请缩短内容或调整表格后重试") from None
    finally:
        Path(temporary).unlink(missing_ok=True)
