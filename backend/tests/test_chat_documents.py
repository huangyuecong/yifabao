import hashlib
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from docx import Document
from pypdf import PdfReader

from content_service import chat_documents
from content_service.chat_documents import (
    ChatDocumentError,
    create_chat_document,
    parse_document_markdown,
)

SAMPLE = """# 雅酿产品传播建议

## 创作方向
依据用户提供的产品事实，采用**克制清晰**的表达方式，面向成年消费者。

- 保留品牌名称与产品事实。
- 避免未经资料证实的功效、价格和排名。

1. 阅读资料并确认核心信息。
2. 输出候选文案供审核。

| 项目 | 内容 | 状态 |
| --- | --- | --- |
| 产品事实 | 雅酿产品信息以用户资料为准 | 待核对 |
| 图片建议 | 展示瓶身与真实使用场景 | 待确认 |

> 本文为生成示例，具体事实需要业务人员确认。
"""


def test_docx_contains_editable_chinese_structure_and_tables(tmp_path):
    result = create_chat_document(tmp_path, content=SAMPLE, title="雅酿产品传播建议", format="docx")
    source = result["path"]
    assert result["size"] == source.stat().st_size
    assert result["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert source.read_bytes().startswith(b"PK")
    document = Document(source)
    assert document.paragraphs[0].style.name == "Title"
    assert document.paragraphs[0].text == "雅酿产品传播建议"
    assert (
        len(
            [paragraph for paragraph in document.paragraphs if paragraph.text == "雅酿产品传播建议"]
        )
        == 1
    )
    assert any(
        paragraph.style.name == "Heading 2" and paragraph.text == "创作方向"
        for paragraph in document.paragraphs
    )
    assert (
        len(
            [
                paragraph
                for paragraph in document.paragraphs
                if paragraph.style.name == "List Bullet"
            ]
        )
        == 2
    )
    assert any(
        run.bold and run.text == "克制清晰"
        for paragraph in document.paragraphs
        for run in paragraph.runs
    )
    assert document.tables[0].cell(1, 1).text == "雅酿产品信息以用户资料为准"
    with zipfile.ZipFile(source) as archive:
        assert b"w:tblHeader" in archive.read("word/document.xml")
        assert b"eastAsia" in archive.read("word/styles.xml")


def test_pdf_embeds_chinese_font_and_contains_readable_text(tmp_path):
    result = create_chat_document(tmp_path, content=SAMPLE, title="雅酿产品传播建议", format="pdf")
    assert result["path"].read_bytes().startswith(b"%PDF-")
    reader = PdfReader(result["path"])
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "雅酿产品传播建议" in text
    assert "面向成年消费者" in text
    assert "产品事实" in text and "待核对" in text
    assert len(reader.pages) == 1
    fonts = [
        font.get_object() for page in reader.pages for font in page["/Resources"]["/Font"].values()
    ]
    assert any(
        "/FontFile2" in font.get("/FontDescriptor", {}).get_object()
        for font in fonts
        if "/FontDescriptor" in font
    )


def test_pdf_long_table_and_paragraphs_paginate_without_truncation(tmp_path):
    rows = ["| 序号 | 内容 |", "| --- | --- |"]
    rows.extend(
        f"| {index} | 第{index}条事实说明请严格依据用户提供的材料，避免未经确认的信息。 |"
        for index in range(1, 101)
    )
    content = (
        "## 跨页内容\n\n"
        + "\n".join(rows)
        + "\n\n"
        + "中文长段落内容需要自动换行并正确分页。" * 500
        + "\n\n文档最后一句结束标记。"
    )
    result = create_chat_document(tmp_path, content=content, title="分页验证", format="pdf")
    reader = PdfReader(result["path"])
    assert len(reader.pages) >= 5
    page_texts = [page.extract_text() for page in reader.pages]
    assert "第100条事实" in "\n".join(page_texts)
    assert "文档最后一句结束标记" in page_texts[-1]
    assert sum("序号" in text for text in page_texts) > 1


def test_output_names_cannot_escape_directory_and_concurrent_outputs_are_distinct(tmp_path):
    def generate(_):
        return create_chat_document(
            tmp_path,
            content="雅酿文档内容",
            title="文件安全",
            format="pdf",
            filename="../../private\\非法文件.pdf",
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(generate, range(8)))
    assert len({result["path"] for result in results}) == 8
    assert all(result["path"].parent == tmp_path.resolve() for result in results)
    assert all(".." not in result["name"] and "\\" not in result["name"] for result in results)
    assert not list(tmp_path.glob(".chat-document-*"))


@pytest.mark.parametrize(
    "content",
    ["", "正文\x00", "| 一 | 二 |\n| --- | --- |\n| 错误列数 |", "文" * 100001],
    ids=["empty", "binary-control", "ragged-table", "over-limit"],
)
def test_invalid_content_never_leaves_partial_document(tmp_path, content):
    with pytest.raises(ChatDocumentError):
        create_chat_document(tmp_path, content=content, title="输入验证", format="pdf")
    assert not list(tmp_path.iterdir())


def test_render_failure_is_safe_and_removes_temporary_file(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("secret-model-key /private/path")

    monkeypatch.setattr(chat_documents, "_pdf", fail)
    with pytest.raises(ChatDocumentError) as error:
        create_chat_document(tmp_path, content="正文", title="失败验证", format="pdf")
    assert "secret" not in str(error.value)
    assert not list(tmp_path.iterdir())


def test_markdown_tables_preserve_escaped_pipes_and_html_is_literal(tmp_path):
    content = "| 标题 | 值 |\n| --- | --- |\n| a\\|b | <script>literal</script> |\n\n**强调** [参考](https://example.invalid/never-fetched)"
    blocks = parse_document_markdown(content)
    assert blocks[0].rows[1][0] == "a|b"
    result = create_chat_document(tmp_path, content=content, title="文本解析", format="pdf")
    text = "\n".join(page.extract_text() for page in PdfReader(result["path"]).pages)
    assert "<script>literal</script>" in text
    assert "https://example.invalid/never-fetched" in text
