"""匹配打分引擎。

两种模式：
- LLM 模式：配置了 API 后启用，输出 0-100 分 + 匹配理由 + 适配点
- 离线降级模式：无 LLM（或某条 LLM 调用失败）时自动切换，基于 RapidFuzz
  的关键词重合度输出 高/中/低 匹配等级与重合关键词

复用点：rapidfuzz/RapidFuzz（MIT）的 fuzz / process 打分器。
"""

from __future__ import annotations

import re
from collections import Counter

from rapidfuzz import fuzz, process

from app.data.models import MatchResult, Supervisor
from app.services.llm import LLMClient, extract_json

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-']+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

STOPWORDS = frozenset(
    """a an the and or of to in for on with at by from as is are was were be been being
    this that these those it its their his her our your my we you they he she i not no
    but if then than so such can could would should may might will shall do does did done
    have has had having about into over under between during before after above below up
    down out off again further once here there when where why how all any both each few
    more most other some only own same too very just now also using used use based""".split()
)

#: 用户侧关键词上限（英文词 + 中文 2-gram）
USER_KEYWORD_LIMIT = 80
#: 命中判定阈值
HIT_THRESHOLD = 85
#: 送进 LLM 的学生材料长度上限
PROFILE_LIMIT = 3000

LLM_SYSTEM = "你是学术导师匹配评估助手。只输出 JSON 对象，不要输出任何其他文字。"
LLM_TEMPLATE = """学生材料（简历/研究计划节选）：
{profile}

候选导师信息：
姓名：{name}
院校：{university}
职称：{title}
研究方向：{areas}
近5年论文：{papers}

请评估该导师与学生研究背景的匹配程度，并只输出如下 JSON：
{{"score": <0-100 的整数>, "reason": "<不超过 120 字的匹配理由>", "fit_points": ["<适配点>", "<适配点>"]}}
"""


def _level(score: int) -> str:
    if score >= 70:
        return "高"
    return "中" if score >= 40 else "低"


def user_keywords(text: str, limit: int = USER_KEYWORD_LIMIT) -> list[str]:
    """用户文档 -> 关键词集合：英文词（去停用词）+ 中文 2-gram。

    中文侧只保留全文出现 ≥ 2 次的 2-gram，用来压掉人名、机构名碎片这类一次性噪声。
    """
    out: list[str] = []
    seen: set[str] = set()

    for word in WORD_RE.findall(text):
        word = word.lower()
        if len(word) >= 3 and word not in STOPWORDS and word not in seen:
            seen.add(word)
            out.append(word)

    bigrams: Counter[str] = Counter()
    for i in range(len(text) - 1):
        if CJK_RE.match(text[i]) and CJK_RE.match(text[i + 1]):
            bigrams[text[i] + text[i + 1]] += 1
    for bigram, count in bigrams.most_common():
        if count < 2:
            break
        if bigram not in seen:
            seen.add(bigram)
            out.append(bigram)

    return out[:limit]


def sup_terms(sup: Supervisor) -> list[str]:
    """导师侧词条：研究方向/职称/院系整词 + 论文标题里的英文词。"""
    terms: list[str] = []
    seen: set[str] = set()

    for raw in (*sup.research_areas, sup.title, sup.department):
        raw = (raw or "").strip()
        if raw and raw.lower() not in seen:
            seen.add(raw.lower())
            terms.append(raw)

    for title in sup.papers:
        for word in WORD_RE.findall(title):
            word = word.lower()
            if len(word) >= 3 and word not in STOPWORDS and word not in seen:
                seen.add(word)
                terms.append(word)

    return terms


def _match_keyword(keyword: str, terms: list[str]) -> tuple[str | None, float]:
    """返回 (命中的导师侧词条, 最高匹配分 0-100)。

    先用 fuzz.ratio 找整体相近的词，再用 fuzz.partial_ratio 兜中文：
    用户侧 2-gram「机器」要能对上导师侧标签「机器学习」。
    """
    best_term: str | None = None
    best_score = 0.0
    for scorer in (fuzz.ratio, fuzz.partial_ratio):
        hit = process.extractOne(keyword, terms, scorer=scorer)
        if hit and hit[1] > best_score:
            best_term, best_score = hit[0], float(hit[1])
    if best_score < HIT_THRESHOLD:
        return None, best_score
    return best_term, best_score


def score_offline(
    user_text: str, sup: Supervisor, keywords: list[str] | None = None
) -> MatchResult:
    """离线打分：0.6 * 关键词覆盖率 + 0.4 * 平均最佳模糊匹配分。"""
    kws = user_keywords(user_text) if keywords is None else keywords
    terms = sup_terms(sup)

    if not kws or not terms:
        return MatchResult(
            supervisor=sup,
            score=0,
            level="低",
            reason="导师信息不足（缺少研究方向与论文），无法评估匹配度。",
            mode="offline",
        )

    hits: list[str] = []
    score_sum = 0.0
    for keyword in kws:
        term, best = _match_keyword(keyword, terms)
        score_sum += best
        if term and term not in hits:
            hits.append(term)

    coverage = len(hits) / len(kws)
    similarity = score_sum / len(kws) / 100.0
    score = max(0, min(100, int(round(60 * coverage + 40 * similarity))))

    reason = (
        f"关键词重合 {len(hits)}/{len(kws)}（覆盖率 {coverage:.0%}），"
        f"平均模糊匹配度 {similarity:.2f}。"
    )
    if hits:
        reason += "重合方向：" + "、".join(hits[:8]) + "。"

    return MatchResult(
        supervisor=sup,
        score=score,
        level=_level(score),
        reason=reason,
        overlap=hits[:20],
        mode="offline",
    )


class Matcher:
    def __init__(self, cfg: dict, llm: LLMClient | None = None) -> None:
        self._cfg = cfg
        self._llm = llm

    def _use_llm(self) -> bool:
        return self._llm is not None and self._llm.available()

    def _llm_match(self, user_text: str, sup: Supervisor) -> MatchResult | None:
        """调 LLM 打分；解析失败或请求失败返回 None，由调用方降级。"""
        prompt = LLM_TEMPLATE.format(
            profile=user_text[:PROFILE_LIMIT],
            name=sup.name,
            university=sup.university,
            title=sup.title or "未获取",
            areas="、".join(sup.research_areas) or "未获取",
            papers="；".join(sup.papers[:10]) or "未获取",
        )
        data = extract_json(self._llm.chat(LLM_SYSTEM, prompt) or "")
        if not data:
            return None
        try:
            score = max(0, min(100, int(round(float(data.get("score", 0))))))
        except (TypeError, ValueError):
            return None
        fit_points = [
            str(point).strip() for point in (data.get("fit_points") or []) if str(point).strip()
        ][:5]
        return MatchResult(
            supervisor=sup,
            score=score,
            level=_level(score),
            reason=str(data.get("reason") or "").strip() or "LLM 未给出匹配理由。",
            fit_points=fit_points,
            mode="llm",
        )

    def match_all(
        self,
        user_text: str,
        supervisors: list[Supervisor],
        on_progress=None,
        should_stop=None,
    ) -> list[MatchResult]:
        """给所有导师打分并按匹配度从高到低排序。

        on_progress(message: str, fraction: float | None)
        should_stop() -> bool
        """
        stop = should_stop or (lambda: False)
        if not supervisors:
            return []

        keywords = user_keywords(user_text)
        use_llm = self._use_llm()
        mode_cn = "LLM" if use_llm else "离线 RapidFuzz"
        results: list[MatchResult] = []
        fallback = 0

        # ponytail: LLM 模式串行调用，导师上百位时会明显变慢。
        # 上限是接口限流风险；要提速就加线程池 + 并发上限。
        for index, sup in enumerate(supervisors, 1):
            if stop():
                break
            if on_progress:
                on_progress(
                    f"[{mode_cn}] ({index}/{len(supervisors)}) {sup.name} · {sup.university}",
                    (index - 1) / len(supervisors),
                )

            result = self._llm_match(user_text, sup) if use_llm else None
            if result is None:
                if use_llm:
                    fallback += 1
                result = score_offline(user_text, sup, keywords)
            results.append(result)

        if fallback:
            if on_progress:
                on_progress(f"{fallback} 位导师的 LLM 评估失败，已降级为离线打分。", None)

        results.sort(key=lambda item: item.score, reverse=True)
        return results
