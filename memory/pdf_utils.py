"""
pdf_utils.py — PDF处理工具（基于 pdfplumber）
能力节点：文字提取/表格/元数据/分页/关键词搜索
"""
from __future__ import annotations
import pathlib
from typing import Optional


def extract_text(
    pdf_path: str | pathlib.Path,
    pages: Optional[list[int]] = None,
    join: str = "\n\n",
) -> str:
    """
    提取PDF全文或指定页文字。

    Args:
        pdf_path: PDF文件路径
        pages: 页码列表(0-based)，None=全部
        join: 页间分隔符

    Returns:
        提取的文字字符串
    """
    import pdfplumber
    pdf_path = pathlib.Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF不存在: {pdf_path}")
    with pdfplumber.open(pdf_path) as pdf:
        target = [pdf.pages[i] for i in pages] if pages else pdf.pages
        texts = [p.extract_text() or "" for p in target]
    return join.join(t for t in texts if t.strip())


def extract_page_text(
    pdf_path: str | pathlib.Path,
    page_num: int = 0,
) -> str:
    """
    提取单页文字（page_num 0-based）。
    """
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        if page_num >= len(pdf.pages):
            raise IndexError(f"页码越界: {page_num} >= {len(pdf.pages)}")
        return pdf.pages[page_num].extract_text() or ""


def extract_tables(
    pdf_path: str | pathlib.Path,
    pages: Optional[list[int]] = None,
) -> list[dict]:
    """
    提取PDF中所有表格（或指定页）。

    Returns:
        list of {"page": int, "table_index": int, "data": list[list[str]]}
    """
    import pdfplumber
    results = []
    with pdfplumber.open(pdf_path) as pdf:
        target_pages = pages if pages else range(len(pdf.pages))
        for pi in target_pages:
            page = pdf.pages[pi]
            tables = page.extract_tables()
            for ti, table in enumerate(tables):
                # 清洗None→空字符串
                clean = [[cell or "" for cell in row] for row in table]
                results.append({"page": pi, "table_index": ti, "data": clean})
    return results


def get_metadata(pdf_path: str | pathlib.Path) -> dict:
    """
    读取PDF元数据（Author/Title/Creator/CreationDate等）。

    Returns:
        元数据字典，已过滤空值
    """
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        meta = pdf.metadata or {}
    return {k: v for k, v in meta.items() if v and str(v).strip()}


def get_page_count(pdf_path: str | pathlib.Path) -> int:
    """返回PDF总页数。"""
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)


def search_text(
    pdf_path: str | pathlib.Path,
    keyword: str,
    case_sensitive: bool = False,
) -> list[dict]:
    """
    在PDF全文中搜索关键词，返回命中页和上下文。

    Returns:
        list of {"page": int, "context": str}  (每页仅返回首次命中上下文)
    """
    import pdfplumber
    results = []
    kw = keyword if case_sensitive else keyword.lower()
    with pdfplumber.open(pdf_path) as pdf:
        for pi, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            haystack = text if case_sensitive else text.lower()
            idx = haystack.find(kw)
            if idx >= 0:
                start = max(0, idx - 60)
                end = min(len(text), idx + len(keyword) + 60)
                results.append({"page": pi, "context": text[start:end].strip()})
    return results


def extract_to_file(
    pdf_path: str | pathlib.Path,
    output_path: str | pathlib.Path,
    pages: Optional[list[int]] = None,
) -> pathlib.Path:
    """
    提取文字并保存到txt文件。

    Returns:
        输出文件路径
    """
    text = extract_text(pdf_path, pages=pages)
    output_path = pathlib.Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path


def page_summary(pdf_path: str | pathlib.Path) -> list[dict]:
    """
    生成每页摘要：页码/字数/表格数/图片数。

    Returns:
        list of {"page": int, "chars": int, "tables": int, "images": int}
    """
    import pdfplumber
    summary = []
    with pdfplumber.open(pdf_path) as pdf:
        for pi, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            tables = page.extract_tables()
            images = page.images
            summary.append({
                "page": pi,
                "chars": len(text),
                "tables": len(tables),
                "images": len(images),
            })
    return summary


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("用法: python pdf_utils.py <pdf_file> [keyword]")
        sys.exit(1)
    path = sys.argv[1]
    print(f"页数: {get_page_count(path)}")
    print(f"元数据: {json.dumps(get_metadata(path), ensure_ascii=False, indent=2)}")
    text = extract_page_text(path, 0)
    print(f"第1页文字({len(text)}字): {text[:200]}")
    if len(sys.argv) > 2:
        hits = search_text(path, sys.argv[2])
        print(f"搜索'{sys.argv[2]}'命中{len(hits)}页: {hits[:3]}")