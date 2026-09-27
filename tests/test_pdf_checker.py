"""最小自检：python tests/test_pdf_checker.py

不依赖 pytest，也不需要真实 PDF —— 只测校验与打分里的纯逻辑部分。
"""

from __future__ import annotations

import itertools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.models import Supervisor
from app.services.matcher import score_offline
from app.services.pdf_checker import (
    clean_text,
    count_chars,
    count_words,
    detect_doc_type,
    detect_language,
    semantic_density,
    trim_page,
)

# 60 个互不相同的技术词，用它们组合出「内容多样」的假中文文本
TERMS = [
    "深度学习", "计算机视觉", "自然语言", "知识图谱", "强化学习", "迁移学习", "图神经网络",
    "多模态", "可解释性", "因果推断", "贝叶斯", "优化算法", "分布式", "联邦学习", "边缘计算",
    "推荐系统", "信息检索", "对话系统", "语音识别", "文本生成", "语义理解", "目标检测",
    "图像分割", "视频理解", "三维重建", "机器人", "自动驾驶", "医学影像", "生物信息",
    "高性能计算", "编译优化", "程序分析", "形式验证", "软件测试", "数据库", "查询优化",
    "操作系统", "内存管理", "网络安全", "密码学", "区块链", "隐私保护", "人机交互",
    "虚拟现实", "增强现实", "数据可视化", "时间序列", "异常检测", "特征工程", "模型压缩",
    "神经架构", "提示学习", "参数高效", "检索增强", "智能体", "多任务", "跨模态", "零样本",
    "小样本", "持续学习",
]


TEMPLATES = [
    "本研究围绕{a}展开，重点解决{b}在真实场景中的泛化与效率问题。",
    "针对{b}的局限性，本文提出结合{a}的新方法，并在多个数据集上验证。",
    "实验表明，{a}可以显著提升{b}的鲁棒性，同时降低推理开销。",
    "本文进一步讨论{a}与{b}的协同机制，分析其理论边界与工程可行性。",
    "在{b}任务中引入{a}后，模型收敛速度与准确率均有明显改善。",
    "我们对比了{a}和{b}两类方案，发现二者在稀疏数据下互补。",
    "为了评估{a}的有效性，设计了消融实验并给出误差来源分析。",
    "最终将{a}应用于{b}场景，验证了方法在工业部署中的可行性。",
]


def fake_zh(length: int) -> str:
    """生成内容多样（非模板灌水）的假中文文本，用于阈值回归。"""
    pairs = itertools.product(TERMS, TERMS)
    parts = (
        TEMPLATES[i % len(TEMPLATES)].format(a=a, b=b)
        for i, (a, b) in enumerate(itertools.islice(pairs, 0, 600))
    )
    return "".join(itertools.islice(parts, 0, 600))[:length]


def check_trim() -> None:
    lines = [f"line{i}" for i in range(10)]
    trimmed = trim_page("\n".join(lines), ratio=0.1)
    assert trimmed.splitlines() == [f"line{i}" for i in range(1, 9)], trimmed
    # 行数太少时不裁，避免把正文裁没
    assert trim_page("a\nb", 0.1) == "a\nb"


def check_clean() -> None:
    # 每页正文各不相同，但页眉水印与页码在每页都出现
    pages = [
        "水印文字\n"
        + "\n".join(f"第{p}页第{i}行有效内容，长度一定超过阈值" for i in range(6))
        + f"\n{p}"
        for p in range(1, 4)
    ]
    # 同一短句在正文里重复 3 次，属于正文，不应被当成水印删掉
    pages.append("水印文字\n重复出现的正文短句\n重复出现的正文短句\n重复出现的正文短句\n4")

    out = clean_text(pages)
    lines = out.splitlines()
    body = [line for line in lines if line.startswith("第")]
    assert len(body) == 18, f"期望 18 行正文，实际 {len(body)}：{lines}"
    assert "水印文字" not in out, "跨页重复的短行应作为水印被去掉"
    assert lines.count("重复出现的正文短句") == 3, "正文内重复短句不应被删除"
    assert not any(line.isdigit() for line in lines), "纯页码行应被去掉"


def check_language_and_type() -> None:
    zh_resume = "我的简历\n教育背景\n" + fake_zh(1200)
    assert detect_language(zh_resume) == "zh"
    assert detect_doc_type("我的简历.pdf", zh_resume) == "中文简历"

    en_resume = (
        "Resume\nEducation\n"
        + "Bachelor of Science in Computer Science, focusing on natural language "
        "processing, machine learning, and distributed systems engineering. " * 40
    )
    assert detect_language(en_resume) == "en"
    assert detect_doc_type("my_resume.pdf", en_resume) == "英文简历"

    zh_plan = "研究计划\n一、研究背景\n" + fake_zh(2500)
    assert detect_doc_type("研究计划.pdf", zh_plan) == "中文研究计划"

    en_plan = (
        "Research Proposal\n1. Research Background\n"
        + "This proposal studies interpretability and reasoning of large language models, "
        "and validates the approach through controlled comparison experiments. " * 60
    )
    assert detect_doc_type("plan.pdf", en_plan) == "英文研究计划"


def check_counts_and_density() -> None:
    assert count_chars(" 中 文 测 试 ") == 4
    assert count_words("hello world, foo-bar") == 3

    good = fake_zh(2400)
    density = semantic_density(good, "zh")
    assert density >= 0.6, f"正常中文文本密度应达标，实际 {density}"

    repetitive = "研究研究" * 1200
    assert semantic_density(repetitive, "zh") < 0.6, "重复灌水文本不应通过密度校验"

    garbage = "".join(chr(0x2500 + (i % 60)) for i in range(2000))
    assert semantic_density(garbage, "zh") < 0.6, "乱码文本不应通过密度校验"

    english = " ".join(f"term{i}" for i in range(400))
    assert semantic_density(english, "en") >= 0.6, "正常英文文本密度应达标"


def check_offline_match() -> None:
    user = (
        "研究背景：自然语言处理与文本生成是我的主要方向。"
        "我使用深度学习完成文本生成任务，并在检索增强生成上做了实验。"
        "文本生成的评测依赖自然语言处理指标，机器学习是基础。"
    )
    related = Supervisor(
        name="张伟",
        university="清华大学",
        research_areas=["自然语言处理", "机器学习"],
        papers=["检索增强生成方法研究", "Large language model alignment"],
    )
    unrelated = Supervisor(
        name="李强",
        university="清华大学",
        research_areas=["计算机体系结构"],
        papers=["Cache coherence protocols for many-core processors"],
    )

    hit = score_offline(user, related)
    miss = score_offline(user, unrelated)

    assert hit.overlap, f"相关导师应有重合关键词：{hit.reason}"
    assert hit.score > miss.score, f"相关导师分数应更高：{hit.score} vs {miss.score}"
    assert hit.score >= 40, f"相关导师至少应为中等匹配：{hit.reason}"
    assert miss.level == "低", f"不相关导师应为低匹配：{miss.reason}"


def check_db() -> None:
    """主页 URL 去重、学位过滤、PDF 缓存往返。"""
    import tempfile

    from app.data.db import Database

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")

        first = Supervisor(
            name="张伟",
            university="清华大学",
            homepage="https://example.com/zhangwei",
            degree_types=["博士"],
        )
        sup_id = db.upsert_supervisor(first)
        # 同一主页再存一次：应更新而不是新增（增量去重）
        first.title = "教授"
        assert db.upsert_supervisor(first) == sup_id, "同一主页应复用同一行"
        assert db.count_supervisors() == 1
        assert db.known_homepages() == {"https://example.com/zhangwei"}

        db.upsert_supervisor(
            Supervisor(
                name="李强",
                university="北京大学",
                homepage="https://example.com/liqiang",
                degree_types=["硕士"],
            )
        )
        # 无主页的导师不参与唯一约束，可重复插入
        db.upsert_supervisor(Supervisor(name="王五", university="浙江大学"))
        db.upsert_supervisor(Supervisor(name="王五", university="浙江大学"))
        assert db.count_supervisors() == 4

        phd = {s.name for s in db.list_supervisors("博士")}
        assert phd == {"张伟", "王五"}, f"博士筛选应含未标注招生的导师：{phd}"
        master = {s.name for s in db.list_supervisors("硕士")}
        assert master == {"李强", "王五"}, f"硕士筛选结果不对：{master}"

        # 院校过滤：不在勾选院校内的导师不能进匹配池
        # （用户反馈：只勾了 3 所院校，LLM 却把库里十几个院校的导师全评了一遍）
        assert {s.name for s in db.list_supervisors(None, ["北京大学"])} == {"李强"}
        assert {s.name for s in db.list_supervisors(None, ["北京大学", "浙江大学"])} == {
            "李强",
            "王五",
        }
        assert {s.name for s in db.list_supervisors(None, [])} == {"张伟", "李强", "王五"}, (
            "空院校列表应等同不限"
        )
        # 两个过滤条件是「与」关系：北大没有博士，结果应为空
        assert db.list_supervisors("博士", ["北京大学"]) == []
        assert {s.name for s in db.list_supervisors("硕士", ["浙江大学"])} == {"王五"}
        assert db.count_supervisors(["北京大学"]) == 1
        assert db.count_supervisors() == 4

        db.put_pdf_cache("abc", "中文简历", "正文", 2, 0, 0.9)
        cached = db.get_pdf_cache("abc")
        assert cached and cached["doc_type"] == "中文简历" and cached["text"] == "正文"

        db.clear_all()
        assert db.count_supervisors() == 0 and db.get_pdf_cache("abc") is None
        assert db.list_supervisors("博士") == []
        db.close()


def check_cancel_is_interruptible() -> None:
    """取消信号必须能打断限速等待与重试退避。

    用户反馈「点取消要等很久」就是这个点：等待写成 time.sleep(1~2) 的话，
    点取消最坏要等满一次退避才生效。
    """
    from app.services.sources import HttpFetcher

    state = {"stop": False}
    fetcher = HttpFetcher(
        min_interval=5.0, max_interval=5.0, should_stop=lambda: state["stop"]
    )

    # 未取消时正常等满
    start = time.monotonic()
    assert fetcher._sleep(0.5) is True
    assert 0.4 <= time.monotonic() - start < 1.5, "正常等待时长不对"

    # 取消后最多等一个轮询间隔
    state["stop"] = True
    start = time.monotonic()
    assert fetcher._sleep(5.0) is False
    assert time.monotonic() - start < 1.0, "取消没有打断 sleep"

    # 限速等待同样可被打断
    state["stop"] = False
    assert fetcher._throttle("h") is True, "首次调用没有历史时间戳，不该等待"
    state["stop"] = True
    start = time.monotonic()
    assert fetcher._throttle("h") is False
    assert time.monotonic() - start < 1.0, "取消没有打断限速等待"


def check_display_name() -> None:
    """CSRankings 姓名带 DBLP 消歧后缀，展示与检索时要去掉。"""
    from app.services.sources import display_name

    assert display_name("Adam Yang 0001") == "Adam Yang"
    assert display_name("Yang Chen 0008") == "Yang Chen"
    assert display_name("Baobao Chang") == "Baobao Chang"
    assert display_name("  Xu Wei  ") == "Xu Wei"
    # 只有 4 位纯数字后缀算消歧号，姓名里的其它数字不能砍
    assert display_name("Henry Ford 2") == "Henry Ford 2"


def check_institutions() -> None:
    """设置页的院校勾选列表依赖这个解析：名称原样保留，国家代码统一大写。"""
    import tempfile

    from app.services.sources import CSRankingsSource, HttpFetcher

    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        (cache / "institutions.csv").write_text(
            "institution,region,countryabbrv,homepage\n"
            "Tsinghua University,asia,cn,https://www.tsinghua.edu.cn/\n"
            "Univ. of Illinois at Urbana-Champaign,northamerica,us,https://cs.illinois.edu/\n"
            "\n"
            ",asia,cn,https://example.com/\n",
            encoding="utf-8",
        )
        # 缓存未过期，这里不会发网络请求
        source = CSRankingsSource(HttpFetcher(retries=1), cache, ttl_days=7)
        assert source.institutions() == [
            ("Tsinghua University", "CN"),
            ("Univ. of Illinois at Urbana-Champaign", "US"),
        ], source.institutions()


def check_per_school_limit() -> None:
    """每校上限按「每所院校各自计数」，不是所有院校共用一个总量。

    用户要求：每校最多抓 N 位，且最大一档是「该校在 CSRankings 里的全部导师」。
    """
    import tempfile

    from app.services.sources import CSRankingsSource, HttpFetcher

    rows = ["name,affiliation,homepage,scholarid,orcid"]
    for school, prefix in (("A University", "AU"), ("B University", "BU")):
        for i in range(5):
            rows.append(f"{prefix} Person {i},{school},https://example.com/{prefix}{i},,")

    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        (cache / "csrankings.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        # 只放表头：让研究方向解析不走网络，但结果为空
        (cache / "generated-author-info.csv").write_text(
            "name,dept,area,count,adjustedcount,year\n", encoding="utf-8"
        )
        source = CSRankingsSource(HttpFetcher(retries=1), cache, ttl_days=7)

        # 每校 2 位：两所学校各 2 位共 4 位；旧的「总量 2」只会给出 2 位
        picked = source.faculty(["A University", "B University"], per_school=2)
        assert len(picked) == 4, [s.name for s in picked]
        assert {s.university for s in picked} == {"A University", "B University"}

        # 0 = 该校全部导师
        everything = source.faculty(["A University", "B University"], per_school=0)
        assert len(everything) == 10, len(everything)

        # 本地已有（主页命中）的不再返回，全部命中时直接返回空
        known = {s.homepage for s in picked}
        assert source.faculty(["A University"], per_school=2, skip_homepages=known) == []

        # 严格按勾选：只勾 B 校时 A 校的导师不能混进来
        only_b = source.faculty(["B University"], per_school=0)
        assert {s.university for s in only_b} == {"B University"}


def check_config_migration() -> None:
    """旧配置的 max_faculty 是「所有院校总量」，语义已变，不能沿用。"""
    import json
    import tempfile

    import app.config as config

    with tempfile.TemporaryDirectory() as tmp:
        original = config.CONFIG_PATH
        config.CONFIG_PATH = Path(tmp) / "config.json"
        try:
            config.CONFIG_PATH.write_text(
                json.dumps({"crawler": {"max_faculty": 120, "schools": ["A University"]}}),
                encoding="utf-8",
            )
            cfg = config.load()
            assert "max_faculty" not in cfg["crawler"], "旧键应被丢弃，否则会一直被写回配置"
            assert cfg["crawler"]["max_per_school"] == 30
            assert cfg["crawler"]["schools"] == ["A University"], "用户勾选的院校不能被默认值覆盖"
        finally:
            config.CONFIG_PATH = original


def check_dead_host_is_not_retried() -> None:
    """连不上的主机不该重试，否则进度条会在同一位导师上冻住好几分钟。

    用户反馈「离线匹配过程中进度卡住不动」的实测数据：sites.google.com 的
    robots.txt 花 60 秒，随后 3 次连接超时共 186 秒，合计 246 秒没有任何进度更新。
    读取超时是服务端慢，仍应重试。
    """
    from urllib.robotparser import RobotFileParser

    import requests

    from app.services.sources import HttpFetcher

    class FakeSession:
        def __init__(self, exc) -> None:
            self.exc = exc
            self.calls = 0

        def get(self, _url, **_kwargs):
            self.calls += 1
            raise self.exc

    def build(exc, retries: int = 3):
        fetcher = HttpFetcher(retries=retries, should_stop=lambda: False)
        parser = RobotFileParser()
        parser.allow_all = True  # 跳过 robots 检查，只测 get 的重试策略
        fetcher._robots["example.com"] = parser
        fetcher._session = FakeSession(exc)
        return fetcher

    fetcher = build(requests.ConnectTimeout("连不上"))
    assert fetcher.get("https://example.com/a") is None
    assert fetcher._session.calls == 1, "连接超时不该重试"

    fetcher = build(requests.ConnectionError("连接被重置"))
    assert fetcher.get("https://example.com/a") is None
    assert fetcher._session.calls == 1, "连接被重置不该重试"

    fetcher = build(requests.ReadTimeout("响应慢"), retries=2)
    assert fetcher.get("https://example.com/a") is None
    assert fetcher._session.calls == 2, "读取超时应该重试满 retries 次"


def main() -> None:
    for check in (
        check_trim,
        check_clean,
        check_language_and_type,
        check_counts_and_density,
        check_offline_match,
        check_db,
        check_cancel_is_interruptible,
        check_display_name,
        check_institutions,
        check_per_school_limit,
        check_config_migration,
        check_dead_host_is_not_retried,
    ):
        check()
        print(f"OK  {check.__name__}")
    print("全部自检通过")


if __name__ == "__main__":
    main()
