"""
B站接口封装（第一阶段：扫码登录 + cookie 管理 + 基础查询）。
设计原则：
- 只用官方 Web 接口，不做任何浏览器自动化；
- cookie 落盘到本地私密目录，插件重启后自动恢复；
- 所有请求带浏览器 UA / Referer，避免被简单风控拦掉。
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse
try:
    import httpx
except Exception:  # 依赖没装时先加载插件，自动安装完成后即可用
    httpx = None
try:  # 允许脱离 AstrBot 单独 import（测试用）
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger("bili_api")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BASE_HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
COOKIE_KEYS = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "buvid3",
    "sid",
    "buvid4",
)
PASSPORT_QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
PASSPORT_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
SPI = "https://api.bilibili.com/x/frontend/finger/spi"
SEARCH = "https://api.bilibili.com/x/web-interface/search/type"
SEARCH_WBI = "https://api.bilibili.com/x/web-interface/wbi/search/type"
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYER = "https://api.bilibili.com/x/player/v2"
API_REPLY = "https://api.bilibili.com/x/v2/reply"
# wbi 签名用的乱序表（B站前端固定值）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

class BiliAPI:
    """极薄的 B站 Web 接口客户端。"""

    def __init__(self, cookie_path: str | Path):
        self.cookie_path = Path(cookie_path)
        self.cookies: Dict[str, str] = {}
        self.login_time: Optional[int] = None
        self.uname: str = ""
        self.mid: str = ""
        self._wbi_key: str = ""
        self._wbi_ts: float = 0.0
        self.risk_until: float = 0.0
        self.risk_code: Optional[int] = None

    # ------------------------------------------------------------------ cookie

    def load(self) -> bool:
        """从磁盘恢复 cookie，返回是否已登录。"""
        try:
            if not self.cookie_path.exists():
                return False
            data = json.loads(self.cookie_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        self.cookies = {k: str(v) for k, v in (data.get("cookies") or {}).items() if v}
        self.login_time = data.get("login_time")
        self.uname = data.get("uname") or ""
        self.mid = str(data.get("mid") or "")
        return bool(self.cookies.get("SESSDATA"))

    def save(self) -> None:
        """落盘 cookie。注意：绝不能用内存里的残缺 cookie 覆盖掉磁盘上已有的登录态
        （曾经因为 ensure_buvid 在未 load 的实例上保存，把 SESSDATA 冲掉过）。"""
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        old: dict = {}
        try:
            if self.cookie_path.exists():
                data = json.loads(self.cookie_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("cookies"), dict):
                    old = {k: str(v) for k, v in data["cookies"].items() if v}
        except Exception:
            old = {}
        merged = dict(old)
        merged.update({k: v for k, v in self.cookies.items() if v})
        if old.get("SESSDATA") and not self.cookies.get("SESSDATA"):
            merged["SESSDATA"] = old["SESSDATA"]
            if old.get("bili_jct") and not self.cookies.get("bili_jct"):
                merged["bili_jct"] = old["bili_jct"]
        if not self.uname and isinstance(old, dict):
            pass
        payload = {
            "cookies": merged,
            "login_time": self.login_time or int(time.time()),
            "uname": self.uname,
            "mid": self.mid,
        }
        self.cookie_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def clear(self) -> None:
        self.cookies = {}
        self.uname = ""
        self.mid = ""
        self.login_time = None
        try:
            if self.cookie_path.exists():
                self.cookie_path.unlink()
        except Exception:
            pass
    @property
    def logged_in(self) -> bool:
        return bool(self.cookies.get("SESSDATA"))
    @property
    def csrf(self) -> str:
        return self.cookies.get("bili_jct", "")

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)

    # -------------------------------------------------------------------- http

    def _headers(self) -> Dict[str, str]:
        h = dict(BASE_HEADERS)
        if self.cookies:
            h["Cookie"] = self.cookie_header()
        return h
    async def get_json(
        self, url: str, params: Optional[dict] = None, timeout: float = 15.0
    ) -> dict:
        async with httpx.AsyncClient(
            headers=self._headers(), timeout=timeout, follow_redirects=True
        ) as client:
            resp = await client.get(url, params=params)
            try:
                data = resp.json()
            except Exception:
                data = {"code": -1, "message": f"非JSON响应 HTTP {resp.status_code}"}
            if not isinstance(data, dict):
                data = {"code": -1, "message": "响应格式异常"}
            self._watch_risk(data.get("code"), data.get("message") or "")
            return data

    # ------------------------------------------------------------------- 风控
    RISK_CODES = {-352, -412, -509, -799, 412}

    def _watch_risk(self, code, message: str = "") -> None:
        """撞到B站风控码就进一小时的冷却，避免硬顶着刷。"""
        try:
            code_i = int(code)
        except Exception:
            return
        if code_i not in self.RISK_CODES:
            return
        self.risk_until = time.time() + 3600
        self.risk_code = code_i
        logger.warning(
            f"[bili] B站风控码 {code_i}（{message or '请求被限制'}），暂停一小时不再请求"
        )
    @property
    def in_cooldown(self) -> bool:
        return time.time() < float(getattr(self, "risk_until", 0) or 0)

    def cooldown_text(self) -> str:
        rest = max(0.0, float(getattr(self, "risk_until", 0) or 0) - time.time())
        left = max(1, int((rest + 59) // 60))
        code = getattr(self, "risk_code", None)
        return f"B站风控冷却中（{left} 分钟后恢复，上次触发码 {code}）"

    # ------------------------------------------------------------------- login
    async def qr_generate(self) -> dict:
        """取一张登录二维码，返回 {url, qrcode_key}。"""
        data = await self.get_json(PASSPORT_QR_GENERATE)
        d = data.get("data") or {}
        if data.get("code") != 0 or not d.get("url"):
            raise RuntimeError(f"获取二维码失败: {data.get('message') or data}")
        return {"url": d["url"], "qrcode_key": d["qrcode_key"]}
    async def qr_poll(self, qrcode_key: str) -> dict:
        """轮询扫码状态，成功时把 cookie 一并返回。"""
        async with httpx.AsyncClient(
            headers=BASE_HEADERS, timeout=15.0, follow_redirects=True
        ) as client:
            resp = await client.get(PASSPORT_QR_POLL, params={"qrcode_key": qrcode_key})
            try:
                body = resp.json()
            except Exception:
                return {"code": -1, "message": "扫码状态查询返回非JSON"}
        data = body.get("data") or {}
        code = data.get("code")
        cookies: Dict[str, str] = {}
        for k, v in resp.cookies.items():
            if k in COOKIE_KEYS:
                cookies[k] = v
        # 兜底：登录成功时 data.url 的 query 里也带着关键 cookie
        url = data.get("url") or ""
        if url:
            qs = parse_qs(urlparse(url).query)
            for k in COOKIE_KEYS:
                if k in qs and k not in cookies:
                    cookies[k] = qs[k][0]
        return {
            "code": code,
            "message": data.get("message") or body.get("message") or "",
            "cookies": cookies,
            "raw": body,
        }
    async def nav(self) -> dict:
        """当前登录状态。"""
        data = await self.get_json(API_NAV)
        d = data.get("data") or {}
        return {
            "code": data.get("code"),
            "is_login": bool(d.get("isLogin")),
            "uname": d.get("uname") or "",
            "mid": str(d.get("mid") or ""),
            "vip": d.get("vipStatus"),
            "message": data.get("message") or "",
        }
    async def login_with_cookies(self, cookies: Dict[str, str]) -> dict:
        """写入 cookie 并校验，成功则落盘。"""
        merged = dict(self.cookies)
        merged.update({k: v for k, v in cookies.items() if v})
        self.cookies = merged
        info = await self.nav()
        if info.get("is_login"):
            self.uname = info.get("uname", "")
            self.mid = info.get("mid", "")
            self.login_time = int(time.time())
            self.save()
        return info
    # ================================================================ 内容读取
    async def ensure_buvid(self) -> None:
        """没有 buvid 时补一个，部分接口需要它才不 412。"""
        if self.cookies.get("buvid3"):
            return
        try:
            data = await self.get_json(SPI)
        except Exception:
            return
        d = data.get("data") or {}
        if d.get("b_3"):
            self.cookies["buvid3"] = d["b_3"]
        if d.get("b_4"):
            self.cookies["buvid4"] = d["b_4"]
        if d.get("b_3"):
            self.save()
    async def _wbi_mixin_key(self) -> str:
        """取 wbi 签名用的 mixinKey（缓存 10 分钟）。"""
        now = time.time()
        if self._wbi_key and now - self._wbi_ts < 600:
            return self._wbi_key
        data = await self.get_json(API_NAV)
        wbi = ((data.get("data") or {}).get("wbi_img")) or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        raw = img + sub
        if len(raw) < 64:
            return ""
        self._wbi_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        self._wbi_ts = now
        return self._wbi_key
    async def signed_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """给参数加上 wts / w_rid。"""
        from hashlib import md5
        from urllib.parse import urlencode
        mixin = await self._wbi_mixin_key()
        if not mixin:
            return params
        p = dict(params)
        p["wts"] = int(time.time())
        query = urlencode(sorted(p.items()))
        p["w_rid"] = md5((query + mixin).encode()).hexdigest()
        return p

    @staticmethod
    def clean_text(text: str) -> str:
        """去掉搜索接口塞进来的高亮标签和转义。"""
        import html
        import re
        if not text:
            return ""
        t = re.sub(r"</?em[^>]*>", "", str(text))
        t = html.unescape(t)
        return t.strip()
    async def search_videos(
        self, keyword: str, page: int = 1, order: str = "totalrank", duration: int = 0
    ) -> list[Dict[str, Any]]:
        """搜索视频，返回精简后的列表。412 时自动换 wbi 接口重试。
        duration: 0 全部 / 1 (0-10分) / 2 (10-30分) / 3 (30-60分) / 4 (60分以上)
        """
        await self.ensure_buvid()
        params: Dict[str, Any] = {"search_type": "video", "keyword": keyword, "page": page}
        if order and order != "totalrank":
            params["order"] = order
        if duration:
            params["duration"] = duration
        data: Dict[str, Any] = {}
        for url, use_wbi in ((SEARCH, False), (SEARCH_WBI, True)):
            try:
                p = await self.signed_params(params) if use_wbi else params
                data = await self.get_json(url, p)
            except Exception as exc:
                logger.debug(f"[bili] 搜索异常 {url}: {exc!r}")
                continue
            if data.get("code") == 0 and (data.get("data") or {}).get("result"):
                break
        items = []
        for it in ((data.get("data") or {}).get("result") or []):
            if it.get("type") not in (None, "video"):
                continue
            items.append(
                {
                    "bvid": it.get("bvid"),
                    "aid": it.get("aid") or it.get("id"),
                    "title": self.clean_text(it.get("title")),
                    "author": self.clean_text(it.get("author")),
                    "mid": it.get("mid"),
                    "duration": it.get("duration") or "",
                    "play": it.get("play") or 0,
                    "danmaku": it.get("video_review") or 0,
                    "pubdate": it.get("pubdate") or 0,
                    "desc": self.clean_text(it.get("description"))[:200],
                    "tag": self.clean_text(it.get("tag")),
                }
            )
        return items
    async def view(self, bvid: str) -> Dict[str, Any]:
        """视频详情（标题、UP、分P、简介）。"""
        data = await self.get_json(API_VIEW, {"bvid": bvid})
        d = data.get("data") or {}
        if data.get("code") != 0:
            return {"ok": False, "message": data.get("message") or "获取视频信息失败"}
        pages = [
            {"cid": p.get("cid"), "page": p.get("page"), "part": p.get("part"),
             "duration": p.get("duration")}
            for p in (d.get("pages") or [])
        ]
        season = d.get("ugc_season") or {}
        season_info = None
        if season:
            eps = []
            for sec in season.get("sections") or []:
                for ep in sec.get("episodes") or []:
                    eps.append(
                        {
                            "bvid": ep.get("bvid"),
                            "aid": ep.get("aid"),
                            "title": ep.get("title")
                            or (ep.get("arc") or {}).get("title")
                            or "",
                        }
                    )
            season_info = {
                "id": season.get("id"),
                "title": season.get("title"),
                "episodes": eps,
            }
        return {
            "ok": True,
            "bvid": bvid,
            "aid": d.get("aid"),
            "cid": d.get("cid"),
            "title": d.get("title"),
            "desc": (d.get("desc") or "").strip(),
            "owner": (d.get("owner") or {}).get("name"),
            "owner_mid": (d.get("owner") or {}).get("mid"),
            "duration": d.get("duration"),
            "view": (d.get("stat") or {}).get("view"),
            "like": (d.get("stat") or {}).get("like"),
            "coin": (d.get("stat") or {}).get("coin"),
            "favorite": (d.get("stat") or {}).get("favorite"),
            "pubdate": d.get("pubdate"),
            "tname": d.get("tname"),
            "pages": pages,
            "season": season_info,
        }
    async def subtitle_text(self, bvid: str, cid: int) -> str:
        """取字幕文本（没有 CC 字幕时返回空串）。"""
        try:
            data = await self.get_json(API_PLAYER, {"bvid": bvid, "cid": cid})
        except Exception:
            return ""
        subs = (((data.get("data") or {}).get("subtitle") or {}).get("subtitles")) or []
        if not subs:
            return ""
        url = subs[0].get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return ""
        try:
            payload = await self.get_json(url)
        except Exception:
            return ""
        body = (payload.get("body") or []) if isinstance(payload, dict) else []
        lines = [str(x.get("content") or "").strip() for x in body]
        return "\n".join([x for x in lines if x])
    async def audio_urls(self, bvid: str, cid: int) -> List[str]:
        """取音频流地址列表（含备用 CDN，用于没有字幕时做语音转文字）。

        B 站会返回一个 mcdn 的 P2P 地址作主地址，那台服务器在有些网络下连不上，
        所以这里把 backupUrl 一起带上，调用方按顺序试。
        """
        try:
            data = await self.get_json(
                "https://api.bilibili.com/x/player/playurl",
                {"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 0},
            )
        except Exception:
            return []
        d = data.get("data") or {}
        urls: List[str] = []
        audios = ((d.get("dash") or {}).get("audio")) or []
        if audios:
            base = audios[0].get("baseUrl") or audios[0].get("base_url") or ""
            if base:
                urls.append(base)
            for u in audios[0].get("backupUrl") or audios[0].get("backup_url") or []:
                if u and u not in urls:
                    urls.append(u)
        if not urls:
            durl = d.get("durl") or []
            for it in durl:
                u = it.get("url") or ""
                if u and u not in urls:
                    urls.append(u)
        return urls

    async def audio_url(self, bvid: str, cid: int) -> str:
        """取第一个音频流地址（兼容旧调用）。"""
        urls = await self.audio_urls(bvid, cid)
        return urls[0] if urls else ""
    async def send_dm(self, uid: int, text: str) -> Dict[str, Any]:
        """给指定 UID 发 B 站私信（纯文本）。"""
        try:
            from bilibili_api import Credential
            from bilibili_api.session import EventType, send_msg
        except Exception as exc:
            return {"ok": False, "error": f"缺少 bilibili_api：{exc!r}"}
        c = self.cookies
        cred = Credential(
            sessdata=c.get("SESSDATA", ""),
            bili_jct=c.get("bili_jct", ""),
            buvid3=c.get("buvid3", ""),
            dedeuserid=c.get("DedeUserID", ""),
        )
        try:
            res = await send_msg(cred, int(uid), EventType.TEXT, text)
        except Exception as exc:
            return {"ok": False, "error": f"{exc!r}"}
        code = (res or {}).get("code") if isinstance(res, dict) else None
        if code in (0, None):
            return {"ok": True, "raw": res}
        return {"ok": False, "error": str(res)}
    async def add_fav_folder(self, title: str, privacy: int = 0) -> Dict[str, Any]:
        """新建收藏夹，成功返回 {"ok": True, "id": ...}。"""
        if not str(title or "").strip():
            return {"ok": False, "error": "收藏夹名字是空的"}
        try:
            res = await self._post(
                "https://api.bilibili.com/x/v3/fav/folder/add",
                {"title": title, "privacy": privacy, "csrf": self.csrf},
            )
        except Exception as exc:
            return {"ok": False, "error": f"{exc!r}"}
        if res.get("code") == 0:
            return {"ok": True, "id": (res.get("data") or {}).get("id"), "raw": res}
        return {"ok": False, "error": res.get("message") or res.get("code")}
    async def send_video_dm(
        self,
        uid: int,
        bvid: str,
        title: str = "",
        owner: str = "",
        comment: str = "",
    ) -> Dict[str, Any]:
        """私信分享一个看过的视频：先说两句人话，再给标题和链接。"""
        if not bvid:
            return {"ok": False, "error": "没有 bvid"}
        head = (comment or "").strip()
        name = (title or bvid).strip()
        by = (owner or "").strip()
        lines = []
        if head:
            lines.append(head)
        lines.append(f"《{name}》" + (f"　{by}" if by else ""))
        lines.append(f"https://www.bilibili.com/video/{bvid}")
        text = "\n".join(lines)
        res = await self.send_dm(uid, text)
        res["text"] = text
        return res
    async def danmaku(self, cid: int, limit: int = 600) -> list[str]:
        """抓弹幕，返回文本列表（接口可能是 raw-deflate 也可能是明文）。"""
        import re
        import zlib
        url = f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}"
        try:
            async with httpx.AsyncClient(
                headers=self._headers(), timeout=20.0, follow_redirects=True
            ) as client:
                resp = await client.get(url)
                raw = resp.content
        except Exception:
            return []
        xml = ""
        for wbits in (-15, 47, 15, 31):
            try:
                xml = zlib.decompress(raw, wbits).decode("utf-8", "ignore")
                break
            except Exception:
                continue
        if not xml:
            xml = raw.decode("utf-8", "ignore")
        items = re.findall(r"<d [^>]*>(.*?)</d>", xml)
        out = []
        for t in items:
            t = t.strip()
            if t and len(t) <= 80:
                out.append(t)
            if len(out) >= limit:
                break
        return out
    async def comments(self, aid: int, top_n: int = 20) -> list[Dict[str, Any]]:
        """取热门评论（按点赞排序）。"""
        data = await self.get_json(
            API_REPLY, {"type": 1, "oid": aid, "sort": 1, "ps": min(30, top_n), "pn": 1}
        )
        reps = ((data.get("data") or {}).get("replies") or [])
        out = []
        for r in reps:
            out.append(
                {
                    "user": (r.get("member") or {}).get("uname"),
                    "like": r.get("like") or 0,
                    "content": ((r.get("content") or {}).get("message") or "")[:200],
                }
            )
        return out[:top_n]
    # ================================================================== 写操作
    async def like(self, aid: int, on: bool = True) -> Dict[str, Any]:
        # 注意：B 站的取消点赞是 like=2，传 0 会回 -400 请求错误
        data = await self._post(
            "https://api.bilibili.com/x/web-interface/archive/like",
            {"aid": aid, "like": 1 if on else 2, "csrf": self.csrf},
        )
        return data
    async def coin(self, aid: int, num: int = 1, also_like: bool = False) -> Dict[str, Any]:
        data = await self._post(
            "https://api.bilibili.com/x/web-interface/coin/add",
            {"aid": aid, "multiply": max(1, min(2, num)),
             "select_like": 1 if also_like else 0, "csrf": self.csrf},
        )
        return data
    async def fav(self, aid: int, folder_id: int, add: bool = True) -> Dict[str, Any]:
        payload = {"rid": aid, "type": 2, "add_media_ids": folder_id if add else "",
                   "del_media_ids": "" if add else folder_id, "csrf": self.csrf}
        data = await self._post("https://api.bilibili.com/x/v3/fav/resource/deal", payload)
        return data
    async def fav_folders(self) -> list[Dict[str, Any]]:
        data = await self.get_json(
            "https://api.bilibili.com/x/v3/fav/folder/created/list-all",
            {"up_mid": self.mid},
        )
        return ((data.get("data") or {}).get("list") or [])
    async def _post(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        # 写操作（点赞/投币/收藏）不带 buvid 时 B 站会回 -400 请求错误
        await self.ensure_buvid()
        async with httpx.AsyncClient(
            headers={**self._headers(), "Content-Type": "application/x-www-form-urlencoded"},
            timeout=20.0,
            follow_redirects=True,
        ) as client:
            resp = await client.post(url, data=payload)
            try:
                return resp.json()
            except Exception:
                return {"code": -1, "message": f"非JSON响应 HTTP {resp.status_code}"}
