"""
scraper_utils.py — 网页内容提取工具封装
依赖: beautifulsoup4, lxml, httpx (via http_utils)
功能:
  - parse(html, parser='lxml')        : HTML字符串→BeautifulSoup对象
  - fetch_parse(url, ...)             : 直接抓取URL并解析为soup
  - css_select(soup_or_html, selector): CSS选择器提取元素列表
  - xpath_select(html, xpath)         : XPath提取（基于lxml）
  - extract_links(soup_or_html, base_url=None) : 提取所有链接（绝对化）
  - extract_table(soup_or_html, index=0)       : 提取HTML表格→list[dict]
  - extract_text(soup_or_html, selector=None)  : 提取净化文本
  - extract_meta(soup_or_html)        : 提取meta标签信息（title/description/og等）
  - clean_html(html)                  : 移除script/style/注释，返回干净HTML

坑:
  - lxml 解析器比 html.parser 快但可能改变结构（如自动闭合标签）
  - XPath 在 lxml.html 上工作，bs4 不原生支持 XPath
  - 表格提取遇到colspan/rowspan会用None占位，不做合并
  - extract_links 需要 base_url 才能正确绝对化相对路径
  - 某些网页用JS渲染，静态抓取拿不到内容（需配合浏览器方案）
  - 编码检测优先用response的encoding，fallback到chardet
"""

import re
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, Tag
from lxml import html as lxml_html


# ── 内部工具 ──────────────────────────────────────────────────

def _to_soup(soup_or_html: Union[str, BeautifulSoup], parser: str = 'lxml') -> BeautifulSoup:
    """统一转换为 BeautifulSoup 对象"""
    if isinstance(soup_or_html, BeautifulSoup):
        return soup_or_html
    return BeautifulSoup(soup_or_html, parser)


def _ensure_html_str(soup_or_html: Union[str, BeautifulSoup]) -> str:
    """确保得到HTML字符串"""
    if isinstance(soup_or_html, BeautifulSoup):
        return str(soup_or_html)
    return soup_or_html


# ── 公开 API ──────────────────────────────────────────────────

def parse(html: str, parser: str = 'lxml') -> BeautifulSoup:
    """
    解析 HTML 字符串为 BeautifulSoup 对象。

    Args:
        html: HTML字符串
        parser: 解析器 ('lxml', 'html.parser', 'html5lib')

    Returns:
        BeautifulSoup 对象
    """
    return BeautifulSoup(html, parser)


def fetch_parse(url: str, *, parser: str = 'lxml', **http_kwargs) -> BeautifulSoup:
    """
    抓取 URL 并解析为 BeautifulSoup 对象。依赖 http_utils.get()。

    Args:
        url: 目标URL
        parser: 解析器
        **http_kwargs: 传递给 http_utils.get() 的参数（headers, cookies, proxy等）

    Returns:
        BeautifulSoup 对象
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from http_utils import get
    resp = get(url, **http_kwargs)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, parser)


def css_select(soup_or_html: Union[str, BeautifulSoup], selector: str) -> List[Tag]:
    """
    CSS 选择器提取元素。

    Args:
        soup_or_html: HTML字符串或BeautifulSoup对象
        selector: CSS选择器字符串

    Returns:
        匹配的 Tag 列表
    """
    soup = _to_soup(soup_or_html)
    return soup.select(selector)


def xpath_select(html: Union[str, BeautifulSoup], xpath: str) -> List[str]:
    """
    XPath 提取（基于 lxml）。

    Args:
        html: HTML字符串或BeautifulSoup对象
        xpath: XPath表达式

    Returns:
        匹配结果列表（文本或元素的text_content）
    """
    html_str = _ensure_html_str(html)
    tree = lxml_html.fromstring(html_str)
    results = tree.xpath(xpath)
    # 统一返回字符串列表
    out = []
    for r in results:
        if isinstance(r, str):
            out.append(r.strip())
        elif hasattr(r, 'text_content'):
            out.append(r.text_content().strip())
        else:
            out.append(str(r).strip())
    return [x for x in out if x]


def extract_links(
    soup_or_html: Union[str, BeautifulSoup],
    base_url: Optional[str] = None,
    *,
    filter_ext: Optional[List[str]] = None,
    internal_only: bool = False,
) -> List[Dict[str, str]]:
    """
    提取所有链接。

    Args:
        soup_or_html: HTML
        base_url: 基础URL（用于将相对路径转为绝对路径）
        filter_ext: 过滤扩展名列表，如 ['.pdf', '.doc']
        internal_only: 只返回同域名链接

    Returns:
        [{'href': url, 'text': 链接文字}]
    """
    soup = _to_soup(soup_or_html)
    links = []
    base_domain = urlparse(base_url).netloc if base_url else None

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:')):
            continue

        # 绝对化
        if base_url and not href.startswith(('http://', 'https://')):
            href = urljoin(base_url, href)

        # 过滤
        if internal_only and base_domain:
            if urlparse(href).netloc and urlparse(href).netloc != base_domain:
                continue

        if filter_ext:
            path = urlparse(href).path.lower()
            if not any(path.endswith(ext) for ext in filter_ext):
                continue

        text = a.get_text(strip=True) or ''
        links.append({'href': href, 'text': text})

    return links


def extract_table(
    soup_or_html: Union[str, BeautifulSoup],
    index: int = 0,
    *,
    header_row: int = 0,
) -> List[Dict[str, str]]:
    """
    提取 HTML 表格为 list[dict]。

    Args:
        soup_or_html: HTML
        index: 第几个表格（0-based）
        header_row: 表头所在行（默认第0行），用于dict的key

    Returns:
        [{'col1': 'val1', 'col2': 'val2', ...}]
    """
    soup = _to_soup(soup_or_html)
    tables = soup.find_all('table')
    if index >= len(tables):
        return []

    table = tables[index]
    rows = table.find_all('tr')
    if not rows:
        return []

    # 提取所有行的单元格文本
    all_rows = []
    for row in rows:
        cells = row.find_all(['td', 'th'])
        all_rows.append([c.get_text(strip=True) for c in cells])

    if not all_rows or header_row >= len(all_rows):
        return []

    headers = all_rows[header_row]
    # 保证 headers 非空
    headers = [h if h else f'col_{i}' for i, h in enumerate(headers)]

    result = []
    for row_data in all_rows[header_row + 1:]:
        # 补齐或截断到 header 长度
        padded = row_data + [''] * (len(headers) - len(row_data))
        result.append(dict(zip(headers, padded[:len(headers)])))

    return result


def extract_tables_all(soup_or_html: Union[str, BeautifulSoup]) -> List[List[Dict[str, str]]]:
    """提取页面中所有表格，返回 list[list[dict]]"""
    soup = _to_soup(soup_or_html)
    count = len(soup.find_all('table'))
    return [extract_table(soup, i) for i in range(count)]


def extract_text(
    soup_or_html: Union[str, BeautifulSoup],
    selector: Optional[str] = None,
    *,
    separator: str = '\n',
    strip: bool = True,
) -> str:
    """
    提取纯净文本。

    Args:
        soup_or_html: HTML
        selector: 可选CSS选择器，限定提取范围
        separator: 块元素间分隔符
        strip: 是否去除首尾空白

    Returns:
        净化后的纯文本
    """
    soup = _to_soup(soup_or_html)

    # 移除 script/style/noscript
    for tag in soup.find_all(['script', 'style', 'noscript']):
        tag.decompose()
    # 移除 hidden 元素
    for tag in soup.find_all(style=re.compile(r'display\s*:\s*none', re.I)):
        tag.decompose()
    for tag in soup.find_all(attrs={'hidden': True}):
        tag.decompose()

    if selector:
        target = soup.select_one(selector)
        if not target:
            return ''
        text = target.get_text(separator=separator, strip=strip)
    else:
        text = soup.get_text(separator=separator, strip=strip)

    # 合并多余空行
    lines = [line.strip() for line in text.splitlines()]
    lines = [l for l in lines if l]
    return '\n'.join(lines)


def extract_meta(soup_or_html: Union[str, BeautifulSoup]) -> Dict[str, str]:
    """
    提取 meta 信息（title, description, keywords, og:* 等）。

    Returns:
        {'title': ..., 'description': ..., 'og:title': ..., ...}
    """
    soup = _to_soup(soup_or_html)
    meta = {}

    # title
    title_tag = soup.find('title')
    if title_tag:
        meta['title'] = title_tag.get_text(strip=True)

    # meta tags
    for tag in soup.find_all('meta'):
        name = tag.get('name', '') or tag.get('property', '')
        content = tag.get('content', '')
        if name and content:
            meta[name.lower()] = content

    return meta


def clean_html(html: Union[str, BeautifulSoup]) -> str:
    """
    清理 HTML：移除 script/style/注释/隐藏元素，保留结构。

    Returns:
        干净的 HTML 字符串
    """
    soup = _to_soup(html)

    # 移除 script, style, noscript
    for tag in soup.find_all(['script', 'style', 'noscript']):
        tag.decompose()

    # 移除注释
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # 移除 hidden 元素
    for tag in soup.find_all(style=re.compile(r'display\s*:\s*none', re.I)):
        tag.decompose()
    for tag in soup.find_all(attrs={'hidden': True}):
        tag.decompose()

    return str(soup)


# ── CLI ──────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    print("scraper_utils — 使用方法:")
    print("  from scraper_utils import fetch_parse, css_select, extract_text, extract_links")
    print("  soup = fetch_parse('https://example.com')")
    print("  links = extract_links(soup, base_url='https://example.com')")
    print("  text = extract_text(soup)")