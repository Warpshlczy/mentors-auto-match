"""爬虫编排：增量去重、限速、robots、重试的调度层。

去重复用 alex-yimingyang/csrankings-faculty-finder 的「按导师个人主页 URL 判重」策略，
并落成 supervisors.homepage 上的唯一索引，因此「已爬取的不再重复爬」在进程重启后依然成立。
限速 / robots / 重试三项约束统一由 HttpFetcher 实现，本模块不自己发请求。
"""

from __future__ import annotations

from app.config import cache_dir
from app.services.sources import (
    CSRankingsSource,
    HomepageSource,
    HttpFetcher,
    OpenAlexSource,
)


class Crawler:
    def __init__(self, db, cfg: dict, on_progress=None, should_stop=None) -> None:
        """
        on_progress(message: str, fraction: float | None) —— fraction 为本阶段内的完成度 0-1
        should_stop() -> bool —— 返回 True 时中止
        """
        self._db = db
        self._cfg = cfg
        self._on_progress = on_progress
        self._should_stop = should_stop or (lambda: False)

    def _emit(self, message: str, fraction: float | None = None) -> None:
        if self._on_progress:
            self._on_progress(message, fraction)

    def run(self, degree: str | None = None) -> dict:
        """抓取一轮，返回 {"saved": n, "matched": n}。

        学位筛选放在查询期（db.list_supervisors）而不是抓取期：即使本次只找博士，
        硕士导师的爬取结果也会入库，下次换学位就不用重爬。
        """
        conf = self._cfg["crawler"]
        fetcher = HttpFetcher(
            min_interval=conf["min_interval"],
            max_interval=conf["max_interval"],
            retries=conf["retries"],
            # 把取消信号交给 fetcher，限速等待与重试退避才能被及时打断
            should_stop=self._should_stop,
        )
        rankings = CSRankingsSource(
            fetcher,
            cache_dir(self._cfg),
            conf["cache_ttl_days"],
            on_progress=lambda message: self._emit(message),
        )
        openalex = OpenAlexSource(fetcher)
        homepage = HomepageSource(fetcher)

        self._emit("正在获取 CSRankings 导师名单…", 0.0)
        # 本地已有的导师（按主页 URL 判重）在这里就交给数据源过滤掉，
        # 「全部已抓过」时不会白解析 18MB 的研究方向 CSV。
        known = self._db.known_homepages()
        candidates = rankings.faculty(
            institutions=conf["schools"],
            per_school=conf["max_per_school"],
            paper_years=conf["paper_years"],
            skip_homepages=known,
        )
        if self._should_stop():
            self._emit("已取消抓取。")
            return {"saved": 0, "matched": 0}
        if not candidates:
            # 两种可能：勾选院校名称对不上，或者这些院校的导师都已抓过
            if self._db.count_supervisors(conf["schools"]):
                self._emit(
                    f"勾选院校的导师已全部在本地（共 {self._db.count_supervisors(conf['schools'])} 位），"
                    "无需重复抓取，直接进入匹配。想抓新导师请调高「每校导师数」上限。",
                    1.0,
                )
            else:
                self._emit(
                    "未匹配到任何院校。请到「设置」确认院校名称与 CSRankings 完全一致"
                    "（例如 Univ. of Illinois at Urbana-Champaign）。",
                    1.0,
                )
            return {"saved": 0, "matched": 0}

        todo = [s for s in candidates if s.homepage not in known]
        if not todo:
            self._emit("候选导师全部已爬取过，无需重复抓取。", 1.0)
            return {"saved": 0, "matched": 0}
        if conf["fetch_papers"]:
            self._emit(
                f"共 {len(todo)} 位待抓取。每位导师需请求主页 + OpenAlex 共 1-2 次"
                f"（间隔 {conf['min_interval']}-{conf['max_interval']} 秒），请耐心等待。"
            )

        saved = 0
        matched = 0
        for index, sup in enumerate(todo, 1):
            if self._should_stop():
                self._emit("已取消抓取。")
                break

            self._emit(
                f"({index}/{len(todo)}) {sup.name} · {sup.university}", (index - 1) / len(todo)
            )
            sup = homepage.enrich(sup)
            if conf["fetch_papers"]:
                papers = openalex.recent_papers(
                    sup.name, sup.university, sup.orcid, conf["paper_years"]
                )
                if papers:
                    sup.papers = papers
            # 抓取中途被取消时不要再落一条没填完的记录
            if self._should_stop():
                break

            self._db.upsert_supervisor(sup)
            saved += 1
            if degree and sup.degree_types and degree not in sup.degree_types:
                continue
            matched += 1

        self._emit(
            f"抓取完成：新增/更新 {saved} 位导师，其中招生类型匹配「{degree or '不限'}」的有 {matched} 位。",
            1.0,
        )
        return {"saved": saved, "matched": matched}
