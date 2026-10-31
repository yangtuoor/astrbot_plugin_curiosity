# -*- coding: utf-8 -*-
"""好奇心 —— 让 bot 自己上网翻新东西，看见有意思的就攒着，回头讲给你听。

工作方式：
    1. 定时（默认每 3 小时）挑一个方向，用搜索引擎真的把网页捞回来
    2. 把捞到的东西拆成「一条一条」，存进本地 SQLite，按 URL / 标题指纹去重
    3. 到点了（或者 bot 自己觉得攒够了）用自己的语气讲给主人听
    4. 也可以顺手发一段到 QQ 空间，配张图
    5. 讲过的标记 delivered，永远不会再讲第二遍

它不是搜索引擎的包装，是一个「会自己产生谈资」的东西。

支持的搜索后端：
    astrbot  —— 复用 AstrBot 自带的 web 搜索（provider 的 web_search 能力）
    qianfan  —— 百度千帆 AI 搜索，真的返回带链接的网页
    bocha    —— 博查 Web Search
    searxng  —— 自建 SearXNG（无 key，完全自主）
    custom   —— 任意兼容 JSON 接口，自己写请求模板和取值路径
    none     —— 不联网，直接问模型（会编，别用）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

PLUGIN_NAME = "astrbot_plugin_curiosity"

# 各家搜索接口的固定端点（custom 引擎不用这些）
QIANFAN_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
BOCHA_ENDPOINT = "https://api.bochaai.com/v1/web-search"

ENGINES = ("astrbot", "qianfan", "bocha", "searxng", "custom", "none")

# AstrBot 的 filter.command 只认 event.message_str（已被 wake_prefix 剥过的那份）。
# 但 wake_prefix 未必是 "/"，有人配成别的字符串，此时 "/讲讲" 永远命中不了指令，
# 会被当成普通聊天丢给 LLM。这里自己写一个过滤器兜底：剥掉 wake_prefix 之后，
# 再允许任意常见指令标记（/ ! 。 空格，或者干脆什么都不加）。
_CMD_STRIP = " \t\r\n/\uff0f!\uff01.\u3002\u3001,\uff0c:\uff1a~\uff5e"


def _strip_cmd(text: str, wake_prefixes: list[str] | None = None) -> str:
    """把消息前面的 wake_prefix 和常见指令标记都剥掉。"""
    s = (text or "").strip()
    changed = True
    while changed:
        changed = False
        for p in (wake_prefixes or []):
            if p and s.startswith(p):
                s = s[len(p):]
                changed = True
        s2 = s.lstrip(_CMD_STRIP)
        if s2 != s:
            s = s2
            changed = True
    return s.strip()


class _CmdFilter(filter.CustomFilter):
    """匹配 `讲讲` / `/讲讲` / `<前缀>讲讲` / `<前缀> /讲讲` 等各种写法。

    注意：AstrBot 的 `filter.custom_filter` 只把 kwargs 转给 `get_handler_or_create`，
    **不会**转给过滤器构造器。所以 names 不能走构造参数，必须做成类属性
    （见 `_cmd()`：每次动态创建子类并把 names 钉在类上）。
    """

    names: tuple = ()

    def _candidates(self, event: AstrMessageEvent) -> list[str]:
        raw = event.message_str or ""
        cands = [raw, _strip_cmd(raw, None)]
        try:
            wps = event.get_extra("_wake_prefixes")
        except Exception:  # noqa: BLE001
            wps = None
        if wps:
            cands.append(_strip_cmd(raw, wps))
        seen, out = set(), []
        for c in cands:
            c = (c or "").strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _match(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        """命中了返回 (剩余参数, 指令名)。"""
        for c in self._candidates(event):
            for name in self.names:
                if c == name:
                    return "", name
                if c.startswith(name + " ") or c.startswith(name + "\u3000"):
                    return c[len(name):].strip(), name
        return None

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        if not event.is_private_chat() and not event.is_at_or_wake_command:
            return False
        hit = self._match(event)
        if not hit:
            return False
        left, name = hit
        # 指令函数里用 event.get_extra("cmd_args") 取参数
        event.set_extra("cmd_args", left)
        event.set_extra("cmd_name", name)
        return True


def _cmd(*names: str):
    """注册一个宽匹配指令：动态子类，把 names 钉在类属性上。"""
    cls = type("_CmdFilter_" + "_".join(names), (_CmdFilter,), {"names": tuple(names)})
    return filter.custom_filter(cls)


SCHEMA = """
CREATE TABLE IF NOT EXISTS finds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fp          TEXT UNIQUE NOT NULL,   -- 指纹，去重用
    title       TEXT NOT NULL,
    gist        TEXT NOT NULL,          -- 一句话说清这是什么
    why         TEXT NOT NULL,          -- 为什么有意思
    url         TEXT,
    topic       TEXT,                   -- 哪个方向翻到的
    score       INTEGER DEFAULT 5,      -- 有趣程度 1-10
    created_at  REAL NOT NULL,
    delivered   INTEGER DEFAULT 0,
    delivered_at REAL,
    shared      INTEGER DEFAULT 0,      -- 是否已经发过 QQ 空间
    shared_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_delivered ON finds(delivered, created_at);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT,
    at         REAL NOT NULL,
    ok         INTEGER,
    got        INTEGER DEFAULT 0,
    fresh      INTEGER DEFAULT 0,       -- 去重后真正新增的
    elapsed    REAL,
    note       TEXT,
    engine     TEXT
);
CREATE TABLE IF NOT EXISTS prefs (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

DEFAULT_TOPICS = [
    "AI 大模型最近有什么新发布或新能力",
    "开源软件、开发工具最近有什么值得关注的新项目",
    "科技圈最近有什么有意思的技术突破",
    "有趣的冷知识、科学发现",
    "独立开发者、小团队做出的有意思的东西",
]

# 老库升级：给已存在的表补列（SQLite 没有 IF NOT EXISTS 的 ADD COLUMN）
_MIGRATIONS = [
    ("finds", "shared", "INTEGER DEFAULT 0"),
    ("finds", "shared_at", "REAL"),
    ("runs", "engine", "TEXT"),
]


def _now() -> float:
    return time.time()


def _log_prefix() -> str:
    return f"[{PLUGIN_NAME}]"


def _strip_md(text: str) -> str:
    """去掉 markdown 装饰，留下干净的话。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_STOPWORDS = (
    "发布", "推出", "宣布", "上线", "公开", "更新", "支持", "新增", "全新",
    "最新", "正式", "重磅", "首次", "首个", "全球", "业内", "官网", "报道",
    "模型", "版本", "系列", "功能", "能力", "性能", "消息", "据悉", "透露",
)


def _norm_title(title: str) -> str:
    """把标题压成去重用的骨架：丢标点、丢版本噪声、丢通用词。

    没有 URL 的来源（比如模型总结式搜索）去重只能靠标题。
    但同一件事两次搜索会被写成不同句子，
    直接比标题必然漏重，必须先洗成骨架。
    """
    t = (title or "").lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    for w in _STOPWORDS:
        t = t.replace(w, "")
    return t


def _fingerprint(title: str, url: str = "") -> str:
    base = (url or "").strip().lower()
    if not base:
        base = _norm_title(title)
        if not base:
            base = re.sub(r"\s+", "", (title or "").lower())
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


def _title_loose(title: str) -> str:
    """宽骨架：只留「字母数字汉字」，用于近似判重查询。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (title or "").lower())


# 硬专名：英文/数字型号串（Kimi K3.1、Genie 4、GPT-6 Astra、LFM3、V4.1 Flash…）
# 和带中文的机构+产品组合。这些是「同一件事」的真正锚点，
# 描述性词语（发布/支持/首次/最强）不是。
_ANCHOR_EN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.\-_ ]?[A-Za-z0-9]+)*")
_ANCHOR_CN_ORG = re.compile(
    r"(月之暗面|深度求索|谷歌|Google|Meta|英伟达|NVIDIA|OpenAI|Anthropic|"
    r"腾讯|阿里|字节|百度|智谱|MiniMax|Mistral|LiquidAI|微软|Amazon|Apple)"
)
# 太通用的英文词不算专名
_ANCHOR_STOP = {
    "ai", "the", "and", "for", "new", "pro", "max", "mini", "flash", "plus",
    "ultra", "light", "beta", "alpha", "api", "app", "web", "dev", "llm",
    # 单位、量纲、常见缩略语——出现频率高但没有辨识度，撞上不算重复
    "gb", "tb", "mb", "kb", "mhz", "ghz", "fps", "ms", "kw", "mah", "w",
    "b", "k", "m", "g", "t", "v", "x",
}

# 同一实体的不同写法，归一到同一个锚点，否则「Google」和「谷歌」撞不上
_ANCHOR_ALIAS = {
    "google": "谷歌", "googledeepmind": "谷歌", "deepmind": "谷歌",
    "gemini": "google gemini",
    "microsoft": "微软", "openai": "openai", "meta": "meta",
    "nvidia": "英伟达", "moonshot": "月之暗面", "deepseek": "深度求索",
}

# 这些锚点太泛，单独撞上不能证明是同一件事
# （谷歌昨天发 Gemini、今天发 Genie，都是「谷歌」但完全两码事）
_SWEEP_ANCHORS = {
    "谷歌", "openai", "meta", "英伟达", "微软", "月之暗面", "深度求索",
    "腾讯", "阿里", "字节", "百度", "智谱", "minimax", "mistral", "liquidai",
    "amazon", "apple", "google gemini",
}


def _anchors(title: str, gist: str = "") -> set[str]:
    """抽出「硬专名」集合，用于跨标题判断是不是同一件事。

    注意锚点必须**够硬**：单位（GB）、通用技术词（AI、API）、
    泛化词（发布、支持）都不能算，否则会大面积误判。

    **只看标题，不看 gist。** gist 里经常拿别的产品做对比
    （「超越 GPT-6 Astra」「仅次于 Claude Opus」），
    那些名字属于别人，拿进来会把不相干的两条判成重复。
    gist 参数保留只为调用方签名兼容。
    """
    text = title or ""
    out: set[str] = set()

    for m in _ANCHOR_EN.finditer(text):
        tok = m.group(0).strip(" .-_").lower()
        if len(tok) < 2:
            continue
        if tok in _ANCHOR_STOP:
            continue
        # 纯数字（年份、金额、百分比）不是专名
        if re.fullmatch(r"[0-9.\-_ ]+", tok):
            continue
        # 去掉量纲尾巴：1GB / 30亿 里的单位不参与
        tok = re.sub(r"\b\d+\s*(gb|tb|mb|kb|mhz|ghz)\b", "", tok).strip()
        if not tok or tok in _ANCHOR_STOP:
            continue

        # 关键：把空格分隔的多词串拆成单锚点并逐一收录。
        # 「DeepMind Genie 4」必须同时产出 deepmind / genie 4，
        # 否则它和「Genie 4」完全撞不上——这是之前漏判的真正原因。
        # 同时保留整串，供「Kimi K3.1 Pro」这类必须连读的产品名使用。
        parts = tok.split()
        if len(parts) > 1:
            out.add(_ANCHOR_ALIAS.get(tok, tok))
            for p in parts:
                if len(p) < 2 or p in _ANCHOR_STOP:
                    continue
                if re.fullmatch(r"[0-9.\-_]+", p):
                    continue
                out.add(_ANCHOR_ALIAS.get(p, p))
            # 相邻词两两拼一起：「genie 4」「kimi k3.1」「k3.1 pro」
            for i in range(len(parts) - 1):
                pair = f"{parts[i]} {parts[i + 1]}"
                if len(pair) >= 3:
                    out.add(_ANCHOR_ALIAS.get(pair, pair))
        else:
            out.add(_ANCHOR_ALIAS.get(tok, tok))

    for m in _ANCHOR_CN_ORG.finditer(text):
        out.add(_ANCHOR_ALIAS.get(m.group(1).lower(), m.group(1).lower()))

    return {a for a in out if a and a not in _ANCHOR_STOP}


def _dig(obj: Any, path: str) -> Any:
    """按 `a.b.c` / `a.0.b` 取 JSON 里的字段。"""
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _render(tpl: str, **kw: Any) -> str:
    """把模板里的 {query} / {key} / {top_k} 等占位换掉，缺的换空串。"""
    out = tpl or ""
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


class CuriosityPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._db: sqlite3.Connection | None = None
        self._task: asyncio.Task | None = None
        self._data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    @property
    def interval_hours(self) -> float:
        try:
            return max(0.25, float(self._cfg("interval_hours", 3)))
        except (TypeError, ValueError):
            return 3.0

    @property
    def provider_id(self) -> str:
        """总结用哪个模型。留空就用 AstrBot 当前的默认对话模型。"""
        return str(self._cfg("search_provider_id", "") or "").strip()

    @property
    def engine(self) -> str:
        raw = str(self._cfg("search_engine", "astrbot") or "").strip().lower()
        return raw if raw in ENGINES else "astrbot"

    # 兼容老配置名
    @property
    def search_engine(self) -> str:
        return self.engine

    @property
    def timeout_seconds(self) -> int:
        try:
            return max(10, int(self._cfg("search_timeout_seconds", 60)))
        except (TypeError, ValueError):
            return 60

    # ---------------- 各引擎参数 ----------------
    @property
    def qianfan_api_key(self) -> str:
        return str(self._cfg("qianfan_api_key", "") or "").strip()

    @property
    def qianfan_top_k(self) -> int:
        try:
            return max(1, min(20, int(self._cfg("qianfan_top_k", 5))))
        except (TypeError, ValueError):
            return 5

    @property
    def qianfan_recency(self) -> str:
        raw = str(self._cfg("qianfan_recency", "oneWeek") or "").strip()
        allowed = ("oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit")
        return raw if raw in allowed else "oneWeek"

    @property
    def bocha_api_key(self) -> str:
        return str(self._cfg("bocha_api_key", "") or "").strip()

    @property
    def bocha_count(self) -> int:
        try:
            return max(1, min(20, int(self._cfg("bocha_count", 5))))
        except (TypeError, ValueError):
            return 5

    @property
    def searxng_base(self) -> str:
        return str(self._cfg("searxng_base_url", "") or "").strip().rstrip("/")

    @property
    def searxng_time_range(self) -> str:
        raw = str(self._cfg("searxng_time_range", "") or "").strip()
        return raw if raw in ("", "day", "week", "month", "year") else ""

    @property
    def custom_endpoint(self) -> str:
        return str(self._cfg("custom_endpoint", "") or "").strip()

    @property
    def custom_method(self) -> str:
        raw = str(self._cfg("custom_method", "GET") or "GET").strip().upper()
        return raw if raw in ("GET", "POST") else "GET"

    @property
    def custom_headers(self) -> dict[str, str]:
        raw = self._cfg("custom_headers", "")
        data: Any = raw
        if isinstance(raw, str):
            txt = raw.strip()
            if not txt:
                return {}
            try:
                data = json.loads(txt)
            except json.JSONDecodeError:
                logger.warning(f"{_log_prefix()} custom_headers 不是合法 JSON，已忽略")
                return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    @property
    def custom_body(self) -> str:
        return str(self._cfg("custom_body", "") or "").strip()

    @property
    def custom_items_path(self) -> str:
        return str(self._cfg("custom_items_path", "results") or "results").strip()

    @property
    def custom_map(self) -> dict[str, str]:
        raw = self._cfg("custom_map", "")
        data: Any = raw
        if isinstance(raw, str):
            txt = raw.strip()
            if not txt:
                return {}
            try:
                data = json.loads(txt)
            except json.JSONDecodeError:
                logger.warning(f"{_log_prefix()} custom_map 不是合法 JSON，已忽略")
                return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    # ---------------- 投递 ----------------
    @property
    def target_umo(self) -> str:
        return str(self._cfg("target_umo", "") or "").strip()

    @property
    def deliver_threshold(self) -> int:
        """攒够几条才自动开口。1 = 有一说一，攒到就讲。"""
        try:
            return max(1, min(50, int(self._cfg("deliver_threshold", 2))))
        except (TypeError, ValueError):
            return 2

    @property
    def auto_deliver(self) -> bool:
        return bool(self._cfg("auto_deliver", True))

    @property
    def quiet_hours(self) -> tuple[int, int]:
        """免打扰时段（小时），默认 1 点到 8 点不讲。"""
        raw = self._cfg("quiet_hours", [1, 8])
        try:
            a, b = int(raw[0]), int(raw[1])
            return a, b
        except (TypeError, ValueError, IndexError):
            return 1, 8

    @property
    def mood_mode(self) -> bool:
        """"看心情"：允许 bot 自己决定什么时候讲、讲不讲。"""
        return bool(self._cfg("mood_mode", False))

    @property
    def mood_threshold(self) -> int:
        try:
            return max(1, min(20, int(self._cfg("mood_threshold", 6))))
        except (TypeError, ValueError):
            return 6

    @property
    def mood_skip_chance(self) -> float:
        """觉得不够有意思时讲出来的概率只有这么多（越小越挑剔）。"""
        try:
            v = float(self._cfg("mood_skip_chance", 0.7))
        except (TypeError, ValueError):
            v = 0.7
        return max(0.05, min(1.0, v))

    @property
    def topics(self) -> list[str]:
        raw = self._cfg("topics", None)
        if isinstance(raw, list) and raw:
            items = [str(x).strip() for x in raw if str(x).strip()]
            if items:
                return items
        return list(DEFAULT_TOPICS)

    @property
    def explore_ratio(self) -> float:
        """AI 自行探索的比例：0 = 永远只按清单搜，1 = 永远自己发挥。"""
        try:
            v = float(self._cfg("explore_ratio", 0.35))
        except (TypeError, ValueError):
            v = 0.35
        return max(0.0, min(1.0, v))

    @property
    def max_items_per_run(self) -> int:
        try:
            return max(1, min(10, int(self._cfg("max_items_per_run", 3))))
        except (TypeError, ValueError):
            return 3

    @property
    def raw_char_limit(self) -> int:
        """喂给模型抽取的资料上限（字符）。越小越省 token。"""
        try:
            return max(500, min(20000, int(self._cfg("raw_char_limit", 3000))))
        except (TypeError, ValueError):
            return 3000

    @property
    def compose_max_chars(self) -> int:
        try:
            return max(60, min(1000, int(self._cfg("compose_max_chars", 220))))
        except (TypeError, ValueError):
            return 220

    @property
    def persona_hint(self) -> str:
        """口吻补充。留空 = 纯用 AstrBot 里配置的人格。"""
        return str(self._cfg("persona_hint", "") or "").strip()

    # ---------------- QQ 空间联动 ----------------
    @property
    def qzone_enabled(self) -> bool:
        return bool(self._cfg("qzone_enabled", False))

    @property
    def qzone_min_score(self) -> int:
        try:
            return max(1, min(10, int(self._cfg("qzone_min_score", 8))))
        except (TypeError, ValueError):
            return 8

    @property
    def qzone_with_image(self) -> bool:
        return bool(self._cfg("qzone_with_image", True))

    # ------------------------------------------------------------------
    # 数据库
    # ------------------------------------------------------------------
    async def _db_open(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._data_dir / "curiosity.db"
        conn = sqlite3.connect(
            path,
            check_same_thread=False,
            timeout=10.0,  # Windows 上 SQLite 的文件锁等待更短，给足重试余量
        )
        conn.row_factory = sqlite3.Row
        # WAL 模式：读写不互相阻塞，跨平台行为一致，Windows 上尤其明显
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 开 WAL 失败（不影响使用）：{exc}")
        conn.executescript(SCHEMA)
        conn.commit()
        self._migrate(conn)
        self._db = conn
        logger.info(f"{_log_prefix()} 数据库就绪 {path}")
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """给老库补新列，升级插件不丢数据。"""
        for table, column, decl in _MIGRATIONS:
            try:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.DatabaseError:
                continue
            if column in cols:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                conn.commit()
                logger.info(f"{_log_prefix()} 升级表 {table}：补列 {column}")
            except sqlite3.DatabaseError as exc:  # noqa: BLE001
                logger.warning(f"{_log_prefix()} 补列 {table}.{column} 失败：{exc}")

    def _pref_get(self, k: str, default: Any = None) -> Any:
        if self._db is None:
            return default
        row = self._db.execute("SELECT v FROM prefs WHERE k=?", (k,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["v"])
        except (TypeError, json.JSONDecodeError):
            return row["v"]

    def _pref_set(self, k: str, v: Any) -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO prefs(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v, ensure_ascii=False)),
        )
        self._db.commit()

    # ------------------------------------------------------------------
    # 模型
    # ------------------------------------------------------------------
    async def _get_provider(self):
        """拿总结用的模型。没配就退回 AstrBot 的默认对话模型。"""
        # 1) 配了具体 id，先按 id 找
        if self.provider_id:
            for getter in (
                getattr(self.context, "get_provider_by_id", None),
                getattr(getattr(self.context, "provider_manager", None), "get_provider_by_id", None),
            ):
                if not callable(getter):
                    continue
                try:
                    provider = getter(self.provider_id)
                except Exception:  # noqa: BLE001
                    continue
                if asyncio.iscoroutine(provider):
                    provider = await provider
                if provider is not None:
                    return provider
        # 2) 回退：AstrBot 当前正在用的那个模型
        for getter in (
            getattr(self.context, "get_using_provider", None),
            getattr(getattr(self.context, "provider_manager", None), "get_using_provider_async", None),
            getattr(getattr(self.context, "provider_manager", None), "get_using_provider", None),
        ):
            if not callable(getter):
                continue
            provider = None
            try:
                provider = getter(None)
            except TypeError:
                try:
                    provider = getter()
                except Exception:  # noqa: BLE001
                    continue
            except Exception:  # noqa: BLE001
                continue
            if asyncio.iscoroutine(provider):
                provider = await provider
            if provider is not None:
                return provider
        # 3) 再回退：配置里写死的默认模型 id
        try:
            settings = self.context.get_config().get("provider_settings", {}) or {}
            default_id = str(settings.get("default_provider_id") or "").strip()
        except Exception:  # noqa: BLE001
            default_id = ""
        if default_id:
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = getter(default_id)
                    if asyncio.iscoroutine(provider):
                        provider = await provider
                    if provider is not None:
                        return provider
                except Exception:  # noqa: BLE001
                    pass
        # 4) 最后一招：provider_manager 里第一个能用的
        pm = getattr(self.context, "provider_manager", None)
        for attr in ("providers", "provider_insts", "insts"):
            items = getattr(pm, attr, None) or []
            try:
                for prov in items:
                    if prov is not None:
                        return prov
            except TypeError:
                continue
        return None

    async def _get_persona_prompt(self, event: AstrMessageEvent | None = None) -> str:
        """取 AstrBot 当前生效的人格，作为讲东西时的口吻。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is not None:
            try:
                umo = event.unified_msg_origin if event else self._persona_umo()
                resolve = getattr(pm, "resolve_selected_persona", None)
                if callable(resolve):
                    try:
                        settings = {}
                        try:
                            settings = self.context.get_config().get("provider_settings", {}) or {}
                        except Exception:  # noqa: BLE001
                            settings = {}
                        persona = await resolve(
                            umo,
                            "",
                            (self.target_umo.split(":") or [""])[0],
                            settings,
                        )
                    except TypeError:
                        persona = await resolve(umo)
                    text = self._persona_text(persona)
                    if text:
                        return text
                getter = getattr(pm, "get_default_persona_v3", None)
                if callable(getter):
                    persona = getter()
                    if asyncio.iscoroutine(persona):
                        persona = await persona
                    text = self._persona_text(persona)
                    if text:
                        return text
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{_log_prefix()} 取人格失败：{exc}")
        return ""

    @staticmethod
    def _persona_text(persona: Any) -> str:
        if not persona:
            return ""
        if isinstance(persona, str):
            return persona.strip()
        for key in ("system_prompt", "prompt", "text"):
            val = None
            if isinstance(persona, dict):
                val = persona.get(key)
            else:
                val = getattr(persona, key, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    # ------------------------------------------------------------------
    # 搜索后端
    # ------------------------------------------------------------------
    async def _astrbot_search(self, query: str) -> dict[str, Any]:
        """复用 AstrBot 自带的 web 搜索能力（provider 的 web_search tool）。"""
        provider = await self._get_provider()
        if provider is None:
            return {"ok": False, "error": "找不到可用于联网搜索的模型"}
        started = time.time()
        tried = []
        # AstrBot 的 web 搜索在不同版本里挂在不同位置，逐个试
        for attr in ("web_search", "search", "tool_web_search"):
            fn = getattr(provider, attr, None)
            if not callable(fn):
                continue
            tried.append(attr)
            try:
                res = await asyncio.wait_for(fn(query), timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                return {"ok": False, "error": f"AstrBot 搜索超时（{self.timeout_seconds}s）"}
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"{_log_prefix()} provider.{attr} 失败：{exc}")
                continue
            text = res if isinstance(res, str) else (
                getattr(res, "completion_text", None)
                or getattr(res, "text", None)
                or json.dumps(res, ensure_ascii=False)
            )
            if text and str(text).strip():
                return {
                    "ok": True,
                    "text": str(text).strip(),
                    "elapsed": round(time.time() - started, 2),
                }
        return {
            "ok": False,
            "error": (
                "当前 AstrBot 版本没暴露出可直接调用的联网搜索接口"
                f"（试过：{'/'.join(tried) or '无'}）。"
                "可以在 AstrBot 里开启「网页搜索」，或把本插件的联网方式换成 qianfan/bocha/searxng/custom。"
            ),
        }

    async def _qianfan_search(self, query: str) -> dict[str, Any]:
        """百度千帆 AI 搜索，返回带 URL 的真实网页条目。"""
        key = self.qianfan_api_key
        if not key:
            return {"ok": False, "error": "没配千帆 key"}
        import aiohttp  # 局部导入，插件在没装 aiohttp 的老环境也能加载

        body = {
            "messages": [{"role": "user", "content": query}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": self.qianfan_top_k}],
            "search_recency_filter": self.qianfan_recency,
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        started = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(QIANFAN_ENDPOINT, json=body, headers=headers) as resp:
                    raw = await resp.text()
                    if resp.status != 200:
                        return {
                            "ok": False,
                            "error": f"千帆返回 HTTP {resp.status}：{raw[:200]}",
                        }
                    data = json.loads(raw)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"千帆超时（{self.timeout_seconds}s）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"千帆失败：{exc}"}

        refs = data.get("references") or []
        if not refs:
            return {"ok": False, "error": "千帆没返回结果"}
        lines = []
        for i, r in enumerate(refs, 1):
            title = str(r.get("title") or "").strip()
            url = str(r.get("url") or "").strip()
            date = str(r.get("date") or "").strip()
            content = _tidy(str(r.get("content") or ""), 220)
            lines.append(f"[{i}] {title}\n日期：{date}\n链接：{url}\n内容：{content}")
        return {
            "ok": True,
            "text": "\n\n".join(lines),
            "elapsed": round(time.time() - started, 2),
            "raw": refs,
        }

    async def _bocha_search(self, query: str) -> dict[str, Any]:
        """博查 Web Search（api.bochaai.com）。"""
        key = self.bocha_api_key
        if not key:
            return {"ok": False, "error": "没配博查 key"}
        import aiohttp

        body = {
            "query": query,
            "summary": True,
            "count": self.bocha_count,
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        started = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(BOCHA_ENDPOINT, json=body, headers=headers) as resp:
                    raw = await resp.text()
                    if resp.status != 200:
                        return {"ok": False, "error": f"博查返回 HTTP {resp.status}：{raw[:200]}"}
                    data = json.loads(raw)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"博查超时（{self.timeout_seconds}s）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"博查失败：{exc}"}

        pages = _dig(data, "data.webPages.value") or []
        if not pages:
            pages = data.get("webPages", {}).get("value", []) if isinstance(data, dict) else []
        if not pages:
            return {"ok": False, "error": "博查没返回结果"}
        lines = []
        for i, p in enumerate(pages, 1):
            title = str(p.get("name") or "").strip()
            url = str(p.get("url") or "").strip()
            snippet = _tidy(str(p.get("snippet") or p.get("summary") or ""), 220)
            date = str(p.get("datePublished") or "").strip()
            lines.append(f"[{i}] {title}\n日期：{date}\n链接：{url}\n内容：{snippet}")
        return {
            "ok": True,
            "text": "\n\n".join(lines),
            "elapsed": round(time.time() - started, 2),
            "raw": pages,
        }

    async def _searxng_search(self, query: str) -> dict[str, Any]:
        """自建 SearXNG：不需要 key，返回 JSON。"""
        base = self.searxng_base
        if not base:
            return {"ok": False, "error": "没配 SearXNG 地址"}
        import aiohttp

        params = {
            "q": query,
            "format": "json",
            "categories": "general",
            "language": "zh-CN",
        }
        if self.searxng_time_range:
            params["time_range"] = self.searxng_time_range
        started = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(f"{base}/search", params=params) as resp:
                    raw = await resp.text()
                    if resp.status != 200:
                        return {
                            "ok": False,
                            "error": f"SearXNG 返回 HTTP {resp.status}（记得在 settings.yml 里开 json 格式）：{raw[:200]}",
                        }
                    data = json.loads(raw)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"SearXNG 超时（{self.timeout_seconds}s）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"SearXNG 失败：{exc}"}

        results = data.get("results") or []
        if not results:
            return {"ok": False, "error": "SearXNG 没返回结果"}
        lines = []
        for i, r in enumerate(results[: self.qianfan_top_k], 1):
            title = str(r.get("title") or "").strip()
            url = str(r.get("url") or "").strip()
            content = _tidy(str(r.get("content") or ""), 220)
            date = str(r.get("publishedDate") or "").strip()
            lines.append(f"[{i}] {title}\n日期：{date}\n链接：{url}\n内容：{content}")
        return {
            "ok": True,
            "text": "\n\n".join(lines),
            "elapsed": round(time.time() - started, 2),
            "raw": results,
        }

    async def _custom_search(self, query: str) -> dict[str, Any]:
        """任意 JSON 搜索接口：自己写请求模板 + 取值路径。

        占位符：{query} {key} {top_k}
        取值路径：custom_items_path 指向结果数组，custom_map 把这个数组里
        每条对象的字段映射成 title / url / snippet / date。
        """
        endpoint = self.custom_endpoint
        if not endpoint:
            return {"ok": False, "error": "没配 custom_endpoint"}
        import aiohttp

        key = str(self._cfg("custom_api_key", "") or "")
        headers = self.custom_headers
        top_k = self.qianfan_top_k
        url = _render(endpoint, query=query, key=key, top_k=top_k)
        headers = {
            k: _render(v, query=query, key=key, top_k=top_k) for k, v in headers.items()
        }
        body_tpl = self.custom_body
        body = None
        if self.custom_method == "POST":
            if body_tpl:
                body = _render(body_tpl, query=query, key=key, top_k=top_k)
            else:
                body = json.dumps(
                    {"query": query, "count": top_k}, ensure_ascii=False
                )
                headers.setdefault("Content-Type", "application/json")

        started = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                req = sess.post if self.custom_method == "POST" else sess.get
                kwargs: dict[str, Any] = {"headers": headers}
                if body is not None:
                    kwargs["data"] = body
                async with req(url, **kwargs) as resp:
                    raw = await resp.text()
                    if resp.status != 200:
                        return {"ok": False, "error": f"自定义接口返回 HTTP {resp.status}：{raw[:200]}"}
                    data = json.loads(raw)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"自定义接口超时（{self.timeout_seconds}s）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"自定义接口失败：{exc}"}

        items = _dig(data, self.custom_items_path)
        if not isinstance(items, list) or not items:
            return {"ok": False, "error": f"custom_items_path「{self.custom_items_path}」没取到数组"}
        m = self.custom_map
        if not m:
            m = {
                "title": "title",
                "url": "url",
                "snippet": "description",
                "date": "date",
            }
        lines = []
        for i, it in enumerate(items[:top_k], 1):
            title = str(_dig(it, m.get("title", "title")) or "").strip()
            u = str(_dig(it, m.get("url", "url")) or "").strip()
            snip = _tidy(str(_dig(it, m.get("snippet", "snippet")) or ""), 220)
            date = str(_dig(it, m.get("date", "date")) or "").strip()
            if not title and not snip:
                continue
            lines.append(f"[{i}] {title}\n日期：{date}\n链接：{u}\n内容：{snip}")
        if not lines:
            return {"ok": False, "error": "自定义接口能取到数组，但字段映射没匹配上"}
        return {
            "ok": True,
            "text": "\n\n".join(lines),
            "elapsed": round(time.time() - started, 2),
            "raw": items,
        }

    async def _none_search(self, query: str) -> dict[str, Any]:
        """不联网，直接问模型。会编、没链接，只作兜底。"""
        provider = await self._get_provider()
        if provider is None:
            return {"ok": False, "error": "找不到模型"}
        started = time.time()
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=query, contexts=[]),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"超时（{self.timeout_seconds}s）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"失败：{exc}"}
        text = getattr(resp, "completion_text", None) or str(resp or "")
        return {
            "ok": True,
            "text": text.strip(),
            "elapsed": round(time.time() - started, 2),
        }

    async def _raw_search(self, query: str) -> dict[str, Any]:
        engine = self.engine
        dispatch = {
            "astrbot": self._astrbot_search,
            "qianfan": self._qianfan_search,
            "bocha": self._bocha_search,
            "searxng": self._searxng_search,
            "custom": self._custom_search,
            "none": self._none_search,
        }
        fn = dispatch.get(engine, self._astrbot_search)
        res = await fn(query)
        res.setdefault("engine", engine)
        # astrbot 引擎没配好时，自动往下有 key 的引擎退一步，免得整轮作废
        if not res.get("ok") and engine == "astrbot":
            for fallback, name in (
                (self._qianfan_search, "qianfan"),
                (self._bocha_search, "bocha"),
                (self._searxng_search, "searxng"),
            ):
                alt = await fallback(query)
                if alt.get("ok"):
                    alt.setdefault("engine", name)
                    logger.info(f"{_log_prefix()} astrbot 搜索不可用，已自动改用 {name}")
                    return alt
        return res

    # ------------------------------------------------------------------
    # 拆条
    # ------------------------------------------------------------------
    async def _extract(self, topic: str, raw_text: str) -> list[dict[str, Any]]:
        """把一段搜索原文，拆成结构化的「一条一条」。

        token 优化要点（都在这一个提示词里）：
          - 资料只喂前 raw_char_limit 个字符，不喂全文
          - 只要求 5 个短字段，明确写了每个字段的字数上限
          - 明确「最多 N 条、宁缺毋滥」，防止模型注水
          - 只输出 JSON 数组，不要解释
        """
        provider = await self._get_provider()
        if provider is None:
            logger.warning(f"{_log_prefix()} 抽取跳过：找不到总结模型")
            return []

        snippet = (raw_text or "")[: self.raw_char_limit]
        prompt = (
            f"主题：{topic}\n"
            f"下面是联网搜索的资料，按 [序号] 分条，每条自带链接。\n\n"
            f"挑出最多 {self.max_items_per_run} 个真正有意思、值得讲给人听的点。宁缺毋滥，都不新鲜就返回 []。\n\n"
            "每条输出这些字段，短、准：\n"
            '- ref: 来自第几号 [序号]（只填数字）\n'
            "- title: 10-30 字，**专有名词（公司/产品/型号）原样保留**，别写成「某公司发布新模型」\n"
            "- gist: 一句话说清是什么，≤60 字\n"
            "- why: 为什么有意思，≤40 字\n"
            "- score: 1-10，有多值得讲\n\n"
            "只输出 JSON 数组，不要任何解释。\n\n"
            f"资料：\n{snippet}"
        )

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, contexts=[]),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 抽取失败：{exc}")
            return []

        text = getattr(resp, "completion_text", None) or str(resp or "")
        text = _strip_md(text)
        m = re.search(r"\[.*\]", text, flags=re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []

        # 资料来源里每条都带「链接：xxx」，按出现顺序收起来，供 ref 回填
        ref_urls = re.findall(r"链接：(\S+)", raw_text)
        ref_titles = re.findall(r"^\[\d+\]\s*(.+)$", raw_text, flags=re.M)

        items: list[dict[str, Any]] = []
        for it in data[: self.max_items_per_run]:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            gist = str(it.get("gist") or "").strip()
            if not title or not gist:
                continue
            try:
                score = int(it.get("score") or 5)
            except (TypeError, ValueError):
                score = 5
            # ref 指向资料里的 [序号]，用它把真实 URL 拷回来；
            # 不让模型「凭记忆写链接」，那是瞎编的重灾区。
            url = ""
            try:
                ref = int(it.get("ref") or 0)
            except (TypeError, ValueError):
                ref = 0
            if 1 <= ref <= len(ref_urls):
                url = ref_urls[ref - 1]
            elif len(ref_titles) == 1 and len(ref_urls) == 1:
                url = ref_urls[0]
            items.append(
                {
                    "title": title[:120],
                    "gist": gist[:200],
                    "why": str(it.get("why") or "").strip()[:160],
                    "url": url[:400],
                    "score": max(1, min(10, score)),
                }
            )
        return items

    def _is_dup(
        self, conn: sqlite3.Connection, title: str, gist: str = ""
    ) -> bool:
        """近似判重：标题二元组相似度 OR 「专名共现」任一命中，就当同一件事。

        为什么只比标题不够：
          「Kimi K3.1 Pro以2.8万亿参数成全球最大开源模型」
          「月之暗面Kimi K3.1 Pro开源，3D建模得分92.7%」
        标题描述的是同一件事的不同侧面，二元组杰卡德只有 0.2 出头，
        但两句话里都出现了同一个**硬专名**（Kimi K3.1 Pro）。
        专名才是「同一件事」的真正锚点，描述词不是。

        所以判重分三层：
          1. 标题里至少 1 个硬专名两边都有（应对同事件不同侧面）
          2. 一方完整包含另一方，且短方够长
          3. 标题骨架二元组杰卡德 ≥ 0.28（应对换句话重写同一描述）
        """
        new = _title_loose(title)
        if len(new) < 4:
            return False
        new_grams = {new[i : i + 2] for i in range(len(new) - 1)}
        if not new_grams:
            return False
        new_anchors = _anchors(title, gist)

        rows = conn.execute(
            "SELECT title, gist FROM finds ORDER BY id DESC LIMIT 500"
        ).fetchall()
        for row in rows:
            old = _title_loose(row["title"])
            if len(old) < 4:
                continue
            if new == old:
                return True

            # 层 1：硬专名共现。专名是同一件事的铁锚，命中即重复。
            # 但「谷歌」「meta」这类机构名太泛，单独撞上不算：
            # 谷歌今天发 Genie、昨天发 Gemini，都是「谷歌」却完全不是一件事。
            # 所以要求重叠里至少有一个是**具体的**（不是纯机构名）。
            if new_anchors:
                old_anchors = _anchors(row["title"], row["gist"] or "")
                common = new_anchors & old_anchors
                if any(a not in _SWEEP_ANCHORS for a in common):
                    return True

            # 层 2：一方完整包含另一方，且短的一方够长
            shorter, longer = (new, old) if len(new) <= len(old) else (old, new)
            if len(shorter) >= 8 and shorter in longer:
                return True

            # 层 3：二元组杰卡德相似度
            old_grams = {old[i : i + 2] for i in range(len(old) - 1)}
            union = len(new_grams | old_grams)
            if union and len(new_grams & old_grams) / union >= 0.28:
                return True
        return False

    # ------------------------------------------------------------------
    # 跑一轮
    # ------------------------------------------------------------------
    async def _pick_topic(self) -> str:
        """挑方向：按配置清单，还是让 AI 自己发挥。

        explore_ratio 控制「自己发挥」的概率。
        自己发挥时复用清单里的方向做变体提示，避免多一次模型调用（省 token）。
        """
        topics = self.topics
        if random.random() < self.explore_ratio:
            # 自行探索：从清单里随机取一个方向做「换个角度」的变体
            base = random.choice(topics)
            angles = (
                "换个角度",
                "最近一周的新进展",
                "冷门但有意思的那面",
                "普通人会感兴趣的角度",
                "反常识的地方",
            )
            return f"{base}（{random.choice(angles)}）"
        return random.choice(topics)

    async def _run_once(self, topic: str | None = None) -> dict[str, Any]:
        """完整跑一轮：挑方向 → 搜 → 拆 → 存。"""
        conn = await self._db_open()
        if topic is None:
            topic = await self._pick_topic()

        started = time.time()
        res = await self._raw_search(topic)
        engine = res.get("engine") or self.engine
        if not res.get("ok"):
            conn.execute(
                "INSERT INTO runs(topic,at,ok,got,fresh,elapsed,note,engine) VALUES(?,?,?,?,?,?,?,?)",
                (topic, _now(), 0, 0, 0, round(time.time() - started, 2), res.get("error", ""), engine),
            )
            conn.commit()
            logger.warning(f"{_log_prefix()} 本轮失败 topic={topic!r} {res.get('error')}")
            return {
                "ok": False,
                "error": res.get("error"),
                "topic": topic,
                "fresh": 0,
                "got": 0,
            }

        items = await self._extract(topic, res.get("text", ""))
        fresh = 0
        for it in items:
            fp = _fingerprint(it["title"], it.get("url", ""))
            # 第二道闸：指纹之外，再按宽骨架做一次近似判重。
            # 有的来源是散文总结、没有 URL，同一件事换句话写就会绕过哈希，
            # 所以必须再加一层「专名 / 包含」检查。
            if self._is_dup(conn, it["title"]):
                continue
            try:
                conn.execute(
                    """INSERT INTO finds(fp,title,gist,why,url,topic,score,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        fp,
                        it["title"],
                        it["gist"],
                        it["why"],
                        it.get("url", ""),
                        topic,
                        it["score"],
                        _now(),
                    ),
                )
                fresh += 1
            except sqlite3.IntegrityError:
                continue  # 老的，跳过
        conn.execute(
            "INSERT INTO runs(topic,at,ok,got,fresh,elapsed,note,engine) VALUES(?,?,?,?,?,?,?,?)",
            (topic, _now(), 1, len(items), fresh, round(time.time() - started, 2), "", engine),
        )
        conn.commit()
        logger.info(
            f"{_log_prefix()} 本轮完成 topic={topic!r} engine={engine} "
            f"抽出={len(items)} 新增={fresh} 用时={res.get('elapsed')}s"
        )
        return {
            "ok": True,
            "topic": topic,
            "got": len(items),
            "fresh": fresh,
            "engine": engine,
            "elapsed": res.get("elapsed"),
        }

    # ------------------------------------------------------------------
    # 讲
    # ------------------------------------------------------------------
    def _count_ready(self) -> int:
        if self._db is None:
            return 0
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM finds WHERE delivered=0"
        ).fetchone()
        return int(row["n"]) if row else 0

    def _take_ready(self, limit: int, prefer_keyword: str = "") -> list[sqlite3.Row]:
        if self._db is None:
            return []
        kw = (prefer_keyword or "").strip()
        if kw:
            like = f"%{kw}%"
            rows = self._db.execute(
                """SELECT * FROM finds WHERE delivered=0
                   AND (title LIKE ? OR gist LIKE ? OR topic LIKE ?)
                   ORDER BY score DESC, created_at DESC LIMIT ?""",
                (like, like, like, limit),
            ).fetchall()
            if rows:
                return list(rows)
        rows = self._db.execute(
            """SELECT * FROM finds WHERE delivered=0 ORDER BY score DESC, created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return list(rows)

    def _mark_delivered(self, find_id: int) -> None:
        if self._db is None:
            return
        self._db.execute(
            "UPDATE finds SET delivered=1, delivered_at=? WHERE id=?",
            (_now(), find_id),
        )
        self._db.commit()

    def _in_quiet_hours(self) -> bool:
        a, b = self.quiet_hours
        h = datetime.now().hour
        if a <= b:
            return a <= h < b
        return h >= a or h < b

    async def _compose(self, rows: list[sqlite3.Row]) -> str:
        """让模型用当前人格的口吻把这几条讲出来。

        token 优化：
          - 只喂 title/gist/why，不喂完整网页正文
          - 硬性长度上限 compose_max_chars
          - 没有 AstrBot 人格时，不硬塞一大段预设人格，只用极简指令
        """
        provider = await self._get_provider()
        if provider is None:
            return ""

        persona = await self._get_persona_prompt()
        extra = self.persona_hint
        lines = []
        for i, r in enumerate(rows, 1):
            seg = f"{i}. {r['title']}｜{r['gist']}"
            why = (r["why"] or "").strip()
            if why:
                seg += f"｜{why}"
            lines.append(seg)
        body = "\n".join(lines)

        head = persona.strip() if persona else ""
        if extra:
            head = (head + "\n" + extra).strip()
        if not head:
            head = "你就是平时跟主人聊天的那个机器人本人。"

        prompt = (
            f"{head}\n\n"
            f"你刚才自己上网翻了点东西，现在要跟主人念叨一下。\n"
            f"要求：说人话，口语短句，不要列 1 2 3 4，不要写成资讯播报，"
            f"不要说「这条新闻」「据报道」这种词。挑最有意思的讲，全篇不超过 {self.compose_max_chars} 字。\n\n"
            f"翻到的东西：\n{body}"
        )

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, contexts=[]),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 组织语言失败：{exc}")
            return ""

        text = getattr(resp, "completion_text", None) or str(resp or "")
        return _strip_md(text).strip()

    async def _deliver(self, force: bool = False) -> dict[str, Any]:
        """讲一次。force=True 忽略门限和免打扰（手动触发用）。"""
        conn = await self._db_open()
        umo = self.target_umo
        if not umo:
            return {"ok": False, "error": "还没设置要讲给谁（target_umo）"}

        ready = self._count_ready()
        if ready <= 0:
            return {"ok": False, "error": "手里还没攒到新东西"}

        if not force:
            threshold = self.deliver_threshold
            if self.mood_mode:
                # 看心情：到了 mood_threshold 之后随机决定讲不讲
                if ready < threshold:
                    return {"ok": False, "error": f"只攒了 {ready} 条"}
                if ready >= self.mood_threshold or random.random() > self.mood_skip_chance:
                    pass  # 心情上来了，讲
                else:
                    return {"ok": False, "error": f"攒了 {ready} 条，还没想讲"}
            elif ready < threshold:
                return {
                    "ok": False,
                    "error": f"只攒了 {ready} 条，不到 {threshold} 条",
                }
            if self._in_quiet_hours():
                return {"ok": False, "error": "免打扰时段"}

        want = max(1, min(ready, self.max_items_per_run + 1))
        rows = self._take_ready(want)
        if not rows:
            return {"ok": False, "error": "没取到条目"}

        text = await self._compose(rows)
        if not text:
            return {"ok": False, "error": "组织语言失败"}

        try:
            sent = await self._send(umo, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 发送失败：{exc}")
            return {"ok": False, "error": f"发送失败：{exc}"}
        if not sent:
            return {"ok": False, "error": "没有可用的活跃平台会话"}

        ids = [r["id"] for r in rows]
        qs = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE finds SET delivered=1, delivered_at=? WHERE id IN ({qs})",
            (_now(), *ids),
        )
        conn.commit()
        logger.info(f"{_log_prefix()} 已讲，{len(ids)} 条，umo={umo}")
        return {
            "ok": True,
            "delivered": len(ids),
            "text": text,
            "umo": umo,
            "rows": [dict(r) for r in rows],
        }

    async def _send(self, umo: str, text: str) -> bool:
        """按 umo 找到平台实例，主动发一条消息。

        AstrBot 的实际 API（踩过坑，记在这里）：
          - 平台实例挂在 `platform_manager.platform_insts`（list），
            没有 `get_instances()` 这个方法
          - 平台 id 用 `inst.meta().id` 取，不是 `platform_name`
          - aiocqhttp 的 client 是 `inst.get_client()` 返回的 CQHttp，
            它的签名是 `call_action(action: str, **params)`

        不用 `context.send_message`，因为那是被动回复，需要 event 上下文；
        这里是定时主动发，必须直接走底层 action。
        """
        platform_id, _, rest = umo.partition(":")
        msg_type = "friend"
        target_id = ""
        if ":" in rest:
            kind, _, target_id = rest.partition(":")
            msg_type = "group" if "Group" in kind else "friend"
        else:
            target_id = rest

        inst = None
        getter = getattr(self.context, "get_platform_inst", None)
        if callable(getter):
            try:
                inst = getter(platform_id)
            except Exception:  # noqa: BLE001
                inst = None
            if asyncio.iscoroutine(inst):
                inst = await inst
        if inst is None:
            insts = getattr(self.context.platform_manager, "platform_insts", None) or []
            if not insts:
                getter2 = getattr(self.context.platform_manager, "get_insts", None)
                if callable(getter2):
                    insts = getter2() or []
            for cand in insts:
                try:
                    pid = cand.meta().id
                except Exception:  # noqa: BLE001
                    pid = ""
                if str(pid) == platform_id:
                    inst = cand
                    break
        if inst is None:
            logger.warning(f"{_log_prefix()} 找不到平台实例 {platform_id}")
            return False

        client = None
        getter_c = getattr(inst, "get_client", None)
        if callable(getter_c):
            client = getter_c()
            if asyncio.iscoroutine(client):
                client = await client
        if client is None:
            client = getattr(inst, "bot", None)
        if client is None:
            logger.warning(f"{_log_prefix()} 平台 {platform_id} 拿不到 client")
            return False

        try:
            if msg_type == "group":
                await client.call_action(
                    "send_group_msg", group_id=int(target_id), message=text
                )
            else:
                await client.call_action(
                    "send_private_msg", user_id=int(target_id), message=text
                )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 发送到 {umo} 失败：{exc}")
        return False

    # ------------------------------------------------------------------
    # QQ 空间
    # ------------------------------------------------------------------
    async def _share_to_qzone(self, rows: list[sqlite3.Row]) -> dict[str, Any]:
        """把最有意思的一条发到 QQ 空间，尽量带上配图。

        不写死任何插件名：按「有没有 publish 相关的 LLM 工具」自己探。
        探不到就老老实实说一声，不影响主流程。

        筛选口径：没分享过 + 评分够（默认 8）。挑完没货就自动放宽到
        「没分享过的任意条目」，免得用户手动点「发空间」时总扑空——
        毕竟主动攒下来的东西本来就已经过滤过一轮了。
        """
        await self._db_open()
        assert self._db is not None

        def _pick(min_score: int, require_unshared: bool) -> sqlite3.Row | None:
            sql = "SELECT * FROM finds WHERE score >= ?"
            if require_unshared:
                sql += " AND COALESCE(shared, 0) = 0"
            sql += " ORDER BY score DESC, created_at DESC LIMIT 1"
            row = self._db.execute(sql, (min_score,)).fetchone()
            return row

        cand = _pick(self.qzone_min_score, True)
        relaxed = ""
        if cand is None:
            cand = _pick(1, True)
            if cand is not None:
                relaxed = "（没有够分的，挑了分最高的）"
        if cand is None:
            cand = _pick(1, False)
            if cand is not None:
                relaxed = "（都在空间发过了，再发一条）"
        if cand is None:
            return {"ok": False, "error": "库里是空的，先翻一轮再发"}

        text = await self._compose_qzone(cand)
        if not text:
            return {"ok": False, "error": "组织说说文案失败"}

        args = self._qzone_tool_args(cand, text)
        try:
            result = None
            # 首选：走官方 LLM 工具对象（FunctionTool.handler 是真实业务方法）
            tool = self._find_qzone_tool()
            if tool is not None:
                h = getattr(tool, "handler", None)
                if callable(h):
                    fake_event = self._qzone_fake_event()
                    result = await h(fake_event, **args)
                    if asyncio.iscoroutine(result):
                        result = await result
            # 兜底：直接从插件实例上取工具方法，自己造 event
            if result is None:
                found = self._find_qzone_caller()
                if found is None:
                    return {
                        "ok": False,
                        "error": "没检测到可用的 QQ 空间发布工具（装上 QQ空间类插件后重试）",
                        "text": text,
                    }
                fn, fake_event = found
                result = await fn(fake_event, **args)
                if asyncio.iscoroutine(result):
                    result = await result
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 发 QQ 空间失败：{exc}")
            return {"ok": False, "error": f"发布失败：{exc}", "text": text}

        self._db.execute(
            "UPDATE finds SET shared=1, shared_at=? WHERE id=?",
            (_now(), cand["id"]),
        )
        self._db.commit()
        logger.info(f"{_log_prefix()} 已发 QQ 空间：{cand['title']}{relaxed}")
        return {
            "ok": True,
            "title": cand["title"],
            "text": text,
            "relaxed": relaxed,
            "result": str(result)[:300],
        }

    async def _compose_qzone(self, row: sqlite3.Row) -> str:
        """写一条说说：口语、有观点、不抄标题。"""
        provider = await self._get_provider()
        if provider is None:
            return ""
        persona = await self._get_persona_prompt()
        head = persona.strip() if persona else "你就是平时跟主人聊天的那个机器人本人。"
        extra = self.persona_hint
        if extra:
            head = head + "\n" + extra
        prompt = (
            f"{head}\n\n"
            "你要发一条 QQ 空间说说，内容是下面这件事。\n"
            "要求：像你自己随口发的那种，不要标题党、不要贴链接、不要照抄下面的原文，"
            "带一点你自己的看法或感受，两三句话，不超过 100 字。\n\n"
            f"事情：{row['title']}\n"
            f"是什么：{row['gist']}\n"
            f"为什么有意思：{row['why']}"
        )
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, contexts=[]),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 写说说失败：{exc}")
            return ""
        text = getattr(resp, "completion_text", None) or str(resp or "")
        return _strip_md(text).strip()[:400]

    # 按优先级探测的发布工具名。不同 QQ 空间插件叫法不一样，
    # 这里只列「发一条说说」这类，不列查看/评论/点赞。
    _QZONE_TOOL_NAMES = (
        "qzone_publish_post",
        "publish_qzone",
        "qzone_publish",
        "qzone_post",
        "post_qzone",
        "send_qzone",
        "publish_mood",
    )

    def _find_qzone_tool(self):
        """在已注册的 LLM 工具里找 QQ 空间发布工具（返回 FunctionTool 对象）。

        不写死插件名，也不写死管理器属性：
        AstrBot 4.29.x 的正规入口是 context.get_llm_tool_manager()，
        老版本可能挂在 provider_manager.func_tool_manager 上，这里都试一遍。
        """
        mgrs: list[Any] = []
        # AstrBot 4.29.x 的正规入口是 context.get_llm_tool_manager()
        getter = getattr(self.context, "get_llm_tool_manager", None)
        if callable(getter):
            try:
                tm = getter()
            except Exception:  # noqa: BLE001
                tm = None
            if tm is not None:
                mgrs.append(tm)
        for owner in (getattr(self.context, "provider_manager", None), self.context):
            if owner is None:
                continue
            ft = getattr(owner, "func_tool_manager", None)
            if ft is not None:
                mgrs.append(ft)
        for mgr in mgrs:
            getter = getattr(mgr, "get_func", None)
            if not callable(getter):
                continue
            for name in self._QZONE_TOOL_NAMES:
                try:
                    fn = getter(name)
                except Exception:  # noqa: BLE001
                    fn = None
                if fn is not None:
                    return fn
        return None

    def _qzone_fake_event(self):
        """造一个够用的假事件：带管理员 QQ 号 + 空消息链。

        QQ 空间插件普遍会先 `_is_admin(event)`，而它们取发送者的方式五花八门：
        `event.get_sender_id()`、`event.message_obj.sender.user_id`、
        `event.get_sender_name()`……所以这里把常见的几条路都铺上，
        并且 get_sender_id 返回 int（有些实现直接 int()）。
        """
        sid = self._admin_sender_id()
        try:
            sid_int = int(sid)
        except Exception:  # noqa: BLE001
            sid_int = 0
        sender = SimpleNamespace(user_id=sid_int or sid, nickname="", user_id_str=str(sid))

        def _get_sender_id():
            return sid_int

        def _get_sender_name():
            return ""

        return SimpleNamespace(
            unified_msg_origin=self.target_umo or "",
            message_obj=SimpleNamespace(
                sender=sender,
                group=None,
                message=[],
                messages=[],
                raw_message={},
            ),
            message=[],
            messages=[],
            raw_message={},
            get_sender_id=_get_sender_id,
            get_sender_name=_get_sender_name,
            is_admin=lambda: True,
            _curiosity_direct=True,
        )

    def _find_qzone_caller(self):
        """退回方案：直接拿 QQ 空间插件的工具方法，自己造一个 event 去调。

        为什么需要这一步：很多 QQ 空间插件的发布方法会先做管理员校验
        （`_is_admin(event)` 里读 `event.message_obj.sender.user_id`），
        假事件过不了这一关。这里就用真实 plugin 实例 + 真实 sender id。

        返回 (bound_method, fake_event) 或 None。
        """
        sender_id = self._admin_sender_id()
        fake_event = SimpleNamespace(
            unified_msg_origin=self.target_umo or "",
            message_obj=SimpleNamespace(
                sender=SimpleNamespace(user_id=sender_id, nickname=""),
                group=None,
            ),
        )
        for inst, handler in self._iter_star_handlers():
            name = getattr(handler, "handler_name", "") or ""
            if name not in ("tool_publish_post", "_publish_post", "publish_post"):
                continue
            fn = getattr(handler, "handler", None)
            if not callable(fn):
                continue
            # handler 是被 functools.partial 包过的，用 __self__ 拿到绑定方法
            owner = getattr(fn, "__self__", None) or inst
            if not callable(fn):
                continue
            return (fn, fake_event)
        return None

    def _iter_star_handlers(self):
        """遍历所有已加载插件的 handler 元数据：yield (star_instance, handler)。

        AstrBot 把插件元数据放在全局 star_map（module_path -> StarMetadata）
        和 star_registry（list）里，handler 在 star_handlers_registry 里；
        这里按 handler_module_path 对上号，再取出插件实例。
        """
        try:
            from astrbot.core.star.star import star_map
            from astrbot.core.star.star_handler import star_handlers_registry
        except Exception:  # noqa: BLE001
            return
        handlers = list(getattr(star_handlers_registry, "_handlers", None) or [])
        for handler in handlers:
            path = getattr(handler, "handler_module_path", "") or ""
            md = star_map.get(path)
            inst = getattr(md, "star_cls", None) if md is not None else None
            if inst is None:
                continue
            yield inst, handler

    def _admin_sender_id(self) -> str:
        """拿一个管理员 QQ 号当假事件的发送者（过 QQ 空间插件的权限校验）。"""
        ids = getattr(self.context, "astrbot_config", {}) or {}
        try:
            admins = ids.get("admins_id") or []
        except Exception:  # noqa: BLE001
            admins = []
        for a in admins:
            s = str(a).strip()
            if s:
                return s
        return ""

    def _qzone_tool_args(self, row: sqlite3.Row, text: str) -> dict[str, Any]:
        """按常见发布工具的参数名组织实参。

        实测 qzone_ultra 的 qzone_publish_post(content, sync_weibo, media)
        用 `media: list[str]` 放配图，所以这里默认给 content + media。
        """
        args: dict[str, Any] = {"content": text}
        if self.qzone_with_image:
            url = safe_url(row["url"])
            if url:
                args["media"] = [url]
        return args

    def _persona_umo(self) -> str:
        """取人格时用哪个会话：优先 target_umo，没有就用当前平台随便一个私聊。"""
        umo = self.target_umo
        if umo:
            return umo
        try:
            insts = getattr(self.context.platform_manager, "platform_insts", None) or []
            for inst in insts:
                try:
                    pid = inst.meta().id
                except Exception:  # noqa: BLE001
                    continue
                if pid:
                    return f"{pid}:FriendMessage:"
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ------------------------------------------------------------------
    # 定时循环
    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        # 启动后先等一会，避开 AstrBot 冷启动
        await asyncio.sleep(90)
        while True:
            try:
                if not self.enabled:
                    await asyncio.sleep(600)
                    continue
                last = float(self._pref_get("last_run", 0) or 0)
                gap = self.interval_hours * 3600
                if _now() - last >= gap:
                    res = await self._run_once()
                    self._pref_set("last_run", _now())
                    # 刚翻完顺手看看要不要发 QQ 空间
                    if self.qzone_enabled and res.get("fresh"):
                        try:
                            await self._share_to_qzone(self._take_all_new())
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"{_log_prefix()} QQ 空间流程出错：{exc}")
                if self.auto_deliver and self._count_ready() >= 1:
                    out = await self._deliver()
                    if not out.get("ok"):
                        logger.debug(f"{_log_prefix()} 本轮没讲：{out.get('error')}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{_log_prefix()} 循环出错：{exc}")
            await asyncio.sleep(600)

    def _take_all_new(self, limit: int = 20) -> list[sqlite3.Row]:
        if self._db is None:
            return []
        rows = self._db.execute(
            """SELECT * FROM finds WHERE delivered=0 ORDER BY score DESC, created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return list(rows)

    # ------------------------------------------------------------------
    # LLM 工具（自然语言触发，不靠指令）
    # ------------------------------------------------------------------
    @filter.llm_tool(name="tell_me_something_interesting")
    async def tool_tell(self, event: AstrMessageEvent, topic_hint: str = "") -> str:
        """从自己网上攒下来的东西里挑有意思的讲出来。

        当对方问「有什么新鲜事」「最近看到啥」「讲讲你翻到的东西」「有什么好玩的」，
        或者聊天时明显想听你分享见闻、而不是要查资料时，调用这个。
        不要用它回答事实性问题（那种情况用搜索工具）。

        Args:
            topic_hint(string): 对方想听哪方面的，比如「科技」「AI」「随便」。留空就是随便挑。
        """
        try:
            await self._db_open()
        except Exception as exc:  # noqa: BLE001
            return f"数据库没打开：{exc}"

        hint = (topic_hint or "").strip()
        rows = self._take_ready(1, prefer_keyword=hint) if hint else self._take_ready(1)
        if not rows:
            return (
                "兜里现在空的，还没攒到新东西。可以告诉对方你这就去翻翻，"
                "或者说刚才翻过了没碰到什么有意思的，别编。"
            )
        r = rows[0]
        # 取一条并标记已讲，但不走主动推送通道（这次是对话内直接回答）
        self._mark_delivered(r["id"])
        return (
            "你要讲的半分钟就是这条：\n"
            f"- 标题：{r['title']}\n"
            f"- 一句话概括：{r['gist']}\n"
            f"- 为什么值得讲：{r['why']}\n"
            f"{('- 出处：' + r['url']) if r['url'] else ''}\n"
            "\n用自己的语气讲给对方听：口语、短句、带点你自己的看法，"
            "像随口聊到的一样，不要列条目、不要写成资讯播报、不要说「这条新闻」这种词。"
        )

    @filter.llm_tool(name="check_curiosity_backlog")
    async def tool_backlog(self, event: AstrMessageEvent, unused: str = "") -> str:
        """看看自己攒了多少还没讲的东西。

        当对方问「你攒了多少」「手头有啥存货」「最近翻到几样东西」时调用。

        Args:
            unused(string): 这个参数没用，传空字符串即可。
        """
        await self._db_open()
        ready = self._count_ready()
        total = self._db.execute("SELECT COUNT(*) AS n FROM finds").fetchone()["n"]
        if not ready:
            return f"累计攒下 {total} 条，没讲的有 0 条。"
        rows = self._take_ready(min(ready, 5))
        lines = [f"累计攒下 {total} 条，没讲的有 {ready} 条。其中几条是："]
        for r in rows:
            lines.append(f"- {r['title']}（{r['gist']}）")
        return "\n".join(lines)

    @filter.llm_tool(name="go_find_something")
    async def tool_find(self, event: AstrMessageEvent, direction: str = "") -> str:
        """立刻联网翻一轮新东西，把有意思的攒下来。

        只有对方明确让你「现在去翻」「去找点新的」「查最新的」时才调用。
        这个要花几秒到几十秒，别随便调。

        Args:
            direction(string): 想让你翻哪个方向，比如「AI 模型」「太空」「生物」。留空就按自己的兴趣挑。
        """
        try:
            res = await self._run_once((direction or "").strip() or None)
        except Exception as exc:  # noqa: BLE001
            return f"翻的时候出错了：{exc}"
        if not res.get("ok"):
            return f"没翻成：{res.get('error')}"
        return (
            f"翻了「{res['topic']}」，抽出 {res.get('got', 0)} 条，新增 {res.get('fresh', 0)} 条。"
            "（重复的会自动丢掉，不用重复讲。）"
        )

    @filter.llm_tool(name="share_curiosity_to_qzone")
    async def tool_share_qzone(self, event: AstrMessageEvent, unused: str = "") -> str:
        """把手里最有趣的一条发到 QQ 空间，配一张图。

        只在对方明确说「发个说说」「发空间」「分享到 QQ 空间」时调用。

        Args:
            unused(string): 这个参数没用，传空字符串即可。
        """
        await self._db_open()
        rows = self._take_all_new()
        if not rows:
            return "手里没存货，先翻一轮再说。"
        res = await self._share_to_qzone(rows)
        if res.get("ok"):
            return f"发出去了：{res.get('text', '')[:80]}"
        return f"没发出去：{res.get('error')}"

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    def _args(self, event: AstrMessageEvent) -> str:
        """取指令后面的参数（兼容 wake_prefix 不是 / 的情况）。"""
        got = event.get_extra("cmd_args")
        if isinstance(got, str):
            return got.strip()
        name = event.get_extra("cmd_name") or ""
        s = _strip_cmd(event.message_str or "")
        if name and s.startswith(name):
            s = s[len(name):]
        return s.strip()

    @_cmd("翻翻")
    async def cmd_browse(self, event: AstrMessageEvent):
        """立刻出去翻一轮。"""
        topic = self._args(event) or None
        yield event.plain_result("我出去翻一下……")
        res = await self._run_once(topic)
        if not res.get("ok"):
            yield event.plain_result(f"没翻成：{res.get('error')}")
            return
        yield event.plain_result(
            f"翻了「{res['topic']}」，抽出 {res.get('got', 0)} 条，新增 {res.get('fresh', 0)} 条。"
        )

    @_cmd("攒了什么")
    async def cmd_backlog(self, event: AstrMessageEvent):
        """看看攒了多少还没讲的。"""
        await self._db_open()
        ready = self._count_ready()
        rows = self._take_ready(8)
        if not rows:
            yield event.plain_result("现在手里空的，还没攒到新东西。")
            return
        lines = [f"没讲的有 {ready} 条："]
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. {r['title']} —— {r['gist']}（{r['score']}分）")
        yield event.plain_result("\n".join(lines))

    @_cmd("讲讲")
    async def cmd_tell(self, event: AstrMessageEvent):
        """马上讲给他听。"""
        res = await self._deliver(force=True)
        if not res.get("ok"):
            yield event.plain_result(f"讲不了：{res.get('error')}")
            return
        yield event.plain_result(f"（{res['delivered']} 条已发到 {res['umo']}）\n{res['text']}")

    @_cmd("发空间")
    async def cmd_share(self, event: AstrMessageEvent):
        """把最有趣的一条发到 QQ 空间。"""
        await self._db_open()
        rows = self._take_all_new()
        if not rows:
            yield event.plain_result("手里没存货，先翻一轮。")
            return
        res = await self._share_to_qzone(rows)
        if not res.get("ok"):
            yield event.plain_result(f"没发成：{res.get('error')}")
            return
        yield event.plain_result(f"发出去了：\n{res.get('text', '')}")

    @_cmd("翻翻状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """看看它最近翻得怎么样。"""
        await self._db_open()
        total = self._db.execute("SELECT COUNT(*) AS n FROM finds").fetchone()["n"]
        ready = self._count_ready()
        runs = self._db.execute("SELECT * FROM runs ORDER BY at DESC LIMIT 5").fetchall()
        lines = [
            f"启用：{'开' if self.enabled else '关'}",
            f"联网：{self.engine}",
            f"节奏：每 {self.interval_hours} 小时翻一次",
            f"累计攒下：{total} 条 / 没讲：{ready} 条",
            f"讲给谁：{self.target_umo or '（没设）'}",
            f"QQ 空间：{'开' if self.qzone_enabled else '关'}",
            f"心情模式：{'开' if self.mood_mode else '关'}",
        ]
        for r in runs:
            t = datetime.fromtimestamp(r["at"]).strftime("%m-%d %H:%M")
            lines.append(
                f"· {t} {r['topic']} → {'成' if r['ok'] else '败'} "
                f"抽{r['got']} 新{r['fresh']} {r['elapsed'] or 0}s"
            )
        yield event.plain_result("\n".join(lines))

    @_cmd("清空攒的")
    async def cmd_clear(self, event: AstrMessageEvent):
        """把没讲的都扔掉。"""
        await self._db_open()
        n = self._count_ready()
        self._db.execute("DELETE FROM finds WHERE delivered=0")
        self._db.commit()
        yield event.plain_result(f"扔了 {n} 条。")

    # ------------------------------------------------------------------
    # 面板 API（复古科幻 UI 用）
    # ------------------------------------------------------------------
    def _api_ok(self, data: Any) -> dict[str, Any]:
        return {"status": "ok", "data": data}

    def _api_err(self, msg: str) -> dict[str, Any]:
        return {"status": "error", "message": msg}

    async def api_status(self) -> dict[str, Any]:
        try:
            db = await self._db_open()
            total = db.execute("SELECT count(*) FROM finds").fetchone()[0]
            delivered = db.execute(
                "SELECT count(*) FROM finds WHERE delivered=1"
            ).fetchone()[0]
            run_count = db.execute("SELECT count(*) FROM runs").fetchone()[0]
            last = db.execute(
                "SELECT topic, at, ok, got, fresh, elapsed FROM runs ORDER BY at DESC LIMIT 1"
            ).fetchone()
            q = self.quiet_hours
            return self._api_ok({
                "enabled": self.enabled,
                "interval_hours": self.interval_hours,
                "ready_count": self._count_ready(),
                "total": total,
                "delivered": delivered,
                "run_count": run_count,
                "last_run": last["at"] if last else None,
                "last_topic": last["topic"] if last else "",
                "last_ok": bool(last["ok"]) if last else None,
                "deliver_threshold": self.deliver_threshold,
                "auto_deliver": self.auto_deliver,
                "target_umo": self.target_umo,
                "search_engine": self.engine,
                "engine": self.engine,
                "qianfan_top_k": self.qianfan_top_k,
                "qianfan_recency": self.qianfan_recency,
                "provider_id": self.provider_id,
                "timeout_seconds": self.timeout_seconds,
                "max_items_per_run": self.max_items_per_run,
                "quiet_hours": [q[0], q[1]] if q else [],
                "topics": self.topics,
                "explore_ratio": self.explore_ratio,
                "mood_mode": self.mood_mode,
                "persona_hint": self.persona_hint,
                "in_quiet_hours": self._in_quiet_hours(),
                "qzone_enabled": self.qzone_enabled,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 面板 status 出错：{exc}")
            return self._api_err(str(exc))

    async def api_finds(self, q: str = "", limit: int = 40, offset: int = 0) -> dict[str, Any]:
        try:
            db = await self._db_open()
            limit = max(1, min(200, int(limit or 40)))
            offset = max(0, int(offset or 0))
            where = ""
            args: list[Any] = []
            kw = (q or "").strip()
            if kw:
                where = "WHERE title LIKE ? OR gist LIKE ? OR why LIKE ? OR topic LIKE ?"
                like = f"%{kw}%"
                args = [like, like, like, like]
            total = db.execute(
                f"SELECT count(*) FROM finds {where}", args
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT id, title, gist, why, url, topic, score, created_at, delivered "
                f"FROM finds {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                args + [limit, offset],
            ).fetchall()
            return self._api_ok(
                {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 面板 finds 出错：{exc}")
            return self._api_err(str(exc))

    async def api_runs(self, limit: int = 60) -> dict[str, Any]:
        try:
            db = await self._db_open()
            limit = max(1, min(300, int(limit or 60)))
            rows = db.execute(
                "SELECT id, topic, at, ok, got, fresh, elapsed, note, engine FROM runs "
                "ORDER BY at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return self._api_ok({"items": [dict(r) for r in rows]})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 面板 runs 出错：{exc}")
            return self._api_err(str(exc))

    async def api_run(self, topic: str = "") -> dict[str, Any]:
        try:
            t = (topic or "").strip() or None
            return self._api_ok(await self._run_once(t))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 面板手动扫描出错：{exc}")
            return self._api_err(str(exc))

    async def api_tell(self) -> dict[str, Any]:
        try:
            await self._db_open()
            rows = self._take_ready(max(1, self.max_items_per_run))
            if not rows:
                return self._api_err("库里没有待讲的东西，先扫一轮")
            text = await self._compose(rows)
            text = (text or "").strip()
            if not text:
                return self._api_err("模型没给出内容")
            umo = self.target_umo
            sent = False
            if umo:
                sent = await self._send(umo, text)
                if sent:
                    for r in rows:
                        self._mark_delivered(r["id"])
            return self._api_ok({"text": text, "delivered": sent, "umo": umo})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 面板手动讲述出错：{exc}")
            return self._api_err(str(exc))

    async def api_share(self) -> dict[str, Any]:
        try:
            await self._db_open()
            res = await self._share_to_qzone(self._take_all_new())
            if res.get("ok"):
                return self._api_ok(res)
            return self._api_err(res.get("error", "发布失败"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 面板发空间出错：{exc}")
            return self._api_err(str(exc))

    def _register_apis(self) -> None:
        reg = self.context.register_web_api
        base = f"/{PLUGIN_NAME}"
        routes = [
            ("page/status", self.api_status, ["GET"], "观察站状态"),
            ("page/finds", self.api_finds, ["GET"], "藏品库列表"),
            ("page/runs", self.api_runs, ["GET"], "航行日志"),
            ("page/run", self.api_run, ["POST"], "立刻扫一轮"),
            ("page/tell", self.api_tell, ["POST"], "提炼并发送"),
            ("page/share", self.api_share, ["POST"], "发到 QQ 空间"),
        ]
        for path, handler, methods, desc in routes:
            reg(f"{base}/{path}", handler, methods, desc)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self):
        await self._db_open()
        try:
            self._register_apis()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{_log_prefix()} 注册面板 API 失败：{exc}")
        if self.enabled:
            self._task = asyncio.create_task(self._loop())
        logger.info(
            f"{_log_prefix()} 已加载；启用={self.enabled} 节奏={self.interval_hours}h "
            f"联网={self.engine} 总结模型={self.provider_id or '（跟随默认）'} "
            f"{'口吻=用 AstrBot 人格' if not self.persona_hint else '口吻=人格+自定义补充'} "
            f"讲给={self.target_umo or '（未设置）'} 门限={self.deliver_threshold}条 "
            f"心情模式={'开' if self.mood_mode else '关'} QQ空间={'开' if self.qzone_enabled else '关'}"
        )

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._db:
            try:
                # Windows 上文件句柄没释放会让重装/升级时删不掉目录
                self._db.commit()
            except Exception:  # noqa: BLE001
                pass
            self._db.close()
            self._db = None
        logger.info(f"{_log_prefix()} 已卸载")


def _tidy(text: str, limit: int) -> str:
    """压掉空白、截断，省 token。"""
    s = re.sub(r"\s+", " ", (text or "")).strip()
    return s[:limit]


def safe_url(u: Any) -> str:
    """只放行 http/https，避免把奇怪的东西塞进说说。"""
    s = str(u or "").strip()
    if not s:
        return ""
    if not re.match(r"^https?://", s, flags=re.I):
        return ""
    return s
