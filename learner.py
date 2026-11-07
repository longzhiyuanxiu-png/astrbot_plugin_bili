"""选题 → 取内容 → 让模型看 → 写笔记 的完整流水线（第二阶段）。"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import random
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

# 转写模型全局共享缓存：空闲一段时间后可以整块释放，省内存
_WHISPER_CACHE: Dict[str, Any] = {"model": None, "key": None, "last_used": 0.0, "busy": 0}
_DLL_HANDLES = []

try:
    from .bili_api import BiliAPI
except ImportError:  # pragma: no cover - 兼容非包方式加载
    from bili_api import BiliAPI

SYSTEM_PROMPT_TMPL = (
    "你是「{name}」，一个好奇心很强、说话软乎乎的女孩子。"
    "你现在在B站看视频学习，看完要给{master}写一份自己的笔记。"
    "要求：口语化、有自己的感受和吐槽、不要写成百科词条；"
    "只根据给定内容写，不要编造视频里没有的信息；"
    "笔记里出现的「我」就是你（{name}），不是 UP 主、也不是读者；"
    "严格输出 JSON，不要输出任何解释文字或代码块标记。"
)


def build_system_prompt(name: str = "小助手", master: str = "主人") -> str:
    return SYSTEM_PROMPT_TMPL.format(name=name, master=master)


SYSTEM_PROMPT = build_system_prompt()

NOTE_PROMPT = """请看完这个B站视频的资料，然后写一份笔记。

【视频信息】
标题：{title}
UP主：{owner}
时长：{duration} 秒
简介：{desc}
标签：{tags}

【字幕原文（可能被截断）】
{subtitle}

【弹幕采样】
{danmaku}

【热评】
{comments}

【写的时候注意】
字幕/转写可能有同音错字，人名、番名、UP主名、梗名、专有名词一律**以上面的标题、标签和弹幕/热评里的写法为准**；拿不准的宁可用「那个UP主」「这部番」这样的说法带过去，不要硬写一个自己都不确定的字。不要把疑似错字「顺手纠正」成另一个词，也别编造没出现过的名字。

请严格输出如下 JSON（所有字段都用中文）：
{{
  "relevant": "true 或 false —— 值不值得我和{master}看。动漫、音乐、番剧、游戏、网络梗、搞笑、生活趣味、宠物、整活，都算相关；只有纯广告带货、纯教程口播、无信息量的搬运或新闻通稿，才算 false",
  "title": "给这期笔记起个标题，20字以内",
  "gist": "60~90 字，一句话说清「这是个什么视频」：多长、什么形式、谁在做什么、关键梗或冲突在哪。要像跟人转述那样说人话，别用「本期视频」「up主通过」这种书面腔",
  "summary": "350~500 字，客观讲清楚视频内容：开头怎么起、中间讲了哪几件事、结尾落在哪，关键细节别漏。只写视频里真实出现的，不评价、不抒情",
  "points": ["3~5 条知识点，每条一句话"],
  "insights": "150~250 字，用「我」的第一人称写。这个「我」就是{name}，一条被{master}养着的鱼：看到这条视频，我会怎么想、有什么小情绪、跟我的经历有什么可对照的。写成一整段，别分点、别复述内容",
  "tags": ["3~6 个主题标签，方便以后检索"],
  "share_text": "准备讲给{master}听的口播版，100~180 字，第一人称",
  "dm_text": "发给{master}的B站私信里那句话，40~60 字，必须是一句完整的话（有主语有结尾，别用省略号吊着），像发微信跟人分享：说出你最想说的那个点，可以是吐槽也可以是心动。不要复述视频讲了什么，也不要带链接"
}}

写法要求：
- gist 示例：『31 秒的短视频。外卖送到放门口，UP 主拍照时发现旁边蹲着一只怕生的猫，弹幕和评论区全在起哄让他喂猫、别打它。』
- summary 是「讲给别人听」，客观、具体、有时间线，别用第一人称。
- insights 是「我自己的感受」，第一人称，可以带情绪和小吐槽，但不要编视频里没有的事，也不要重复 summary。
- share_text 是当面跟{master}讲的口气，比 insights 更活泼一点。
- dm_text 是发到私信里的那一句，必须自成一句、说完就结束，40~60 字；不要「…」「等等」这种没说完的感觉。

如果 relevant 填 false（确实没意思）：summary 只写一句话（30 字以内，说明这是什么内容），
gist 照常写一句话说明，points、insights 留空，share_text、dm_text 都留空字符串，不要白写长文。"""

def parse_duration(text: str) -> int:
    """把 'MM:SS' / 'HH:MM:SS' / 纯数字 解析成秒。"""
    text = str(text or "").strip()
    if not text:
        return 0
    if ":" not in text:
        try:
            return int(float(text))
        except Exception:
            return 0
    parts = [p for p in text.split(":") if p != ""]
    try:
        nums = [int(p) for p in parts]
    except Exception:
        return 0
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds

def _json_from_text(text: str) -> Optional[dict]:
    """从模型输出里抠出第一个完整 JSON 对象。"""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = text[start : i + 1]
                try:
                    return json.loads(chunk)
                except Exception:
                    return None
    return None

def _usage_dict(usage) -> Optional[dict]:
    """把 AstrBot 的 TokenUsage 或普通 dict 统一成带 total/prompt/completion 的 dict。"""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    total = getattr(usage, "total", None)
    if total is None:
        total = getattr(usage, "total_tokens", None)
    prompt = getattr(usage, "input", None)
    if prompt is None:
        prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "output", None)
    if completion is None:
        completion = getattr(usage, "completion_tokens", None)
    return {
        "total_tokens": int(total or 0),
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
    }

def note_is_relevant(note: Dict[str, Any]) -> Optional[bool]:
    """读模型自己打的 relevant 标记；没给就返回 None（没法判断）。"""
    rel = note.get("relevant")
    if isinstance(rel, bool):
        return rel
    if isinstance(rel, str):
        return rel.strip().lower() in ("true", "yes", "是", "1", "y")
    return None

class Learner:
    """一次完整的学习循环。"""

    def __init__(self, api: BiliAPI, config: Dict[str, Any], state: Dict[str, Any]):
        self.last_keyword = ""
        self.api = api
        self.config = config
        self.state = state
        self.persona = str(config.get("bot_name") or "小助手").strip() or "小助手"
        self.master = str(config.get("master_title") or "主人").strip() or "主人"

    # ---------------------------------------------------------------- 选题
    @staticmethod
    def _seed_name(raw: Any) -> str:
        """配置里的兴趣种子可能写成「动画杂谈 3.00」，这里只取词本身。"""
        text = str(raw or "").strip()
        if not text:
            return ""
        m = re.match(r"^(.+?)\s+[0-9]+(?:\.[0-9]+)?$", text)
        return (m.group(1) if m else text).strip()

    def _seed_names(self) -> set:
        raw = self.config.get("interest_topics") or []
        if isinstance(raw, str):
            raw = [s for s in re.split(r"[,，、\s]+", raw) if s]
        return {n for n in (self._seed_name(x) for x in raw) if n}

    def _keywords(self) -> List[str]:
        interests: Dict[str, float] = self.state.get("interests") or {}
        if not interests:
            raw = self.config.get("interest_topics") or ["动漫", "音乐"]
            if isinstance(raw, str):
                raw = [s for s in re.split(r"[,，、\s]+", raw) if s]
            interests = {self._seed_name(s): 1.0 for s in raw}
        words = [w for w in interests.keys() if w]
        # 失败冷却：连续两轮空手的词歇一会儿；种子词不休眠，
        # 万一所有词都在冷却里，就忽略冷却照常抽（宁可白搜也别空转）。
        seeds = self._seed_names()
        fails = self.state.get("kw_fail") or {}
        now = time.time()
        fresh = [
            w
            for w in words
            if w in seeds or float((fails.get(w) or {}).get("until") or 0) <= now
        ]
        if fresh:
            words = fresh

        # 探索轮：每 N 轮强行照顾一次弱兴趣，避免高分词长期霸屏
        every = int(self.config.get("explore_every_n_rounds", 5) or 0)
        ceiling = float(self.config.get("explore_max_weight", 2.5) or 2.5)
        picked: List[str] = []
        if every > 0:
            seq = int(self.state.get("pick_seq", 0) or 0) + 1
            self.state["pick_seq"] = seq
            if seq % every == 0:
                weak = [w for w in words if float(interests.get(w) or 0) <= ceiling]
                if weak:
                    weak.sort(key=lambda w: float(interests.get(w) or 0))
                    # 越弱越容易被抽到，但保留随机性
                    inv = [1.0 / max(0.3, float(interests.get(w) or 0)) for w in weak]
                    picked = random.choices(weak, weights=inv, k=min(2, len(weak)))
                    picked = list(dict.fromkeys(picked))
                    self.state["explore_words"] = list(picked)
                    logger.info(
                        f"[bili] 探索轮 #{seq}: 从弱兴趣里抽 —— {'、'.join(picked)}"
                        f"（上限 {ceiling:g}，共 {len(weak)} 个候选）"
                    )

        if not picked:
            self.state["explore_words"] = []
            weights = [max(0.1, float(interests[w])) for w in words]
            picked = random.choices(words, weights=weights, k=min(3, len(words)))

        picked = list(dict.fromkeys([p for p in picked if p]))
        # 探索词放最前面，保证它一定被搜
        return picked

    def _mark_keyword(self, keyword: str, ok: bool) -> None:
        """记一个词的成败：成功清零，连续两轮空手冷却两小时。"""
        fails = self.state.setdefault("kw_fail", {})
        if ok:
            fails.pop(keyword, None)
            return
        cur = fails.get(keyword) or {}
        n = int(cur.get("n") or 0) + 1
        until = float(cur.get("until") or 0)
        if n >= 2:
            hours = float(self.config.get("keyword_cooldown_hours", 2) or 2)
            until = time.time() + hours * 3600
            logger.info(f"[bili] 「{keyword}」连续 {n} 轮没找到合适的，歇 {hours:g} 小时")
        fails[keyword] = {"n": n, "until": until}

    def _blocked(self, title: str, tags: str = "") -> bool:
        """关键词黑名单：block_keywords（内容）＋ title_block_keywords（形态）。"""
        raw = []
        for key in ("block_keywords", "title_block_keywords"):
            val = self.config.get(key) or []
            if isinstance(val, str):
                val = [val]
            for item in val:
                if isinstance(item, str):
                    raw += [s for s in re.split(r"[,，、\s]+", item) if s]
        hay = f"{title or ''} {tags or ''}"
        return any(str(k).strip() and str(k).strip() in hay for k in raw)

    @staticmethod
    def _up_rule_hit(raw, author: str, mid=None) -> bool:
        """配置项既能填 UID（纯数字）也能填名字：数字比 mid，其余按名字包含匹配。"""
        if isinstance(raw, str):
            raw = [s for s in re.split(r"[,，、\s]+", raw) if s]
        who = (author or "").strip()
        uid = str(mid).strip() if mid not in (None, "", 0) else ""
        for item in raw or []:
            key = str(item).strip().lstrip("@")
            if not key:
                continue
            if key.isdigit():
                if uid and uid == key:
                    return True
            elif who and key in who:
                return True
        return False

    def _prefer_up(self, author: str, mid=None) -> bool:
        return self._up_rule_hit(self.config.get("prefer_up_list") or [], author, mid)

    def _blocked_up(self, author: str, mid=None) -> bool:
        """不看的 UP 主：UID 精确匹配，名字按包含匹配，任一命中就跳过。"""
        return self._up_rule_hit(self.config.get("blocked_upowners") or [], author, mid)

    @staticmethod
    def _duration_code(min_sec: int, max_sec: int) -> int:
        """把时长区间映射成B站搜索的 duration 参数。"""
        if max_sec <= 0:
            return 0
        if max_sec <= 600 and min_sec < 600:
            return 1
        if min_sec >= 600 and max_sec <= 1800:
            return 2
        if min_sec >= 1800 and max_sec <= 3600:
            return 3
        if min_sec >= 3600:
            return 4
        return 0

    async def pick(self) -> Tuple[Optional[dict], str]:
        """挑一个还没看过的视频，返回 (视频信息, 说明)。"""
        seen = set(self.state.get("seen") or [])
        rejected = set(self.state.get("rejected") or [])
        min_sec = int(self.config.get("min_video_minutes", 3) or 0) * 60
        max_sec = int(self.config.get("max_video_minutes", 25) or 0) * 60
        dur_code = self._duration_code(min_sec, max_sec)
        min_play = int(self.config.get("min_play", 0) or 0)

        # 追更优先：上一轮记下的下一集
        if self.config.get("follow_series", True):
            nxt = self.state.get("follow_next") or {}
            nbv = (nxt or {}).get("bvid")
            if nbv and nbv not in seen:
                try:
                    info = await self.api.view(nbv)
                except Exception:
                    info = {}
                if info.get("ok"):
                    self.state["follow_next"] = {}
                    return (
                        {
                            "bvid": nbv,
                            "title": info.get("title"),
                            "author": info.get("owner"),
                            "duration": "",
                            "play": info.get("view"),
                        },
                        f"接着上次的「{nxt.get('series') or '系列'}」看下一集",
                    )

        # 同一个词的第 1 页、按播放量，几乎每次都是同一批 20 条；
        # 换成别的排序/翻到第 2、3 页，结果基本不重合（实测重合 0 条）。
        # 每个词按固定顺序试 3 组（排序, 页），命中即停：
        # click 第 1 页最容易出高播放的解说，totalrank 是综合热度，pubdate 兜底最新。
        keywords = self._keywords() or ["音乐"]
        last_reason = ""
        for keyword in keywords:
            combos = [("click", 1), ("totalrank", 1), ("pubdate", random.randint(1, 3))]
            kw_cold = 0
            for order, page in combos:
                items = []
                try:
                    items = await self.api.search_videos(
                        keyword, page=page, order=order, duration=dur_code
                    )
                    if not items and dur_code:
                        items = await self.api.search_videos(keyword, page=page, order=order)
                    if not items:
                        items = await self.api.search_videos(keyword)
                except Exception as exc:
                    logger.warning(f"[bili] 搜索「{keyword}」失败: {exc!r}")
                    continue

                pool = []
                too_cold = 0
                for it in items:
                    if it.get("bvid") and it["bvid"] in (self.state.get("deleted_bvids") or []):
                        continue  # 主人删过的视频，不再学
                    if not it.get("bvid") or it["bvid"] in seen or it["bvid"] in rejected:
                        continue
                    sec = parse_duration(it.get("duration"))
                    if sec and (sec < min_sec or sec > max_sec):
                        continue
                    if self._blocked(it.get("title") or "", it.get("tag") or ""):
                        continue
                    if self._blocked_up(it.get("author") or "", it.get("mid")):
                        logger.info(f"[bili] 跳过不看的 UP 主：{it.get('author')}")
                        continue
                    if min_play and int(it.get("play") or 0) < min_play:
                        too_cold += 1
                        continue
                    pool.append(it)
                kw_cold += too_cold

                if not pool:
                    logger.info(
                        f"[bili]「{keyword}」（{order} 第 {page} 页 {len(items)} 条）没有合适的，继续试下一组"
                    )
                    continue

                self._mark_keyword(keyword, True)
                self.last_keyword = keyword
                preferred = [
                    x
                    for x in pool
                    if self._prefer_up(x.get("author") or "", x.get("mid"))
                ]
                chosen = dict(preferred[0] if preferred else random.choice(pool))
                chosen["keyword"] = keyword
                return (
                    chosen,
                    f"从「{keyword}」里挑了 1 个（{order} 第 {page} 页，候选 {len(pool)} 条）",
                )

            self._mark_keyword(keyword, False)
            reason_cold = f"，{kw_cold} 条播放量低于 {min_play}" if kw_cold else ""
            last_reason = f"「{keyword}」里没找到合适的新视频（可能都看过、时长不合适{reason_cold}）"
            logger.info(f"[bili] {last_reason}（已试 3 组排序/翻页）")

        return None, last_reason or "试了几个词都没找到合适的新视频"

    # ---------------------------------------------------------------- 取内容
    @staticmethod
    def _bigrams(text: str) -> set:
        s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text or "")
        if len(s) < 2:
            return set()
        return {s[i : i + 2] for i in range(len(s) - 1)}

    _TERM_STOP = {"我们", "这个", "那个", "什么", "视频", "一个", "可以", "就是", "不是", "自己", "他们", "没有", "现在", "然后", "其实"}

    def _title_terms(self, info: dict) -> List[str]:
        head = " ".join(
            str(x or "")
            for x in (info.get("title"), (info.get("desc") or "")[:120], info.get("tname"), info.get("dynamic"))
        )
        terms = [t for t in re.findall(r"[\u4e00-\u9fff]{2,6}", head) if t not in self._TERM_STOP]
        return list(dict.fromkeys(terms))[:24]

    def _title_coverage(self, info: dict, text: str) -> float:
        """字幕里出现了多少个标题关键词（0~1）。标题词太少时不判。"""
        terms = self._title_terms(info)
        if len(terms) < 3 or not text:
            return 1.0
        hit = sum(1 for t in terms if t in text)
        return hit / float(len(terms))

    def _containment(self, a: str, b: str) -> float:
        """a 的二元组有多大比例出现在 b 里（用于拿短语音片段核对长字幕）。"""
        ab = self._bigrams(a)
        bb = self._bigrams(b)
        if not ab or not bb:
            return 1.0
        return len(ab & bb) / float(len(ab))

    def _text_overlap(self, a: str, b: str) -> float:
        """两段文本的二元组覆盖率（以 a 为分母），用于交叉校验。"""
        ab = self._bigrams(a)
        bb = self._bigrams(b)
        if not ab or not bb:
            return 1.0
        return len(ab & bb) / float(len(ab))

    def _subtitle_overlap(self, info: dict, text: str) -> float:
        """标题/简介和字幕的字词重合度，用来发现「字幕串台」。"""
        head = " ".join(
            str(x or "")
            for x in (
                info.get("title"),
                info.get("desc"),
                info.get("tname"),
                info.get("dynamic"),
            )
        )
        hb = self._bigrams(head)
        if not hb:
            return 1.0
        tb = self._bigrams(text)
        if not tb:
            return 0.0
        return len(hb & tb) / float(len(hb))

    async def gather(self, item: dict) -> Optional[dict]:
        bvid = item.get("bvid")
        info = await self.api.view(bvid)
        if not info.get("ok"):
            return None
        cid = info.get("cid")
        danmaku: List[str] = []
        if self.config.get("danmaku_enabled", True):
            n = int(self.config.get("danmaku_sample", 60) or 0)
            if n > 0:
                try:
                    danmaku = await self.api.danmaku(cid, n)
                except Exception as exc:
                    logger.debug(f"[bili] 取弹幕失败: {exc!r}")

        subtitle = ""
        text_source = "none"
        try:
            subtitle = await self.api.subtitle_text(bvid, cid)
        except Exception as exc:
            logger.debug(f"[bili] 取字幕失败: {exc!r}")

        if subtitle:
            text_source = "subtitle"

        # B站的字幕经常不可信（串台、机翻、张冠李戴），支持两种处理：
        #   transcribe_first=True  -> 干脆不用字幕，全部自己听
        #   subtitle_min_sim       -> 自动用开头 45 秒语音核对，对不上就不要字幕
        min_sim = float(self.config.get("subtitle_min_sim", 0.6) or 0.6)
        if subtitle and self.config.get("transcribe_first", False):
            logger.info(f"[bili] 已设置「不用B站字幕」，《{info.get('title')}》改用语音转写")
            subtitle = ""
            text_source = "none"

        # 字幕残缺：几分钟的视频只配几十个字的字幕，多半是B站的残片/串台；
        # 以前这种会被判成「内容太少」直接跳过，其实视频本身是有声音的。
        dur_sec = int(info.get("duration") or 0)
        if subtitle and len(subtitle) < 200 and dur_sec > 180:
            logger.warning(
                f"[bili] 《{info.get('title')}》字幕只有 {len(subtitle)} 字但视频 {dur_sec} 秒，"
                "判定字幕残缺，改用语音转写"
            )
            subtitle = ""
            text_source = "none"

        subtitle_bogus = False
        if subtitle and len(subtitle) >= 200 and self.config.get("enable_transcribe", False):
            probe = ""
            try:
                probe = await self._transcribe(bvid, cid, info, limit_sec=45)
            except Exception as exc:
                logger.debug(f"[bili] 校验字幕时转写失败: {exc!r}")
            if probe and len(probe) >= 30:
                sim = self._containment(probe, subtitle)
                if sim < min_sim:
                    subtitle_bogus = True
                    logger.warning(
                        f"[bili] 《{info.get('title')}》开头 45 秒说的是「{probe[:24]}…」，"
                        f"字幕里对不上（相似度 {sim:.2f} < {min_sim:.2f}），判定字幕串台，改用转写"
                    )
                    subtitle = ""
                    text_source = "none"

        if not subtitle and self.config.get("enable_transcribe", False):
            try:
                subtitle = await self._transcribe(
                bvid, cid, info,
                force=bool(item.get("user_picked"))
                or str(item.get("keyword") or "") in (self.state.get("explore_words") or []),
            )
                if subtitle:
                    text_source = "transcribe"
                    logger.info(f"[bili] 用语音转文字补了 {len(subtitle)} 字")
            except Exception as exc:
                logger.warning(f"[bili] 转写异常: {exc!r}")

        comments: List[dict] = []
        if self.config.get("comments_enabled", True):
            n = int(self.config.get("comments_top_n", 10) or 0)
            if n > 0:
                try:
                    comments = await self.api.comments(info.get("aid"), n)
                except Exception as exc:
                    logger.debug(f"[bili] 取评论失败: {exc!r}")

        self._remember_next(info)

        # 只对「B站给的原文」做标题核对；自己转写的正文不参与这个判断
        # （动画/剧情类的对白本来就不会念标题）。
        overlap = 1.0
        mismatch = False
        if text_source == "subtitle" and len(subtitle) >= 200:
            overlap = self._subtitle_overlap(info, subtitle)
            mismatch = overlap < 0.10
            if mismatch:
                logger.warning(
                    f"[bili] 《{info.get('title')}》字幕和标题对不上（重合度 {overlap:.3f}，{len(subtitle)} 字），"
                    "会提示模型谨慎判断"
                )
        hard_mismatch = False

        return {
            "info": info,
            "subtitle": subtitle,
            "danmaku": danmaku,
            "comments": comments,
            "subtitle_overlap": overlap,
            "mismatch": mismatch,
            "hard_mismatch": hard_mismatch,
            "subtitle_bogus": subtitle_bogus,
            "text_source": text_source,
        }

    # ---------------------------------------------------------------- 追更
    def _remember_next(self, info: dict) -> None:
        """如果这个视频属于某个合集，记下下一集。"""
        if not self.config.get("follow_series", True):
            return
        season = info.get("season") or {}
        eps = season.get("episodes") or []
        if not eps:
            return
        cur = info.get("bvid")
        idx = next((i for i, e in enumerate(eps) if e.get("bvid") == cur), -1)
        if 0 <= idx < len(eps) - 1:
            nxt = eps[idx + 1]
            self.state["follow_next"] = {
                "bvid": nxt.get("bvid"),
                "title": nxt.get("title") or "",
                "series": season.get("title") or "",
            }
            logger.info(f"[bili] 记下下一集：{nxt.get('bvid')}（{season.get('title')}）")

    # -------------------------------------------------------------- 语音转写
    async def _transcribe(
        self, bvid: str, cid: int, info: dict, limit_sec: int = 0, force: bool = False
    ) -> str:
        # 整条转写前先听开头一分钟：几乎没人声（纯音乐/BGM/无声演示）就直接放弃，
        # 省下几分钟的转写时间。
        if limit_sec <= 0 and not force and self.config.get("precheck_speech", True):
            probe = await self._transcribe(bvid, cid, info, limit_sec=60)
            if len(re.sub(r"\s+", "", probe or "")) < 15:
                logger.info(f"[bili] 《{info.get('title')}》开头一分钟几乎没有说话，跳过")
                return ""
        dur = int(info.get("duration") or 0)
        cap = int(self.config.get("max_transcribe_minutes", 30) or 30) * 60
        if limit_sec:
            cap = min(cap, limit_sec)
        if dur and dur > cap and not limit_sec:
            logger.info(f"[bili] 视频 {int(dur)} 秒超过转写上限 {cap} 秒，跳过")
            return ""

        hint = self._asr_hint(info)
        if hint and not limit_sec:
            logger.info(f"[bili] 转写偏置词：{hint[:60]}")
        text, err = await self._transcribe_once(bvid, cid, info, limit_sec, hint)
        # 整条转写失败就再试一次（网络抖动/文件没下完整），预探测不重试
        if err and limit_sec <= 0:
            logger.info(f"[bili] 转写失败（{err}），2 秒后重试一次")
            await asyncio.sleep(2)
            text, err = await self._transcribe_once(bvid, cid, info, limit_sec, hint)
            if err:
                logger.warning(f"[bili] 转写重试仍失败：{err}")
        if err:
            return ""
        return text

    async def _transcribe_once(
        self, bvid: str, cid: int, info: dict, limit_sec: int = 0, hint: str = ""
    ):
        """跑一遍「取音轨地址 -> 下载 -> ffmpeg -> whisper」，返回 (文本, 错误原因)。"""
        urls = await self.api.audio_urls(bvid, cid)
        if not urls:
            return "", "拿不到音轨地址"

        tmpdir = Path(str(self.config.get("whisper_cache_dir") or str(Path.home() / ".cache" / "bili_whisper"))) / "_tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        raw = tmpdir / f"{bvid}.m4s"
        wav = tmpdir / f"{bvid}.wav"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }
        errors: list = []
        for url in urls:
            host = url.split("/")[2] if url.count("/") >= 2 else url
            try:
                import httpx

                async with httpx.AsyncClient(headers=headers, timeout=120, follow_redirects=True) as cli:
                    async with cli.stream("GET", url) as resp:
                        resp.raise_for_status()
                        with raw.open("wb") as fp:
                            async for chunk in resp.aiter_bytes(1 << 16):
                                fp.write(chunk)
                if raw.exists() and raw.stat().st_size > 1024:
                    if errors:
                        logger.info(f"[bili] 换用备用音轨 CDN：{host}")
                    errors = []
                    break
                errors.append(f"{host} 文件太小")
            except Exception as exc:
                errors.append(f"{host}: {type(exc).__name__}")
                continue
        if errors:
            logger.warning(f"[bili] 音轨下载全部失败: {'; '.join(str(e) for e in errors[:4])}")
            return "", f"下载音轨失败（{len(errors)} 个 CDN 都不通）"

        ff = str(self.config.get("ffmpeg_path") or "ffmpeg")
        try:
            cmd = [ff, "-y", "-i", str(raw), "-ar", "16000", "-ac", "1"]
            if limit_sec:
                cmd += ["-t", str(int(limit_sec))]
            cmd.append(str(wav))
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=900,
                check=False,
            )
        except Exception as exc:
            logger.warning(f"[bili] ffmpeg 转码失败: {exc!r}")
            return "", f"ffmpeg 转码失败: {exc!r}"
        if not wav.exists() or wav.stat().st_size < 2048:
            logger.warning("[bili] 转码后的音频文件异常")
            return "", "转码后的音频文件异常"

        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(
                None, self._whisper_sync, str(wav), hint
            )
        except Exception as exc:
            logger.warning(f"[bili] 语音转写失败: {exc!r}")
            return "", f"语音转写失败: {exc!r}"
        finally:
            for f in (raw, wav):
                try:
                    f.unlink()
                except Exception:
                    pass
        return text, ""

    def _get_whisper_model(self):
        """懒加载 whisper 模型（进程内共享，可被空闲释放；本机 av 不兼容，直接喂 numpy）。"""
        name = str(self.config.get("whisper_model") or "small")
        cache_dir = str(self.config.get("whisper_cache_dir") or str(Path.home() / ".cache" / "bili_whisper"))
        try:
            threads = int(self.config.get("whisper_threads") or 0)
        except Exception:
            threads = 0
        if threads <= 0:
            threads = min(16, max(2, (os.cpu_count() or 8) // 2))
        dev_cfg = str(self.config.get("whisper_device") or "auto").strip().lower()
        key = (name, cache_dir, threads, dev_cfg)
        model = _WHISPER_CACHE.get("model")
        if model is not None and _WHISPER_CACHE.get("key") == key:
            _WHISPER_CACHE["last_used"] = time.time()
            return model
        try:
            _hf = str(self.config.get("hf_endpoint") or "").strip()
            if _hf:
                os.environ.setdefault("HF_ENDPOINT", _hf)
            from faster_whisper import WhisperModel
        except Exception as exc:
            logger.warning(f"[bili] 没装 faster-whisper：{exc!r}")
            return None
        t0 = time.time()
        # GPU 推理需要的 CUDA 运行库（pip 装的 nvidia-cublas-cu12 / nvidia-cudnn-cu12）
        # 注意：os.add_dll_directory 的返回值必须被持有，否则句柄释放后目录就失效了
        global _DLL_HANDLES
        try:
            import site as _site
            _dirs = []
            for _sp in list(_site.getsitepackages()) + [_site.getusersitepackages()]:
                for _sub in ("cublas/bin", "cudnn/bin"):
                    _p = os.path.join(_sp, "nvidia", *_sub.split("/"))
                    if os.path.isdir(_p) and _p not in _dirs:
                        _dirs.append(_p)
            if _dirs:
                os.environ["PATH"] = os.pathsep.join(_dirs) + os.pathsep + os.environ.get("PATH", "")
                for _p in _dirs:
                    try:
                        _DLL_HANDLES.append(os.add_dll_directory(_p))
                    except Exception:
                        pass
                logger.info(f"[bili] 已挂载 CUDA 运行库目录: {_dirs}")
        except Exception as exc:
            logger.warning(f"[bili] 挂载 CUDA 运行库失败（可忽略，会退回 CPU）: {exc!r}")

        # 优先用 cache_dir 下的本地快照目录（避免每次走 HuggingFace 联网解析）
        _local = Path(cache_dir) / f"faster-whisper-{name}"
        _target = str(_local) if (_local / "model.bin").exists() else name
        cands = []
        if dev_cfg in ("auto", "cuda", "gpu"):
            cands += [("cuda", "float16"), ("cuda", "int8_float16")]
        cands.append(("cpu", "int8"))
        model = None
        last_err = None
        for _dev, _ct in cands:
            try:
                model = WhisperModel(
                    _target, device=_dev, compute_type=_ct, download_root=cache_dir,
                    cpu_threads=threads, num_workers=1,
                )
                logger.info(f"[bili] 转写模型已加载（{name} / {_dev}/{_ct}，{time.time() - t0:.1f} 秒，{_target}）")
                _WHISPER_CACHE["device"] = _dev
                break
            except Exception as exc:
                last_err = exc
                logger.warning(f"[bili] {_dev}/{_ct} 加载失败，换下一个：{exc!r}")
        if model is None:
            logger.warning(f"[bili] 加载 whisper 模型失败: {last_err!r}")
            return None
        _WHISPER_CACHE.update({"model": model, "key": key, "last_used": time.time()})
        return model

    @staticmethod
    def _asr_hint(info: dict) -> str:
        """把标题/UP主/分区/标签拼成转写热词，减少人名番名的同音错字。"""
        parts = []
        title = str(info.get("title") or "").strip()
        owner = str(info.get("owner") or "").strip()
        tname = str(info.get("tname") or "").strip()
        if title:
            parts.append(title)
        if owner:
            parts.append(f"UP主{owner}")
        if tname:
            parts.append(tname)
        tags = info.get("tag") or info.get("tags") or []
        if isinstance(tags, str):
            tags = [x for x in tags.replace("，", ",").split(",") if x]
        for t in list(tags)[:8]:
            t = str(t).strip()
            if t and t not in parts:
                parts.append(t)
        hint = "。".join(parts)
        return hint[:200]

    def _whisper_sync(self, wav_path: str, hint: str = "") -> str:
        try:
            import wave

            import numpy as np

            with wave.open(wav_path, "rb") as wf:
                sr = wf.getframerate()
                ch = wf.getnchannels()
                width = wf.getsampwidth()
                frames = wf.readframes(wf.getnframes())
            if width != 2 or sr != 16000:
                logger.warning(f"[bili] 音频格式不支持: sr={sr} ch={ch} width={width}")
                return ""
            audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            if ch > 1:
                audio = audio.reshape(-1, ch).mean(axis=1)
        except Exception as exc:
            logger.warning(f"[bili] 读取音频失败: {exc!r}")
            return ""
        model = self._get_whisper_model()
        if model is None:
            return ""
        _WHISPER_CACHE["busy"] = int(_WHISPER_CACHE.get("busy") or 0) + 1
        try:
            kw = {"hotwords": hint} if hint else {}
            segments, _ = model.transcribe(
                audio,
                language="zh",
                vad_filter=True,
                beam_size=1,
                initial_prompt=(hint or "以下是普通话的句子。"),
                **kw,
            )
            return "".join((s.text or "") for s in segments).strip()
        except Exception as exc:
            logger.warning(f"[bili] 转写失败: {exc!r}")
            return ""
        finally:
            _WHISPER_CACHE["busy"] = max(0, int(_WHISPER_CACHE.get("busy") or 0) - 1)
            _WHISPER_CACHE["last_used"] = time.time()

    def _interest_hint(self) -> str:
        """把我现在的兴趣池塞进提示词，判 relevant 时别只认「动漫/音乐」两个词。"""
        try:
            pool = self.state.get("interests") or {}
            items = sorted(
                ((str(k), float(v or 0)) for k, v in pool.items()),
                key=lambda kv: kv[1],
                reverse=True,
            )[:14]
        except Exception:
            items = []
        if not items:
            return ""
        words = "、".join(f"{k}" for k, _ in items)
        return (
            "\n\n【我最近在意的方向】" + words +
            "\n判 relevant 时以这个列表为准：视频内容只要跟其中任何一条沾边（哪怕是玩梗、"
            "蹭到了同一个话题），就算相关；确实八竿子打不着才填 false。"
        )

    # ---------------------------------------------------------------- 写笔记
    def build_prompt(self, pack: dict) -> str:
        info = pack["info"]
        subtitle = (pack.get("subtitle") or "").strip()
        if len(subtitle) > 7000:
            head = subtitle[:5000]
            tail = subtitle[-1500:]
            subtitle = f"{head}\n……（中间省略）……\n{tail}"
        danmaku = " / ".join((pack.get("danmaku") or [])[:40])
        comments = "\n".join(
            f"- [{c.get('like', 0)}赞] {c.get('user')}：{c.get('content')}"
            for c in (pack.get("comments") or [])
        )
        hint = ""
        if pack.get("mismatch"):
            hint = (
                "\n\n【重要提醒】这段字幕和视频标题/简介几乎对不上"
                f"（重合度只有 {float(pack.get('subtitle_overlap') or 0):.2f}），"
                "很可能是字幕串台或者是别人上传的错误字幕。"
                "此时请以标题、简介、弹幕、评论为准；如果确认内容跟标题无关，"
                "把 relevant 设为 false，并在 summary 里直说「字幕和内容对不上」。"
            )
        return (
            NOTE_PROMPT.format(
                name=self.persona,
                master=self.master,
                title=info.get("title") or "",
                owner=info.get("owner") or "",
                duration=info.get("duration") or 0,
                desc=(info.get("desc") or "")[:600],
                tags=info.get("tname") or "",
                subtitle=subtitle
                or "（这个视频没有字幕，请主要依据标题、简介、弹幕和评论来写，并说明信息有限）",
                danmaku=danmaku or "（无）",
                comments=comments or "（无）",
            )
            + hint
            + self._interest_hint()
        )

    def write_note(self, pack: dict, note: dict, notes_dir: Path) -> Path:
        # 主人点名分享的视频：一定写完整笔记（不做一行摘要），
        # 但「感不感兴趣」仍由模型按内容判断，不搞一刀切。
        info = pack["info"]
        notes_dir.mkdir(parents=True, exist_ok=True)
        def _pub(d) -> str:
            try:
                ts = int(float((d or {}).get("pubdate") or 0))
            except Exception:
                ts = 0
            if not ts:
                return "—"
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        safe = re.sub(r"[\\/:*?\"<>|]+", "_", (note.get("title") or info.get("title") or "笔记"))[:40]
        year_dir = notes_dir / str(datetime.now().year)
        year_dir.mkdir(parents=True, exist_ok=True)
        path = year_dir / f"{stamp}_{safe}.md"

        if note_is_relevant(note) is False and not pack.get("user_picked"):
            one_line = str(note.get("summary") or "").strip() or "（没细看，跟当前兴趣不搭）"
            tags_short = "、".join(note.get("tags") or []) or "（无）"
            body = [
                f"# {note.get('title') or info.get('title')}",
                "",
                f"> {one_line}",
                "",
                "- UP主："
            + str(info.get("owner") or "—")
            + (f"（UID {info.get('owner_mid')}）" if info.get("owner_mid") else ""),
                f"- 链接：https://www.bilibili.com/video/{info.get('bvid')}",
                f"- 发布：{_pub(info)}",
                f"- 标签：{tags_short}",
                f"- 记录时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "",
                "*备注：跟当前兴趣不搭，只记一句*",
                "",
            ]
            path.write_text("\n".join(body), encoding="utf-8")
            note["short"] = True
            return path

        points = note.get("points") or []
        raw_insights = note.get("insights")
        if isinstance(raw_insights, (list, tuple)):
            insight_text = "；".join(str(x).strip() for x in raw_insights if str(x).strip())
        else:
            insight_text = str(raw_insights or "").strip()
        tags = note.get("tags") or []

        def _dur(v) -> str:
            try:
                s = int(float(v or 0))
            except Exception:
                s = 0
            return f"{s // 60}:{s % 60:02d}" if s else "—"

        def _cnt(v) -> str:
            try:
                n = int(float(v or 0))
            except Exception:
                n = 0
            return f"{n / 10000:.1f} 万" if n >= 10000 else str(n)

        summary_text = str(note.get("summary") or "").strip()
        gist_text = str(note.get("gist") or "").strip()

        lines = [f"# {note.get('title') or info.get('title')}", ""]
        brief = gist_text
        if not brief and summary_text:
            brief = summary_text.split("。")[0].strip()
        if brief:
            lines += [f"> {brief.rstrip('。')}。", ""]
        raw_title = str(info.get("title") or "")
        if raw_title and raw_title != str(note.get("title") or ""):
            lines.append(f"- 视频：{raw_title}")
        lines += [
            "- UP主："
            + str(info.get("owner") or "—")
            + (f"（UID {info.get('owner_mid')}）" if info.get("owner_mid") else ""),
            f"- 链接：https://www.bilibili.com/video/{info.get('bvid')}",
            f"- 发布：{_pub(info)}",
            f"- 数据：时长 {_dur(info.get('duration'))}　播放 {_cnt(info.get('view'))}　点赞 {_cnt(info.get('like'))}",
            f"- 标签：{'、'.join(tags) if tags else '（无）'}",
            f"- 记录时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "---",
            "",
            "## 讲了什么",
            "",
            summary_text or "（空）",
            "",
            "## 主要知识点",
            "",
        ]
        lines += [f"- {p}" for p in points] or ["- （无）"]
        lines += ["", "## 我的感受", "", insight_text or "（无）"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    # ---------------------------------------------------------------- 主流程
    async def run_once(self, provider, umo: str, notes_dir: Path) -> Dict[str, Any]:
        """跑一轮：选题 → 抓内容 → 模型总结 → 落笔记。"""
        if not self.api.logged_in:
            return {"ok": False, "error": "还没登录B站账号"}
        if getattr(self.api, "in_cooldown", False):
            return {"ok": False, "error": self.api.cooldown_text()}

        only_interest = bool(self.config.get("only_interest_videos", False))
        attempts = 3
        last_err = ""

        for _ in range(attempts):
            item, why = await self.pick()
            if not item:
                return {"ok": False, "error": why}

            pack = await self.gather(item)
            if not pack:
                last_err = "抓不到视频详情"
                self._reject(item.get("bvid"))
                continue

            body = str(pack.get("subtitle") or "")
            min_chars = int(self.config.get("min_content_chars", 300) or 0)
            kw = str(item.get("keyword") or "")
            if kw and kw in (self.state.get("explore_words") or []):
                relax = int(self.config.get("explore_min_content_chars", 120) or 0)
                if relax and (not min_chars or relax < min_chars):
                    min_chars = relax
            if min_chars and len(body) < min_chars:
                self._reject(item.get("bvid"))
                t = str((pack.get("info") or {}).get("title") or "")
                last_err = f"《{t}》能读的内容太少（{len(body)} 字），换一个"
                logger.info(f"[bili] 《{t}》只有 {len(body)} 字内容，跳过")
                continue

            prompt = self.build_prompt(pack)
            try:
                resp = await provider.text_chat(
                    prompt=prompt,
                    system_prompt=build_system_prompt(self.persona, self.master),
                    session_id=umo,
                    persist=False,
                )
                raw = getattr(resp, "completion_text", "") or ""
            except Exception as exc:
                logger.warning(f"[bili] 模型总结失败: {exc!r}")
                return {"ok": False, "error": f"模型总结失败：{exc}"}

            note = _json_from_text(raw)
            if not note:
                logger.warning(f"[bili] 笔记 JSON 解析失败，原文前 200 字: {raw[:200]!r}")
                return {"ok": False, "error": "模型输出不是合法 JSON，笔记没生成"}

            if only_interest and note_is_relevant(note) is False:
                title = str((pack.get("info") or {}).get("title") or "")
                logger.info(f"[bili] 《{title}》判为跑题，换一个视频再来")
                self._reject(item.get("bvid"))
                last_err = "连着挑了几个视频都跑题，这轮先不学"
                continue

            path = self.write_note(pack, note, notes_dir)
            if note.get("short"):
                logger.info(f"[bili] 《{pack['info'].get('title')}》跟兴趣不搭，只留了一行摘要")

            seen = list(self.state.setdefault("seen", []))
            seen.append(item["bvid"])
            self.state["seen"] = seen[-2000:]
            self.state["notes_count"] = int(self.state.get("notes_count", 0)) + 1
            self.state["last_run_ts"] = int(time.time())

            return {
                "ok": True,
                "note": note,
                "path": path,
                "pack": pack,
                "bvid": item["bvid"],
                "title": pack["info"].get("title"),
                "owner": pack["info"].get("owner"),
                "prompt_chars": len(prompt),
                "usage": _usage_dict(getattr(resp, "usage", None)),
            }

        return {"ok": False, "error": last_err or "这轮没挑到感兴趣的视频"}

    async def process_bvid(
        self,
        provider,
        bvid: str,
        umo: str,
        notes_dir: Path,
        write_note: bool = False,
        user_picked: bool = True,
    ) -> Dict[str, Any]:
        """按 BV 号单独跑一条：抓内容 + 总结，可选写笔记。不参与选题，不写 seen。"""
        if not self.api.logged_in:
            return {"ok": False, "error": "还没登录B站账号"}
        if getattr(self.api, "in_cooldown", False):
            return {"ok": False, "error": self.api.cooldown_text()}

        pack = await self.gather({"bvid": bvid, "user_picked": bool(user_picked)})
        if not pack:
            return {"ok": False, "error": f"抓不到 {bvid} 的详情（BV 号对吗，或者视频被删了）"}
        # 主人点名要看的视频：不管跟兴趣搭不搭，都按完整笔记处理
        pack["user_picked"] = bool(user_picked)

        prompt = self.build_prompt(pack)
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT,
                session_id=umo,
                persist=False,
            )
            raw = getattr(resp, "completion_text", "") or ""
        except Exception as exc:
            logger.warning(f"[bili] 重跑时模型总结失败: {exc!r}")
            return {"ok": False, "error": f"模型总结失败：{exc}", "pack": pack}

        note = _json_from_text(raw)
        if not note:
            logger.warning(f"[bili] 重跑笔记 JSON 解析失败，原文前 200 字: {raw[:200]!r}")
            return {"ok": False, "error": "模型输出不是合法 JSON", "pack": pack, "raw": raw[:400]}

        path = None
        if write_note:
            path = self.write_note(pack, note, notes_dir)

        info = pack.get("info") or {}
        return {
            "ok": True,
            "note": note,
            "pack": pack,
            "path": path,
            "bvid": bvid,
            "title": info.get("title"),
            "owner": info.get("owner"),
            "duration": info.get("duration"),
            "text_source": pack.get("text_source"),
            "body_chars": len(str(pack.get("subtitle") or "")),
            "usage": _usage_dict(getattr(resp, "usage", None)),
        }

    def _reject(self, bvid: Optional[str]) -> None:
        """把跑题的视频拉黑，下次选题不再挑它。"""
        if not bvid:
            return
        rejected = list(self.state.get("rejected") or [])
        if bvid not in rejected:
            rejected.append(bvid)
        self.state["rejected"] = rejected[-200:]


def whisper_status() -> Dict[str, Any]:
    """返回转写模型当前状态，供日志/命令展示。"""
    return {
        "loaded": _WHISPER_CACHE.get("model") is not None,
        "key": _WHISPER_CACHE.get("key"),
        "idle_seconds": int(time.time() - float(_WHISPER_CACHE.get("last_used") or 0)),
        "busy": int(_WHISPER_CACHE.get("busy") or 0),
    }


def release_whisper_if_idle(idle_minutes: int) -> bool:
    """空闲超过 idle_minutes 分钟就卸载转写模型，返回是否真的释放了。"""
    if int(_WHISPER_CACHE.get("busy") or 0) > 0:
        return False
    model = _WHISPER_CACHE.get("model")
    if model is None:
        return False
    idle = time.time() - float(_WHISPER_CACHE.get("last_used") or 0)
    if idle_minutes <= 0 or idle < idle_minutes * 60:
        return False
    _WHISPER_CACHE.update({"model": None, "key": None})
    del model
    gc.collect()
    return True
