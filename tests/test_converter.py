from zhihu_archiver.converter import html_to_markdown, pin_blocks_to_markdown, unwrap_zhihu_redirect
from zhihu_archiver.models import parse_item_types, parse_user_url_token
from zhihu_archiver.providers.tikhub import parse_answer_dict, parse_question_dict


def test_parse_user_url_token():
    assert parse_user_url_token("https://www.zhihu.com/people/xiao-chuan-1412") == "xiao-chuan-1412"
    assert parse_user_url_token("https://www.zhihu.com/people/xiao-chuan-1412/posts") == "xiao-chuan-1412"
    assert parse_user_url_token("xiao-chuan-1412") == "xiao-chuan-1412"


def test_parse_item_types():
    assert parse_item_types("articles,pins,answers,questions") == (
        "article",
        "pin",
        "answer",
        "question",
    )
    assert parse_item_types("posts, asks, pins") == ("article", "question", "pin")


def test_parse_answer_and_edited_detection():
    raw = {
        "id": 998877,
        "question": {"id": 123456, "title": "普通人的出路在哪里？"},
        "author": {"id": "hash_1", "url_token": "xiao-chuan-1412", "name": "矢口"},
        "created_time": 1700000000,
        "updated_time": 1700500000,
        "voteup_count": 42,
        "comment_count": 7,
        "content": "<p>初始回答</p><hr/><p>2024补充更新</p>",
    }
    item = parse_answer_dict(raw)
    assert item.type == "answer"
    assert item.url == "https://www.zhihu.com/question/123456/answer/998877"
    assert item.question_url == "https://www.zhihu.com/question/123456"
    assert item.was_edited is True
    assert item.voteup_count == 42
    assert item.is_full_content is True


def test_parse_question():
    raw = {
        "id": 556677,
        "title": "为什么人们喜欢复读术语？",
        "detail": "<p>想听听不同视角。</p>",
        "author": {"id": "hash_1", "url_token": "xiao-chuan-1412", "name": "矢口"},
        "created": 1700000000,
        "updated_time": 1700000000,
        "follower_count": 10,
        "answer_count": 3,
    }
    q = parse_question_dict(raw)
    assert q.type == "question"
    assert q.url == "https://www.zhihu.com/question/556677"
    assert q.was_edited is False
    assert "想听听不同视角" in q.content_markdown


def test_unwrap_zhihu_redirect():
    wrapped = "https://link.zhihu.com/?target=https%3A//example.com/paper%3Fid%3D42"
    assert unwrap_zhihu_redirect(wrapped) == "https://example.com/paper?id=42"
    assert unwrap_zhihu_redirect("https://example.com/plain") == "https://example.com/plain"


def test_html_to_markdown_preserves_structure_and_formulas():
    html = """
    <h2>核心问题</h2>
    <p>第一段包含<strong>强调</strong>和<a href="https://link.zhihu.com/?target=https%3A//arxiv.org/abs/1234">论文链接</a>。</p>
    <blockquote><p>这是一条引文。</p></blockquote>
    <ul>
      <li>要点一：内联公式 <span class="ztext-math" data-tex="E = mc^2">E=mc^2</span></li>
      <li>要点二：带图片公式 <img eeimg="1" alt="a^2 + b^2 = c^2" src="https://www.zhihu.com/equation?tex=a"/></li>
    </ul>
    <figure>
      <img src="data:image/svg+xml;utf8,placeholder" data-original="https://picx.zhimg.com/v2-real.jpg" alt="图1"/>
      <figcaption>实验结果图</figcaption>
    </figure>
    <pre><code class="language-python">print("hello")</code></pre>
    """
    md, images = html_to_markdown(html)
    assert "## 核心问题" in md
    assert "**强调**" in md
    assert "[论文链接](https://arxiv.org/abs/1234)" in md
    assert "> 这是一条引文。" in md
    assert "- 要点一：内联公式 $E = mc^2$" in md
    assert "- 要点二：带图片公式 $a^2 + b^2 = c^2$" in md
    assert "![实验结果图](https://picx.zhimg.com/v2-real.jpg)" in md
    assert "```python\nprint(\"hello\")\n```" in md
    assert images == ["https://picx.zhimg.com/v2-real.jpg"]


def test_pin_blocks_to_markdown():
    blocks = [
        {"type": "text", "content": "<p>作者附言第一段</p>"},
        {"type": "link", "title": "参考资料", "url": "https://link.zhihu.com/?target=https%3A//example.org/doc"},
        {"type": "image", "original_url": "https://picx.zhimg.com/pin-img-1.png"},
        {"type": "quote", "content": "摘录一段原话"},
    ]
    md, images, count = pin_blocks_to_markdown(blocks)
    assert count == 4
    assert "作者附言第一段" in md
    assert "[参考资料](https://example.org/doc)" in md
    assert "![](https://picx.zhimg.com/pin-img-1.png)" in md
    assert "> 摘录一段原话" in md
    assert images == ["https://picx.zhimg.com/pin-img-1.png"]
