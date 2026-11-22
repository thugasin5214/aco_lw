"""Novel → series: chapter splitting (episode flow lives in test_film.py, which has the FreeVideo fake)."""

from video_agent.series import split_chapters


def test_split_by_chinese_and_english_headings():
    zh = "书名\n\n第一章 雨夜\n" + "她走进修表铺。" * 20 + "\n\n第二章 旧表\n" + "老人抬起头。" * 20
    cs = split_chapters(zh)
    assert [c["title"] for c in cs] == ["第一章 雨夜", "第二章 旧表"]
    assert [c["id"] for c in cs] == ["c001", "c002"] and cs[0]["text"].startswith("她走进")
    en = "Chapter 1\nIt rained.\n\nCHAPTER TWO: The Watch\nHe looked up."
    assert [c["title"] for c in split_chapters(en)] == ["Chapter 1", "CHAPTER TWO: The Watch"]


def test_split_without_headings_and_long_chapters_by_length():
    paras = "\n\n".join("段落" * 300 for _ in range(10))  # 10 × 600 chars
    cs = split_chapters(paras, limit=2000)
    assert len(cs) == 4 and all(c["chars"] <= 2000 for c in cs)
    assert cs[0]["title"] == "Part (1/4)"
    long_ch = "第一章 长\n" + "\n\n".join("字" * 900 for _ in range(5)) + "\n\n第二章 短\n短。"
    cs = split_chapters(long_ch, limit=2000)
    assert [c["title"] for c in cs][:3] == ["第一章 长 (1/3)", "第一章 长 (2/3)", "第一章 长 (3/3)"]
    assert cs[-1]["title"] == "第二章 短" and split_chapters("  ") == []


def test_no_output_cap_by_default():
    """llm_max_tokens = 0: LM Studio gets -1 (until the context is full), other servers no cap."""
    from video_agent.backends import OpenAICompatBackend

    lm = OpenAICompatBackend.__new__(OpenAICompatBackend)
    lm.name, lm.model = "lmstudio", "m"
    assert lm._body([], None, None)["max_tokens"] == -1
    other = OpenAICompatBackend.__new__(OpenAICompatBackend)
    other.name, other.model = "openai", "m"
    body = other._body([], None, None)
    assert "max_tokens" not in body and "max_completion_tokens" not in body
    assert other._body([], None, 500)["max_completion_tokens"] == 500
