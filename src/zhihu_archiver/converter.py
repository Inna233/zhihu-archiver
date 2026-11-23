from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag


def unwrap_zhihu_redirect(url: str) -> str:
    """Decode Zhihu outbound redirect URLs like https://link.zhihu.com/?target=..."""
    if not url:
        return ""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.netloc in {"link.zhihu.com", "link.zhihu.com:443"}:
        target = parse_qs(parsed.query).get("target")
        if target and target[0]:
            return unquote(target[0])
    return url


def _pick_image_url(tag: Tag) -> str:
    """Choose the highest-resolution non-placeholder URL from an <img> tag."""
    for attr in ("data-original", "data-actualsrc", "data-default-watermark-src", "src"):
        val = tag.get(attr)
        if isinstance(val, str):
            val = val.strip()
            if val and not val.startswith("data:image/svg+xml"):
                return val
    return ""


def _is_math_img(tag: Tag) -> bool:
    return bool(tag.get("eeimg") or "ee_img" in (tag.get("class") or []))


def _format_tex(tex: str, block: bool = False) -> str:
    cleaned = tex.strip()
    if not cleaned:
        return ""
    if block or cleaned.startswith("\\displaystyle") or "\n" in cleaned:
        return f"\n$$\n{cleaned}\n$$\n"
    return f"${cleaned}$"


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_markdown(html: str) -> tuple[str, list[str]]:
    """Convert Zhihu HTML content into Markdown and collect image URLs.

    Preserves paragraphs, headings, lists, links, blockquotes, code blocks,
    LaTeX formulas, and images.
    """
    if not html or not html.strip():
        return "", []

    soup = BeautifulSoup(html, "html.parser")
    images: list[str] = []
    seen_images: set[str] = set()

    def record_image(url: str) -> None:
        if url and url not in seen_images:
            seen_images.add(url)
            images.append(url)

    def render_children(node: Tag, list_depth: int = 0) -> str:
        return "".join(render_node(child, list_depth=list_depth) for child in node.children)

    def render_node(node: Any, list_depth: int = 0) -> str:
        if isinstance(node, NavigableString):
            return str(node)
        if not isinstance(node, Tag):
            return ""

        name = node.name.lower()

        if name in {"script", "style", "noscript"}:
            return ""

        # Zhihu math span
        if name == "span" and (
            "ztext-math" in (node.get("class") or []) or node.get("data-tex") is not None
        ):
            tex = str(node.get("data-tex") or node.get_text() or "").strip()
            return _format_tex(tex, block=False)

        if name == "br":
            return "\n"

        if name == "hr":
            return "\n\n---\n\n"

        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(name[1])
            inner = render_children(node, list_depth).strip()
            return f"\n\n{'#' * level} {inner}\n\n" if inner else ""

        if name == "p":
            inner = render_children(node, list_depth).strip()
            return f"\n\n{inner}\n\n" if inner else ""

        if name == "blockquote":
            inner = _normalize_whitespace(render_children(node, list_depth))
            if not inner:
                return ""
            quoted = "\n".join(f"> {line}" if line else ">" for line in inner.splitlines())
            return f"\n\n{quoted}\n\n"

        if name == "pre":
            code_tag = node.find("code")
            lang = ""
            if code_tag:
                for cls in code_tag.get("class") or []:
                    if cls.startswith("language-"):
                        lang = cls.removeprefix("language-")
                        break
                code_text = code_tag.get_text()
            else:
                code_text = node.get_text()
            code_text = code_text.strip("\n")
            return f"\n\n```{lang}\n{code_text}\n```\n\n"

        if name == "code":
            if node.parent and node.parent.name and node.parent.name.lower() == "pre":
                return node.get_text()
            text = node.get_text()
            fence = "``" if "`" in text else "`"
            return f"{fence}{text}{fence}"

        if name in {"strong", "b"}:
            inner = render_children(node, list_depth).strip()
            return f"**{inner}**" if inner else ""

        if name in {"em", "i"}:
            inner = render_children(node, list_depth).strip()
            return f"*{inner}*" if inner else ""

        if name in {"del", "s"}:
            inner = render_children(node, list_depth).strip()
            return f"~~{inner}~~" if inner else ""

        if name == "a":
            href = unwrap_zhihu_redirect(str(node.get("href") or ""))
            inner = render_children(node, list_depth).strip()
            if not href:
                return inner
            if not inner:
                return href
            if inner == href:
                return f"<{href}>"
            return f"[{inner}]({href})"

        if name == "figure":
            img = node.find("img")
            caption_tag = node.find("figcaption")
            caption = caption_tag.get_text(" ", strip=True) if caption_tag else ""
            if img:
                if _is_math_img(img):
                    tex = str(img.get("alt") or "").strip()
                    return _format_tex(tex, block=True)
                url = _pick_image_url(img)
                if url:
                    record_image(url)
                    alt = caption or str(img.get("alt") or "").strip()
                    md = f"![{alt}]({url})"
                    if caption and caption != alt:
                        md += f"\n*{caption}*"
                    return f"\n\n{md}\n\n"
            return f"\n\n{render_children(node, list_depth).strip()}\n\n"

        if name == "img":
            if _is_math_img(node):
                tex = str(node.get("alt") or "").strip()
                return _format_tex(tex, block=False)
            url = _pick_image_url(node)
            if not url:
                return ""
            record_image(url)
            alt = str(node.get("alt") or "").strip()
            return f"![{alt}]({url})"

        if name in {"ul", "ol"}:
            lines: list[str] = []
            indent = "  " * list_depth
            idx = 1
            for child in node.children:
                if isinstance(child, Tag) and child.name.lower() == "li":
                    bullet = f"{idx}." if name == "ol" else "-"
                    idx += 1
                    item_md = _normalize_whitespace(render_children(child, list_depth + 1))
                    if not item_md:
                        continue
                    item_lines = item_md.splitlines()
                    first = f"{indent}{bullet} {item_lines[0]}"
                    rest = [
                        f"{indent}  {ln}" if ln.strip() else ""
                        for ln in item_lines[1:]
                    ]
                    lines.append("\n".join([first, *rest]))
            if not lines:
                return ""
            joined = "\n".join(lines)
            return f"\n\n{joined}\n\n" if list_depth == 0 else f"\n{joined}"

        if name == "table":
            rows = node.find_all("tr")
            if not rows:
                return ""
            md_rows: list[str] = []
            for i, tr in enumerate(rows):
                cells = [
                    _normalize_whitespace(render_children(cell, list_depth)).replace("\n", " ")
                    for cell in tr.find_all(["th", "td"])
                ]
                if not cells:
                    continue
                md_rows.append("| " + " | ".join(cells) + " |")
                if i == 0:
                    md_rows.append("| " + " | ".join("---" for _ in cells) + " |")
            return "\n\n" + "\n".join(md_rows) + "\n\n" if md_rows else ""

        return render_children(node, list_depth)

    rendered = render_children(soup)
    return _normalize_whitespace(rendered), images


def pin_blocks_to_markdown(
    content_blocks: list[dict[str, Any]] | None,
    fallback_html: str = "",
) -> tuple[str, list[str], int]:
    """Convert Zhihu pin content blocks (or fallback HTML) into Markdown.

    Returns:
        (markdown_text, image_urls, blocks_count)
    """
    images: list[str] = []
    seen_images: set[str] = set()

    def add_image(url: str) -> None:
        if url and url not in seen_images:
            seen_images.add(url)
            images.append(url)

    if isinstance(content_blocks, list) and content_blocks:
        parts: list[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "").lower()
            if btype == "text":
                raw_text = str(block.get("content") or "")
                md, block_imgs = html_to_markdown(raw_text)
                for img in block_imgs:
                    add_image(img)
                if md:
                    parts.append(md)
            elif btype == "image":
                url = str(
                    block.get("original_url")
                    or block.get("url")
                    or block.get("src")
                    or ""
                ).strip()
                if url:
                    add_image(url)
                    parts.append(f"![]({url})")
            elif btype == "link":
                url = unwrap_zhihu_redirect(str(block.get("url") or ""))
                title = str(block.get("title") or url).strip()
                if url:
                    parts.append(f"[{title}]({url})")
                elif title:
                    parts.append(title)
            elif btype == "quote":
                raw_q = str(block.get("content") or "")
                md, block_imgs = html_to_markdown(raw_q)
                for img in block_imgs:
                    add_image(img)
                if md:
                    quoted = "\n".join(f"> {line}" if line else ">" for line in md.splitlines())
                    parts.append(quoted)
            elif btype == "video":
                title = str(block.get("title") or "视频").strip()
                url = str(block.get("url") or block.get("thumbnail") or "").strip()
                if url:
                    parts.append(f"[{title}]({url})")
                else:
                    parts.append(f"[{title}]")
            else:
                raw_fallback = str(block.get("content") or block.get("title") or "").strip()
                if raw_fallback:
                    md, block_imgs = html_to_markdown(raw_fallback)
                    for img in block_imgs:
                        add_image(img)
                    if md:
                        parts.append(md)

        combined = _normalize_whitespace("\n\n".join(parts))
        return combined, images, len(content_blocks)

    if fallback_html:
        md, html_imgs = html_to_markdown(fallback_html)
        for img in html_imgs:
            add_image(img)
        return md, images, 1 if md else 0

    return "", [], 0
