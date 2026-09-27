"""PDF 解析校验模块。

复用点（见 README「开源复用对照表」）：
- interviewstreet/hiring-agent：中间产物按 key 落盘缓存的结构。
  原项目用文件名当 key，这里换成 sha256，重复上传才真的能命中。
- srbhr/Resume-Matcher：文本清洗与关键词抽取的规则思路（不引入其 FastAPI/Node 依赖）。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

# ---------- 可调参数 ----------

#: 页眉页脚裁剪比例（页面上下各裁 10%）
HEADER_FOOTER_RATIO = 0.10
#: 短行过滤阈值：清洗后长度小于该值的行直接丢弃（页码、图标残留）
MIN_LINE_LEN = 4
#: 语义密度阈值
MIN_DENSITY = 0.6
#: 词汇多样性统计窗口（取前 N 个 token 计算 TTR，避免长文天然拉低多样性）
DIVERSITY_WINDOW = 400

#: 文档类型 -> 阈值。unit 为 chars 时按去空白字符数，words 时按英文单词数
THRESHOLDS: dict[str, dict] = {
    "中文简历": {"unit": "chars", "min": 800},
    "英文简历": {"unit": "words", "min": 500},
    "中文研究计划": {"unit": "chars", "min": 2000},
    "英文研究计划": {"unit": "words", "min": 1200},
}

# ---------- 正则 ----------

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 允许数字，否则 Python3 / GPT4 / ResNet50 这类词会被截断成 Python / GPT / ResNet
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-']+")
EFFECTIVE_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
JUNK_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd]")
OK_PUNCT = set(".,;:!?()[]{}<>/-_+=*&%$#@'\"`~|\\，。；：！？（）【】《》、·—…“”‘’")

RESUME_HINTS = ("简历", "resume", "cv", "curriculum vitae")
PLAN_HINTS = ("研究计划", "research plan", "research proposal", "proposal", "statement of purpose")
PLAN_SECTIONS = (
    "研究目标", "研究背景", "研究问题", "时间安排", "研究方法",
    "research objective", "research question", "methodology", "timeline", "expected outcome",
)
RESUME_SECTIONS = (
    "教育背景", "工作经历", "项目经历", "技能", "获奖",
    "education", "experience", "skills", "projects", "awards", "publications",
)


@dataclass
class PdfCheck:
    ok: bool
    doc_type: str = ""
    sha256: str = ""
    text: str = ""
    char_count: int = 0
    word_count: int = 0
    density: float = 0.0
    reason: str = ""
    cached: bool = False


# ---------- 基础工具 ----------


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def count_chars(text: str) -> int:
    """有效字符数：去掉所有空白后的字符数。"""
    return len(re.sub(r"\s", "", text))


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


def detect_language(text: str) -> str:
    """按汉字与拉丁字母的数量占比判定中/英。"""
    cjk = len(CJK_RE.findall(text))
    latin = sum(len(w) for w in WORD_RE.findall(text))
    if cjk + latin == 0:
        return "zh"
    return "zh" if cjk / (cjk + latin) >= 0.3 else "en"


def _has_hint(text: str, hints: tuple[str, ...]) -> bool:
    for hint in hints:
        if hint.isascii():
            if re.search(rf"\b{re.escape(hint)}\b", text):
                return True
        elif hint in text:
            return True
    return False


def detect_doc_type(filename: str, text: str) -> str:
    """自动识别文档类型，返回 THRESHOLDS 的四个 key 之一。

    先看文件名 + 正文开头 2000 字的类型关键词，识别不出再按章节词得分兜底。
    """
    lang = "中文" if detect_language(text) == "zh" else "英文"
    head = (filename + "\n" + text[:2000]).lower()
    is_resume = _has_hint(head, RESUME_HINTS)
    is_plan = _has_hint(head, PLAN_HINTS)

    if is_resume and not is_plan:
        kind = "简历"
    elif is_plan and not is_resume:
        kind = "研究计划"
    else:
        plan_score = sum(head.count(k) for k in PLAN_SECTIONS)
        resume_score = sum(head.count(k) for k in RESUME_SECTIONS)
        kind = "研究计划" if plan_score > resume_score else "简历"
    return lang + kind


def trim_page(text: str, ratio: float = HEADER_FOOTER_RATIO) -> str:
    """按行裁掉页面上下各 ratio，近似过滤页眉页脚。

    ponytail: 用行数比例代替 PDF 坐标裁剪，省掉解析页面几何的成本。
    上限是页眉页脚若占满多行会被裁不干净；要精确就改成读取文字块的 y 坐标。
    """
    lines = text.splitlines()
    cut = int(len(lines) * ratio)
    if cut <= 0 or len(lines) <= 4:
        return text
    return "\n".join(lines[cut : len(lines) - cut])


def _garbage_ratio(line: str) -> float:
    """一行里「不像正常文本」的字符占比。"""
    bad = sum(1 for c in line if not (c.isalnum() or c in OK_PUNCT))
    return bad / len(line)


def clean_text(pages: list[str]) -> str:
    """清洗：裁页眉页脚 -> 去乱码/控制符 -> 丢短行 -> 去跨页重复的水印残留。"""
    per_page: list[list[str]] = []
    for page in pages:
        lines = [line.strip() for line in trim_page(page).splitlines()]
        per_page.append([line for line in lines if line])

    # 在多个页面重复出现且很短的整行 = 页眉/页脚/水印残留。
    # 按「出现在几个不同页面」而不是「全文出现几次」计数，避免误删正文里的重复短句。
    pages_of: Counter[str] = Counter()
    for lines in per_page:
        pages_of.update(set(lines))
    repeated = {line for line, n in pages_of.items() if n >= 2 and len(line) <= 30}

    out: list[str] = []
    for lines in per_page:
        for raw in lines:
            line = JUNK_RE.sub("", raw).strip()
            if len(line) < MIN_LINE_LEN or line in repeated:
                continue
            if _garbage_ratio(line) > 0.4:
                continue
            out.append(re.sub(r"[ \t]+", " ", line))
    return "\n".join(out)


def semantic_density(text: str, lang: str) -> float:
    """语义密度代理指标 = 0.5 * 有效字符占比 + 0.5 * 标准化窗口词汇多样性。

    ponytail: 需求只给了「语义密度 ≥ 0.6」而没给定义。这里把它落成一个可复现、
    可回归的数字代理指标，阈值 0.6 是按四类文档实测标定的。换文档类型需重新标定。
    """
    if not text:
        return 0.0
    effective = len(EFFECTIVE_RE.findall(text)) / len(text)

    if lang == "zh":
        tokens: list[str] = []
        for i in range(len(text) - 1):
            pair = text[i : i + 2]
            if all(ch.isalnum() for ch in pair):
                tokens.append(pair)
    else:
        tokens = [w.lower() for w in WORD_RE.findall(text)]

    tokens = tokens[:DIVERSITY_WINDOW]
    diversity = len(set(tokens)) / len(tokens) if tokens else 0.0
    return round(0.5 * effective + 0.5 * diversity, 3)


def extract_pages(path: str | Path) -> list[str]:
    """逐页抽取文本。加密 PDF 先尝试空密码解密。"""
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        reader.decrypt("")
    return [(page.extract_text() or "") for page in reader.pages]


# ---------- 对外入口 ----------


class PdfChecker:
    """PDF 有效内容校验。校验通过的结果按 sha256 缓存，重复上传不再解析。"""

    def __init__(self, db) -> None:
        self._db = db

    def check(self, path: str | Path) -> PdfCheck:
        path = Path(path)
        if not path.exists():
            return PdfCheck(ok=False, reason=f"文件不存在：{path}")

        sha = sha256_file(path)

        try:
            hit = self._db.get_pdf_cache(sha)
        except Exception:
            hit = None
        if hit:
            return PdfCheck(ok=True, sha256=sha, cached=True, **hit)

        try:
            pages = extract_pages(path)
        except Exception as exc:  # pypdf 对损坏文件会抛各种异常
            return PdfCheck(ok=False, sha256=sha, reason=f"文件损坏，无法解析：{exc}")

        if not any(p.strip() for p in pages):
            return PdfCheck(
                ok=False,
                sha256=sha,
                reason="文件损坏或为扫描件：未提取到任何文本层，请上传可选中文字的 PDF",
            )

        text = clean_text(pages)
        if not text.strip():
            return PdfCheck(
                ok=False,
                sha256=sha,
                reason="清洗后无有效内容：疑似整页水印或纯图片扫描件",
            )

        lang = detect_language(text)
        doc_type = detect_doc_type(path.name, text)
        chars = count_chars(text)
        words = count_words(text)
        density = semantic_density(text, lang)

        spec = THRESHOLDS[doc_type]
        actual = chars if spec["unit"] == "chars" else words
        unit_cn = "字" if spec["unit"] == "chars" else "单词"
        if actual < spec["min"]:
            return PdfCheck(
                ok=False,
                sha256=sha,
                doc_type=doc_type,
                char_count=chars,
                word_count=words,
                density=density,
                reason=(
                    f"{doc_type}有效{unit_cn}数 {actual} 不足，要求 ≥ {spec['min']}。"
                    f"文件可能不完整，或页眉页脚裁剪比例（{HEADER_FOOTER_RATIO:.0%}）偏大。"
                ),
            )
        if density < MIN_DENSITY:
            return PdfCheck(
                ok=False,
                sha256=sha,
                doc_type=doc_type,
                char_count=chars,
                word_count=words,
                density=density,
                reason=(
                    f"语义密度 {density} 低于阈值 {MIN_DENSITY}。"
                    "常见原因：PDF 为扫描件导致文字识别乱码、或整页水印干扰。"
                ),
            )

        self._db.put_pdf_cache(sha, doc_type, text, chars, words, density)
        return PdfCheck(
            ok=True,
            doc_type=doc_type,
            sha256=sha,
            text=text,
            char_count=chars,
            word_count=words,
            density=density,
        )
