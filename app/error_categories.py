"""错题的细分类别。

为什么要细分类：初学者错得多，几十上百条平铺在一页上看不完，也看不出「我到底弱在哪个点」。
粗分类（grammar / tense / word / order / pronunciation）只有 5 个桶，时态错误和
介词错误会挤在同一个「语法」里，用户点进去还是看到一堆不相关的东西。

所以这里用「二级分类」：保留原来的 type 做一级（前端配色/统计兼容），
再加一个 category 做二级（错题本按它分组建卡片）。category 的粒度按「一个可以
单独练的知识点」来定，例如「介词搭配」「冠词」「时态-过去式」「不规则动词」——
同一个知识点的错题放一起，用户点进去看到的是一组同源错误，练起来才有意义。

新增类别只在这里加一行，review 的 prompt、错题本的分组、prompt 注入都不用改。
"""
from __future__ import annotations

# code -> (中文名, 英文给模型看的一句话说明, 属于哪一级 type)
# 说明文字会原样拼进复盘 prompt 的候选清单，让模型知道有哪些桶可以选。
CATEGORIES: dict[str, tuple[str, str, str]] = {
    # ---- 语法 ----
    "preposition":  ("介词搭配", "wrong preposition: on Monday / in the table / talk with sb",
                     "grammar"),
    "article":      ("冠词", "missing or wrong article: a / an / the / no article",
                     "grammar"),
    "plural":       ("名词单复数", "singular vs plural, countable vs uncountable nouns",
                     "grammar"),
    "agreement":    ("主谓一致", "subject-verb agreement: he go / they goes",
                     "grammar"),
    "comparison":   ("比较级最高级", "comparative / superlative forms: more better / the most easiest",
                     "grammar"),
    "pronoun":      ("代词", "wrong pronoun form: me vs I, my vs mine, its vs it's",
                     "grammar"),
    "sentence":     ("句子结构", "run-on sentence, missing subject/verb, fragment, connecting two clauses",
                     "grammar"),
    "modal":        ("情态与助动词", "can / could / should / must / have to, do-support, double auxiliary",
                     "grammar"),

    # ---- 时态 ----
    "tense_past":      ("时态-过去式", "simple past used where required, or present used for a past event",
                        "tense"),
    "tense_perfect":   ("时态-完成时", "present perfect vs simple past, missing have/has + participle",
                        "tense"),
    "tense_progressive": ("时态-进行时", "missing or wrong -ing form, progressive vs simple",
                          "tense"),
    "tense_future":    ("时态-将来", "will / going to / present continuous for future",
                        "tense"),
    "verb_form":       ("动词形式", "irregular verb forms, infinitive vs gerund, -ed / -ing",
                        "tense"),

    # ---- 用词 ----
    "word_choice":  ("用词不准", "understandable but not what a native speaker would say",
                     "word"),
    "collocation":  ("固定搭配", "wrong word partner: make a photo / do a mistake",
                     "word"),
    "chinese_english": ("中式英语", "direct translation from Chinese that sounds unnatural",
                        "word"),
    "phrasal":      ("动词短语", "wrong or missing particle: look after / look for / look up",
                     "word"),

    # ---- 语序 ----
    "order_question": ("疑问句语序", "word order in questions, missing do/does/did",
                       "order"),
    "order_adverb":   ("副词位置", "adverb placement: frequency, manner, time",
                       "order"),
    "order_adj":      ("形容词顺序", "adjective order, adjective vs adverb after a verb",
                       "order"),

    # ---- 发音 ----
    "pronunciation": ("发音", "specific sound or stress problem, only when clearly audible",
                      "pronunciation"),

    # 兜底：模型判断不出具体类别时才用，正常情况下不该出现
    "other":        ("其他", "anything that does not fit the buckets above",
                     "grammar"),
}

# 一级 type 的中文名（前端配色/统计沿用原有 5 个）
TYPES: dict[str, str] = {
    "grammar": "语法",
    "tense": "时态",
    "word": "用词",
    "order": "语序",
    "pronunciation": "发音",
}

# 给模型看的清单：每个一级下面列二级
def prompt_block() -> str:
    """拼成复盘 prompt 里的一段：类别清单 + 要求。"""
    by_type: dict[str, list[str]] = {}
    for code, (zh, en, t) in CATEGORIES.items():
        if code == "other":
            continue
        by_type.setdefault(t, []).append(f'  - "{code}" = {zh}：{en}')
    lines = []
    for t, items in by_type.items():
        lines.append(f"{TYPES.get(t, t)} ({t}):")
        lines.extend(items)
    return "\n".join(lines)


# 一级 type → 该级下最泛用的类别。模型只肯给一级 type 时用这个，
# 而不是「这一级里的第一个」（那会把所有语法类错误压成「介词搭配」）。
_GENERIC_BY_TYPE: dict[str, str] = {
    "grammar": "sentence",          # 「句子结构」最能涵盖笼统的语法问题
    "tense": "verb_form",           # 「动词形式」比具体某个时态更泛
    "word": "word_choice",          # 「用词不准」本身就是泛称
    "order": "order_question",      # 语序里最常见的一类
    "pronunciation": "pronunciation",
}


def label(code: str) -> str:
    """category code → 中文名。未知 code 原样返回。"""
    return CATEGORIES.get(code, (code, "", ""))[0]


def type_of(code: str) -> str:
    """category code → 一级 type（未知回退 grammar）。"""
    return CATEGORIES.get(code, ("", "", "grammar"))[2]


def normalize(code: str | None, fallback_type: str = "grammar") -> str:
    """把模型给的 category 规整成合法 code。

    模型可能给空串、给一级 type 名（"tense"）、给中文名（"介词"）、给不存在的值。
    依次退化：合法 code → 中文名反查 → 一级 type 下最贴切的类别 → other。

    注意：**绝不能「随便取第一个类别」兜底**。CATEGORIES 的第一个是 preposition，
    那样所有给不出类别的错误都会被塞进「介词搭配」—— 错题本会变成一堆假的介词错误。
    判断不出来就说判断不出来（other），宁可如实归到「其他」。
    """
    c = (code or "").strip()
    if c in CATEGORIES:
        return c
    # 模型给了中文名
    for k, (zh, _en, _t) in CATEGORIES.items():
        if zh == c:
            return k
    # 模型只给了一级 type：用该一级下最泛用的那个类别
    t = c if c in TYPES else (fallback_type or "").strip()
    return _GENERIC_BY_TYPE.get(t, "other")
