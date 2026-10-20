"""词卡数据源：有道 web 词典 → dictionaryapi.dev → 大模型保底。

为什么要换源：
  原来每次点开一个生词都要调一次文本模型，一个词一个词地在烧 token；
  而音标 / 中文释义 / 词形变化 / 双语例句这些东西词典本来就有，而且更准
  （模型偶尔会把 IPA 写错、把过去分词写成过去式）。

三级顺序（每一级单独兜底，前一级失败自动落到下一级）：
  1. 有道 web 词典 https://dict.youdao.com/jsonapi?q=<词>
     - 零 Key、零额度，给中文释义 + usphone/ukphone + wfs（词形变化）+ 双语例句
     - 是非官方接口：字段或风控随时可能变。所以超时短、失败快、抓不到就往下走，
       并且**失败不缓存**（下次重新试），只有成功结果才落库。
     - trs 里的释义是中文，界面语言为英文时直接用第 2 级。
  2. dictionaryapi.dev https://api.dictionaryapi.dev/api/v2/entries/en/<词>
     - 免费无 Key，给英文释义 + 音标 + 真人发音 audio（Wiktionary 数据）
     - 不支持短语（"break the ice" 会 530），短语只能指望第 1 / 第 3 级
  3. 文本模型（任何 OpenAI 兼容端点）—— 保底，只在 1/2 都拿不到可用内容时调用；
     用户在设置里关掉这一级后，1/2 也失败就如实报错。

卡片结构（前端 cards.js 依赖，别改字段名）：
  {word, phonetic_us, phonetic_uk, pos, meaning, senses:[{pos,def}],
   forms, examples:[{en,zh}], audio, source}
  source ∈ youdao | dictapi | llm —— 词卡底部显示来源，用户能判断数据靠不靠谱。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from ..store import db
from .llm import LLMError, chat, conf_for

log = logging.getLogger("sayit.words")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SayIt/0.2"
TIMEOUT = 8.0          # 词典要"快"：拿不到立刻走下一级，别让用户干等

# ---------------------------------------------------------------------------
# 第 3 级：文本模型
# ---------------------------------------------------------------------------
SYSTEM = """You are an English learner's dictionary for a Chinese-speaking student. \
You produce compact, accurate dictionary cards. Output ONLY valid JSON."""

SCHEMA_HINT = """Return exactly this JSON shape (no markdown fences):
{
  "word": "<the word as given>",
  "phonetic_us": "/.../ IPA, American English",
  "phonetic_uk": "/.../ IPA, British English",
  "pos": "<n. / v. / adj. / adv. / phrase>",
  "meaning": "<meaning in <<UI>>, one short line>",
  "senses": [{"pos": "<n. / v. / ...>", "def": "<meaning in <<UI>>, one short line>"}],
  "forms": "<verb: base / past / past participle / present participle / third person singular; \
noun: plural; adjective: comparative / superlative. Leave out what does not apply. \
If the word has no inflections write 无>",
  "examples": [{"en": "<one natural example sentence, 6-14 words, spoken register>", \
"zh": "<that sentence translated into <<UI>>>"}]
}
Rules:
- Use real IPA, not respelling.
- 2 example sentences, one of them in a spoken-phone-call register.
- senses: at most 3, ordered by how common they are.
- Never invent a word that does not exist. If the input is an expression \
(e.g. "break the ice"), treat it as one entry and put 无 in forms."""

UI_LANG_NAME = {"zh": "Chinese (simplified)", "en": "English"}


def _ui_name(ui_lang: str | None) -> str:
    code = (ui_lang or "zh").lower()
    return UI_LANG_NAME.get(code, UI_LANG_NAME["zh"])


def _ui_code(ui_lang: str | None) -> str:
    code = (ui_lang or "zh").lower()
    return code if code in UI_LANG_NAME else "zh"


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    start = t.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return None
    return None


def _s(v: Any) -> str:
    return str(v or "").strip()


def _blank(word: str) -> dict[str, Any]:
    return {"word": word, "phonetic_us": "", "phonetic_uk": "", "pos": "",
            "meaning": "", "senses": [], "forms": "", "examples": [],
            "audio": "", "source": ""}


# ---------------------------------------------------------------------------
# 第 1 级：有道 web 词典
# ---------------------------------------------------------------------------
_YD_POS = {
    "n": "n.", "v": "v.", "vi": "vi.", "vt": "vt.", "adj": "adj.", "adv": "adv.",
    "prep": "prep.", "conj": "conj.", "pron": "pron.", "num": "num.",
    "art": "art.", "interj": "interj.", "phr": "phrase", "abbr": "abbr.",
}
_POS_RE = re.compile(r"^\s*([a-zA-Z]{1,6})\.\s*")


def _yd_senses(ec_word: dict[str, Any]) -> list[tuple[str, str]]:
    """把有道的 trs 拍平成 [(词性, 释义)]。

    trs 是嵌套结构：trs[].tr[].l.i[] 里既有普通释义字符串，
    也有带 sentence 的例句型条目（短语常见）。只要释义，跳过例句。
    """
    out: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            txt = node.strip()
            if not txt:
                return
            m = _POS_RE.match(txt)
            if m:
                pos = _YD_POS.get(m.group(1).lower(), m.group(1) + ".")
                body = txt[m.end():].strip()
            else:
                pos, body = "", txt
            # 例句型条目长这样："例. He told a joke to break the ice..."
            if body and not body.startswith("例"):
                out.append((pos, body))
        elif isinstance(node, dict):
            for k in ("i", "tr", "l"):
                if k in node:
                    walk(node[k])
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(ec_word.get("trs") or [])
    return out


# 词卡上每条释义最多留几条义项：有道的释义经常是"得到；收到；遭受；接待；接收…"
# 一次列十几条，整张卡被撑满反而不好记。只留前两条，剩下的用"…"提示。
_MEANING_MAX = 2
_MEANING_CHAR_CAP = 90


def _trim_meaning(text: str) -> str:
    """把一长串用「；」连起来的释义压到前 2 条，过长再截断。"""
    parts = [p.strip() for p in re.split(r"[；;]", text) if p.strip()]
    if len(parts) > _MEANING_MAX:
        head = "；".join(parts[:_MEANING_MAX])
        return head + " …"
    out = "；".join(parts) if parts else text
    return out if len(out) <= _MEANING_CHAR_CAP else out[:_MEANING_CHAR_CAP].rstrip() + "…"


def _en_gloss(ec_word: dict[str, Any]) -> str:
    """有道 ee 词典（WordNet）里的英文短释义，给短语/单词做补充。"""
    ee = ec_word.get("ee") or {}
    for t in (ee.get("word") or {}).get("trs") or []:
        for x in (t.get("tr") or []):
            body = x.get("l")
            if isinstance(body, str) and body.strip():
                return body.strip()
            if isinstance(body, dict):
                i = body.get("i")
                if isinstance(i, str) and i.strip():
                    return i.strip()
    return ""


def _yd_forms(ec_word: dict[str, Any]) -> str:
    parts = []
    for f in ec_word.get("wfs") or []:
        wf = f.get("wf") or f
        name, val = _s(wf.get("name")), _s(wf.get("value"))
        if name and val:
            parts.append(f"{name}: {val}")
    return " / ".join(parts)


def _yd_audio(word: str) -> str:
    """有道自己的发音 mp3。simple.word[0].usspeech 形如 "receive&type=2"。"""
    try:
        r = httpx.get("https://dict.youdao.com/jsonapi", params={"q": word, "dicts": '{"count":1,"dicts":[["simple"]]}'},
                      headers={"User-Agent": UA}, timeout=TIMEOUT)
        w0 = ((r.json().get("simple") or {}).get("word") or [{}])[0]
        speech = _s(w0.get("usspeech"))
        if speech:
            return f"https://dict.youdao.com/dictvoice?audio={speech}"
    except Exception:
        pass
    return ""


def _yd_examples(data: dict[str, Any]) -> list[dict[str, str]]:
    """双语例句：优先《精编例句》(blng_sents_part)，其次美剧台词。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    for key in ("blng_sents_part", "media_sents_part", "auth_sents_part"):
        block = data.get(key) or {}
        pairs = block.get("sentence-pair") if isinstance(block, dict) else None
        for p in pairs or []:
            if not isinstance(p, dict):
                continue
            en = p.get("sentence-eng") or p.get("sentence") or ""
            en = re.sub(r"</?b>", "", str(en)).strip()
            zh = _s(p.get("sentence-translation"))
            if not en or en in seen:
                continue
            seen.add(en)
            out.append({"en": en, "zh": zh})
            if len(out) >= 3:
                return out
    return out


def youdao_lookup(word: str) -> dict[str, Any] | None:
    """同步查有道。拿不到可用内容返回 None（调用方会落到下一级）。"""
    try:
        # 注意：不要传 dicts 参数。传了 {dicts:[["ec"]]} 服务端会只回 ec，
        # 把 blng_sents_part（双语例句）/ media_sents_part（美剧台词）整块过滤掉，
        # 词卡就只剩释义没有例句。不带参数时返回全部词典块。
        r = httpx.get("https://dict.youdao.com/jsonapi",
                      params={"q": word},
                      headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200:
            log.info("有道 HTTP %s for %s", r.status_code, word)
            return None
        data = r.json()
    except Exception as e:
        log.info("有道查询失败 %s: %s: %s", word, type(e).__name__, e)
        return None

    items = ((data.get("ec") or {}).get("word") or [])
    w0 = items[0] if items and isinstance(items[0], dict) else None
    senses_raw = _yd_senses(w0) if w0 else []
    if not senses_raw:
        # 短语等词条 ec 里可能为空，退回 simple 的释义
        simple = data.get("simple") or {}
        senses_raw = _yd_senses(simple) if simple else []
    if not senses_raw:
        return None

    card = _blank(word)
    if w0:
        card["phonetic_us"] = _s(w0.get("usphone"))
        card["phonetic_uk"] = _s(w0.get("ukphone"))
        card["forms"] = _yd_forms(w0)
        # 有道自带发音（dictvoice），比浏览器 TTS 更像词典
        speech = _s(w0.get("usspeech"))
        if speech:
            card["audio"] = f"https://dict.youdao.com/dictvoice?audio={speech}"

    # 同词性的多条释义并成一条，避免词卡被 5 行 "v. 收到" 撑满
    merged: list[dict[str, str]] = []
    for pos, d in senses_raw[:8]:
        d = _trim_meaning(d)
        if merged and merged[-1]["pos"] == pos:
            merged[-1]["def"] = merged[-1]["def"] + "；" + d
        else:
            merged.append({"pos": pos, "def": d})
    card["senses"] = merged[:3]
    is_phrase = " " in word.strip()
    card["pos"] = card["senses"][0]["pos"] or ("phrase" if is_phrase else "")
    card["meaning"] = card["senses"][0]["def"]

    # 短语在 ec 里没有词性标签，用 ee（WordNet）的英文短释义补一行，卡片不至于空着
    if not card["pos"] and not card["senses"][0]["pos"]:
        gloss = _en_gloss(data)
        if gloss:
            card["senses"][0]["def"] = card["senses"][0]["def"] + f"（{gloss}）"
            card["meaning"] = card["senses"][0]["def"]

    card["examples"] = _yd_examples(data)
    card["source"] = "youdao"
    return card


# ---------------------------------------------------------------------------
# 第 2 级：dictionaryapi.dev（英文释义 + 真人发音）
# ---------------------------------------------------------------------------
def dictapi_lookup(word: str) -> dict[str, Any] | None:
    try:
        r = httpx.get("https://api.dictionaryapi.dev/api/v2/entries/en/" + word,
                      headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200:
            log.info("dictionaryapi HTTP %s for %s", r.status_code, word)
            return None
        data = r.json()
    except Exception as e:
        log.info("dictionaryapi 查询失败 %s: %s: %s", word, type(e).__name__, e)
        return None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    e0 = data[0]

    card = _blank(word)
    card["phonetic_us"] = _s(e0.get("phonetic"))
    for p in e0.get("phonetics") or []:
        if isinstance(p, dict):
            if not card["phonetic_us"]:
                card["phonetic_us"] = _s(p.get("text"))
            a = _s(p.get("audio"))
            if a and not card["audio"]:
                card["audio"] = ("https:" + a) if a.startswith("//") else a

    senses, examples = [], []
    for m in e0.get("meanings") or []:
        if not isinstance(m, dict):
            continue
        pos = _s(m.get("partOfSpeech"))
        for d in (m.get("definitions") or [])[:2]:
            if not isinstance(d, dict):
                continue
            if _s(d.get("definition")):
                senses.append({"pos": pos, "def": _s(d.get("definition"))})
            ex = _s(d.get("example"))
            if ex and len(examples) < 2:
                examples.append({"en": ex, "zh": ""})
        if len(senses) >= 3:
            break
    if not senses:
        return None
    card["senses"] = senses[:3]
    card["pos"] = senses[0]["pos"]
    card["meaning"] = senses[0]["def"]
    card["examples"] = examples
    card["source"] = "dictapi"
    return card


# ---------------------------------------------------------------------------
# 第 3 级：大模型
# ---------------------------------------------------------------------------
async def llm_lookup(user: str, word: str, ui_lang: str) -> dict[str, Any] | None:
    conf = conf_for(user)
    if not conf.get("enabled", True):
        return None
    ui_name = _ui_name(ui_lang)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f'Word: "{word}"\n'
            f"Write all explanatory text (meanings, translations) in {ui_name}.\n"
            f"{SCHEMA_HINT.replace('<<UI>>', ui_name)}"},
    ]
    try:
        text = await chat(
            messages,
            base_url=conf.get("base_url", ""),
            api_key=conf.get("api_key", ""),
            model=conf.get("model", "deepseek-chat"),
            temperature=0.2,
            max_tokens=900,
            timeout=45.0,
            response_format={"type": "json_object"},
        )
    except LLMError as e:
        log.info("词卡 LLM 保底失败 %s: %s", word, e)
        return None

    raw = _extract_json(text)
    if raw is None:
        log.warning("词卡 LLM JSON 解析失败 %s: %s", word, text[:200])
        return None

    card = _blank(word)
    card["word"] = _s(raw.get("word")) or word
    card["phonetic_us"] = _s(raw.get("phonetic_us"))
    card["phonetic_uk"] = _s(raw.get("phonetic_uk"))
    card["pos"] = _s(raw.get("pos"))
    card["senses"] = [{"pos": _s(x.get("pos")), "def": _s(x.get("def"))}
                      for x in (raw.get("senses") or [])[:3]
                      if isinstance(x, dict) and _s(x.get("def"))]
    card["meaning"] = _s(raw.get("meaning")) or (card["senses"][0]["def"]
                                                if card["senses"] else "")
    card["forms"] = _s(raw.get("forms"))
    card["examples"] = [{"en": _s(x.get("en")), "zh": _s(x.get("zh"))}
                        for x in (raw.get("examples") or [])[:3]
                        if isinstance(x, dict) and _s(x.get("en"))]
    card["source"] = "llm"
    return card if (card["senses"] or card["meaning"]) else None


# ---------------------------------------------------------------------------
# 对外：一块查（带缓存）
# ---------------------------------------------------------------------------
async def card(user: str, word: str, ui_lang: str = "zh", force: bool = False,
               allow_llm: bool = True) -> dict[str, Any]:
    """取（必要时查询）一张词卡。返回 {ok, word, card, cached, source, message?}

    allow_llm=False 只用词典：不花 token，但词典都没收录的词会直接报错。
    """
    word = (word or "").strip()[:64]
    ui_lang = _ui_code(ui_lang)
    if not word:
        return {"ok": False, "message": "没有这个词", "word": word}

    row = db.get_word(user, word)
    cached = (row or {}).get("card")
    if cached and not force and (row or {}).get("ui_lang", "zh") == ui_lang:
        return {"ok": True, "word": word, "card": cached, "cached": True,
                "source": cached.get("source", ""),
                "meaning": (row or {}).get("meaning", "")}

    # 1) 有道（中文释义）。英文界面下中文释义用不上，直接用第 2 级
    found = youdao_lookup(word) if ui_lang == "zh" else None

    # 2) dictionaryapi：有道缺发音或没有例句时也去补一次（补到就合并）
    if found is None:
        found = dictapi_lookup(word)
    elif not found.get("audio") or not found.get("examples"):
        extra = dictapi_lookup(word)
        if extra:
            found["audio"] = found.get("audio") or extra.get("audio", "")
            if not found.get("examples"):
                found["examples"] = extra.get("examples") or []
            if not found.get("phonetic_us"):
                found["phonetic_us"] = extra.get("phonetic_us", "")

    # 3) 大模型保底
    if found is None and allow_llm:
        found = await llm_lookup(user, word, ui_lang)

    if found is None:
        msg = ("词典没有收录这个词" if not allow_llm
               else "词典没查到，文本模型也没给出结果（可在设置里检查 Key）")
        return {"ok": False, "word": word, "card": None, "message": msg}

    db.save_word_card(user, word, found, ui_lang)
    log.info("词卡 %s 来自 %s", word, found.get("source"))
    return {"ok": True, "word": word, "card": found, "cached": False,
            "source": found.get("source", ""),
            "meaning": (row or {}).get("meaning", "")}
