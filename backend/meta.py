#!/usr/bin/env python3
"""书籍元数据处理工具：标题清洗等。"""
import re
from typing import List


# 无效 tag：开头为这些词（不区分大小写）
_INVALID_TAG_PREFIX = re.compile(
    r'^(?:关注|标题|制作|下载|出版社|http|ftp|mail|isbn)', re.IGNORECASE
)

# 无效 tag：含有这些内容（不区分大小写）
_INVALID_TAG_CONTAINS = re.compile(
    r'(?:\s|公众号|微信|下载|下載|汇书网|书屋|，|www\.|\.com|出品|@|商务印书馆|SANQIU)', re.IGNORECASE
)

# 无效 tag：结尾为这些词
_INVALID_TAG_SUFFIX = re.compile(r'(?:制作|印刷)$')

# 无效 tag：纯数字
_PURE_DIGITS = re.compile(r'^\d+$')

# 无效 tag：开头为(或（，且结尾为)或）
_WRAPPED_IN_BRACKETS = re.compile(r'^[\(（].*[\)）]$')

# 分隔符：单个 tag 内混杂多个词，用；或;隔开的，拆成多个独立 tag
_TAG_SPLIT = re.compile(r'[;；]')

# 规则说明，仅用于前端展示（跟上面的判断逻辑一一对应，不参与实际清理）
RULES = [
    {
        "id": "prefix",
        "title": "广告类前缀",
        "description": "标签以「关注」「标题」「制作」「下载」「出版社」「http」「ftp」「mail」"
                        "「isbn」开头（不区分大小写）",
        "examples": ["关注公众号", "http://example.com", "ISBN 978-x"],
    },
    {
        "id": "contains",
        "title": "广告/网址类关键词",
        "description": "标签中含空白字符、「公众号」「微信」「，」「www.」「.com」「出品」「@」"
                        "「商务印书馆」「SANQIU」（不区分大小写）",
        "examples": ["加微信xxx", "www.example.com", "联系邮箱a@b.com"],
    },
    {
        "id": "suffix",
        "title": "广告类后缀",
        "description": "标签以「制作」「印刷」结尾",
        "examples": ["独家制作", "精美印刷"],
    },
    {
        "id": "digits",
        "title": "纯数字",
        "description": "标签整体为纯数字",
        "examples": ["12345"],
    },
    {
        "id": "brackets",
        "title": "整体被括号包裹",
        "description": "标签以「(」或「（」开头，且以「)」或「）」结尾",
        "examples": ["(备注内容)", "（备注内容）"],
    },
    {
        "id": "split",
        "title": "按分隔符拆分",
        "description": "标签中含「；」或「;」时，按其拆分为多个独立标签，拆分后每一段仍会按"
                        "上面几条规则单独校验",
        "examples": ["政治改革；研究；中国 → 政治改革 / 研究 / 中国"],
    },
    {
        "id": "min_length",
        "title": "过滤单字符标签",
        "description": "长度不超过 1 个字符的标签（含拆分产生的空/单字符碎片）会被丢弃",
        "examples": ["中； → 丢弃「中」后面的空碎片"],
    },
    {
        "id": "dedupe",
        "title": "去重与清理空白",
        "description": "空标签、None 值会被丢弃；标签首尾空白会被清理；重复标签只保留第一个",
        "examples": ["  小说  → 小说", "小说, 小说 → 小说"],
    },
]


def _is_invalid_tag(tag: str) -> bool:
    """判断单个 tag 是否为无效标签（广告/推广/网址/纯数字/纯括号包裹等）。"""
    if not tag:
        return True
    if _INVALID_TAG_PREFIX.match(tag):
        return True
    if _INVALID_TAG_CONTAINS.search(tag):
        return True
    if _INVALID_TAG_SUFFIX.search(tag):
        return True
    if _PURE_DIGITS.match(tag):
        return True
    if _WRAPPED_IN_BRACKETS.match(tag):
        return True
    return False


def guess_tags(tags: List[str]) -> List[str]:
    """清理原始 tags 列表，过滤掉广告/推广类无效标签，返回去重后的有效 tags。

    单个 tag 内如果用；或;混杂了多个词，会先拆成多个独立 tag，再逐个校验；拆分/清理后
    长度不超过 1 个字符的片段一并丢弃。
    """
    result: List[str] = []
    for raw in tags or []:
        if raw is None:
            continue
        tag = str(raw).strip()
        if not tag:
            continue
        for part in _TAG_SPLIT.split(tag):
            part = part.strip()
            if len(part) <= 1 or _is_invalid_tag(part):
                continue
            cleaned = ''.join(c for c in part if c.isprintable())
            if cleaned and cleaned not in result:
                result.append(cleaned)
    return result


if __name__ == "__main__":
    # 测试 guess_tags
    print("\n" + "=" * 60)
    print("=== guess_tags ===")
    tag_tests = [
        # (输入, 期望输出, 说明)
        (["小说", "文学"], ["小说", "文学"], "正常 tags 保留"),
        (["关注公众号"], [], "开头 关注"),
        (["标题：xxx"], [], "开头 标题"),
        (["制作组"], [], "开头 制作"),
        (["下载地址"], [], "开头 下载"),
        (["出版社信息"], [], "开头 出版社"),
        (["http://example.com"], [], "开头 http"),
        (["HTTP://example.com"], [], "开头 http 大写不区分"),
        (["ftp://xxx"], [], "开头 ftp"),
        (["mail:xxx@xxx.com"], [], "开头 mail"),
        (["ISBN 978-x"], [], "开头 isbn 大写不区分"),
        (["小说 精选"], [], "含空格"),
        (["扫描公众号获取"], [], "含公众号"),
        (["加微信xxx"], [], "含微信"),
        (["小说，文学"], [], "含中文逗号"),
        (["www.example.com"], [], "含 www."),
        (["访问xxx.com"], [], "含 .com"),
        (["xxx出品"], [], "含出品"),
        (["联系邮箱a@b.com"], [], "含 @"),
        (["商务印书馆藏书"], [], "含商务印书馆"),
        (["SANQIU小组"], [], "含 SANQIU"),
        (["sanqiu小组"], [], "含 sanqiu 大写不区分"),
        (["独家制作"], [], "结尾 制作"),
        (["精美印刷"], [], "结尾 印刷"),
        (["12345"], [], "纯数字"),
        (["(备注内容)"], [], "半角括号包裹"),
        (["（备注内容）"], [], "全角括号包裹"),
        (["小说", "小说"], ["小说"], "去重"),
        ([""], [], "空字符串过滤"),
        ([None], [], "None 过滤"),
        (["  小说  "], ["小说"], "首尾空格清理"),
        (["历史", "关注公众号", "小说，", "12306", "(x)", "科幻"], ["历史", "科幻"], "混合过滤"),
        (["政治改革；研究；中国；"], ["政治改革", "研究", "中国"], "分号拆分，丢弃末尾空碎片"),
        (["政治改革;研究;中国"], ["政治改革", "研究", "中国"], "半角分号拆分"),
        (["历史；中"], ["历史"], "拆分后单字符碎片被丢弃"),
        (["中"], [], "单字符 tag 本身也被丢弃"),
        (["历史；历史"], ["历史"], "拆分后按去重只保留一份"),
    ]
    all_pass = True
    for idx, (inp, exp, desc) in enumerate(tag_tests, 1):
        got = guess_tags(inp)
        ok = got == exp
        all_pass = all_pass and ok
        status = "PASS" if ok else "FAIL"
        print(f"\n[{status}] #{idx} {desc}")
        print(f"  输入: {inp}")
        if not ok:
            print(f"  期望: {exp}")
            print(f"  实际: {got}")
        else:
            print(f"  结果: {got}  ✓")

    print(f"\n{'=' * 60}")
    print(f"共 {len(tag_tests)} 个测试，{'全部通过 ✓' if all_pass else '存在失败 ✗'}")
