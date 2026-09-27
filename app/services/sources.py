"""外部数据源适配层：只负责「把外部世界的数据变成 Supervisor 字段」。

已核实的数据源（见 README「数据源与覆盖范围」）：
- CSRankings gh-pages 公开 CSV
    csrankings.csv            4.0MB  name,affiliation,homepage,scholarid,orcid
    generated-author-info.csv 17.8MB name,dept,area,count,adjustedcount,year
  → 姓名、院校、个人主页（增量去重键）、ORCID（论文检索）、研究方向（近 N 年发表领域）
- OpenAlex API → 近 N 年论文标题
- 导师个人主页 → 职称、招生类型（硕/博）

两个大 CSV 会按 cache_ttl_days 落到本地缓存，否则每次启动都要重新拉 22MB。
"""

from __future__ import annotations

import csv
import io
import random
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from app.data.models import Supervisor

DEFAULT_UA = "auto-match-mentors/0.1 (local desktop app)"

#: 连接超时单独设短值。sites.google.com 这类连不上的主页，用 30 秒默认值会让
#: 「robots + 3 次重试」把进度条在同一个导师上冻住 4 分钟（实测 246 秒）。
CONNECT_TIMEOUT = 5.0
#: robots.txt 是礼节性检查，不值得为它等：拿不到就按允许处理。
ROBOTS_TIMEOUT = (2.0, 5.0)

CSRANKINGS_BASE = "https://raw.githubusercontent.com/emeryberger/CSrankings/gh-pages"
FACULTY_CSV = f"{CSRANKINGS_BASE}/csrankings.csv"
AUTHOR_INFO_CSV = f"{CSRANKINGS_BASE}/generated-author-info.csv"
INSTITUTIONS_CSV = f"{CSRANKINGS_BASE}/institutions.csv"
OPENALEX_API = "https://api.openalex.org"

#: CSRankings 的姓名带 DBLP 消歧后缀（"Adam Yang 0001"），不是人名的一部分
DBLP_SUFFIX_RE = re.compile(r"\s+\d{4}$")
#: CSRankings 用这个占位值表示「没有 ORCID」
NO_ORCID = "0000-0000-0000-0000"


def display_name(raw: str) -> str:
    """去掉 CSRankings 的 DBLP 消歧后缀，用于界面展示与外部检索。"""
    return DBLP_SUFFIX_RE.sub("", (raw or "").strip())


#: CSRankings 的 area 是会议缩写，这里映射成可读研究方向。
#: ponytail: 覆盖 generated-author-info.csv 里实际出现的全部 78 个 code；
#: 将来新增 code 会自动回落显示原始缩写，不会报错。
AREA_NAMES: dict[str, str] = {
    "nips": "机器学习", "icml": "机器学习", "iclr": "机器学习",
    "aaai": "人工智能", "ijcai": "人工智能",
    "cvpr": "计算机视觉", "iccv": "计算机视觉", "eccv": "计算机视觉",
    "acl": "自然语言处理", "emnlp": "自然语言处理", "naacl": "自然语言处理",
    "kdd": "数据挖掘", "www": "万维网与信息检索", "sigir": "信息检索",
    "vldb": "数据库", "icde": "数据库", "sigmod": "数据库", "pods": "数据库理论",
    "soda": "算法与理论", "stoc": "算法与理论", "focs": "算法与理论",
    "ccs": "信息安全", "usenixsec": "信息安全", "oakland": "信息安全", "ndss": "信息安全",
    "crypto": "密码学", "eurocrypt": "密码学", "pets": "隐私增强技术",
    "eurographics": "计算机图形学", "siggraph": "计算机图形学", "siggraph-asia": "计算机图形学",
    "vis": "可视化", "vr": "虚拟现实",
    "icse": "软件工程", "fse": "软件工程", "ase": "软件工程", "issta": "软件测试",
    "dac": "芯片设计自动化", "iccad": "芯片设计自动化",
    "ubicomp": "普适计算", "uist": "人机交互", "chiconf": "人机交互",
    "icra": "机器人学", "iros": "机器人学", "rss": "机器人学",
    "sc": "高性能计算", "ics": "高性能计算", "hpdc": "高性能计算",
    "asplos": "计算机体系结构", "isca": "计算机体系结构", "micro": "计算机体系结构",
    "hpca": "计算机体系结构",
    "popl": "程序语言", "oopsla": "程序语言", "pldi": "程序语言", "icfp": "函数式编程",
    "lics": "逻辑与形式化方法", "cav": "形式化验证",
    "sigcomm": "计算机网络", "nsdi": "计算机网络", "mobicom": "移动计算",
    "mobisys": "移动系统", "sensys": "传感器网络", "imc": "网络测量",
    "sigmetrics": "性能分析", "usenixatc": "系统", "eurosys": "操作系统",
    "osdi": "操作系统", "sosp": "操作系统", "fast": "存储系统",
    "rtss": "实时系统", "rtas": "实时系统", "emsoft": "嵌入式系统",
    "ismb": "生物信息学", "recomb": "计算生物学",
    "sigcse": "计算机教育", "ec": "电子商务", "wine": "网络经济学",
}

TITLE_PATTERNS: list[tuple[str, str]] = [
    (r"讲席教授|首席教授", "讲席教授"),
    (r"特聘教授", "特聘教授"),
    (r"副教授|associate professor", "副教授"),
    (r"助理教授|青年研究员|assistant professor", "助理教授"),
    (r"研究员", "研究员"),
    (r"教授|full professor|\bprofessor\b", "教授"),
]

MASTER_HINTS = (
    "招收硕士", "硕士生招生", "硕士研究生", "招收研究生", "硕士招生",
    "master student", "masters student", "ms student", "master's student",
)
PHD_HINTS = (
    "招收博士", "博士生招生", "博士研究生", "博士招生",
    "phd student", "ph.d. student", "doctoral student", "phd position",
)


class HttpFetcher:
    """带 robots 校验、请求间隔、重试的 HTTP 客户端。

    所有外部请求都必须经过它，保证「请求间隔 1-2 秒 + 遵守 robots + 异常重试 3 次」
    这三条约束只在一个地方实现。

    传入 should_stop 后，限速等待与重试退避都是可中断的：用户点取消最多等 0.2 秒，
    而不是等完当前那次 1-2 秒限速（或最坏 4 秒的重试退避）。连接本身的超时无法中断，
    只能靠 CONNECT_TIMEOUT 压短。
    """

    #: 可中断 sleep 的检查间隔（秒）
    POLL_INTERVAL = 0.2

    def __init__(
        self,
        min_interval: float = 1.0,
        max_interval: float = 2.0,
        retries: int = 3,
        user_agent: str = DEFAULT_UA,
        timeout: int = 20,
        should_stop=None,
    ) -> None:
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.retries = max(1, retries)
        #: (连接超时, 读取超时)：连接阶段必须快速失败，读取阶段给足带宽
        self.timeout = (CONNECT_TIMEOUT, timeout)
        self.user_agent = user_agent
        self._should_stop = should_stop or (lambda: False)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()

    def stopped(self) -> bool:
        return bool(self._should_stop())

    def _sleep(self, seconds: float) -> bool:
        """可中断的等待；返回 False 表示等待期间收到了取消。"""
        deadline = time.monotonic() + seconds
        while True:
            if self._should_stop():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(self.POLL_INTERVAL, remaining))

    def _throttle(self, host: str) -> bool:
        with self._lock:
            elapsed = time.monotonic() - self._last_request.get(host, 0.0)
            wait = random.uniform(self.min_interval, self.max_interval) - elapsed
            self._last_request[host] = time.monotonic() + max(wait, 0.0)
        return wait <= 0 or self._sleep(wait)

    def robots_allows(self, url: str) -> bool:
        parts = urlparse(url)
        host = parts.netloc
        with self._lock:
            parser = self._robots.get(host)
        if parser is None:
            parser = RobotFileParser()
            parser.set_url(f"{parts.scheme}://{host}/robots.txt")
            try:
                # 走 self._session 而不是 parser.read()：后者内部用 urlopen 且没有超时，
                # 一个卡住的 robots.txt 会把后台线程永久挂住
                resp = self._session.get(parser.url, timeout=ROBOTS_TIMEOUT)
                parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
                if resp.status_code != 200:
                    parser.allow_all = True
            except requests.RequestException:
                # robots.txt 拿不到时按允许处理，但请求间隔仍然生效
                parser.allow_all = True
            with self._lock:
                self._robots[host] = parser
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def get(self, url: str, params: dict | None = None):
        """返回 200 的 Response；4xx 直接放弃，5xx / 读取超时重试到上限。

        连不上（域名不存在、被墙、连接被重置）不重试：重试换不来结果，
        只会让进度条在同一个导师上多冻几十秒——这正是「匹配时进度卡住不动」的来源。
        取消时立刻返回 None，调用方按「没拿到数据」处理即可。
        """
        if self._should_stop():
            return None
        if not self.robots_allows(url):
            return None
        host = urlparse(url).netloc
        for attempt in range(1, self.retries + 1):
            if self._should_stop() or not self._throttle(host):
                return None
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp
                if resp.status_code < 500:
                    return None
            except requests.ConnectTimeout:
                return None
            except requests.ConnectionError:
                return None
            except requests.RequestException:
                pass
            if attempt < self.retries and not self._sleep(attempt * 2):
                return None
        return None

    def text(self, url: str, params: dict | None = None) -> str | None:
        resp = self.get(url, params)
        return resp.text if resp is not None else None

    def json(self, url: str, params: dict | None = None) -> dict | None:
        resp = self.get(url, params)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            return None


class CSRankingsSource:
    """导师基础信息 + 研究方向（来自 CSRankings 的公开 CSV）。"""

    name = "csrankings"

    def __init__(
        self,
        fetcher: HttpFetcher,
        cache_dir: str | Path,
        ttl_days: int = 7,
        on_progress=None,
    ) -> None:
        self._fetcher = fetcher
        self._cache_dir = Path(cache_dir)
        self._ttl = max(0, ttl_days) * 86400
        self._on_progress = on_progress or (lambda message: None)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _cached_csv(self, url: str, filename: str, label: str) -> str | None:
        path = self._cache_dir / filename
        if path.exists() and (time.time() - path.stat().st_mtime) < self._ttl:
            return path.read_text(encoding="utf-8", errors="replace")
        # 下载 4MB/18MB 期间拿不到取消信号，先说清楚，避免用户以为卡死
        self._on_progress(f"正在下载{label}（下载阶段无法取消）…")
        text = self._fetcher.text(url)
        if text is None:
            # 网络失败时退回过期缓存，总比完全没有数据强
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace")
            return None
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return text

    def institutions(self) -> list[tuple[str, str]]:
        """匹配池：(院校名, 国家/地区) 列表，供设置页给用户勾选。

        名称必须原样写进配置，所以这里直接返回 CSV 里的写法，不做任何规范化。
        """
        csv_text = self._cached_csv(
            INSTITUTIONS_CSV, "institutions.csv", "CSRankings 院校名单（约 50KB）"
        )
        if not csv_text:
            return []
        out: list[tuple[str, str]] = []
        for row in csv.DictReader(io.StringIO(csv_text)):
            name = (row.get("institution") or "").strip()
            if name:
                out.append((name, (row.get("countryabbrv") or "").strip().upper()))
        out.sort(key=lambda item: (item[1], item[0]))
        return out

    def _areas_by_name(self, years: int) -> dict[str, list[str]]:
        csv_text = self._cached_csv(
            AUTHOR_INFO_CSV, "generated-author-info.csv", "CSRankings 研究方向数据（约 18MB）"
        )
        if not csv_text:
            return {}
        cutoff = datetime.now().year - years + 1
        counters: dict[str, Counter] = defaultdict(Counter)
        for row in csv.DictReader(io.StringIO(csv_text)):
            try:
                if int(row.get("year") or 0) < cutoff:
                    continue
            except (TypeError, ValueError):
                continue
            name = (row.get("name") or "").strip().lower()
            area = (row.get("area") or "").strip()
            if name and area:
                counters[name][area] += 1
        result: dict[str, list[str]] = {}
        for name, counter in counters.items():
            labels = dict.fromkeys(
                AREA_NAMES.get(area, area) for area, _ in counter.most_common(10)
            )
            result[name] = list(labels)[:6]
        return result

    def faculty(
        self,
        institutions: list[str],
        per_school: int = 0,
        paper_years: int = 5,
        skip_homepages: set[str] | None = None,
    ) -> list[Supervisor]:
        """按院校白名单取导师，每所院校最多 per_school 位（0 = 全部）。

        注意：institutions 里的名字必须与 CSRankings 的 institutions.csv 完全一致
        （例："Univ. of Illinois at Urbana-Champaign"），否则会被静默过滤掉。
        skip_homepages 传入本地已有导师的主页 URL，命中的直接不返回——这样「全部已抓过」
        时连 18MB 的研究方向 CSV 都不用解析。
        """
        csv_text = self._cached_csv(
            FACULTY_CSV, "csrankings.csv", "CSRankings 导师名单（约 4MB）"
        )
        if not csv_text:
            return []

        wanted = {s.strip().lower() for s in institutions if s.strip()}
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in csv.DictReader(io.StringIO(csv_text)):
            affiliation = (row.get("affiliation") or "").strip()
            homepage = (row.get("homepage") or "").strip()
            # 没有主页就没法做增量去重，直接跳过
            if not homepage or affiliation.lower() not in wanted:
                continue
            grouped[affiliation].append(row)

        # 每校单独计数（而不是所有院校共用一个总量），并按名单均匀取样，
        # 避免大院校把限额吃光、也避免只取到姓名排序靠前的那几位。
        picked: list[dict] = []
        for rows in grouped.values():
            if per_school and len(rows) > per_school:
                step = len(rows) / per_school
                rows = [rows[int(i * step)] for i in range(per_school)]
            picked.extend(rows)

        if skip_homepages:
            picked = [
                row
                for row in picked
                if (row.get("homepage") or "").strip() not in skip_homepages
            ]
        if not picked:
            return []
        self._on_progress(
            f"已从 CSRankings 选出 {len(picked)} 位待抓取导师，正在读取研究方向…"
        )
        areas = self._areas_by_name(paper_years)

        out: list[Supervisor] = []
        for row in picked:
            raw_name = (row.get("name") or "").strip()
            orcid = (row.get("orcid") or "").strip()
            out.append(
                Supervisor(
                    # 研究方向按原始姓名（含消歧后缀）查，避免 "Yang Chen 0008/0012" 串味
                    name=display_name(raw_name),
                    university=(row.get("affiliation") or "").strip(),
                    homepage=(row.get("homepage") or "").strip(),
                    orcid="" if orcid == NO_ORCID else orcid,
                    research_areas=areas.get(raw_name.lower(), []),
                    source=self.name,
                )
            )
        return out


class OpenAlexSource:
    """近 N 年论文标题（来自 OpenAlex）。

    ponytail: 原计划用 DBLP 检索 API，但它现在挡在 Anubis 反爬后面
    （返回 200 但内容是 JS 挑战页），一条数据都拿不到。换成 OpenAlex：
    免额度、不需要 key，且能用 CSRankings 自带的 ORCID 精确锁定作者 —— 按姓名搜会
    抓错人（"Baobao Chang" 第一条命中的是 King University 的另一个人）。
    """

    name = "openalex"

    def __init__(self, fetcher: HttpFetcher, limit: int = 20) -> None:
        self._fetcher = fetcher
        self.limit = limit

    def _author_id(self, name: str, university: str) -> str | None:
        """无 ORCID 时按姓名找作者 id，用机构名优先消歧。"""
        data = self._fetcher.json(
            f"{OPENALEX_API}/authors", {"search": name, "per_page": 5}
        )
        results = (data or {}).get("results") or []
        if not results:
            return None
        target = (university or "").lower()
        if target:
            for author in results:
                names = " ".join(
                    inst.get("display_name") or ""
                    for inst in author.get("last_known_institutions") or []
                ).lower()
                if target in names:
                    return str(author["id"]).rsplit("/", 1)[-1]
        # 机构都对不上就退到论文最多的那个，总比点错人强
        best = max(results, key=lambda a: a.get("works_count") or 0)
        return str(best["id"]).rsplit("/", 1)[-1]

    def recent_papers(
        self, name: str, university: str = "", orcid: str = "", years: int = 5
    ) -> list[str]:
        cutoff = datetime.now().year - years + 1
        if orcid:
            author_filter = f"author.orcid:{orcid}"
        else:
            author_id = self._author_id(name, university)
            if not author_id:
                return []
            author_filter = f"author.id:{author_id}"

        data = self._fetcher.json(
            f"{OPENALEX_API}/works",
            {
                "filter": f"{author_filter},from_publication_date:{cutoff}-01-01",
                "sort": "publication_date:desc",
                "per_page": self.limit,
            },
        )
        hits = (data or {}).get("results") or []
        titles: list[str] = []
        for hit in hits:
            title = re.sub(r"\s+", " ", str(hit.get("title") or "")).strip()
            if title:
                titles.append(title.rstrip("."))
        return titles


class HomepageSource:
    """导师个人主页：职称、招生类型，以及 CSRankings 没覆盖时的研究方向兜底。"""

    name = "homepage"

    def __init__(self, fetcher: HttpFetcher) -> None:
        self._fetcher = fetcher

    def enrich(self, sup: Supervisor) -> Supervisor:
        html = self._fetcher.text(sup.homepage)
        if not html:
            return sup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        low = text.lower()

        if not sup.title:
            for pattern, label in TITLE_PATTERNS:
                if re.search(pattern, low):
                    sup.title = label
                    break

        degrees: list[str] = []
        if any(hint in low for hint in MASTER_HINTS):
            degrees.append("硕士")
        if any(hint in low for hint in PHD_HINTS):
            degrees.append("博士")
        sup.degree_types = degrees

        if not sup.research_areas:
            meta = soup.find("meta", attrs={"name": re.compile("keywords", re.I)})
            content = meta.get("content") if meta else None
            if content:
                sup.research_areas = [
                    part.strip()
                    for part in re.split(r"[,，;；]", content)
                    if part.strip()
                ][:6]
        return sup
