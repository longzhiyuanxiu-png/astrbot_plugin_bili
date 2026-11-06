"""astrbot_plugin_bili — 让她用自己的 B站小号自主学习。
第一阶段（P1）：插件骨架、后台配置页、扫码登录、基础状态管理。
后续阶段在同一个插件里扩展：选题 → 看 → 记 → 说 → 点赞收藏。
"""
from __future__ import annotations
import asyncio
import inspect
import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
import qrcode
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
try:
    from astrbot.api.web import request as web_request
except Exception:  # 老版本没有插件 Web 请求代理
    web_request = None
try:
    from .bili_api import BiliAPI
    from .learner import Learner, release_whisper_if_idle, whisper_status
except ImportError:  # pragma: no cover - 兼容非包方式加载
    from bili_api import BiliAPI
    from learner import Learner
PLUGIN_NAME = "astrbot_plugin_bili"
DESC = (
    "📺 让她用自己的B站小号自主学习：自己选题、看视频、写笔记、主动分享收获。"
    "支持点赞/收藏（限额）、弹幕热评输入、知识入库、语音汇报。默认干跑模式，所有限制都能在后台配置页调整。"
)
DEFAULT_ADMIN = ""  # 留空则只认配置里的 admin_ids 或 AstrBot 管理员
DEFAULT_NOTES_DIR = str(Path(__file__).parent / "notes")
_CMD_RE = re.compile(r"^[/／]?\s*(?:bili|b站)\s*", re.I)

class _FlatConfig(dict):
    """配置文件按分组嵌套存放，插件内部照旧当一层字典用。
    - 读取：self.config.get("runs_per_day") 直接取到分组里的值；
    - 写入：自动写回对应分组，再 save_config() 就落盘；
    - dict(self.config) 得到的是一层扁平字典（给 Learner 用）。
    """

    def __init__(self, raw: dict):
        super().__init__()
        self.raw = raw
        self.group_of: Dict[str, str] = {}
        for gk, gv in raw.items():
            if isinstance(gv, dict):
                for k, v in gv.items():
                    self.group_of[k] = gk
                    dict.__setitem__(self, k, v)
        for k, v in raw.items():
            if not isinstance(v, dict):
                self.group_of.setdefault(k, "")
                dict.setdefault(self, k, v)

    def __setitem__(self, key, value):  # type: ignore[override]
        dict.__setitem__(self, key, value)
        gk = self.group_of.get(key)
        if gk is None:
            return
        if gk:
            self.raw.setdefault(gk, {})[key] = value
        else:
            dict.__setitem__(self.raw, key, value)

    def save_config(self):
        return self.raw.save_config()

@register(PLUGIN_NAME, "CHENHUIJIE", DESC, "0.1.0", "")
class BiliLearningPlugin(Star):
    """B站自主学习插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._raw_config = config
        self.config = _FlatConfig(config)
        self.notes_dir = Path(self.config.get("notes_dir") or DEFAULT_NOTES_DIR)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_path = Path(str(self.config.get("cookie_path") or "").strip()
                               or (Path(__file__).parent / "bili-cookie.json"))
        self.state_path = self.notes_dir / "state.json"
        self.log_path = self.notes_dir / "activity.log"
        self.qr_path = self.notes_dir / "login_qr.png"
        self.api = BiliAPI(self.cookie_path)
        if self.api.load():
            logger.info(f"[bili] 已载入本地登录态：{self.api.uname or '未知'} ({self.api.mid or '-'})")
        self.state = self._load_state()
        # 实例标记：只有最新启动的实例才有权写 state / 跑后台循环
        self._instance_id = uuid.uuid4().hex[:8]
        self._superseded = False
        self.state["_owner"] = self._instance_id
        self.state["_heartbeat"] = time.time()
        # 兴趣算法迁移：首次启动把旧的浮点权重转成 hits/tries/misses（饱和曲线）
        try:
            self._migrate_interest_stats(self.state.setdefault("interests", {}))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili] 兴趣算法迁移失败：{exc!r}")
        self._save_state(force=True)  # 让 state.json 里的兴趣池和配置保持一致
        self._poll_tasks: set[asyncio.Task] = set()
        # 三个分享开关：B站私信 / QQ汇报 / 语音（兼容旧键 bili_dm_enabled、voice_report）
        migrated = False
        if "share_dm" not in self.config:
            self.config["share_dm"] = bool(self.config.get("bili_dm_enabled", True))
            migrated = True
        if "share_voice" not in self.config:
            self.config["share_voice"] = bool(self.config.get("voice_report", True))
            migrated = True
        if "share_qq" not in self.config:
            self.config["share_qq"] = True
            migrated = True
        if migrated:
            try:
                self.config.save_config()
            except Exception:
                pass
        logger.info(
            f"[bili] 初始化完成: notes={self.notes_dir} "
            f"登录={'是' if self.api.logged_in else '否'} "
            f"干跑={bool(self.config.get('dry_run', True))}"
        )
        self._busy = False
        self._loop_task: Optional[asyncio.Task] = None
        if config.get("enabled", True):
            self._loop_task = asyncio.create_task(self._schedule_loop())
        self._whisper_task: Optional[asyncio.Task] = None
        try:
            self._whisper_task = asyncio.create_task(self._whisper_idle_loop())
        except RuntimeError:
            self._whisper_task = None
        self._report_task: Optional[asyncio.Task] = None
        try:
            self._report_task = asyncio.create_task(self._report_loop())
        except RuntimeError:
            self._report_task = None
        self._notes_sig: Optional[tuple] = None
        self._notes_task: Optional[asyncio.Task] = None
        try:
            self._notes_task = asyncio.create_task(self._notes_watch_loop())
        except RuntimeError:
            self._notes_task = None
        try:
            self._register_panel()
        except Exception as exc:  # 老版本没有插件 Web 接口也不该让插件起不来
            logger.warning(f"[bili] 页面接口注册失败: {exc!r}")
        self._panel_web_runner = None
        self._panel_web_task: Optional[asyncio.Task] = None
        if config.get("panel_web_enabled", True):
            try:
                self._panel_web_task = asyncio.create_task(self._start_panel_web())
            except RuntimeError:
                self._panel_web_task = None

    # -------------------------------------------------------------- 独立面板（脱离 AstrBot 后台）
    async def _panel_web_page(self, request):
        from aiohttp import web

        html_path = Path(__file__).parent / "webui" / "panel" / "index.html"
        try:
            html = html_path.read_text(encoding="utf-8")
        except Exception as exc:
            return web.Response(text=f"读不到页面文件：{exc}", status=500)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _panel_dispatch(self, ep: str = "", **kwargs) -> dict:
        """AstrBot 插件页面的兜底分发：/{plugin}/<ep> -> _panel_<ep>。"""
        name = str(ep or "").strip()
        if not name or name.startswith("_") or "/" in name:
            return {"ok": False, "error": f"未知接口: {name}", "data": {}}
        handler = getattr(self, f"_panel_{name}", None)
        if not callable(handler):
            return {"ok": False, "error": f"未知接口: {name}", "data": {}}
        payload: Dict[str, Any] = {}
        try:
            from astrbot.api.web import request as _req

            if str(getattr(_req, "method", "") or "").upper() == "POST":
                data = await _req.json({})
                if isinstance(data, dict):
                    payload = data
        except Exception as exc:
            logger.debug(f"[bili] 读取请求体失败: {exc!r}")
        try:
            out = handler(payload=payload)
            if inspect.isawaitable(out):
                out = await out
            return out if isinstance(out, dict) else {"ok": True, "data": out}
        except TypeError:
            out = handler()
            if inspect.isawaitable(out):
                out = await out
            return out if isinstance(out, dict) else {"ok": True, "data": out}
        except Exception as exc:
            logger.error(f"[bili] 接口 {name} 出错: {exc!r}")
            return {"ok": False, "error": str(exc), "data": {}}

    async def _panel_web_dispatch(self, request):
        from aiohttp import web

        ep = str(request.match_info.get("ep") or "").strip()
        handler = getattr(self, f"_panel_{ep}", None)
        if not handler or ep.startswith("web_"):
            return web.json_response({"status": "error", "message": "未知接口", "data": {}}, status=404)
        try:
            kwargs = dict(request.query)
            if request.method == "POST":
                try:
                    body = await request.json()
                except Exception:
                    body = None
                if isinstance(body, dict):
                    kwargs["payload"] = body
            result = await handler(**kwargs)
        except Exception as exc:
            logger.warning(f"[bili] 独立面板接口 {ep} 出错: {exc!r}")
            result = {"ok": False, "error": str(exc), "data": {}}
        return web.json_response(result if isinstance(result, dict) else {"ok": True, "data": result})

    async def _start_panel_web(self) -> None:
        try:
            from aiohttp import web
        except Exception as exc:
            logger.warning(f"[bili] 没有 aiohttp，独立面板没起来: {exc!r}")
            return
        host = "127.0.0.1"
        try:
            port = int(self.config.get("panel_web_port", 4123) or 4123)
        except Exception:
            port = 4123
        app = web.Application()
        app.router.add_get("/", self._panel_web_page)
        app.router.add_get("/api/{ep}", self._panel_web_dispatch)
        app.router.add_post("/api/{ep}", self._panel_web_dispatch)
        try:
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()
            self._panel_web_runner = runner
            logger.info(f"[bili] 独立面板已启动：http://{host}:{port}")
        except Exception as exc:
            logger.warning(f"[bili] 独立面板启动失败（端口 {port}）：{exc!r}")

    async def _stop_panel_web(self) -> None:
        runner = getattr(self, "_panel_web_runner", None)
        self._panel_web_runner = None
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                pass

    # ------------------------------------------------------------------ 后台页面接口

    def _panel_interest_rows(self) -> List[Dict[str, Any]]:
        """兴趣池：种子 + 拓展，按权重倒序。"""
        rows: List[Dict[str, Any]] = []
        try:
            seeds: Dict[str, float] = {}
            for word, weight in self._parse_interest_list(
                self.config.get("interest_topics")
            ):
                seeds[str(word)] = float(weight) if weight is not None else 2.0
            pool = dict(self.state.get("interests") or {})
            for word, weight in pool.items():
                rows.append(
                    {
                        "word": word,
                        "weight": round(float(weight or 0), 2),
                        "seed": word in seeds,
                    }
                )
            for word, weight in seeds.items():
                if not any(r["word"] == word for r in rows):
                    rows.append(
                        {"word": word, "weight": round(float(weight or 0), 2), "seed": True}
                    )
            rows.sort(key=lambda r: (-r["weight"], r["word"]))
        except Exception as exc:
            logger.warning(f"[bili] 页面读取兴趣失败: {exc!r}")
        return rows

    def _panel_next_run(self) -> str:
        try:
            plan = self.state.get("plan") or {}
            now = time.time()
            upcoming = [
                float(v)
                for day in plan.values()
                if isinstance(day, dict)
                for k, v in day.items()
                if re.fullmatch(r"w\d+", str(k))
                and isinstance(v, (int, float))
                and float(v) > now
            ]
            if not upcoming:
                return ""
            return datetime.fromtimestamp(min(upcoming)).strftime("%H:%M")
        except Exception:
            return ""

    def _panel_switches(self) -> Dict[str, Any]:
        """后台页面所有开关的统一快照（唯一事实来源）。"""
        return {
            "share_dm": bool(self.config.get("share_dm", True)),
            "share_qq": bool(self.config.get("share_qq", True)),
            "share_voice": bool(self.config.get("share_voice", True)),
            "dry_run": bool(self.config.get("dry_run", True)),
            "transcribe_first": bool(self.config.get("transcribe_first", False)),
            "only_interest_videos": bool(
                self.config.get("only_interest_videos", False)
            ),
            "paused": bool(self.state.get("paused")),
            "report_mode": "instant"
            if str(self.config.get("report_mode", "daily")) == "instant"
            else "daily",
        }

    async def _panel_summary(self, **kwargs) -> dict:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            usage = self._usage_today()
            plan = ((self.state.get("plan") or {}).get(today) or {})
            if not isinstance(plan, dict):
                plan = {}
            dm_raw = ((self.state.get("dm") or {}).get(today) or 0)
            dm_count = (
                dm_raw
                if isinstance(dm_raw, int)
                else int((dm_raw or {}).get("count", 0) or 0)
            )
            return {
                "ok": True,
                "data": {
                    "bot_name": self._bot_name(),
                    "panel_title": str(self.config.get("panel_title") or "") or self._bot_name(),
                    "panel_subtitle": str(self.config.get("panel_subtitle") or "") or "B站学习面板",
                    "today": today,
                    "notes_today": len(self._today_notes()),
                    "notes_total": len(
                        [p for p in self._notes_files() if p.name != "index.md"]
                    ),
                    "videos_today": max(int(usage.get("videos", 0) or 0), len(self._today_notes())),
                    "tokens_today": int(usage.get("tokens", 0) or 0),
                    "runs_today": int(plan.get("runs", 0) or 0),
                    "runs_per_day": int(self.config.get("runs_per_day", 0) or 0),
                    "last_round": self.state.get("last_round") or {},
                    "dm_today": dm_count,
                    "dm_limit": int(self.config.get("bili_dm_daily_limit", 0) or 0),
                    "busy": bool(self._busy),
                    "paused": bool(self.state.get("paused")),
                    "logged_in": bool(self.api.logged_in),
                    "cooldown": (
                        self.api.cooldown_text()
                        if getattr(self.api, "in_cooldown", False)
                        else ""
                    ),
                    "next_run": self._panel_next_run(),
                    "share": {
                        "dm": bool(self.config.get("share_dm", True)),
                        "qq": bool(self.config.get("share_qq", True)),
                        "voice": bool(self.config.get("share_voice", True)),
                    },
                    "flags": {
                        "dry_run": bool(self.config.get("dry_run", False)),
                        "transcribe_first": bool(
                            self.config.get("transcribe_first", False)
                        ),
                        "only_interest_videos": bool(
                            self.config.get("only_interest_videos", False)
                        ),
                        "report_mode": str(self.config.get("report_mode", "daily")),
                    },
                    "switches": self._panel_switches(),
                    "interests": self._panel_interest_rows(),
                },
            }
        except Exception as exc:
            logger.warning(f"[bili] 页面读取概览失败: {exc!r}")
            return {"ok": False, "error": str(exc), "data": {}}

    # ---------- 余额 / 今日花费（面板气泡用） ----------
    _PRICE_FLASH = {
        # 元 / 百万 token；[谷价, 峰价]
        "hit": (0.02, 0.04),
        "miss": (1.0, 2.0),
        "out": (4.0, 8.0),
    }

    def _is_peak_now(self) -> bool:
        """工作日 9:00-12:00、14:00-18:00 为高峰（北京时间）。"""
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        hour = now.hour
        return (9 <= hour < 12) or (14 <= hour < 18)

    def _peak_info(self) -> dict:
        """峰谷状态 + 距离下一次切换还有多久。"""
        now = datetime.now()
        weekday = now.weekday()
        today = now.date()
        # 今天所有切换点（高峰开始/结束）
        marks = []
        base = datetime.combine(today, datetime.min.time())
        for h in (9, 12, 14, 18):
            marks.append(base + timedelta(hours=h))
        nxt = None
        for m in marks:
            if m > now:
                nxt = m
                break
        if nxt is None:
            # 今天没有切换点了，明天 9 点（周末顺延到周一）
            days_ahead = 1
            while (weekday + days_ahead) % 7 >= 5:
                days_ahead += 1
            nxt = base + timedelta(days=days_ahead, hours=9)
        secs = int((nxt - now).total_seconds())
        hrs, mins = secs // 3600, (secs % 3600) // 60
        if hrs >= 1:
            left = f"{hrs} 小时 {mins} 分"
        else:
            left = f"{mins} 分钟"
        peak = self._is_peak_now()
        if peak:
            text = f"高峰还剩 {left}"
        else:
            # 谷价期间，判断下一个是高峰开始还是只是跨天
            text = f"{left}后进高峰" if nxt.hour in (9, 14) else f"{left}后进新的一天"
            if weekday >= 4 and nxt.hour == 9:
                text = f"{left}后进下周高峰"
        return {
            "peak_now": peak,
            "peak_text": "高峰时段" if peak else "谷价时段",
            "peak_left_secs": secs,
            "peak_left_text": text,
        }

    def _cost_estimate(self, tokens: int) -> float:
        """按混合单价估算花费：假设 输入:输出 = 3:1、缓存命中率 50%。"""
        if tokens <= 0:
            return 0.0
        i = 1 if self._is_peak_now() else 0
        hit, miss, out = (self._PRICE_FLASH[k][i] for k in ("hit", "miss", "out"))
        blended = 0.75 * (0.5 * hit + 0.5 * miss) + 0.25 * out
        return round(tokens / 1_000_000 * blended, 4)

    async def _fetch_deepseek_balance(self) -> dict:
        """读本机 key 调余额接口，60 秒缓存；失败沿用上次成功值。"""
        now = time.time()
        cache = getattr(self, "_bal_cache", None)
        if cache and now - cache.get("ts", 0) < 60:
            return dict(cache["data"], stale=False)

        key_path = Path(str(self.config.get("deepseek_key_path") or "").strip()
                       or (Path(__file__).parent / "deepseek-key.txt"))
        try:
            key = key_path.read_text(encoding="utf-8-sig").strip().strip('"').strip()
        except Exception as exc:
            logger.warning(f"[bili] 读不到 DeepSeek key: {exc!r}")
            key = ""

        if key:
            try:
                import httpx as _httpx

                async with _httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(
                        "https://api.deepseek.com/user/balance",
                        headers={"Authorization": f"Bearer {key}"},
                    )
                    data = r.json()
                info = (data.get("balance_infos") or [{}])[0]
                payload = {
                    "ok": True,
                    "balance": float(info.get("total_balance") or 0),
                    "granted": float(info.get("granted_balance") or 0),
                    "topped_up": float(info.get("topped_up_balance") or 0),
                    "currency": info.get("currency") or "CNY",
                    "updated_at": now,
                }
                self._bal_cache = {"ts": now, "data": payload}
                return dict(payload, stale=False)
            except Exception as exc:
                logger.warning(f"[bili] 余额接口失败: {exc!r}")

        if cache:
            return dict(cache["data"], stale=True)
        return {"ok": False, "balance": None, "currency": "CNY", "updated_at": now}

    async def _panel_balance(self, **kwargs) -> dict:
        bal = await self._fetch_deepseek_balance()
        try:
            today = self._today()
            usage = (self.state.get("usage") or {}).get(today, {}) or {}
            tokens = int(usage.get("tokens") or 0)
            videos = int(usage.get("videos") or 0)
            notes = int(usage.get("notes") or len(self._today_notes()))
        except Exception:
            tokens, videos, notes = 0, 0, 0

        peak = self._is_peak_now()
        cost_today = self._cost_estimate(tokens)
        return {
            "ok": True,
            "data": {
                **bal,
                "tokens_today": tokens,
                "cost_today": cost_today,
                "videos_today": videos,
                "notes_today": notes,
                **self._peak_info(),
            },
        }

    async def _panel_history(self, **kwargs) -> dict:
        """近 N 天趋势：视频数、笔记数、token。"""
        try:
            try:
                days = int(kwargs.get("days", 7) or 7)
            except Exception:
                days = 7
            days = max(1, min(30, days))
            usage = self.state.get("usage") or {}
            per_day: Dict[str, int] = {}
            for f in self._notes_files():
                if f.name == "index.md":
                    continue
                try:
                    d = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
                except Exception:
                    continue
                per_day[d] = per_day.get(d, 0) + 1
            out = []
            now = datetime.now()
            for i in range(days - 1, -1, -1):
                d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
                u = usage.get(d) or {}
                if not isinstance(u, dict):
                    u = {}
                out.append(
                    {
                        "date": d,
                        "videos": int(u.get("videos", 0) or 0),
                        "tokens": int(u.get("tokens", 0) or 0),
                        "notes": int(per_day.get(d, 0)),
                    }
                )
            return {"ok": True, "data": {"days": out}}
        except Exception as exc:
            logger.warning(f"[bili] 页面读取趋势失败: {exc!r}")
            return {"ok": False, "error": str(exc), "data": {"days": []}}

    def _panel_note_meta(self, path: Path) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "name": path.name,
            "title": path.stem,
            "mtime": 0,
            "size": 0,
            "tags": "",
            "pub": "",
            "short": False,
        }
        try:
            meta["mtime"] = int(path.stat().st_mtime)
            meta["size"] = int(path.stat().st_size)
            head = path.read_text(encoding="utf-8", errors="ignore")[:1500]
            for line in head.splitlines():
                if line.startswith("# ") and meta["title"] == path.stem:
                    meta["title"] = line[2:].strip()
                elif line.startswith("- 发布") and not meta["pub"]:
                    meta["pub"] = re.split(r"[：:]", line, maxsplit=1)[-1].strip()
                elif line.startswith("- 标签") and not meta["tags"]:
                    meta["tags"] = re.split(r"[：:]", line, maxsplit=1)[-1].strip()
            meta["short"] = "跟当前兴趣不搭" in head
        except Exception:
            pass
        return meta

    async def _panel_notes(self, **kwargs) -> dict:
        try:
            files = [
                p
                for p in sorted(
                    self._notes_files(), key=lambda p: p.stat().st_mtime, reverse=True
                )
                if p.name != "index.md"
            ]
            items = [self._panel_note_meta(p) for p in files[:60]]
            archive_dir = self.notes_dir / "notes" / "archive"
            archived = (
                len(list(archive_dir.rglob("*.md"))) if archive_dir.exists() else 0
            )
            return {
                "ok": True,
                "data": {"items": items, "total": len(files), "archived": archived},
            }
        except Exception as exc:
            logger.warning(f"[bili] 页面读取笔记失败: {exc!r}")
            return {"ok": False, "error": str(exc), "data": {"items": []}}

    async def _panel_note(self, **kwargs) -> dict:
        try:
            name = str(kwargs.get("name") or "")
            if not name and web_request is not None:
                name = str(web_request.query.get("name") or "")
            safe = Path(name).name
            if not safe.endswith(".md") or safe != name:
                return {"ok": False, "error": "文件名不合法", "data": {}}
            path = self.notes_dir / "notes" / safe
            if not path.exists():
                return {"ok": False, "error": "找不到这篇笔记", "data": {}}
            return {
                "ok": True,
                "data": {"name": safe, "content": path.read_text(encoding="utf-8")},
            }
        except Exception as exc:
            logger.warning(f"[bili] 页面读取笔记内容失败: {exc!r}")
            return {"ok": False, "error": str(exc), "data": {}}

    async def _panel_note_delete(
        self, payload=None, name: str = "", with_memory: bool = False, **kwargs
    ) -> dict:
        """删除笔记：移进 notes/trash/（可恢复）；with_memory=True 时一并删掉她长期记忆里这条。"""
        import shutil as _shutil

        pl = payload if isinstance(payload, dict) else {}
        name = str(pl.get("name") or name or "").strip()
        if pl.get("with_memory") is not None:
            with_memory = bool(pl.get("with_memory"))
        safe = Path(name).name
        if not safe.endswith(".md") or safe != name or safe == "index.md":
            return {"ok": False, "error": "文件名不合法", "data": {}}
        src = self.notes_dir / "notes" / safe
        if not src.exists():
            return {"ok": False, "error": "找不到这篇笔记", "data": {}}
        trash = self.notes_dir / "notes" / "trash"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = trash / f"{stamp}_{safe}"
        try:
            _shutil.move(str(src), str(dst))
        except Exception as exc:
            logger.warning(f"[bili] 删除笔记失败: {exc!r}")
            return {"ok": False, "error": f"删除失败：{exc}", "data": {}}
        try:
            self._rebuild_index()
        except Exception as exc:
            logger.warning(f"[bili] 删除后重建索引失败: {exc!r}")
        logger.info(f"[bili] 笔记已移到回收站: {safe} -> trash/{dst.name}")
        removed = 0
        if with_memory:
            try:
                _txt = dst.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                _txt = ""
            _m = re.search(r"BV[0-9A-Za-z]{10}", _txt)
            if _m:
                removed = await self._drop_memory_by_bvid(_m.group(0))
        return {
            "ok": True,
            "data": {"name": safe, "trash": dst.name, "memory_removed": removed},
        }

    async def _drop_memory_by_bvid(self, bvid: str) -> int:
        """删掉某 BV 对应的长期记忆，返回删除条数。

        走 livingmemory 的公开检索接口（search_memories + delete_memory），
        不直连它的 sqlite —— 这样插件改名/搬家也不会断。
        """
        bvid = (bvid or "").strip()
        if not bvid:
            return 0
        eng = self._memory_engine()
        if eng is None:
            logger.info("[bili] 没有可用的记忆引擎，跳过删记忆")
            return 0
        try:
            results = await eng.search_memories(bvid, k=10)
        except Exception as exc:
            logger.warning(f"[bili] 记忆检索失败，跳过删记忆: {exc!r}")
            return 0
        ids = []
        for r in results or []:
            body = str(getattr(r, "content", "") or "") + json.dumps(
                getattr(r, "metadata", {}) or {}, ensure_ascii=False, default=str
            )
            if bvid in body:
                try:
                    ids.append(int(getattr(r, "doc_id")))
                except Exception:
                    continue
        removed = 0
        for mid in dict.fromkeys(ids):
            try:
                if await eng.delete_memory(mid):
                    removed += 1
            except Exception as exc:
                logger.warning(f"[bili] 删除记忆 {mid} 失败: {exc!r}")
        return removed

    async def _panel_note_delete(
        self, payload=None, name: str = "", with_memory: bool = False, **kwargs
    ) -> dict:
        """删除笔记：移进 notes/trash/（可恢复）；with_memory=True 时一并删掉她长期记忆里这条。"""
        import shutil as _shutil

        pl = payload if isinstance(payload, dict) else {}
        name = str(pl.get("name") or name or "").strip()
        if pl.get("with_memory") is not None:
            with_memory = bool(pl.get("with_memory"))
        safe = Path(name).name
        if not safe.endswith(".md") or safe != name or safe == "index.md":
            return {"ok": False, "error": "文件名不合法", "data": {}}
        src = self.notes_dir / "notes" / safe
        if not src.exists():
            return {"ok": False, "error": "找不到这篇笔记", "data": {}}
        trash = self.notes_dir / "notes" / "trash"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = trash / f"{stamp}_{safe}"
        try:
            _shutil.move(str(src), str(dst))
        except Exception as exc:
            logger.warning(f"[bili] 删除笔记失败: {exc!r}")
            return {"ok": False, "error": f"删除失败：{exc}", "data": {}}
        try:
            self._rebuild_index()
        except Exception as exc:
            logger.warning(f"[bili] 删除后重建索引失败: {exc!r}")
        logger.info(f"[bili] 笔记已移到回收站: {safe} -> trash/{dst.name}")
        removed = 0
        if with_memory:
            try:
                _txt = dst.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                _txt = ""
            _m = re.search(r"BV[0-9A-Za-z]{10}", _txt)
            if _m:
                removed = await self._drop_memory_by_bvid(_m.group(0))
        return {
            "ok": True,
            "data": {"name": safe, "trash": dst.name, "memory_removed": removed},
        }

    async def _drop_memory_by_bvid(self, bvid: str) -> int:
        """按 BV 号删掉她长期记忆里对应的条目，返回删除条数。"""
        import sqlite3 as _sqlite3

        try:
            db = (
                Path(__file__).resolve().parents[2]
                / "plugin_data"
                / "astrbot_plugin_livingmemory"
                / "livingmemory.db"
            )
            if not db.exists():
                return 0
            con = _sqlite3.connect(str(db))
            try:
                ids = [
                    int(r[0])
                    for r in con.execute(
                        "select id from documents where metadata like ?", (f"%{bvid}%",)
                    ).fetchall()
                ]
            finally:
                con.close()
            if not ids:
                return 0
            eng = self._memory_engine()
            if eng is None:
                logger.warning("[bili] 记忆引擎不可用，跳过删记忆")
                return 0
            n = 0
            for mid in ids:
                try:
                    if await eng.delete_memory(mid):
                        n += 1
                except Exception as exc:
                    logger.warning(f"[bili] 删除记忆 {mid} 失败: {exc!r}")
            logger.info(f"[bili] 已删除 {bvid} 的记忆 {n}/{len(ids)} 条")
            return n
        except Exception as exc:
            logger.warning(f"[bili] 删记忆出错: {exc!r}")
            return 0

    async def _panel_interest_save(
        self,
        payload: Optional[Dict[str, Any]] = None,
        kind: str = "seed",
        items: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> dict:
        """整表保存兴趣：kind=seed→interest_topics，kind=extra→extra_interests。"""
        pl = payload if isinstance(payload, dict) else {}
        kind = str(pl.get("kind") or kind or "seed")
        if pl.get("items") is not None:
            items = pl.get("items")
        key = "interest_topics" if str(kind) == "seed" else "extra_interests"
        rows: List[str] = []
        for it in items or []:
            if isinstance(it, str):
                word, weight = it, None
            elif isinstance(it, dict):
                word = str(it.get("word") or "").strip()
                weight = it.get("weight")
            else:
                continue
            if not word:
                continue
            try:
                weight = round(float(weight), 2) if weight is not None else None
            except (TypeError, ValueError):
                weight = None
            rows.append(f"{word} {weight:.2f}" if weight is not None else word)
        try:
            self.config[key] = rows
            self.config.save_config()
            pool = dict(self.state.get("interests") or {})
            for name, _w in self._parse_interest_list(self.config.get(key)):
                pool.setdefault(name, 1.0)
            allowed = {n for n, _ in self._parse_interest_list(self.config.get("interest_topics"))}
            allowed |= {n for n, _ in self._parse_interest_list(self.config.get("extra_interests"))}
            self.state["interests"] = self._merge_interests(
                {k: v for k, v in pool.items() if k in allowed}
            )
            self._save_state()
            logger.info(f"[bili] 兴趣已保存（{key}）：{rows}")
        except Exception as exc:
            logger.warning(f"[bili] 保存兴趣失败: {exc!r}")
            return {"ok": False, "error": str(exc), "data": {}}
        return {"ok": True, "data": {"items": self._panel_interest_rows(), "kind": kind}}

    async def _panel_interests(self, **kwargs) -> dict:
        return {"ok": True, "data": {"items": self._panel_interest_rows()}}

    def _panel_schema_data(self) -> Dict[str, Any]:
        """把 _conf_schema.json 拍平成前端可渲染的设置项（带当前值）。"""
        groups: List[Dict[str, Any]] = []
        try:
            raw = json.loads(
                (Path(__file__).parent / "_conf_schema.json").read_text(
                    encoding="utf-8-sig"
                )
            )
        except Exception as exc:
            logger.warning(f"[bili] 读配置 schema 失败: {exc!r}")
            return {"groups": [], "runtime": []}
        secret_hints = (
            "key",
            "token",
            "cookie",
            "secret",
            "sessdata",
            "bili_jct",
            "buvid",
            "password",
        )
        reload_prefix = ("panel_web", "whisper_model", "whisper_device")
        for gk, gv in raw.items():
            if not isinstance(gv, dict):
                continue
            items: List[Dict[str, Any]] = []
            for k, meta in (gv.get("items") or {}).items():
                if not isinstance(meta, dict):
                    continue
                t = str(meta.get("type") or "string").lower()
                try:
                    val = self.config.get(k, meta.get("default"))
                except Exception:
                    val = meta.get("default")
                opts = meta.get("options") or meta.get("enum") or []
                if isinstance(opts, dict):
                    opts = list(opts.keys())
                items.append(
                    {
                        "key": k,
                        "label": str(meta.get("description") or k),
                        "type": t,
                        "hint": str(meta.get("hint") or ""),
                        "value": val,
                        "options": opts if isinstance(opts, list) else [],
                        "secret": any(h in k.lower() for h in secret_hints),
                        "reload": any(k.startswith(p) for p in reload_prefix),
                    }
                )
            if items:
                groups.append(
                    {
                        "key": gk,
                        "title": str(gv.get("description") or gk),
                        "items": items,
                    }
                )
        required_keys = [
            "bot_name",
            "memory_persona",
            "cookie_path",
            "bili_dm_target",
            "deepseek_key_path",
        ]
        picked = []
        for g in groups:
            keep = []
            for it in g["items"]:
                if it["key"] in required_keys:
                    picked.append(it)
                else:
                    keep.append(it)
            g["items"] = keep
        groups = [g for g in groups if g["items"]]
        picked.sort(key=lambda x: required_keys.index(x["key"]) if x["key"] in required_keys else 99)
        for it in picked:
            it["reload"] = False
        if picked:
            groups.insert(0, {
                "key": "required",
                "title": "必填（首次使用先填这里）",
                "items": picked,
            })

        runtime = [
            {
                "key": "paused",
                "label": "暂停定时学习",
                "type": "bool",
                "hint": "只停定时任务；你手动喂视频照样能学",
                "value": bool(self.state.get("paused")),
                "options": [],
                "secret": False,
                "reload": False,
            }
        ]
        return {"groups": groups, "runtime": runtime}

    async def _panel_today_watched(self, **kwargs) -> dict:
        """今天看过的视频（从 activity.log 里刮），给概览右栏用。"""
        import datetime as _dt
        import re as _re

        nd = str(self.config.get("notes_dir") or DEFAULT_NOTES_DIR)
        log = Path(nd) / "activity.log"
        today = _dt.date.today().strftime("%Y-%m-%d")
        items, seen = [], set()
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            lines = []
        pat = _re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\]\s*(.+)$")

        def _title_of(body: str) -> str:
            # 标题里可能本身带《》，所以用「固定后缀 + 贪婪」而不是找第一个 》
            for rx in (
                r"看完《(.+)》并写了笔记",
                r"看完《(.+)》，笔记",
                r"分享了视频《(.+)》",
                r"^《(.+)》跟兴趣不搭",
            ):
                mm = _re.search(rx, body)
                if mm:
                    return mm.group(1).strip()
            return ""

        # 先扫一遍标记「跟兴趣不搭」的标题
        bored = set()
        for line in lines:
            m = pat.match(line.strip())
            if not m or m.group(1) != today:
                continue
            if "跟兴趣不搭" in m.group(3):
                t = _title_of(m.group(3))
                if t:
                    bored.add(t)

        for line in lines:
            m = pat.match(line.strip())
            if not m or m.group(1) != today:
                continue
            body = m.group(3)
            if ("看完" not in body) and ("并写了笔记" not in body):
                continue
            title = _title_of(body)
            if not title or title in seen:
                continue
            seen.add(title)
            nm = ""
            mm = _re.search(r"([^\\/\s]+\.md)", body)
            if mm:
                nm = mm.group(1)
            tags = []
            if nm and title not in bored:
                try:
                    txt = (Path(nd) / "notes" / nm).read_text(encoding="utf-8", errors="replace")
                    mt = _re.search(r"^-\s*标签[：:]\s*(.+)$", txt, _re.M)
                    if mt:
                        raw = mt.group(1).strip()
                        if raw and raw not in ("（无）", "(无)", "无"):
                            tags = [x.strip() for x in _re.split(r"[、,，;；]", raw) if x.strip()][:4]
                except Exception:
                    tags = []
            items.append(
                {
                    "time": m.group(2)[:5],
                    "title": title,
                    "note": bool(nm),
                    "name": nm,
                    "bored": title in bored,
                    "tags": tags,
                }
            )
        items.reverse()
        return {
            "ok": True,
            "data": {"date": today, "items": items[:20], "count": len(items)},
        }

    async def _panel_schema(self, **kwargs) -> dict:
        return {"ok": True, "data": self._panel_schema_data()}

    async def _panel_config_set(self, **kwargs) -> dict:
        """POST {"key": "...", "value": ...}：改任意一项配置并落盘。"""
        payload = kwargs.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        key = str(payload.get("key") or "").strip()
        value = payload.get("value")
        if not key:
            return {"ok": False, "error": "缺少 key", "data": {}}
        try:
            if key == "paused":
                self.state["paused"] = bool(value)
                self._save_state()
                return {"ok": True, "data": {"key": key, "value": bool(value)}}
            meta: Dict[str, Any] = {}
            try:
                raw = json.loads(
                    (Path(__file__).parent / "_conf_schema.json").read_text(
                        encoding="utf-8-sig"
                    )
                )
                for gv in raw.values():
                    if isinstance(gv, dict) and isinstance(gv.get("items"), dict):
                        if key in gv["items"]:
                            meta = gv["items"][key] or {}
                            break
            except Exception:
                meta = {}
            t = str(meta.get("type") or "").lower()
            cur = self.config.get(key)
            if t in ("bool", "boolean") or (not t and isinstance(cur, bool)):
                val: Any = bool(value)
            elif t in ("int", "integer") or (not t and isinstance(cur, int)):
                val = int(float(value))
            elif t in ("float", "double", "number"):
                val = float(value)
            elif t in ("list", "array") or isinstance(cur, list):
                if isinstance(value, list):
                    val = [str(x).strip() for x in value if str(x).strip()]
                else:
                    val = [
                        x.strip()
                        for x in re.split(r"[,，、\n\t]+", str(value or ""))
                        if x.strip()
                    ]
            else:
                val = "" if value is None else str(value)
            opts = meta.get("options") or meta.get("enum") or []
            if isinstance(opts, dict):
                opts = list(opts.keys())
            if isinstance(opts, list) and opts:
                if str(val) not in [str(o) for o in opts]:
                    return {
                        "ok": False,
                        "error": f"只能填：{', '.join(str(o) for o in opts)}",
                        "data": {},
                    }
            self.config[key] = val
            self.config.save_config()
            logger.info(f"[bili] 面板改设置 {key} = {val!r}")
            return {"ok": True, "data": {"key": key, "value": val}}
        except Exception as exc:
            logger.warning(f"[bili] 面板改设置失败 {key}: {exc!r}")
            return {"ok": False, "error": str(exc), "data": {}}

    async def _panel_toggle(self, **kwargs) -> dict:
        """POST {"key": "share_dm" | "paused" | ..., "value": true}"""
        payload: Dict[str, Any] = {}
        provided = kwargs.get("payload")
        if isinstance(provided, dict) and provided:
            payload = provided
        try:
            if not payload and web_request is not None:
                raw = await web_request.json({})
                if isinstance(raw, dict):
                    payload = raw
        except Exception:
            payload = {}
        key = str(payload.get("key") or "")
        value = payload.get("value")
        allowed = {
            "share_dm",
            "share_qq",
            "share_voice",
            "dry_run",
            "transcribe_first",
            "only_interest_videos",
        }
        try:
            if key == "paused":
                self.state["paused"] = bool(value)
                self._save_state()
            elif key == "report_mode":
                self.config["report_mode"] = (
                    "instant" if str(value).lower() in ("instant", "即时") else "daily"
                )
                self.config.save_config()
            elif key in allowed:
                self.config[key] = bool(value)
                self.config.save_config()
            else:
                return {"ok": False, "error": f"不认识的开关: {key}", "data": {}}
            return {
                "ok": True,
                "data": {
                    "key": key,
                    "value": value,
                    "switches": self._panel_switches(),
                },
            }
        except Exception as exc:
            logger.warning(f"[bili] 页面改开关失败: {exc!r}")
            return {"ok": False, "error": str(exc), "data": {}}

    async def _panel_run(self, **kwargs) -> dict:
        if self._busy:
            return {"ok": False, "error": "这一轮还没跑完，等一会儿再点", "data": {}}
        if not self.api.logged_in:
            return {"ok": False, "error": "小号还没登录，先在 QQ 里发 /bili 登录", "data": {}}
        try:
            asyncio.create_task(self._auto_learn())
        except Exception as exc:
            return {"ok": False, "error": str(exc), "data": {}}
        self._log("后台页面点了「立刻学一轮」")
        return {"ok": True, "data": {"started": True}}

    async def _panel_report(self, **kwargs) -> dict:
        """从后台页面发一条日报（可以带自定义文本）。"""
        try:
            payload: Dict[str, Any] = {}
            provided = kwargs.get("payload")
            if isinstance(provided, dict) and provided:
                payload = provided
            elif web_request is not None:
                try:
                    payload = await web_request.json({}) or {}
                except Exception:
                    payload = {}
            text = str((payload or {}).get("text") or "").strip()
            if not text:
                text = self._build_daily_summary()
            if not text:
                return {"ok": False, "error": "今天还没有可汇总的内容", "data": {}}
            umo = str(self.state.get("share_umo") or "").strip()
            if not umo:
                return {"ok": False, "error": "还不知道往哪个会话发（先在 QQ 里发一次 /bili）", "data": {}}
            await self._notify(umo, text)
            card = self._build_daily_card()
            if card:
                await self._notify_image(umo, card)
            self.state["daily_report_date"] = self._today()
            self._save_state()
            self._log("从后台页面发了一次日报")
            return {"ok": True, "data": {"sent": True, "chars": len(text)}}
        except Exception as exc:
            logger.warning(f"[bili] 页面发日报失败: {exc!r}")
            return {"ok": False, "error": str(exc), "data": {}}

    def _register_panel(self) -> None:
        routes = []
        for prefix in (f"/{PLUGIN_NAME}",):
            routes.extend(
                [
                    (f"{prefix}/summary", self._panel_summary, ["GET", "POST"], "B站学习：今日概览"),
                    (f"{prefix}/notes", self._panel_notes, ["GET", "POST"], "B站学习：笔记列表"),
                    (f"{prefix}/note", self._panel_note, ["GET", "POST"], "B站学习：单篇笔记"),
                    (f"{prefix}/interests", self._panel_interests, ["GET", "POST"], "B站学习：兴趣权重"),
                    (f"{prefix}/toggle", self._panel_toggle, ["POST"], "B站学习：切换开关"),
                    (f"{prefix}/run", self._panel_run, ["POST"], "B站学习：立即学一轮"),
                    (f"{prefix}/report", self._panel_report, ["POST"], "B站学习：发一条日报"),
                ]
            )
        # 兜底：/{plugin}/{ep} 全量分发，覆盖后加的面板接口
        for prefix in (f"/{PLUGIN_NAME}",):
            routes.append(
                (
                    f"{prefix}/<ep>",
                    self._panel_dispatch,
                    ["GET", "POST"],
                    "B站学习：面板接口（分发）",
                )
            )
        for route, handler, methods, desc in routes:
            self.context.register_web_api(route, handler, methods, desc)
        logger.info(f"[bili] 后台页面接口已注册（{len(routes)} 条）")

    # ------------------------------------------------------------------ 状态存取

    def _load_state(self) -> Dict[str, Any]:
        default = {
            "paused": False,
            "interests": {},
            "usage": {},
            "last_run_ts": 0,
            "notes_count": 0,
            "actions": {},
            "seen": [],
        }
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    default.update(data)
        except Exception as exc:
            logger.warning(f"[bili] 读取 state.json 失败: {exc!r}")
        default["interests"] = self._merge_interests(default.get("interests") or {})
        return default

    # ------------------------------------------------------- 兴趣：种子 + 拓展

    @staticmethod
    def _parse_interest_list(raw) -> List[Tuple[str, Optional[float]]]:
        """把 ["动漫", "装机 1.3"] 这种配置解析成 [(名字, 权重或 None)]。"""
        if isinstance(raw, str):
            raw = re.split(r"[,，、\n]+", raw)
        out: List[Tuple[str, Optional[float]]] = []
        for it in raw or []:
            s = str(it).strip()
            if not s:
                continue
            m = re.match(r"^(.*?)[\s:：]+(-?[0-9]+(?:\.[0-9]+)?)$", s)
            if m and m.group(1).strip():
                out.append((m.group(1).strip(), float(m.group(2))))
            else:
                out.append((s, None))
        return out

    def _merge_interests(self, current: Dict[str, float]) -> Dict[str, float]:
        """兴趣池 = 配置里的种子 + 配置里的拓展，配置说了算。"""
        merged: Dict[str, float] = {}
        for name, w in self._parse_interest_list(self.config.get("interest_topics")):
            merged[name] = max(float(w if w is not None else 2.0), 2.0)
        for name, w in self._parse_interest_list(self.config.get("extra_interests")):
            base = float(w if w is not None else current.get(name, 1.0))
            merged[name] = max(merged.get(name, 0.0), base)
        return merged

    def _save_interests_to_config(self) -> None:
        """把当前权重写回配置，方便在插件配置页直接看到和修改。"""
        inter = self.state.get("interests") or {}
        seed_names = [
            n for n, _ in self._parse_interest_list(self.config.get("interest_topics"))
        ]
        seed_items = [f"{n} {float(inter.get(n, 2.0)):.2f}" for n in seed_names]
        extra_items = [
            f"{k} {v:.2f}"
            for k, v in sorted(inter.items(), key=lambda kv: -kv[1])
            if k not in seed_names
        ]
        changed = False
        try:
            if seed_names and list(self.config.get("interest_topics") or []) != seed_items:
                self.config["interest_topics"] = seed_items
                changed = True
            if list(self.config.get("extra_interests") or []) != extra_items:
                self.config["extra_interests"] = extra_items
                changed = True
            if changed:
                self.config.save_config()
        except Exception as exc:
            logger.warning(f"[bili] 写回兴趣权重失败: {exc!r}")

    def _disk_owner(self) -> str:
        """读一下磁盘上 state.json 现在的属主是谁。"""
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return str(data.get("_owner") or "")
        except Exception:
            pass
        return ""

    def _check_owner(self) -> bool:
        """自己还是不是当前实例；被新实例顶替了就把自己标记为退役。"""
        if self._superseded:
            return False
        owner = self._disk_owner()
        if owner and owner != self._instance_id:
            self._superseded = True
            logger.info(
                f"[bili] 检测到更新的插件实例（{owner}），本实例（{self._instance_id}）停止写状态"
            )
            return False
        return True

    def _save_state(self, force: bool = False) -> None:
        if not force and not self._check_owner():
            return
        try:
            self.state["_owner"] = self._instance_id
            self.state["_heartbeat"] = time.time()
            self._merge_usage_from_disk()
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.state_path)  # 原子替换，避免读到写了一半的文件
        except Exception as exc:
            logger.warning(f"[bili] 写入 state.json 失败: {exc!r}")

    def _merge_usage_from_disk(self) -> None:
        """写盘前先合并磁盘上的今日计数：插件重载/多实例时避免把新数据覆盖掉。"""
        try:
            if not self.state_path.exists():
                return
            disk = json.loads(self.state_path.read_text(encoding="utf-8"))
            duse = disk.get("usage") or {}
            if not isinstance(duse, dict):
                return
            muse = self.state.setdefault("usage", {})
            for day, dv in duse.items():
                if not isinstance(dv, dict):
                    continue
                mv = muse.get(day)
                if not isinstance(mv, dict):
                    muse[day] = dict(dv)
                    continue
                for k in ("videos", "tokens", "notes"):
                    try:
                        mv[k] = max(int(mv.get(k, 0) or 0), int(dv.get(k, 0) or 0))
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning(f"[bili] 合并 usage 失败: {exc!r}")

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _usage_today(self) -> Dict[str, int]:
        usage = self.state.setdefault("usage", {})
        return usage.setdefault(self._today(), {"tokens": 0, "videos": 0})

    def _log(self, msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
        try:
            with self.log_path.open("a", encoding="utf-8") as fp:
                fp.write(line)
        except Exception:
            pass
        logger.info(f"[bili] {msg}")

    # -------------------------------------------------------------------- 小工具

    def _notes_signature(self) -> tuple:
        """笔记目录的指纹：文件数量 + 最新修改时间，用来判断有没有人手改过。"""
        notes = self.notes_dir / "notes"
        if not notes.exists():
            return (0, 0)
        files = [p for p in notes.glob("*.md") if p.name != "index.md"]
        if not files:
            return (0, 0)
        newest = max(int(p.stat().st_mtime) for p in files)
        return (len(files), newest)

    async def _notes_watch_loop(self) -> None:
        """每 10 分钟看一眼笔记目录：手动删/改过就重建索引页。"""
        while True:
            try:
                await asyncio.sleep(600)
                if self._superseded:
                    return
                sig = self._notes_signature()
                if sig == getattr(self, "_notes_sig", None):
                    continue
                count = self._rebuild_index()
                self._notes_sig = self._notes_signature()
                logger.info(f"[bili] 笔记有变化，索引已重建（{count} 篇）")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[bili] 索引自检出错: {exc!r}")

    async def _whisper_idle_loop(self) -> None:
        """每分钟检查一次：转写模型闲置太久就卸载，省内存。"""
        while True:
            try:
                await asyncio.sleep(60)
                if self._superseded:
                    return
                minutes = int(self.config.get("whisper_idle_minutes", 10) or 0)
                if minutes <= 0:
                    continue
                if release_whisper_if_idle(minutes):
                    logger.info(f"[bili] 转写模型空闲 {minutes} 分钟，已释放（{whisper_status()}）")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[bili] 转写模型释放检查出错: {exc!r}")

    def _bot_name(self) -> str:
        return str(self.config.get("bot_name") or "小助手").strip() or "小助手"

    def _memory_persona(self) -> str:
        return str(self.config.get("memory_persona") or "").strip() or "default"

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admins = self.config.get("admin_ids") or [DEFAULT_ADMIN]
        if isinstance(admins, str):
            admins = [admins]
        ids = {str(x).strip() for x in admins if str(x).strip()}
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
        if sender and sender in ids:
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    @staticmethod
    def _split_sub(text: str) -> tuple[str, str]:
        body = _CMD_RE.sub("", (text or "").strip(), count=1).strip()
        if not body:
            return "", ""
        parts = body.split(None, 1)
        return parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
    async def _notify(self, session: str, text: str) -> None:
        try:
            await self.context.send_message(session, MessageChain(chain=[Plain(text)]))
        except Exception as exc:
            logger.warning(f"[bili] 主动推送失败: {exc!r}")

    # ------------------------------------------------------------------ 命令入口

    @filter.command("bili", alias={"b站", "B站"})
    async def bili_cmd(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        sub, arg = self._split_sub(event.message_str)
        # 记住最近说话的会话，定时学习结束后往这里汇报
        if event.unified_msg_origin:
            self.state["share_umo"] = event.unified_msg_origin
            self._save_state()
        if not sub or sub in ("帮助", "help", "?"):
            yield event.plain_result(self._help_text())
            return
        if sub in ("登录", "login", "扫码"):
            async for r in self._cmd_login(event):
                yield r
            return
        if sub in ("退出登录", "logout"):
            async for r in self._cmd_logout(event):
                yield r
            return
        if sub in ("状态", "status"):
            async for r in self._cmd_status(event):
                yield r
            return
        # 以下命令只允许管理员
        if not self._is_admin(event):
            yield event.plain_result("这个只有主人能操作哦～")
            return
        if sub in ("暂停", "pause"):
            self.state["paused"] = True
            self._save_state()
            self._log("手动暂停自主学习")
            yield event.plain_result("好，我先把自动刷停掉。想让我继续自己刷的时候，发 /bili 继续。")
            return
        if sub in ("继续", "resume"):
            self.state["paused"] = False
            self._save_state()
            self._log("恢复自主学习")
            yield event.plain_result("恢复啦，到了时间段我就自己去看看。")
            return
        if sub in ("兴趣", "interest"):
            yield event.plain_result(self._cmd_interest(arg))
            return
        if sub in ("权重", "weight"):
            if not self._is_admin(event):
                yield event.plain_result("改权重只有主人能改。")
                return
            yield event.plain_result(self._cmd_weight(arg))
            return
        if sub in ("只看兴趣", "兴趣视频", "onlyinterest"):
            if not self._is_admin(event):
                yield event.plain_result("这个开关只有主人能改。")
                return
            yield event.plain_result(self._cmd_toggle_interest(arg))
            return
        if sub in ("分享", "share"):
            if not self._is_admin(event):
                yield event.plain_result("这个开关只有主人能改。")
                return
            yield event.plain_result(self._cmd_toggle_share(arg))
            return
        if sub in ("日志", "log"):
            yield event.plain_result(self._cmd_log(arg))
            return
        if sub in ("学", "learn", "看", "现在看一个"):
            async for r in self._cmd_learn(event, arg):
                yield r
            return
        if sub in ("日报", "daily", "今天看了啥"):
            async for r in self._cmd_daily(event, arg):
                yield r
            return
        if sub in ("全部", "all", "全部笔记", "今天全部", "看全部"):
            async for r in self._cmd_daily(event, "全部"):
                yield r
            return
        if sub in ("模式", "汇报模式", "reportmode"):
            yield event.plain_result(self._cmd_report_mode(arg))
            return
        if sub in ("重跑", "replay", "再来一遍"):
            async for r in self._cmd_replay(event, arg):
                yield r
            return
        if sub in ("报告", "report"):
            await self._send_daily(event, full=True)
            return
        if sub in ("笔记", "notes", "note", "找笔记"):
            yield event.plain_result(self._cmd_notes(arg))
            return
        if sub in ("周报", "weekly"):
            yield event.plain_result(self._cmd_weekly())
            return
        if sub in ("复习", "review", "考考我"):
            async for r in self._cmd_review(event):
                yield r
            return
        if sub in ("私信", "dm"):
            async for r in self._cmd_dm(event, arg):
                yield r
            return
        if sub in ("语音测试", "voicetest", "测试语音"):
            if not self._is_admin(event):
                yield event.plain_result("只有主人能让我测语音。")
                return
            tts = self.context.get_using_tts_provider(event.unified_msg_origin)
            if tts is None:
                yield event.plain_result("❌ 当前没有启用任何语音合成提供商，语音汇报用不了。")
                return
            ok = await self._voice_report(
                "这是一条语音汇报测试，如果你听到这句话，说明语音汇报已经能用了。",
                event.unified_msg_origin,
            )
            yield event.plain_result(
                f"语音合成提供商：{getattr(tts, 'provider_config', {}).get('id', '未知')}\n"
                f"发送结果：{'✅ 已发出' if ok else '❌ 没发出去（看日志里的“语音汇报失败”）'}"
            )
            return
        if sub in ("索引", "index", "目录"):
            yield event.plain_result(self._cmd_index())
            return
        yield event.plain_result(f"「{sub}」这个我不太懂，发 /bili 帮助 我给你列一下能做的。")

    # -------------------------------------------------------------------- 登录
    async def _cmd_login(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        if self.api.logged_in:
            info = await self.api.nav()
            if info.get("is_login"):
                yield event.plain_result(
                    f"已经登录着啦：{info.get('uname')}（UID {info.get('mid')}）。\n"
                    f"要换号先发 /bili 退出登录"
                )
                return
            logger.info("[bili] 本地 cookie 已失效，重新扫码")
            self.api.clear()
        try:
            qr = await self.api.qr_generate()
        except Exception as exc:
            logger.warning(f"[bili] 取二维码失败: {exc!r}")
            yield event.plain_result(f"取二维码失败了：{exc}")
            return
        try:
            img = qrcode.make(qr["url"])
            img.save(self.qr_path)
        except Exception as exc:
            logger.warning(f"[bili] 生成二维码图片失败: {exc!r}")
            yield event.plain_result(f"二维码图片生成失败：{exc}")
            return
        self._log("发起扫码登录")
        yield event.chain_result(
            [
                Plain("用B站 App 扫这个码登录小号（3 分钟内有效）："),
                Image.fromFileSystem(str(self.qr_path)),
            ]
        )
        task = asyncio.create_task(self._poll_qr(qr["qrcode_key"], event.unified_msg_origin))
        self._poll_tasks.add(task)
        task.add_done_callback(self._poll_tasks.discard)
    async def _poll_qr(self, qrcode_key: str, session: str) -> None:
        deadline = time.time() + 185
        notified_scan = False
        while time.time() < deadline:
            await asyncio.sleep(2)
            try:
                result = await self.api.qr_poll(qrcode_key)
            except Exception as exc:
                logger.debug(f"[bili] 轮询扫码状态出错: {exc!r}")
                continue
            code = result.get("code")
            if code == 0:
                info = await self.api.login_with_cookies(result.get("cookies") or {})
                if info.get("is_login"):
                    self._log(f"登录成功：{info.get('uname')} ({info.get('mid')})")
                    await self._notify(
                        session,
                        f"登录成功：{info.get('uname')}（UID {info.get('mid')}）\n"
                        f"以后我就用这个号看视频啦。",
                    )
                else:
                    self._log(f"扫码后校验失败: {info.get('message')}")
                    await self._notify(
                        session, f"扫码成功了但校验没通过：{info.get('message') or '未知原因'}"
                    )
                return
            if code == 86090 and not notified_scan:
                notified_scan = True
                await self._notify(session, "扫到了，等你在手机上点「确认登录」～")
            elif code == 86038:
                self._log("二维码过期")
                await self._notify(session, "二维码过期了，要登录就再发一次 /bili 登录")
                return
        self._log("扫码超时")
        await self._notify(session, "等了三分钟没扫上，要登录就再发一次 /bili 登录")
    async def _cmd_logout(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        if not self._is_admin(event):
            yield event.plain_result("这个只有主人能操作哦～")
            return
        self.api.clear()
        self._log("已清除本地登录态")
        yield event.plain_result("好，本地登录态清掉了，重新 /bili 登录 可以换号。")

    # -------------------------------------------------------------------- 状态
    async def _cmd_status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        lines = ["📺 B站学习状态"]
        if self.api.logged_in:
            info = await self.api.nav()
            if info.get("is_login"):
                lines.append(f"账号：{info.get('uname')}（UID {info.get('mid')}）")
            else:
                lines.append(f"账号：登录态失效（{info.get('message') or '需要重新扫码'}）")
        else:
            lines.append("账号：未登录，发 /bili 登录")
        enabled = bool(self.config.get("enabled", True))
        paused = bool(self.state.get("paused"))
        if not enabled:
            lines.append("自动学习：已关闭（配置里的总开关）")
        elif paused:
            lines.append("自动学习：已暂停（/bili 继续 恢复）")
        else:
            windows = self.config.get("time_windows", "15:00-16:30,20:30-22:00")
            lines.append(
                f"自动学习：开 · 每天 {self.config.get('runs_per_day', 2)} 次 · {windows}"
            )
        usage = self._usage_today()
        budget = int(self.config.get("daily_token_budget", 150000) or 0)
        budget_text = f"/{budget}" if budget else "（不限）"
        lines.append(f"今天：{usage.get('videos', 0)} 个视频 · {usage.get('tokens', 0)} tokens{budget_text}")
        lines.append(f"干跑模式：{'开（不会真的点赞）' if self.config.get('dry_run', True) else '关（会真的点赞/收藏）'}")
        lines.append(
            "只看兴趣视频："
            + (
                "开（跑题的直接跳过，不学也不分享）"
                if self.config.get("only_interest_videos", False)
                else "关（挑到什么学什么）"
            )
        )
        lines.append(
            "分享：B站私信"
            + ("开" if self.config.get("share_dm", True) else "关")
            + " · QQ汇报"
            + ("开" if self.config.get("share_qq", True) else "关")
            + " · 语音"
            + ("开" if self.config.get("share_voice", True) else "关")
        )
        lines.append("兴趣：" + "、".join(self.state.get("interests", {}).keys()))
        lines.append(f"笔记：{self.state.get('notes_count', 0)} 篇 · {self.notes_dir}")
        yield event.plain_result("\n".join(lines))

    # -------------------------------------------------------------------- 兴趣

    def _cmd_interest(self, arg: str) -> str:
        interests: Dict[str, float] = self.state.setdefault("interests", {})
        arg = (arg or "").strip()
        seeds = [n for n, _ in self._parse_interest_list(self.config.get("interest_topics"))]
        if not arg:
            seed_txt = (
                "、".join(f"{s}({float(interests.get(s, 2.0)):.1f})" for s in seeds) or "（空）"
            )
            ext = [
                (k, v)
                for k, v in sorted(interests.items(), key=lambda kv: -kv[1])
                if k not in seeds
            ]
            ext_txt = "、".join(f"{k}({v:.1f})" for k, v in ext) or "（还没长出来）"
            return (
                "现在的兴趣：\n"
                f"种子：{seed_txt}\n"
                f"拓展：{ext_txt}\n"
                "加：/bili 兴趣 +动画制作   删：/bili 兴趣 -猎奇\n"
                "种子在配置页 interest_topics 里改；拓展也能直接改配置里的 extra_interests"
            )
        sign = arg[0]
        topic = arg[1:].strip()
        if sign in "+＋加" and topic:
            interests[topic] = round(float(interests.get(topic, 1.0)) + 0.5, 2)
            self._save_state()
            self._save_interests_to_config()
            self._log(f"兴趣 +{topic}")
            return f"加上了：{topic}（权重 {interests[topic]}）"
        if sign in "-－减" and topic:
            if topic in interests:
                interests.pop(topic)
                self._save_state()
                self._save_interests_to_config()
                self._log(f"兴趣 -{topic}")
                return f"去掉了：{topic}"
            return f"本来就没有「{topic}」"
        return "用法：/bili 兴趣 +动画制作 或 /bili 兴趣 -猎奇"

    def _cmd_weight(self, arg: str) -> str:
        """/bili 权重 动漫 2.6 —— 直接调某个兴趣的权重。"""
        parts = (arg or "").strip().split()
        if len(parts) != 2:
            return (
                "用法：/bili 权重 动漫 2.6\n"
                "数字范围 0.5–5.0；写 删 或 0 就是去掉这个兴趣。"
            )
        name, val = parts[0], parts[1]
        interests: Dict[str, float] = self.state.setdefault("interests", {})
        if val in ("删", "删掉", "0", "0.0"):
            if name in interests:
                interests.pop(name, None)
                self._save_state()
                self._save_interests_to_config()
                self._log(f"兴趣去掉：{name}")
                return f"去掉了：{name}"
            return f"本来就没有「{name}」"
        try:
            w = max(0.5, min(5.0, float(val)))
        except Exception:
            return "权重得是数字，比如 2.6"
        interests[name] = round(w, 2)
        self._save_state()
        self._save_interests_to_config()
        self._log(f"权重 {name}={w:.2f}")
        return f"✅ {name} 的权重改成 {w:.2f}"

    def _cmd_toggle_interest(self, arg: str) -> str:
        """只看兴趣视频的总开关。"""
        cur = bool(self.config.get("only_interest_videos", False))
        arg = (arg or "").strip().lower()
        if arg in ("开", "on", "打开", "启用", "true", "1"):
            self.config["only_interest_videos"] = True
            self.config.save_config()
            self._log("只看兴趣视频：开")
            return "✅ 只看兴趣视频：开。以后挑到跑题的直接跳过，不写笔记、不进记忆、不互动也不私信，最多连着换 3 个。"
        if arg in ("关", "off", "关闭", "false", "0"):
            self.config["only_interest_videos"] = False
            self.config.save_config()
            self._log("只看兴趣视频：关")
            return "✅ 只看兴趣视频：关。以后挑到什么就学什么，跑题的也会写笔记。"
        return f"现在：{'开' if cur else '关'}。切换：/bili 只看兴趣 开 或 /bili 只看兴趣 关"

    def _cmd_toggle_share(self, arg: str) -> str:
        """三个分享开关：B站私信 / QQ汇报 / 语音，各管各的。"""
        keys = {
            "私信": ("share_dm",),
            "汇报": ("share_qq",),
            "语音": ("share_voice",),
        }
        names = {"私信": "B站私信", "汇报": "QQ汇报", "语音": "语音"}
        alias = {
            "私信": "私信", "dm": "私信", "b站": "私信",
            "汇报": "汇报", "qq": "汇报", "文字": "汇报", "qq汇报": "汇报",
            "语音": "语音", "voice": "语音",
        }
        cur = {k: bool(self.config.get(v[0], True)) for k, v in keys.items()}
        arg = (arg or "").strip().lower()
        if not arg:
            body = "、".join(f"{names[k]}{'开' if cur[k] else '关'}" for k in keys)
            return (
                f"现在：{body}\n"
                "切换：/bili 分享 私信 关 · /bili 分享 汇报 关 · /bili 分享 语音 关\n"
                "也可以：/bili 分享 全开 或 /bili 分享 全关"
            )
        if arg in ("全开", "全关", "都开", "都关"):
            on = arg.endswith("开")
            for k in keys:
                for key in keys[k]:
                    self.config[key] = on
            self.config.save_config()
            self._log(f"分享开关：{'全开' if on else '全关'}")
            return f"✅ B站私信、QQ汇报、语音 都{'开' if on else '关'}了。"
        parts = re.split(r"\s+", arg)
        ch = alias.get(parts[0])
        if not ch:
            return "用法：/bili 分享 私信 开|关 · /bili 分享 汇报 开|关 · /bili 分享 语音 开|关"
        state_txt = parts[1] if len(parts) > 1 else ""
        if state_txt not in ("开", "关", "on", "off", "打开", "关闭", "true", "false", "1", "0"):
            return (
                f"{names[ch]}现在是{'开' if cur[ch] else '关'}。"
                f"切换：/bili 分享 {parts[0]} {'关' if cur[ch] else '开'}"
            )
        on = state_txt in ("开", "on", "打开", "true", "1")
        for key in keys[ch]:
            self.config[key] = on
        self.config.save_config()
        self._log(f"{names[ch]}分享：{'开' if on else '关'}")
        return f"✅ {names[ch]}：{'开' if on else '关'}"

    # -------------------------------------------------------------------- 日志

    def _cmd_log(self, arg: str) -> str:
        n = 10
        try:
            n = max(1, min(50, int(arg))) if arg.strip() else 10
        except Exception:
            n = 10
        if not self.log_path.exists():
            return "还没有活动记录。"
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            return f"读日志失败：{exc}"
        return "\n".join(lines[-n:]) or "还没有活动记录。"

    # -------------------------------------------------------------------- 学习
    async def _cmd_learn(
        self, event: AstrMessageEvent, arg: str
    ) -> AsyncGenerator[MessageEventResult, None]:
        """手动跑一轮：选题 → 看 → 写笔记。"""
        if not self.api.logged_in:
            yield event.plain_result("我还没登录 B站，得先扫个码，发 /bili 登录 我就把二维码发出来。")
            return
        if self._busy:
            yield event.plain_result(
                "我这轮还没看完呢（正在筛选、转写），等这一轮结束再喊我，大概一两分钟。"
            )
            return
        if self.state.get("paused") and not arg:
            yield event.plain_result("我现在被你按了暂停。要是想让我看点什么，直接说就行，说清楚是哪期/哪个 up 就更好了。")
            return
        provider = None
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
        except Exception as exc:
            logger.warning(f"[bili] 取模型失败: {exc!r}")
        if provider is None:
            yield event.plain_result("没找到能用的模型，检查一下默认模型配置。")
            return
        self._busy = True
        try:
            async for item in self._cmd_learn_inner(event, arg, provider):
                yield item
        finally:
            self._busy = False

    async def _cmd_learn_inner(
        self, event: AstrMessageEvent, arg: str, provider
    ) -> AsyncGenerator[MessageEventResult, None]:
        keyword = (arg or "").strip()
        notes_path = self.notes_dir / "notes"
        learner = Learner(self.api, dict(self.config), self.state)
        bv = re.search(r"(BV[0-9A-Za-z]{10})", keyword, re.I)
        if bv:
            yield event.plain_result(f"好，去看你给的这集：{bv.group(1)}")
        elif keyword:
            self.state.setdefault("interests", {})[keyword] = round(
                float(self.state.get("interests", {}).get(keyword, 1.0)) + 0.2, 2
            )
            self._save_state()
            learner.state["interests"] = {
                keyword: 5.0,
                **{k: v for k, v in self.state.get("interests", {}).items() if k != keyword},
            }
            yield event.plain_result(f"好，这次专门去看「{keyword}」，挑个视频…")
        else:
            yield event.plain_result("我去B站逛逛，挑个视频看看…")
        if bv:
            result = await learner.process_bvid(
                provider, bv.group(1), event.unified_msg_origin, notes_path,
                write_note=True, user_picked=True,
            )
        else:
            result = await learner.run_once(provider, event.unified_msg_origin, notes_path)
        self._save_state()
        if not result.get("ok"):
            self._log(f"学习失败: {result.get('error')}")
            yield event.plain_result(f"这轮没学成：{result.get('error')}")
            return
        note = result["note"]
        usage = self._usage_today()
        usage["videos"] = int(usage.get("videos", 0)) + 1
        raw_usage = result.get("usage") or {}
        if isinstance(raw_usage, dict):
            usage["tokens"] = int(usage.get("tokens", 0)) + int(
                (raw_usage.get("total_tokens") or 0)
                or ((raw_usage.get("prompt_tokens") or 0) + (raw_usage.get("completion_tokens") or 0))
            )
        self._save_state()
        self._log(f"看完《{result.get('title')}》并写了笔记 {result.get('path')}")
        memory_ok = await self._push_memory(result, event.unified_msg_origin)
        self._drift_interests(note)
        self._archive_notes()
        self._rebuild_index()
        worth = self._worth_actions(
            (result.get("pack") or {}).get("info") or {}, result.get("note") or {}
        )
        acts = await self._do_actions(result, worth)
        dm_line = await self._share_video_dm(result, worth)
        lines = [
            f"看完啦：《{result.get('title')}》",
            f"UP主：{result.get('owner')}",
            "",
            note.get("share_text") or note.get("summary") or "",
            "",
            f"笔记：{Path(result['path']).name}",
        ]
        if acts:
            lines.append(f"顺手{'、'.join(acts)}了。")
        if dm_line:
            lines.append(dm_line)
        if memory_ok:
            lines.append("已经记进脑子里了，以后能想起来。")
        yield event.plain_result("\n".join([x for x in lines if x is not None]))
        if self.config.get("share_voice", False):
            await self._voice_report(note.get("share_text") or note.get("summary") or "", event.unified_msg_origin)
    async def _push_memory(self, result: Dict[str, Any], umo: str) -> bool:
        """把笔记写进 livingmemory 的长期记忆（插件没装就跳过）。"""
        if not self.config.get("memory_write", True):
            return False
        try:
            from astrbot.core.star.star import star_map
            meta = next(
                (m for m in star_map.values() if (m.name or "") == "astrbot_plugin_livingmemory"),
                None,
            )
            engine = getattr(getattr(meta.star_cls, "initializer", None), "memory_engine", None)
            if engine is None:
                logger.info("[bili] 没找到 livingmemory 的记忆引擎，跳过入库")
                return False
            note = result["note"]
            per = int(self.config.get("memory_per_note", 1) or 1)
            parts = [f"我看了B站视频《{result.get('title')}》（UP主 {result.get('owner')}）。"]
            parts.append(note.get("summary") or "")
            points = (note.get("points") or [])[:3]
            if points:
                parts.append("要点：" + "；".join(points))
            raw_ins = note.get("insights")
            if isinstance(raw_ins, (list, tuple)):
                ins = "；".join(str(x).strip() for x in raw_ins if str(x).strip())
            else:
                ins = str(raw_ins or "").strip()
            if ins:
                parts.append("我的感受：" + ins[:160])
            content = "\n".join([p for p in parts if p]).strip()
            content = content[:1200]
            last_exc = None
            for attempt in range(3):
                try:
                    await engine.add_memory(
                        content=content,
                        session_id=umo,
                        persona_id=self._memory_persona(),
                        importance=0.6,
                        metadata={"source": "bili_learning", "bvid": result.get("bvid"), "kind": "note"},
                    )
                    last_exc = None
                    break
                except Exception as exc:  # 网络/接口抖动：退避重试
                    last_exc = exc
                    if attempt < 2:
                        wait = (2, 5)[attempt]
                        logger.warning(
                            f"[bili] 写记忆失败，{wait}s 后重试（{attempt + 1}/3）: {exc!r}"
                        )
                        await asyncio.sleep(wait)
            if last_exc is not None:
                raise last_exc
            logger.info(f"[bili] 笔记已写入长期记忆（{per} 段）")
            return True
        except Exception as exc:
            logger.warning(f"[bili] 写入长期记忆失败: {exc!r}")
            return False

    # ------------------------------------------------------------ 补录记忆
    async def _panel_backfill_pubdate(self, **kwargs) -> dict:
        """给还没有「- 发布：」行的老笔记补上发布日期（按 BV 现查）。"""
        import asyncio

        notes_dir = self.notes_dir / "notes"
        fixed, failed, skipped = 0, [], 0
        files = [p for p in sorted(notes_dir.glob("*.md")) if p.name != "index.md"]
        for p in files:
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            if "- 发布：" in text:
                skipped += 1
                continue
            m_bv = re.search(r"bilibili\.com/video/(BV[0-9A-Za-z]+)", text)
            if not m_bv:
                skipped += 1
                continue
            bvid = m_bv.group(1)
            try:
                info = await self.api.view(bvid)
            except Exception as exc:
                failed.append(bvid)
                logger.warning(f"[bili] 补发布日失败 {bvid}: {exc!r}")
                await asyncio.sleep(0.6)
                continue
            try:
                ts = int(float((info or {}).get("pubdate") or 0))
            except Exception:
                ts = 0
            if not ts:
                failed.append(bvid)
                continue
            pub = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            lines = text.splitlines()
            out, inserted = [], False
            for line in lines:
                out.append(line)
                if not inserted and line.startswith("- 链接："):
                    out.append(f"- 发布：{pub}")
                    inserted = True
            if not inserted:
                failed.append(bvid)
                continue
            p.write_text("\n".join(out), encoding="utf-8")
            fixed += 1
            await asyncio.sleep(0.4)
        return {"ok": True, "data": {"fixed": fixed, "skipped": skipped, "failed": failed}}

    async def _panel_backfill_uid(self, payload=None, **kwargs) -> dict:
        """给已有笔记补 UP 的 UID：按 BV 拉详情，写回 `- UP主：名字（UID 123）`。"""
        import asyncio as _asyncio

        fixed, skipped, failed = 0, 0, []
        _nd = Path(self.notes_dir) / "notes"
        for p in sorted(_nd.glob("*.md")):
            if p.name == "index.md":
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            if "（UID " in text:
                skipped += 1
                continue
            mv = re.search(r"BV[0-9A-Za-z]{10}", text)
            mu = re.search(r"^- UP主：(.+)$", text, flags=re.M)
            if not mv or not mu:
                skipped += 1
                continue
            try:
                info = await self.api.view(mv.group(0))
            except Exception as exc:
                failed.append(mv.group(0))
                logger.warning(f"[bili] 补 UID 失败 {mv.group(0)}: {exc!r}")
                continue
            mid = info.get("owner_mid")
            if not mid:
                failed.append(mv.group(0))
                continue
            who = str(info.get("owner") or mu.group(1)).strip()
            text = text.replace(mu.group(0), f"- UP主：{who}（UID {mid}）", 1)
            p.write_text(text, encoding="utf-8", newline=chr(10))
            fixed += 1
            await _asyncio.sleep(0.4)
        logger.info(f"[bili] 补 UID 完成: fixed={fixed} skipped={skipped} failed={len(failed)}")
        return {
            "ok": True,
            "data": {"fixed": fixed, "skipped": skipped, "failed": failed},
        }

    async def _panel_backfill_memory(self, payload=None, **kwargs):
        """把指定 BV 的笔记补写进长期记忆（POST /api/backfill_memory）。"""
        body = payload if isinstance(payload, dict) else {}
        bvids = [str(x).strip() for x in (body.get("bvids") or []) if str(x).strip()]
        root = Path(self.config.get("notes_dir") or DEFAULT_NOTES_DIR)
        notes_dir = root / "notes" if (root / "notes").is_dir() else root
        engines = self._memory_engines()
        if not engines:
            return {"ok": False, "error": "没有找到 livingmemory 的记忆引擎"}
        if not bvids:
            return {"ok": False, "error": "没有指定 bvid"}
        added, skipped, failed = [], [], []
        for bvid in bvids:
            target = None
            for f in notes_dir.glob("*.md"):
                try:
                    if bvid in f.read_text(encoding="utf-8", errors="ignore"):
                        target = f
                        break
                except Exception:
                    continue
            if target is None:
                skipped.append({"bvid": bvid, "why": "找不到笔记文件"})
                continue
            text = target.read_text(encoding="utf-8", errors="ignore")
            title = ""
            owner = ""
            summary = ""
            points = []
            insight = ""
            section = ""
            for line in text.splitlines():
                s2 = line.strip()
                if s2.startswith("# ") and not title:
                    title = s2[2:].strip()
                elif s2.startswith("- 视频："):
                    title = title or s2[5:].strip()
                elif s2.startswith("- UP主："):
                    owner = s2[5:].strip()
                elif s2.startswith("## "):
                    section = s2[3:].strip()
                elif section.startswith("讲了什么") and s2 and not s2.startswith(("-", ">", "#")):
                    summary += s2
                elif section.startswith("主要知识点") and s2.startswith(("-", "*", "·")):
                    points.append(s2.lstrip("-*· ").strip())
                elif section.startswith("我的感受") and s2 and not s2.startswith(("#", ">")):
                    insight += s2
            if not title:
                skipped.append({"bvid": bvid, "why": "笔记里没有标题"})
                continue
            parts = [f"我看了B站视频《{title}》（UP主 {owner or '未知'}）。"]
            if summary:
                parts.append(summary[:200])
            if points:
                parts.append("要点：" + "；".join(points[:3]))
            if insight:
                parts.append("我的感受：" + insight[:160])
            content = "\n".join([x for x in parts if x]).strip()[:1200]
            last_exc = None
            for eng in engines:
                for attempt in range(3):
                    try:
                        await eng.add_memory(
                            content=content,
                            session_id=self.state.get("share_umo") or "bili_learning",
                            persona_id=self._memory_persona(),
                            importance=0.6,
                            metadata={"source": "bili_learning", "bvid": bvid, "kind": "note", "backfill": True},
                        )
                        last_exc = None
                        break
                    except Exception as exc:
                        last_exc = exc
                        if attempt < 2:
                            await asyncio.sleep((2, 5)[attempt])
                if last_exc is None:
                    break
            if last_exc is None:
                added.append(bvid)
                logger.info(f"[bili] 补录记忆成功 {bvid}")
            else:
                failed.append({"bvid": bvid, "error": repr(last_exc)})
                logger.warning(f"[bili] 补录记忆失败 {bvid}: {last_exc!r}")
        return {"ok": True, "data": {"added": added, "skipped": skipped, "failed": failed}}

    def _memory_engines(self) -> list:
        """列出 livingmemory 的所有记忆引擎（重载后可能残留未初始化的旧实例）。"""
        out = []
        try:
            from astrbot.core.star.star import star_map

            for meta in star_map.values():
                if (meta.name or "") != "astrbot_plugin_livingmemory":
                    continue
                eng = getattr(getattr(meta.star_cls, "initializer", None), "memory_engine", None)
                if eng is not None and eng not in out:
                    out.append(eng)
        except Exception:
            pass
        # 有 db 的排前面
        out.sort(key=lambda e: 0 if getattr(e, "db", None) is not None else 1)
        return out

    def _memory_engine(self):
        """取第一个可用的 livingmemory 记忆引擎（没装就返回 None）。"""
        engines = self._memory_engines()
        return engines[0] if engines else None

    # -------------------------------------------------------------------- 帮助

    # ------------------------------------------------------------------ 重跑
    async def _cmd_replay(
        self, event: AstrMessageEvent, arg: str
    ) -> AsyncGenerator[MessageEventResult, None]:
        """按 BV 号重跑一条视频：主人点名的，默认写完整笔记、不发私信。"""
        raw = (arg or "").strip()
        if not raw:
            yield event.plain_result(
                "用法：/bili 重跑 BV1xxxxxxxxx （默认写笔记；-r 只看、-s 发B站私信）"
            )
            return
        write_note = not bool(re.search(r"(^|\s)(-r|--read|只看|不写笔记)(\s|$)", raw))
        do_share = bool(re.search(r"(^|\s)(-s|--share|发私信)(\s|$)", raw))
        cleaned = re.sub(r"(^|\s)(-w|--write|--note|写笔记|-s|--share|发私信|-r|--read|只看|不写笔记)(\s|$)", " ", raw)
        m = re.search(r"(BV[0-9A-Za-z]{10})", cleaned, re.I)
        if not m:
            am = re.search(r"av(\d+)", cleaned, re.I)
            if am:
                yield event.plain_result(
                    f"只认 BV 号哦，av{am.group(1)} 请在视频页复制 BV 号再发给我。"
                )
                return
            yield event.plain_result("没找到 BV 号，格式像 BV1GxGtzREQK 这种。")
            return
        bvid = m.group(1)

        if not self.api.logged_in:
            yield event.plain_result("我还没登录 B站，得先扫个码，发 /bili 登录 我就把二维码发出来。")
            return
        if self._busy:
            yield event.plain_result(
                "我这儿正跑着一轮呢，等它结束再重跑（大概一两分钟）。"
            )
            return
        provider = None
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
        except Exception as exc:
            logger.warning(f"[bili] 取模型失败: {exc!r}")
        if provider is None:
            yield event.plain_result("没找到能用的模型，检查一下默认模型配置。")
            return

        yield event.plain_result(f"🔁 开始重跑 {bvid}（抓弹幕、听音频，可能要一两分钟）…")
        self._busy = True
        try:
            learner = Learner(self.api, dict(self.config), self.state)
            res = await learner.process_bvid(
                provider, bvid, event.unified_msg_origin, self.notes_dir / "notes",
                write_note, user_picked=True,
            )
        finally:
            self._busy = False

        if not res.get("ok"):
            self._log(f"重跑失败 {bvid}: {res.get('error')}")
            yield event.plain_result(f"重跑没成：{res.get('error')}")
            return

        note = res.get("note") or {}
        secs = int(res.get("duration") or 0)
        mm, ss = divmod(secs, 60)
        src_map = {"subtitle": "B站字幕", "transcribe": "语音转写", "none": "无正文"}
        lines = [
            f"🔁 《{res.get('title')}》",
            f"UP：{res.get('owner') or '?'} ｜ 时长 {mm:02d}:{ss:02d}",
            f"正文来源：{src_map.get(res.get('text_source'), res.get('text_source'))}"
            f"（{res.get('body_chars') or 0} 字）",
            f"标签：{('、'.join(note.get('tags') or []) or '—')}",
            f"摘要：{str(note.get('summary') or '')[:200]}",
        ]
        if note.get("fun_line"):
            lines.append(f"一句话：{note.get('fun_line')}")
        if write_note and res.get("path"):
            self._rebuild_index()
            lines.append(f"📝 笔记已写：{res.get('path')}")
        if do_share:
            msg = await self._share_video_dm(res)
            if msg:
                lines.append(f"📨 {msg}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ 日报
    def _today_notes(self) -> List[dict]:
        """今天写的笔记（标题/链接/标签），按时间倒序。"""
        d = self.notes_dir / "notes"
        if not d.exists():
            return []
        today = self._today()
        out: List[dict] = []
        for p in d.glob("*.md"):
            if p.name == "index.md":
                continue
            try:
                if datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d") != today:
                    continue
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            title, link, tags = "", "", []
            for line in text.splitlines():
                s = line.strip()
                if not title and s.startswith("# "):
                    title = s[2:].strip()
                elif not link and "bilibili.com/video/" in s:
                    link = s.split("bilibili.com/video/", 1)[1].split()[0].strip(")（(）")
                elif s.startswith("- 标签"):
                    raw_tags = s.split("：", 1)[-1] if "：" in s else s.split(":", 1)[-1]
                    tags = [t.strip() for t in re.split(r"[、,，/]", raw_tags) if t.strip()]
            gist, pub = "", ""
            for line in text.splitlines():
                st = line.strip()
                if not gist and st.startswith("> "):
                    gist = st[2:].strip()
                if not pub and st.startswith("- 发布"):
                    pub = re.split(r"[：:]", st, maxsplit=1)[-1].strip()
                    if pub == "—":
                        pub = ""
            out.append(
                {
                    "title": title or p.stem,
                    "link": link,
                    "tags": tags,
                    "gist": gist,
                    "pub": pub,
                    "short": "跟当前兴趣不搭" in text,
                    "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime,
                }
            )
        return sorted(out, key=lambda x: x["mtime"], reverse=True)

    # 汇报开头：几种口吻，随机挑一种
    REPORT_OPENERS = (
        "报告报告～",
        "晚上好呀，今天的作业交一下：",
        "嗐，又到了汇报时间：",
        "喂喂，我今天的战绩——",
        "叮，今日学习小结：",
        "汇报：",
        "不废话，直接报数：",
        "今日份的我看完了这些：",
        "来交作业了：",
        "今天的份儿，收好：",
    )
    # 一条视频都没看时：几种说法，随机挑一种
    EMPTY_LINES = (
        (
            "今天一个视频都没看成。",
            "翻来翻去找不到中意的，不是偷懒。",
        ),
        (
            "今天颗粒无收…… 挑了半小时，一个都没点进去。",
            "胃口有点挑，得缓缓。",
        ),
        (
            "汇报：今天 0 个视频、0 篇笔记。",
            "原因：挑食。不是没空。",
        ),
        (
            "今天…啥也没看。",
            "不是不想看，是刷了半天都没一个对胃口的。",
        ),
    )

    def _build_daily_report(self, limit: int = 5) -> str:
        """当日汇总；今天什么都没看也要发，换个说法也要发。"""
        notes = self._today_notes()
        usage = (self.state.get("usage") or {}).get(self._today()) or {}
        videos = int(usage.get("videos", 0) or 0) or len(notes)

        if not notes and not videos:
            a, b = random.choice(self.EMPTY_LINES)
            empty_openers = (
                "报告报告～",
                "叮，今日学习小结：",
                "汇报：",
                "检查作业时间：",
                "今日份的汇报：",
            )
            return "\n".join([random.choice(empty_openers), a, "", b])

        tag_count: Dict[str, int] = {}
        for n in notes:
            for t in n["tags"]:
                tag_count[t] = tag_count.get(t, 0) + 1
        pool = self.state.get("interests") or {}
        top_tags = [
            t
            for t, _ in sorted(
                tag_count.items(),
                key=lambda kv: (float(pool.get(kv[0], 0) or 0), kv[1]),
                reverse=True,
            )[:3]
        ]
        n_notes = len(notes)
        if videos == 1:
            count = "1 个视频，别嫌少，一个也是看了"
        else:
            count = f"{videos} 个视频"

        opener = random.choice(self.REPORT_OPENERS)
        if n_notes:
            head = random.choice(
                [
                    f"{opener}看了 {count}，记了 {n_notes} 篇笔记",
                    f"{opener}{count}，笔记 {n_notes} 篇",
                    f"{opener}战报：{count} · {n_notes} 篇笔记",
                    f"{opener}今天 {count}，顺手写了 {n_notes} 篇笔记",
                ]
            )
        else:
            head = random.choice(
                [
                    f"{opener}看了 {count}，笔记还没来得及写",
                    f"{opener}{count}，先把素材存着",
                ]
            )

        lines = [head, ""]
        for i, n in enumerate(notes[:limit], 1):
            lines.append(f"{i}.《{n['title']}》")
        if len(notes) > limit:
            lines.append(f"…还有 {len(notes) - limit} 个，都躺在笔记里，没丢")
        if top_tags:
            lines.append("")
            lines.append(f"今天最上心的是：{'、'.join(top_tags)}")
        if notes:
            def _w(n):
                return sum(float(pool.get(t, 0) or 0) for t in n["tags"])
            best = max(notes, key=lambda n: (_w(n), n.get("size") or 0))
            lines.append("")
            lines.append(f"🌟 今天最值得回头看的一条：《{best['title']}》")
            if best.get("gist"):
                lines.append(f"   {best['gist'][:70]}")
        return "\n".join(lines)

    REPORT_LEADS = (
        "晚上好呀，今天的作业交一下。",
        "报告报告，今天的收获都在图里。",
        "叮，今日学习小结已生成。",
        "汇报时间到。",
        "今日份的笔记，收好。",
        "来交作业了。",
        "不废话，看图。",
        "今天的份儿，给你。",
    )

    def _build_daily_summary(self) -> str:
        """日报的文字部分：只说结论，不列清单（清单交给图片）。"""
        notes = self._today_notes()
        usage = (self.state.get("usage") or {}).get(self._today()) or {}
        videos = int(usage.get("videos", 0) or 0) or len(notes)
        if not notes and not videos:
            a, b = random.choice(self.EMPTY_LINES)
            return "\n".join([random.choice(["报告报告～", "叮，今日学习小结：", "汇报：", "检查作业时间："]), a, "", b])
        return random.choice(self.REPORT_LEADS)

    def _build_daily_card(self):
        """把今日笔记画成一张图，省得文字刷屏。返回图片路径，失败返回 None。"""
        try:
            from PIL import Image as PILImage, ImageDraw, ImageFont
        except Exception:
            return None
        notes = self._today_notes()
        if not notes:
            return None
        usage = (self.state.get("usage") or {}).get(self._today()) or {}
        videos = int(usage.get("videos", 0) or 0) or len(notes)
        pool = self.state.get("interests") or {}
        tag_count = {}
        for n in notes:
            for t in n["tags"]:
                tag_count[t] = tag_count.get(t, 0) + 1
        top_tags = [t for t, _ in sorted(tag_count.items(), key=lambda kv: (float(pool.get(kv[0], 0) or 0), kv[1]), reverse=True)[:3]]

        def _w(n):
            return sum(float(pool.get(t, 0) or 0) for t in n["tags"])

        best = max(notes, key=lambda n: (_w(n), n.get("size") or 0))
        rows = notes
        W, pad, gap, row_h = 1080, 44, 36, 40
        col_w = (W - pad * 2 - gap) // 2
        half = (len(rows) + 1) // 2
        n_rows = max(half, len(rows) - half)
        H = 200 + row_h * n_rows + 44 + 330
        img = PILImage.new("RGB", (W, H), "#ffffff")
        d = ImageDraw.Draw(img)
        for i in range(8):
            c = (
                int(47 + (244 - 47) * i / 7),
                int(109 + (82 - 109) * i / 7),
                int(246 + (126 - 246) * i / 7),
            )
            d.rectangle([0, i, W, i + 1], fill=c)
        def _pick_font(paths, size):
            for path in paths:
                try:
                    if Path(path).exists():
                        return ImageFont.truetype(path, size)
                except Exception:
                    continue
            return ImageFont.load_default()

        _BOLD = [r"C:\Windows\Fonts\msyhbd.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
                 "/System/Library/Fonts/PingFang.ttc"]
        _REG = [r"C:\Windows\Fonts\msyh.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/System/Library/Fonts/PingFang.ttc"]
        fb = lambda s: _pick_font(_BOLD, s)
        fr = lambda s: _pick_font(_REG, s)
        d.text((pad, 52), self._bot_name() + "的今日笔记", font=fb(42), fill="#1f2b47")
        d.text((pad, 116), self._today() + " · " + str(videos) + " 个视频 · " + str(len(notes)) + " 篇笔记", font=fr(24), fill="#7a87a8")
        y = 174
        d.line([pad, y, W - pad, y], fill="#e6ecf7", width=2)
        y += 24
        f_row, f_idx = fr(20), fb(19)
        for i, n in enumerate(rows):
            col, row = (0, i) if i < half else (1, i - half)
            x = pad + col * (col_w + gap)
            yy = y + row * row_h
            t = n["title"]
            ym = (n.get("pub") or "")[:7]
            bored = bool(n.get("short"))
            avail = col_w - 44 - (58 if ym else 0) - (62 if bored else 0)
            while d.textlength(t, font=f_row) > avail and len(t) > 6:
                t = t[:-1]
            if t != n["title"]:
                t = t[:-1] + "…"
            d.text((x, yy), str(i + 1).rjust(2), font=f_idx, fill="#a9b6d4")
            d.text((x + 38, yy), t, font=f_row, fill="#26324e")
            if bored:
                off = x + 38 + d.textlength(t, font=f_row) + 8
                d.text((off, yy + 3), "不感兴趣", font=fr(15), fill="#f4527e")
            if ym:
                d.text((x + col_w - 54, yy + 2), ym, font=fr(16), fill="#b3bed6")
        y = y + n_rows * row_h + 4
        y += 10
        d.line([pad, y, W - pad, y], fill="#e6ecf7", width=2)
        y += 26
        if top_tags:
            d.text((pad, y), "今天最上心：" + "、".join(top_tags), font=fr(25), fill="#3aa989")
            y += 42
        d.text((pad, y), "★ 最值得回头看", font=fb(25), fill="#f4527e")
        y += 40
        d.text((pad, y), "《" + best["title"] + "》", font=fb(26), fill="#1f2b47")
        gist = (best.get("gist") or "").strip()
        if gist:
            y += 38
            fg = fr(23)
            line, printed = "", 0
            for ch in gist:
                if d.textlength(line + ch, font=fg) > W - pad * 2:
                    d.text((pad, y), line, font=fg, fill="#7a87a8")
                    line, printed = ch, printed + 1
                    y += 34
                    if printed >= 2:
                        line = ""
                        break
                else:
                    line += ch
            if line and printed < 3:
                d.text((pad, y), line, font=fg, fill="#7a87a8")
        out = self.notes_dir / "daily" / (self._today() + ".png")
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out)
        return out

    async def _notify_image(self, session: str, path) -> None:
        try:
            await self.context.send_message(
                session, MessageChain(chain=[Image.fromFileSystem(str(path))])
            )
        except Exception as exc:
            logger.warning(f"[bili] 推送图片失败: {exc!r}")

    async def _panel_report_preview(self, **kwargs):
        """只看不发：返回日报纯文本，方便预览文案。"""
        card = self._build_daily_card()
        return {
            "ok": True,
            "data": {
                "text": self._build_daily_summary(),
                "card": str(card) if card else "",
            },
        }

    async def _send_daily(self, event: AstrMessageEvent, full: bool = False) -> None:
        """日报统一发法：先说一句话，再发图片；full=True 时额外把文字清单也发一份。"""
        try:
            await event.send(event.plain_result(self._build_daily_summary()))
        except Exception as exc:
            logger.warning(f"[bili] 发日报摘要失败: {exc!r}")
        card = self._build_daily_card()
        if card:
            await self._notify_image(event.unified_msg_origin, card)
        if full:
            try:
                await event.send(event.plain_result(self._build_daily_report(limit=999)))
            except Exception as exc:
                logger.warning(f"[bili] 发文字清单失败: {exc!r}")

    async def _cmd_daily(
        self, event: AstrMessageEvent, arg: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        a = (arg or "").strip().lower()
        # 只要透出「想看全」的意思就列全部
        full = any(k in a for k in ("全", "都", "每", "all", "所有", "完整"))
        await self._send_daily(event, full=full)
        if False:
            yield

    async def _report_loop(self) -> None:
        """今天收工后过一个小时发当日汇总（也可固定时间兜底）。"""
        await asyncio.sleep(40)
        while True:
            try:
                if self._superseded:
                    return
                if (
                    str(self.config.get("report_mode", "daily") or "daily").lower() == "daily"
                    and self.config.get("share_qq", True)
                    and self.config.get("enabled", True)
                    and not self.state.get("paused")
                    and self.state.get("daily_report_date") != self._today()
                ):
                    now = datetime.now()
                    if self._in_quiet_hours(now):
                        await asyncio.sleep(60)
                        continue
                    after = int(self.config.get("report_after_minutes", 60) or 0)
                    last = float(self.state.get("last_run_ts", 0) or 0)
                    fixed = str(self.config.get("daily_report_time", "") or "").strip()
                    ready = False
                    if self._day_finished(now) and (
                        not last or (time.time() - last) >= after * 60
                    ):
                        ready = True
                    if not ready and fixed and now.strftime("%H:%M") >= fixed:
                        ready = True
                    if ready:
                        umo = self.state.get("share_umo")
                        text = self._build_daily_summary()
                        self.state["daily_report_date"] = self._today()
                        self._save_state()
                        if text and umo:
                            await self._notify(umo, text)
                            card = self._build_daily_card()
                            if card:
                                await self._notify_image(umo, card)
                            self._log("当日汇总已发送")
            except Exception as exc:
                logger.warning(f"[bili] 日报任务异常: {exc!r}")
            await asyncio.sleep(60)

    def _day_finished(self, now: datetime) -> bool:
        """今天是不是已经收工：配额用完，或者今天的时段全过了。"""
        day = (self.state.get("plan") or {}).get(now.strftime("%Y-%m-%d")) or {}
        runs_today = int(day.get("runs", 0) or 0)
        runs_per_day = int(self.config.get("runs_per_day", 2) or 2)
        if runs_today >= runs_per_day:
            return True
        windows = self._windows()
        if not windows:
            return True
        return now >= windows[-1][1]

    def _help_text(self) -> str:
        return (
            "📺 B站学习命令\n"
            "/bili 学 [关键词] · 现在就看一个、写笔记\n"            "/bili 重跑 BV号 [-w] [-s] · 重跑指定视频（-w 写笔记，-s 发私信）\n"            "/bili 日报 [全部] · 现在就把今天学的东西汇总发给我（加「全部」列全）\n"
            "/bili 模式 日报|即时 · 平时攒着晚上说，还是每学一个就说\n"
            "/bili 登录 · 扫码登录小号\n"
            "/bili 状态 · 看账号、开关、今天用量\n"
            "/bili 暂停 · /bili 继续\n"
            "/bili 兴趣 [+主题|-主题]\n"
            "/bili 权重 主题 2.6 · 直接调某个兴趣的权重（删=去掉）\n"
            "/bili 只看兴趣 [开|关] · 跑题的视频直接跳过，不学也不分享\n"
            "/bili 分享 [私信|汇报|语音] [开|关] · 三路分享各自开关\n"
            "/bili 日志 [条数]\n"
            "/bili 笔记 [关键词] · 翻旧笔记\n"
            "/bili 报告 [天数] · 最近的学习统计\n"
            "/bili 周报 · 这周学了啥\n"
            "/bili 复习 · 抽一篇旧笔记考考你\n"
            "/bili 索引 · 重建笔记索引（notes/index.md）\n"
            "/bili 私信 [内容] · 不带内容就把最近看过的视频分享到你B站私信\n"
            "/bili 退出登录 · 清掉本地登录态\n"
            "（主动汇报、点赞收藏、语音汇报已开）"
        )

    # ------------------------------------------------------------ 笔记与报告

    def _notes_files(self) -> List[Path]:
        d = self.notes_dir / "notes"
        if not d.exists():
            return []
        files = list(d.rglob("*.md"))
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def _archive_notes(self, keep: int = 200) -> int:
        """笔记超过 keep 篇时，把老的按月份挪进 notes/archive/YYYY-MM/。"""
        d = self.notes_dir / "notes"
        if not d.exists():
            return 0
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if len(files) <= keep:
            return 0
        moved = 0
        for p in files[keep:]:
            try:
                stamp = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m")
                dest = d / "archive" / stamp
                dest.mkdir(parents=True, exist_ok=True)
                p.replace(dest / p.name)
                moved += 1
            except Exception as exc:
                logger.warning(f"[bili] 归档笔记失败 {p.name}: {exc!r}")
        if moved:
            logger.info(f"[bili] 已归档 {moved} 篇老笔记（保留最近 {keep} 篇在主目录）")
        return moved

    def _cmd_notes(self, arg: str, full: bool = False) -> str:
        files = self._notes_files()
        if not files:
            return "还没有笔记，先发 /bili 学 让我去看一个。"
        q = (arg or "").strip().lower()
        files = [p for p in files if p.name != "index.md"]
        _tag = datetime.now().strftime("%Y%m%d")
        _todays = [fp for fp in files if fp.stem.startswith(_tag)]
        m_idx = re.fullmatch(r"第?\s*([0-9]{1,3})\s*条?", q)
        if m_idx:
            _n = int(m_idx.group(1))
            _pool = _todays or files
            if not 1 <= _n <= len(_pool):
                return f"今天的笔记一共 {len(_pool)} 条，没有第 {_n} 条。"
            _p = _pool[_n - 1]
            _title = _p.stem.split("_", 1)[-1]
            try:
                _text = _p.read_text(encoding="utf-8")
            except Exception:
                _text = ""
            if full and _text:
                return f"《{_title}》全文（照着讲，别编）：\n\n" + _text[:2400]
            return f"第 {_n} 条是《{_title}》。要讲内容的话，再把 full 设成 true 取一次。"
        if not q:
            _pool = _todays or files
            _head = "今天" if _todays else "最近"
            lines = [f"{_head}的笔记共 {len(_pool)} 条（按时间倒序，序号和日报图一致）："]
            for _i, _p in enumerate(_pool[:30], 1):
                lines.append(f"{_i}. {_p.stem.split('_', 1)[-1]}")
            if len(_pool) > 30:
                lines.append(f"……还有 {len(_pool) - 30} 条")
            lines.append("说「第N条」并要我讲，我就把那篇正文取出来")
            return "\n".join(lines)
        hits = []
        for p in files[:800]:
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            low = text.lower()
            idx = low.find(q)
            if idx >= 0:
                snippet = text[max(0, idx - 40) : idx + 80].replace("\n", " ")
                hits.append(f"· {p.stem.split('_', 1)[-1]}\n  …{snippet}…")
            if len(hits) >= 5:
                break
        if not hits:
            # 宽松一点：按标题再找一次
            for p in files[:800]:
                title = p.stem.split("_", 1)[-1]
                if q and q in title.lower():
                    try:
                        text = p.read_text(encoding="utf-8")
                    except Exception:
                        continue
                    if full:
                        return f"《{title}》全文（照着讲，别编）：\n\n" + text[:2200]
                    return f"· {title}\n  …{text[:140].replace(chr(10), ' ')}…"
            return f"笔记里没找到「{arg}」。"
        if full:
            # 命中就把第一篇的正文整篇给她，让她照着讲
            for p in files[:800]:
                try:
                    text = p.read_text(encoding="utf-8")
                except Exception:
                    continue
                if q in text.lower():
                    title = p.stem.split("_", 1)[-1]
                    return f"《{title}》全文（照着讲，别编）：\n\n" + text[:2200]
        return "找到这些：\n" + "\n".join(hits)

    def _cmd_report(self, arg: str) -> str:
        files = self._notes_files()
        if not files:
            return "还没有笔记，报告要等我看几个视频之后才能写。"
        days = 7
        try:
            if arg.strip().isdigit():
                days = max(1, min(30, int(arg.strip())))
        except Exception:
            days = 7
        cutoff = time.time() - days * 86400
        recent = [p for p in files if p.stat().st_mtime >= cutoff] or files[:5]
        tags: Dict[str, int] = {}
        titles = []
        for p in recent[:20]:
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for line in text.splitlines():
                if line.startswith("- 视频："):
                    titles.append(line[5:].strip())
                if line.startswith("- 标签："):
                    for t in re.split(r"[、,，]", line[5:]):
                        t = t.strip()
                        if t and t not in ("（无）",):
                            tags[t] = tags.get(t, 0) + 1
        top_tags = sorted(tags.items(), key=lambda kv: -kv[1])[:6]
        usage = self._usage_today()
        lines = [
            f"📊 最近 {days} 天的学习报告",
            f"笔记 {len(recent)} 篇 · 今天 {usage.get('videos', 0)} 个视频 / {usage.get('tokens', 0)} tokens",
            f"常看的主题：{'、'.join(t for t, _ in top_tags) if top_tags else '（还不多）'}",
            "",
            "看了这些：",
        ]
        lines += [f"· {t}" for t in titles[:10]] or ["· （暂无标题记录）"]
        return "\n".join(lines)
    async def _cmd_dm(
        self, event: AstrMessageEvent, arg: str
    ) -> AsyncGenerator[MessageEventResult, None]:
        """用B站私信把东西发给主人（默认关闭，怕风控）。"""
        if not self.config.get("share_dm", False):
            yield event.plain_result(
                "B站私信我先关着呢。\n"
                "想开的话发 /bili 分享 私信 开，再确认 bili_dm_target 填的是你大号的B站UID。"
            )
            return
        target = str(self.config.get("bili_dm_target") or "").strip()
        if not target.isdigit():
            yield event.plain_result(
                "bili_dm_target 里还没填你大号的B站UID（要纯数字，不是QQ号）。"
            )
            return
        text = (arg or "").strip()
        if not text:
            last = self.state.get("last_video") or {}
            if last.get("bvid"):
                res = await self.api.send_video_dm(
                    int(target),
                    str(last.get("bvid")),
                    str(last.get("title") or ""),
                    str(last.get("owner") or ""),
                )
                if not res.get("ok"):
                    yield event.plain_result(f"私信没发出去：{res.get('error')}")
                    return
                self.state.setdefault("dm", {})[self._today()] = dm + 1
                self._save_state()
                self._log(f"用B站私信分享了视频《{last.get('title')}》")
                yield event.plain_result(
                    f"把最近看的《{last.get('title')}》分享到你B站私信了，去看一眼？"
                )
                return
            yield event.plain_result("我最近还没看视频呢，先发 /bili 学 让我看一个？")
            return
        dm = self.state.setdefault("dm", {}).setdefault(self._today(), 0)
        limit = int(self.config.get("bili_dm_daily_limit", 5) or 5)
        if dm >= limit:
            yield event.plain_result(f"今天已经私信你 {dm} 条了，明天再说吧。")
            return
        res = await self.api.send_dm(int(target), text[:900])
        if not res.get("ok"):
            self._log(f"私信发送失败：{res.get('error')}")
            yield event.plain_result(f"私信没发出去：{res.get('error')}")
            return
        self.state["dm"][self._today()] = dm + 1
        self._save_state()
        self._log("用B站私信发了一条消息")
        yield event.plain_result("发到B站私信里了，去看一眼？")

    def _cmd_weekly(self) -> str:
        """每周小结：这周看了什么、绕不开的主题。"""
        files = self._notes_files()
        if not files:
            return "还没有笔记，周报要等我看几个视频之后才能写。"
        cutoff = time.time() - 7 * 86400
        recent = [p for p in files if p.stat().st_mtime >= cutoff]
        if not recent:
            return "这周还没看新东西呢，发 /bili 学 让我去看一个？"
        tags: Dict[str, int] = {}
        items: List[str] = []
        chars = 0
        for p in recent[:30]:
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            chars += len(text)
            title = p.stem.split("_", 1)[-1]
            one = ""
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("- 标签："):
                    for t in re.split(r"[、,，]", s[5:]):
                        t = t.strip()
                        if t and t != "（无）":
                            tags[t] = tags.get(t, 0) + 1
                elif s and not s.startswith(("#", "-", ">", "|", "*")) and not one:
                    one = s[:70]
            items.append(f"· {title}" + (f"\n  {one}" if one else ""))
        top = sorted(tags.items(), key=lambda kv: -kv[1])[:5]
        lines = [
            "🗓 这周的学习小结",
            f"看了 {len(recent)} 个视频，写了 {len(recent)} 篇笔记（约 {max(1, chars // 1000)} 千字）",
            f"绕不开的主题：{'、'.join(t for t, _ in top) if top else '（还比较散）'}",
            "",
        ]
        lines += items[:8]
        if len(recent) > 8:
            lines.append(f"…还有 {len(recent) - 8} 篇，发 /bili 笔记 可以翻。")
        return "\n".join(lines)
    async def _cmd_review(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        """随机抽一篇旧笔记，让模型出三个小问题考一考。"""
        files = self._notes_files()
        if not files:
            yield event.plain_result("还没笔记可复习呢，先发 /bili 学 让我去看一个。")
            return
        old = [p for p in files if time.time() - p.stat().st_mtime >= 3 * 86400] or files
        pick = random.choice(old)
        try:
            text = pick.read_text(encoding="utf-8")
        except Exception as exc:
            yield event.plain_result(f"这篇笔记读不出来了：{exc!r}")
            return
        title = pick.stem.split("_", 1)[-1]
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
        except Exception:
            provider = None
        if provider is None:
            yield event.plain_result(f"想考你的《{title}》，但我这会儿没连上大脑，先歇会儿。")
            return
        prompt = (
            "下面是我以前看视频写的笔记。请出 3 个能考我的小问题，口语化、一句一个，"
            "只出题不要给答案。\n\n" + text[:2500]
        )
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=f"你是{self._bot_name()}，说话口语化、有点调皮，只出题不解释。",
                session_id=event.unified_msg_origin,
                persist=False,
            )
            quiz = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning(f"[bili] 复习出题失败: {exc!r}")
            yield event.plain_result(f"出题的时候脑子卡了一下：{exc}")
            return
        if not quiz:
            yield event.plain_result(f"《{title}》我想考你点啥，一时没想出来。")
            return
        yield event.plain_result(
            f"来复习一下《{title}》：\n\n{quiz}\n\n想不起来的发 /bili 笔记 关键词 自己翻。"
        )

    # -------------------------------------------------------------- 点赞收藏
    async def _pick_fav_folder(self) -> Optional[int]:
        cached = self.state.get("fav_folder_id")
        if cached:
            return int(cached)
        try:
            folders = await self.api.fav_folders()
        except Exception as exc:
            logger.debug(f"[bili] 取收藏夹失败: {exc!r}")
            return None
        if not folders:
            return None
        want = str(self.config.get("fav_folder_name") or "").strip()
        chosen = None
        if want:
            chosen = next((f for f in folders if want in str(f.get("title") or "")), None)
        if chosen is None and want:
            created = await self.api.add_fav_folder(want)
            if created.get("ok") and created.get("id"):
                chosen = {"id": created["id"], "title": want}
                self._log(f"新建收藏夹《{want}》")
            else:
                self._log(f"新建收藏夹《{want}》失败：{created.get('error')}")
        chosen = chosen or folders[0]
        fid = chosen.get("id")
        if fid:
            self.state["fav_folder_id"] = int(fid)
            self._save_state()
        return int(fid) if fid else None
    @staticmethod
    def _clip_sentence(text: str, limit: int) -> str:
        """按标点把话截断，宁可短也不断在半句。"""
        s = str(text or "").strip()
        if len(s) <= limit:
            return s
        head = s[:limit]
        for mark in ("。", "！", "？", "…", "；", "!", "?", ";"):
            pos = head.rfind(mark)
            if pos >= 12:
                return head[: pos + 1]
        for mark in ("，", "、", ","):
            pos = head.rfind(mark)
            if pos >= 12:
                return head[:pos] + "…"
        return head + "…"

    def _dm_comment(self, note: Dict[str, Any], title: str = "") -> str:
        """私信开头那句「人话」：优先用模型专门写的 dm_text，其次按标点回退。"""

        def _clean(raw: Any) -> str:
            return str(raw or "").strip().strip("「」\"'“”　 ").strip()

        dm = _clean(note.get("dm_text"))
        if len(dm) >= 10:
            return self._clip_sentence(dm, 90)

        insights = note.get("insights") or []
        if isinstance(insights, str):
            insights = [insights]
        for item in insights:
            text = _clean(item)
            if len(text) >= 8:
                return self._clip_sentence(text, 120)
        fun = _clean(note.get("fun_line"))
        if len(fun) >= 6:
            return f"这期弹幕给我笑死了：「{self._clip_sentence(fun, 90)}」"
        summary = _clean(note.get("summary"))
        if summary:
            first = summary.split("。")[0].strip("，, ")
            if len(first) >= 10:
                return f"刚看完这个：{self._clip_sentence(first, 110)}。"
        if title:
            return f"看完顺手发你，《{title[:40]}》挺对我胃口的。"
        return "看完顺手发你，我挺喜欢的。"

    async def _share_video_dm(
        self, result: Dict[str, Any], worth: Optional[bool] = None
    ) -> str:
        """看完视频后，用B站私信把视频分享给主人（笔记仍然只走QQ）。"""
        if not self.config.get("share_dm", True):
            return ""
        target = str(self.config.get("bili_dm_target") or "").strip()
        if not target.isdigit():
            return ""
        bvid = str(result.get("bvid") or "").strip()
        if not bvid:
            return ""
        # 默认不管兴不感兴趣都分享（私信就是「我看过什么」的流水），
        # 想让私信也过兴趣门槛，把 share_dm_only_interest 打开即可。
        if self.config.get("share_dm_only_interest", False):
            info = (result.get("pack") or {}).get("info") or {}
            if worth is None:
                worth = self._worth_actions(info, result.get("note") or {})
            if not worth:
                self._log(f"《{result.get('title')}》跟兴趣不搭，这轮不分享私信")
                return ""
        today = self._today()
        dm = self.state.setdefault("dm", {}).setdefault(today, 0)
        limit = int(self.config.get("bili_dm_daily_limit", 5) or 5)
        if dm >= limit:
            self._log("B站私信今日已达上限，这轮不分享视频")
            return ""
        res = await self.api.send_video_dm(
            int(target),
            bvid,
            str(result.get("title") or ""),
            str(result.get("owner") or ""),
            comment=self._dm_comment(
                result.get("note") or {}, str(result.get("title") or "")
            ),
        )
        if not res.get("ok"):
            self._log(f"视频私信分享失败：{res.get('error')}")
            await self._notify_issue("dm", f"B站私信没发出去：{res.get('error')}")
            return ""
        self.state["dm"][today] = dm + 1
        self.state["last_video"] = {
            "bvid": bvid,
            "title": result.get("title") or "",
            "owner": result.get("owner") or "",
            "ts": time.time(),
        }
        self._save_state()
        self._log(f"用B站私信分享了视频《{result.get('title')}》")
        return "视频也分享到你B站私信了"

    async def _notify_issue(self, kind: str, text: str) -> None:
        """出问题时主动说一声；同一类问题 30 分钟内只提醒一次。"""
        if not self.config.get("share_qq", True):
            return
        umo = str(self.state.get("share_umo") or "").strip()
        if not umo:
            return
        issues = self.state.setdefault("notify_issue", {})
        now = time.time()
        if now - float(issues.get(kind, 0) or 0) < 1800:
            return
        issues[kind] = now
        self._save_state()
        try:
            await self._notify(umo, f"🐟 {text}")
        except Exception as exc:
            logger.warning(f"[bili] 异常提醒发送失败: {exc!r}")

    def _worth_actions(self, info: Dict[str, Any], note: Dict[str, Any]) -> bool:
        """只有真的对胃口的内容才点赞/投币：优先看模型给的 relevant，没给就比关键词。"""
        rel = note.get("relevant")
        if isinstance(rel, bool):
            return rel
        if isinstance(rel, str):
            return rel.strip().lower() in ("true", "yes", "是", "1", "y")
        text = " ".join(
            str(info.get(k) or "") for k in ("title", "desc", "tname", "dynamic")
        )
        for tag in info.get("tags") or []:
            text += " " + str(tag)
        low = text.lower()
        keys = [str(k).strip().lower() for k in (self.state.get("interests") or {})]
        extra = self.config.get("interest_topics") or []
        if isinstance(extra, str):
            extra = [s.strip() for s in re.split(r"[,，、\s]+", extra) if s.strip()]
        keys += [str(k).strip().lower() for k in extra]
        return any(k and k in low for k in keys)
    async def _do_actions(
        self, result: Dict[str, Any], worth: Optional[bool] = None
    ) -> List[str]:
        """看完之后点赞/投币/收藏（干跑模式什么都不做）。"""
        if self.config.get("dry_run", True):
            return []
        info = (result.get("pack") or {}).get("info") or {}
        aid = info.get("aid")
        if not aid:
            return []
        note = result.get("note") or {}
        if worth is None:
            worth = self._worth_actions(info, note)
        if not worth:
            self._log(f"《{info.get('title')}》跟兴趣不搭，只看不互动")
            return []
        acts = self.state.setdefault("actions", {}).setdefault(
            self._today(), {"like": 0, "fav": 0, "coin": 0}
        )
        gap = int(self.config.get("action_min_interval_sec", 20) or 0)
        last = float(self.state.get("last_action_ts", 0) or 0)
        if gap and last and time.time() - last < gap:
            await asyncio.sleep(max(0.0, gap - (time.time() - last)))
        done: List[str] = []
        try:
            if self.config.get("allow_like", False) and acts["like"] < int(
                self.config.get("like_daily_limit", 10) or 0
            ):
                r = await self.api.like(int(aid), True)
                if r.get("code") in (0, 65006):  # 65006 = 重复点赞
                    acts["like"] += 1
                    done.append("点赞")
                else:
                    logger.info(f"[bili] 点赞失败: {r}")
            if self.config.get("allow_coin", False) and acts["coin"] < int(
                self.config.get("coin_daily_limit", 2) or 0
            ):
                r = await self.api.coin(int(aid), 1, also_like=False)
                if r.get("code") == 0:
                    acts["coin"] += 1
                    done.append("投币")
            if self.config.get("allow_fav", False) and acts["fav"] < int(
                self.config.get("fav_daily_limit", 10) or 0
            ):
                fid = await self._pick_fav_folder()
                if fid:
                    r = await self.api.fav(int(aid), fid, True)
                    if r.get("code") == 0:
                        acts["fav"] += 1
                        done.append("收藏")
        except Exception as exc:
            logger.warning(f"[bili] 点赞收藏失败: {exc!r}")
        finally:
            self.state["last_action_ts"] = time.time()
            self._save_state()
        if done:
            self._log(f"对《{info.get('title')}》做了：{'、'.join(done)}")
        return done
    async def _voice_report(self, text: str, umo: str) -> bool:
        """把汇报念成语音发出去。"""
        if not self.config.get("share_voice", True):
            logger.info("[bili] 语音汇报开关是关的，跳过")
            return False
        if not umo:
            logger.info("[bili] 没有汇报目标会话，跳过语音汇报")
            return False
        if not (text or "").strip():
            logger.info("[bili] 汇报文本是空的，跳过语音汇报")
            return False
        limit = int(self.config.get("voice_max_chars", 300) or 300)
        try:
            tts = self.context.get_using_tts_provider(umo)
            if tts is None:
                logger.info("[bili] 没有可用的语音合成，跳过语音汇报")
                return False
            path = await tts.get_audio(text[:limit])
            if not path:
                logger.info("[bili] 语音合成返回空文件，跳过语音汇报")
                return False
            await self.context.send_message(
                umo, MessageChain(chain=[Record.fromFileSystem(str(path))])
            )
            logger.info(f"[bili] 语音汇报已发送: {path}")
            return True
        except Exception as exc:
            logger.warning(f"[bili] 语音汇报失败: {exc!r}")
            return False

    # 兴趣饱和算法：score = 5 × hits/(hits+3) × 0.5^(天数/14)
    # 命中越多越接近 5（永远到不了），两周不用权重减半；跑题只算“给过机会”不算命中。
    INTEREST_HALF_LIFE_DAYS = 14.0

    def _interest_score(self, hits, tries, last_ts, misses=0) -> float:
        """饱和曲线 × 时间衰减 × 跑题惩罚（misses 越多分越低，命中可部分赎罪）。"""
        import time as _time

        hits = max(0, int(hits or 0))
        misses = max(0, int(misses or 0))
        last = float(last_ts or 0)
        days = max(0.0, (_time.time() - last) / 86400.0) if last else 0.0
        decay = 0.5 ** (days / self.INTEREST_HALF_LIFE_DAYS)
        penalty = 1.0 / (1.0 + misses / 10.0)
        return round(5.0 * hits / (hits + 3.0) * decay * penalty, 3)

    def _migrate_interest_stats(self, interests: Dict[str, float]) -> None:
        """老数据（纯浮点分数）转成 hits/tries，只做一次。"""
        if self.state.get("interest_algo") == "saturate":
            return
        import time as _time

        now = _time.time()
        stats: Dict[str, dict] = {}
        for name, score in (interests or {}).items():
            try:
                val = float(score)
            except (TypeError, ValueError):
                continue
            # 反解饱和曲线：score = 5h/(h+3) → h = 3s/(5-s)
            ratio = max(0.05, min(0.95, val / 5.0))
            base = max(1, round(3.0 * ratio / (1.0 - ratio)))
            # 在 base 附近取反解误差最小的整数 hits，避免迁移后分数无谓漂移
            hits = min(
                range(max(1, base - 2), base + 3),
                key=lambda h: abs(5.0 * h / (h + 3.0) - val),
            )
            stats[str(name)] = {
                "hits": hits,
                "tries": max(hits + 1, round(hits * 1.5)),
                "misses": 0,
                "last": now,
            }
        self.state["interest_stats"] = stats
        self.state["interest_algo"] = "saturate"
        # 迁移后立刻按新算法回写一次分数，页面无需等下一轮
        seeds = {w for w, _ in self._parse_interest_list(self.config.get("interest_topics"))}
        for name, st in stats.items():
            cap = 5.0 if name in seeds else 3.0
            score = self._interest_score(st["hits"], st["tries"], st["last"], 0)
            interests[name] = round(min(cap, max(0.7 if name not in seeds else 2.0, score)), 3)
        logger.info(f"[bili] 兴趣算法换成饱和曲线，已迁移 {len(stats)} 项")

    def _drift_interests(self, note: dict, keyword: str = "") -> None:
        """按命中率重算兴趣分数（饱和曲线）。

        种子兴趣（interest_topics）上限 5.0 且保底 2.0；
        笔记标签长出来的（extra_interests）上限 3.0，跑题多了会掉出池子。
        """
        if not self.config.get("auto_expand_interest", True):
            return
        import time as _time

        interests: Dict[str, float] = self.state.setdefault("interests", {})
        self._migrate_interest_stats(interests)
        stats: Dict[str, dict] = self.state.setdefault("interest_stats", {})

        seed_cfg: Dict[str, float] = {
            n: (float(w) if w is not None else 2.0)
            for n, w in self._parse_interest_list(
                self.config.get("interest_topics") or ["动漫", "音乐"]
            )
        }
        seeds = set(seed_cfg)
        extra_cfg: Dict[str, float] = {
            n: (float(w) if w is not None else 1.0)
            for n, w in self._parse_interest_list(self.config.get("extra_interests"))
        }

        now = _time.time()
        tags: List[str] = []
        for t in (note.get("tags") or [])[:6]:
            t = str(t).strip()
            if t and len(t) <= 12:
                tags.append(t)
        relevant = note.get("relevant", True) is not False
        kw = (keyword or "").strip().lower()

        def _rel(a: str, b: str) -> bool:
            a, b = (a or "").lower(), (b or "").lower()
            return bool(a and b) and (a in b or b in a)

        # 本轮反馈：命中标签算命中（跑题只算机会），本轮关键词用过但没命中也算机会
        for name in set(list(stats.keys()) + list(interests.keys())):
            name = str(name)
            st = stats.setdefault(name, {"hits": 0, "tries": 0, "last": 0.0})
            hit = any(_rel(name, t) for t in tags)
            used = bool(kw) and _rel(name, kw)
            if hit:
                st["tries"] = int(st.get("tries", 0)) + 1
                st["last"] = now
                if relevant:
                    st["hits"] = int(st.get("hits", 0)) + 1
                    st["misses"] = max(0, int(st.get("misses", 0) or 0) * 8 // 10)
                else:
                    st["misses"] = int(st.get("misses", 0) or 0) + 1
            elif used:
                st["tries"] = int(st.get("tries", 0)) + 1
                st["last"] = now
                if not relevant:
                    st["misses"] = int(st.get("misses", 0) or 0) + 1

        # 新标签先用 tag_hits 攒出现次数，够次数才进池
        hits_map: Dict[str, int] = self.state.setdefault("tag_hits", {})
        min_hits = int(self.config.get("extra_interest_min_hits", 3) or 3)
        for tag in tags:
            if tag in seeds:
                continue
            if tag in stats or tag in interests:
                continue
            hits_map[tag] = int(hits_map.get(tag, 0) or 0) + 1
            if hits_map[tag] < min_hits:
                continue
            logger.info(f"[bili] 标签「{tag}」出现 {hits_map[tag]} 次，收进兴趣拓展")
            stats[tag] = {"hits": 1, "tries": 1, "misses": 0, "last": now}

        # 重算分数
        for name, st in list(stats.items()):
            cap = 5.0 if name in seeds else max(3.0, float(extra_cfg.get(name, 0.0) or 0.0))
            floor = (
                max(2.0, float(seed_cfg.get(name, 0.0) or 0.0))
                if name in seeds
                else float(extra_cfg.get(name, 0.0) or 0.0)
            )
            score = self._interest_score(
                st.get("hits"), st.get("tries"), st.get("last"), st.get("misses", 0)
            )
            score = min(cap, max(floor, score))
            if score < 0.7 and name not in seeds:
                stats.pop(name, None)
                interests.pop(name, None)
            else:
                interests[name] = round(score, 2)

        if len(hits_map) > 80:
            for k in sorted(hits_map, key=lambda x: hits_map[x])[: len(hits_map) - 60]:
                hits_map.pop(k, None)
        if len(stats) > 120:
            for k in sorted(
                (k for k in stats if k not in seeds),
                key=lambda x: interests.get(x, 0),
            )[: len(stats) - 100]:
                stats.pop(k, None)
                interests.pop(k, None)
        self._save_state()
        self._save_interests_to_config()

    # ------------------------------------------------------------------ LLM 工具

    @filter.regex(r"^\s*(?:https?://\S*bilibili\.com/\S*|BV[0-9A-Za-z]{10})\s*$")
    async def _auto_bv(self, event: AstrMessageEvent):
        """主人直接甩过来一个 BV 号或视频链接，就当他让我看这一条。"""
        if not self._is_admin(event):
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        async for res in self._cmd_learn(event, text):
            yield res

    @filter.llm_tool(name="bili_learn")
    async def _llm_learn(self, event: AstrMessageEvent, topic: str = "") -> str:
        """去B站看一个视频学习，看完了会写笔记并记进长期记忆。
        Args:
            topic(string): 想学的主题或关键词；如果主人直接甩了 BV 号（BV1xx411c7mD）或视频链接，就把那串原样放进来；留空就按自己的兴趣自动挑
        """
        if not self._is_admin(event):
            return "只有主人能让我自己刷B站。"
        if not self.api.logged_in:
            return "我还没登录B站，让主人发 /bili 登录 扫个码。"
        if self._busy:
            return "我这轮还没看完呢，等这一轮结束再喊我（大概一两分钟）。"
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
        except Exception:
            provider = None
        if provider is None:
            return "现在没有可用模型，学不了。"
        learner = Learner(self.api, dict(self.config), self.state)
        bv = re.search(r"(BV[0-9A-Za-z]{10})", topic or "", re.I)
        self._busy = True
        try:
            if bv:
                result = await learner.process_bvid(
                    provider,
                    bv.group(1),
                    event.unified_msg_origin,
                    self.notes_dir / "notes",
                    write_note=True,
                    user_picked=True,
                )
            else:
                result = await learner.run_once(
                    provider, event.unified_msg_origin, self.notes_dir / "notes"
                )
        finally:
            self._busy = False
        self._save_state()
        if not result.get("ok"):
            return f"没学成：{result.get('error')}"
        usage = self._usage_today()
        usage["videos"] = int(usage.get("videos", 0)) + 1
        raw_usage = result.get("usage") or {}
        if isinstance(raw_usage, dict):
            usage["tokens"] = int(usage.get("tokens", 0)) + int(
                (raw_usage.get("total_tokens") or 0)
                or ((raw_usage.get("prompt_tokens") or 0) + (raw_usage.get("completion_tokens") or 0))
            )
        self._save_state()
        self._log(f"通过对话学完《{result.get('title')}》")
        await self._push_memory(result, event.unified_msg_origin)
        worth = self._worth_actions(
            (result.get("pack") or {}).get("info") or {}, result.get("note") or {}
        )
        acts = await self._do_actions(result, worth)
        dm_line = await self._share_video_dm(result, worth)
        self._drift_interests(
            result.get("note") or {},
            getattr(self.learner, "last_keyword", "") or "",
        )
        self._archive_notes()
        self._rebuild_index()
        note = result.get("note") or {}
        tail = f"\n（顺手{'、'.join(acts)}了）" if acts else ""
        if dm_line:
            tail += f"\n（{dm_line}）"
        return (
            f"看完了《{result.get('title')}》（UP主 {result.get('owner')}）。\n"
            f"{note.get('share_text') or note.get('summary') or ''}{tail}"
        )

    @filter.llm_tool(name="bili_notes")
    async def _llm_notes(
        self, event: AstrMessageEvent, query: str = "", full: bool = False
    ) -> str:
        """在自己写过的B站学习笔记里检索，回想以前看过什么。
        Args:
            query(string): 检索关键词（歌名/番名/UP主/任意词）；主人说「第15条」这类就填数字 15；留空列出带序号的笔记清单
            full(boolean): 主人要你「讲讲/念/复述/说说」某篇笔记的内容时，填 true，才能拿到整篇正文
        """
        return self._cmd_notes(query, full=full)

    @filter.llm_tool(name="bili_daily")
    async def _llm_daily(self, event: AstrMessageEvent, full: bool = False) -> str:
        """把今天在B站学习的情况汇总给主人看（今天看了哪些、记了几篇、花了多少 token）。
        Args:
            full(boolean): 他要是想看全部笔记（说「全部」「都要看」「全列出来」），填 true
        """
        # 直接发出去：一句话 + 图，模型经常会把清单压成「十三篇全在这了」，所以不交给它转述
        try:
            await self._send_daily(event, full=bool(full))
            return "（汇总的一句话和图片都已经直接发出去了，你不用再复述清单，最多补一句收尾）"
        except Exception as exc:
            logger.warning(f"[bili] 直接发汇总失败，改为返回文本: {exc!r}")
            return self._build_daily_report(limit=999 if full else 5)

    @filter.on_llm_request(priority=9998)
    async def _nudge_notes_full(self, event: AstrMessageEvent, request) -> None:
        """主人要她讲/念/复述笔记内容时，逼模型这一轮用 bili_notes(full=true) 拿正文。"""
        try:
            text = (event.message_str or "").strip()
        except Exception:
            return
        if not text or len(text) > 40:
            return
        _m_idx = re.search(r"第\s*[0-9]{1,3}\s*条", text)
        _wants = bool(re.search(r"(讲|念|复述|说说|read|背|看看|哪条|哪篇)", text))
        if not _m_idx and not (_wants and "笔记" in text):
            return
        sysp = getattr(request, "system_prompt", "") or ""
        if "bili_notes" in sysp and "full" in sysp:
            return
        if _m_idx:
            _hint = (
                "\n\n【这一轮必须调用工具】主人说的「" + _m_idx.group(0) + "」指今天笔记列表里的第 "
                + re.sub(r"[^0-9]", "", _m_idx.group(0)) + " 条。"
                "请调用 bili_notes（query 填那个数字、full 设成 true）拿到整篇正文，"
                "再照着正文自然地讲给他听；不要说自己讲不了、不要让他发原件、不要只念标题。"
            )
        else:
            _hint = (
                "\n\n【这一轮必须调用工具】主人要你讲某篇笔记的内容。"
                "请调用 bili_notes 工具（query 填歌名/番名等关键词，full 设成 true）拿到正文，"
                "再照着正文自然地讲给他听，不要说自己讲不了、也不要让他发原件。"
            )
        request.system_prompt = sysp + _hint

    @filter.on_llm_request(priority=9999)
    async def _nudge_daily(self, event: AstrMessageEvent, request) -> None:
        """主人问汇总/全部时，逼模型这一轮必须调 bili_daily。"""
        try:
            text = (event.message_str or "").strip()
        except Exception:
            return
        if not text or len(text) > 30:
            return
        if not re.search(
            r"(汇总|日报|今天.{0,4}(看|学)了|看了啥|学了啥|全部|都列|列出来|清单)", text
        ):
            return
        sysp = getattr(request, "system_prompt", "") or ""
        if "bili_daily" in sysp:
            return
        request.system_prompt = sysp + (
            "\n\n【这一轮必须调用工具】主人在问今天的学习汇总或笔记清单，"
            "请直接调用 bili_daily 工具（他说「全部」「都要看」就把 full 设成 true），"
            "把工具返回的内容原样发给他，不要自己编。"
        )

    # 以下是定时学习相关的方法

    def _windows(self) -> List[Tuple[datetime, datetime]]:
        raw = self.config.get("time_windows", "15:00-16:30,20:30-22:00")
        if isinstance(raw, list):
            raw = ",".join(str(x) for x in raw)
        now = datetime.now()
        out: List[Tuple[datetime, datetime]] = []
        for piece in re.split(r"[,，;；\s]+", str(raw)):
            if "-" not in piece:
                continue
            a, b = piece.split("-", 1)
            try:
                sh, sm = [int(x) for x in a.strip().split(":")]
                eh, em = [int(x) for x in b.strip().split(":")]
                s = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
                e = now.replace(hour=eh, minute=em, second=0, microsecond=0)
                if e <= s:
                    e += timedelta(days=1)
                out.append((s, e))
            except Exception:
                continue
        return out

    def _in_quiet_hours(self, now: datetime) -> bool:
        """免打扰时段内不主动学习，比如 23:30-08:00。"""
        raw = str(self.config.get("quiet_hours") or "").strip()
        if not raw or "-" not in raw:
            return False
        a, b = raw.split("-", 1)
        try:
            sh, sm = [int(x) for x in a.strip().split(":")]
            eh, em = [int(x) for x in b.strip().split(":")]
        except Exception:
            return False
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        if end <= start:
            end += timedelta(days=1)
        return start <= now < end

    # -------------------------------------------------------- 自检 / 风控 / 索引
    async def _daily_login_check(self) -> None:
        """每天查一次登录态，过期就私聊提醒主人。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.get("login_check_date") == today:
            return
        self.state["login_check_date"] = today
        try:
            info = await self.api.nav()
        except Exception as exc:
            logger.warning(f"[bili] 登录态自检失败: {exc!r}")
            self._save_state()
            return
        ok = bool(info.get("is_login"))
        was_ok = bool(self.state.get("login_ok", True))
        self.state["login_ok"] = ok
        self._save_state()
        if ok:
            self._log("登录态自检：正常")
            if not was_ok:
                self.state["login_warned"] = False
                self._save_state()
            return
        self._log("登录态自检：过期了，需要重新扫码")
        umo = str(self.state.get("share_umo") or "").strip()
        if umo and self.config.get("share_qq", True) and not self.state.get("login_warned"):
            self.state["login_warned"] = True
            self._save_state()
            try:
                await self._notify(
                    umo,
                    "🐟 B站小号的登录过期了，我现在翻不了视频、也点不了赞。\n"
                    "有空发 /bili 登录 扫个码就行。",
                )
            except Exception as exc:
                logger.warning(f"[bili] 登录过期提醒发送失败: {exc!r}")
    async def _risk_notice(self) -> None:
        """撞风控时只提醒一次，别每分钟刷屏。"""
        if not self.api.in_cooldown:
            return
        key = f"{self.api.risk_code}@{int(self.api.risk_until // 600)}"
        if self.state.get("risk_notified") == key:
            return
        self.state["risk_notified"] = key
        self._save_state()
        until = datetime.fromtimestamp(self.api.risk_until).strftime("%H:%M")
        self._log(f"触发B站风控（码 {self.api.risk_code}），暂停到 {until}")
        umo = str(self.state.get("share_umo") or "").strip()
        if umo and self.config.get("share_qq", True):
            try:
                await self._notify(
                    umo,
                    f"🐟 刚撞上B站风控了（码 {self.api.risk_code}），我歇到 {until} 再继续，"
                    "这期间不学了也不动了。",
                )
            except Exception as exc:
                logger.warning(f"[bili] 风控提醒发送失败: {exc!r}")

    def _rebuild_index(self) -> int:
        """重写 <notes>/index.md：按标签分组 + 时间倒序。"""
        notes_dir = self.notes_dir / "notes"
        if not notes_dir.exists():
            return 0
        entries = []
        for p in notes_dir.glob("*.md"):
            if p.name == "index.md":
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            title = ""
            m = re.search(r"^#\s+(.+)$", text, re.M)
            if m:
                title = m.group(1).strip()
            link = ""
            m = re.search(r"^- 链接：(\S+)", text, re.M)
            if m:
                link = m.group(1)
            tags: List[str] = []
            m = re.search(r"^- 标签：(.+)$", text, re.M)
            if m and m.group(1).strip() not in ("（无）", ""):
                tags = [t.strip() for t in re.split(r"[、,，]", m.group(1)) if t.strip()]
            when = ""
            m = re.search(r"^- 记录时间：(.+)$", text, re.M)
            if m:
                when = m.group(1).strip()
            entries.append(
                {"file": p.name, "title": title or p.stem, "link": link, "tags": tags, "when": when}
            )
        entries.sort(key=lambda e: e["when"], reverse=True)
        by_tag: Dict[str, List[dict]] = {}
        for e in entries:
            for t in e["tags"] or ["未分类"]:
                by_tag.setdefault(t, []).append(e)
        lines = [f"# {self._bot_name()}的B站笔记索引", "", f"共 {len(entries)} 篇 · 更新于 {datetime.now():%Y-%m-%d %H:%M}", ""]
        lines += ["## 最新", ""]
        for e in entries[:20]:
            lines.append(f"- {e['when'][:10]} [《{e['title']}》]({e['link']})")
        lines += ["", "## 按标签", ""]
        for tag in sorted(by_tag, key=lambda t: -len(by_tag[t])):
            lines.append(f"### {tag}（{len(by_tag[tag])}）")
            for e in by_tag[tag][:15]:
                lines.append(f"- {e['when'][:10]} [《{e['title']}》]({e['link']})")
            lines.append("")
        try:
            (notes_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[bili] 写索引失败: {exc!r}")
            return 0
        return len(entries)

    def _cmd_index(self) -> str:
        n = self._rebuild_index()
        path = self.notes_dir / "notes" / "index.md"
        return f"📇 索引已更新：{n} 篇笔记\n{path}"
    async def _schedule_loop(self) -> None:
        await asyncio.sleep(25)
        while True:
            try:
                if self._superseded or not self._check_owner():
                    logger.info("[bili] 本实例已被新实例接管，调度循环退出")
                    return
                await self._tick()
            except Exception as exc:
                logger.warning(f"[bili] 调度异常: {exc!r}")
                try:
                    await self._notify_issue("tick", f"调度出了点问题：{exc}")
                except Exception:
                    pass
            await asyncio.sleep(60)
    async def _tick(self) -> None:
        if not self.config.get("enabled", True) or self.state.get("paused"):
            return
        await self._daily_login_check()
        if self.api.in_cooldown:
            await self._risk_notice()
            return
        if not self.api.logged_in:
            return
        now = datetime.now()
        if self._in_quiet_hours(now):
            return
        windows = self._windows()
        if not windows:
            return
        day_key = now.strftime("%Y-%m-%d")
        plan: Dict[str, Dict[str, Any]] = self.state.setdefault("plan", {})
        day: Dict[str, Any] = plan.setdefault(day_key, {})
        # 只留最近 7 天的计划，避免 state 越来越大
        for old in sorted(plan.keys())[:-7]:
            plan.pop(old, None)
        budget = int(self.config.get("daily_token_budget", 150000) or 0)
        if budget and int(self._usage_today().get("tokens", 0)) >= budget:
            return
        # 周报：周六/周日晚上 21 点后发一次，每周只发一次
        if self.config.get("weekly_report", True):
            iso = now.isocalendar()
            week_key = f"{iso[0]}-W{iso[1]}"
            if (
                now.weekday() >= 5
                and 21 <= now.hour < 24
                and self.state.get("weekly_sent") != week_key
            ):
                self.state["weekly_sent"] = week_key
                self._save_state()
                share_umo = str(self.state.get("share_umo") or "").strip()
                if share_umo:
                    try:
                        await self._notify(share_umo, self._cmd_weekly())
                        self._log("发出本周学习小结")
                    except Exception as exc:
                        logger.warning(f"[bili] 周报发送失败: {exc!r}")
        runs_per_day = int(self.config.get("runs_per_day", 2) or 2)
        runs_today = int(day.get("runs", 0) or 0)
        min_gap = int(self.config.get("min_gap_minutes", 45) or 0)
        for idx, (s, e) in enumerate(windows):
            key = f"w{idx}"
            if not (s <= now < e):
                continue
            if runs_today >= runs_per_day:
                return
            if day.get(key + "_done"):
                # 配额被调大了就允许这个时段继续排，否则跳过
                if runs_today < runs_per_day:
                    day.pop(key + "_done", None)
                    day.pop(key, None)
                    self._save_state()
                else:
                    continue
            due = day.get(key)
            if due is None:
                # 到点就开始：窗口一开立刻跑这一轮，不再随机延后
                due = now.timestamp()
                day[key] = due
                self._save_state()
                self._log(f"窗口到了，马上开始第 {runs_today + 1} 次学习")
            if now.timestamp() < float(due):
                continue
            last = int(self.state.get("last_run_ts", 0) or 0)
            if min_gap and last and (time.time() - last) < min_gap * 60:
                wait = min_gap * 60 - (time.time() - last) + 30
                nxt = now + timedelta(seconds=wait)
                if nxt < e:
                    day[key] = nxt.timestamp()
                    self._log("和上一轮离得太近，顺延到后面")
                else:
                    day[key] = 0
                    day[key + "_done"] = True
                    self._log("这个时段排不下了，换下一个时段")
                self._save_state()
                continue
            day.pop(key, None)
            day["runs"] = int(day.get("runs", 0) or 0) + 1
            runs_today = day["runs"]
            self._save_state()
            await self._auto_learn()
            if runs_today < runs_per_day:
                gap = max(min_gap, 1)
                nxt = datetime.now() + timedelta(minutes=gap)
                if nxt < e:
                    day[key] = nxt.timestamp()
                    self._log(
                        f"这个时段还有空，预约第 {runs_today + 1} 次学习：{gap} 分钟后"
                    )
                else:
                    day[key] = 0
                    day[key + "_done"] = True
            else:
                day[key + "_done"] = True
            self._save_state()
            return
    async def _auto_learn(self) -> None:
        """到点自主学习一次（同一时间只跑一轮，避免转写把机器堆满）。"""
        if self._busy:
            self._log("上一轮还在跑，这轮先跳过")
            return
        self._busy = True
        try:
            await self._auto_learn_inner()
        finally:
            self._busy = False

    async def _auto_learn_inner(self) -> None:
        """到点自主学习一次，然后把收获发给最近聊过的人。"""
        share_umo = str(self.state.get("share_umo") or "").strip()
        try:
            provider = self.context.get_using_provider(share_umo or None)
        except Exception as exc:
            logger.warning(f"[bili] 定时任务取模型失败: {exc!r}")
            return
        if provider is None:
            self._log("定时学习跳过：没有可用模型")
            return
        if self.config.get("dry_run", True):
            self._log("干跑模式：本轮只记录，不点赞不收藏")
        learner = Learner(self.api, dict(self.config), self.state)
        result = await learner.run_once(provider, share_umo or "scheduler", self.notes_dir / "notes")
        self._save_state()
        if not result.get("ok"):
            self._log(f"定时学习失败：{result.get('error')}")
            await self._notify_issue("learn", f"这轮学习没成：{result.get('error')}")
            return
        usage = self._usage_today()
        usage["videos"] = int(usage.get("videos", 0)) + 1
        raw_usage = result.get("usage") or {}
        if isinstance(raw_usage, dict):
            usage["tokens"] = int(usage.get("tokens", 0)) + int(
                (raw_usage.get("total_tokens") or 0)
                or ((raw_usage.get("prompt_tokens") or 0) + (raw_usage.get("completion_tokens") or 0))
            )
        self.state["last_round"] = {
            "title": str(result.get("title") or "")[:80],
            "tokens": int(
                (raw_usage.get("total_tokens") or 0)
                or ((raw_usage.get("prompt_tokens") or 0) + (raw_usage.get("completion_tokens") or 0))
            ) if isinstance(raw_usage, dict) else 0,
            "ts": int(time.time()),
        }
        try:
            self.state["last_round"]["cost"] = round(
                self._cost_estimate(int(self.state["last_round"]["tokens"])), 4)
        except Exception:
            self.state["last_round"]["cost"] = 0.0
        self._save_state()
        self._log(f"自主学习看完《{result.get('title')}》，笔记 {result.get('path')}")
        await self._push_memory(result, share_umo or "scheduler")
        note = result.get("note") or {}
        self._drift_interests(note)
        self._archive_notes()
        self._rebuild_index()
        worth = self._worth_actions(
            (result.get("pack") or {}).get("info") or {}, result.get("note") or {}
        )
        acts = await self._do_actions(result, worth)
        dm_line = await self._share_video_dm(result, worth)
        if not share_umo:
            self._log("还没有可以汇报的会话（先私聊我说句话）")
            return
        text = note.get("share_text") or note.get("summary") or ""
        msg = f"我刚才自己看了个视频：《{result.get('title')}》\n\n{text}"
        if acts:
            msg += f"\n\n（顺手{'、'.join(acts)}了）"
        if dm_line:
            msg += f"\n（{dm_line}）"
        if self.config.get("ask_in_report", True):
            msg += "\n\n要我给你细讲哪一点？"
        mode = str(self.config.get("report_mode", "daily") or "daily").lower()
        if not self.config.get("share_qq", True):
            self._log("QQ汇报开关是关的，这轮不推文字")
        elif mode == "instant":
            await self._notify(share_umo, msg)
        else:
            self._log("日报模式：这轮不单独推，攒到晚上一起说")
        if mode == "instant":
            await self._voice_report(text, share_umo)
    async def terminate(self) -> None:
        """插件卸载/重载时停掉所有后台协程，避免旧实例的循环还在跑。"""
        for name in ("_report_task", "_whisper_task", "_notes_task", "_loop_task", "_panel_web_task"):
            task = getattr(self, name, None)
            if task is None:
                continue
            try:
                if not task.done():
                    task.cancel()
                    await task
            except (asyncio.CancelledError, Exception):
                pass
            setattr(self, name, None)
        try:
            await self._stop_panel_web()
        except Exception:
            pass
        for task in list(self._poll_tasks):
            task.cancel()
        self._poll_tasks.clear()
        logger.info("[bili] 插件已卸载")
