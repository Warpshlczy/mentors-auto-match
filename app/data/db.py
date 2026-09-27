"""数据层：SQLite 建表 + DAO。

业务层与 GUI 层都只通过本模块访问数据库，不直接持有 sqlite3 连接。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.data.models import Supervisor

SCHEMA = """
CREATE TABLE IF NOT EXISTS supervisors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    university     TEXT NOT NULL,
    department     TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    homepage       TEXT NOT NULL DEFAULT '',
    orcid          TEXT NOT NULL DEFAULT '',
    research_areas TEXT NOT NULL DEFAULT '[]',
    papers         TEXT NOT NULL DEFAULT '[]',
    degree_types   TEXT NOT NULL DEFAULT '[]',
    source         TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL DEFAULT ''
);
-- 增量去重靠这条唯一索引：homepage 为空的行不参与约束
CREATE UNIQUE INDEX IF NOT EXISTS idx_sup_homepage
    ON supervisors(homepage) WHERE homepage <> '';
CREATE INDEX IF NOT EXISTS idx_sup_university ON supervisors(university);

CREATE TABLE IF NOT EXISTS pdf_cache (
    sha256     TEXT PRIMARY KEY,
    doc_type   TEXT NOT NULL,
    text       TEXT NOT NULL,
    char_count INTEGER NOT NULL DEFAULT 0,
    word_count INTEGER NOT NULL DEFAULT 0,
    density    REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _row_to_supervisor(row: sqlite3.Row) -> Supervisor:
    return Supervisor(
        id=row["id"],
        name=row["name"],
        university=row["university"],
        department=row["department"],
        title=row["title"],
        homepage=row["homepage"],
        orcid=row["orcid"],
        research_areas=json.loads(row["research_areas"]),
        papers=json.loads(row["papers"]),
        degree_types=json.loads(row["degree_types"]),
        source=row["source"],
    )


class Database:
    """SQLite 访问对象。

    ponytail: 一把全局锁 + check_same_thread=False。数据量在千级导师、单人使用，
    锁竞争可以忽略；真要并发写再换连接池。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """给旧库补新增的列。

        ponytail: CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，所以 orcid 这种
        后加的字段要单独补。列再攒多几轮就该换成版本号 + 迁移脚本了。
        """
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(supervisors)")}
        if "orcid" not in cols:
            self._conn.execute(
                "ALTER TABLE supervisors ADD COLUMN orcid TEXT NOT NULL DEFAULT ''"
            )

    # ---------- 导师 ----------

    def known_homepages(self) -> set[str]:
        """已爬取过的导师主页，用于增量去重。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT homepage FROM supervisors WHERE homepage <> ''"
            ).fetchall()
        return {row["homepage"] for row in rows}

    def upsert_supervisor(self, sup: Supervisor) -> int:
        values = (
            sup.name,
            sup.university,
            sup.department,
            sup.title,
            sup.orcid,
            json.dumps(sup.research_areas, ensure_ascii=False),
            json.dumps(sup.papers, ensure_ascii=False),
            json.dumps(sup.degree_types, ensure_ascii=False),
            sup.source,
            _now(),
        )
        with self._lock, self._conn:
            existing = None
            if sup.homepage:
                existing = self._conn.execute(
                    "SELECT id FROM supervisors WHERE homepage = ?", (sup.homepage,)
                ).fetchone()
            if existing:
                self._conn.execute(
                    """UPDATE supervisors SET name=?, university=?, department=?, title=?,
                       orcid=?, research_areas=?, papers=?, degree_types=?, source=?, updated_at=?
                       WHERE id=?""",
                    values + (existing["id"],),
                )
                return int(existing["id"])
            cur = self._conn.execute(
                """INSERT INTO supervisors
                   (name, university, department, title, homepage, orcid, research_areas,
                    papers, degree_types, source, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                values[:4] + (sup.homepage,) + values[4:],
            )
            return int(cur.lastrowid)

    def list_supervisors(
        self, degree: str | None = None, universities: list[str] | None = None
    ) -> list[Supervisor]:
        """degree 为 "硕士" / "博士" 时按招生类型过滤，universities 非空时按院校过滤。

        未标注招生类型（degree_types 为空）的导师会被保留，否则结果会大面积为空。
        院校过滤放在 SQL 里而不是 Python 里：库是累积的，勾选 3 所院校时不该把
        全库几百位导师读出来再丢掉。
        """
        clauses: list[str] = []
        params: list[str] = []
        if degree:
            clauses.append("(degree_types = '[]' OR degree_types LIKE ?)")
            params.append(f'%"{degree}"%')
        if universities:
            placeholders = ",".join("?" * len(universities))
            clauses.append(f"university IN ({placeholders})")
            params.extend(universities)

        sql = "SELECT * FROM supervisors"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_supervisor(row) for row in rows]

    def count_supervisors(self, universities: list[str] | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM supervisors"
        params: tuple = ()
        if universities:
            sql += f" WHERE university IN ({','.join('?' * len(universities))})"
            params = tuple(universities)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["n"])

    # ---------- PDF 清洗结果缓存 ----------

    def get_pdf_cache(self, sha256: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pdf_cache WHERE sha256 = ?", (sha256,)
            ).fetchone()
        if row is None:
            return None
        return {
            "doc_type": row["doc_type"],
            "text": row["text"],
            "char_count": row["char_count"],
            "word_count": row["word_count"],
            "density": row["density"],
        }

    def put_pdf_cache(
        self,
        sha256: str,
        doc_type: str,
        text: str,
        char_count: int,
        word_count: int,
        density: float,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO pdf_cache
                   (sha256, doc_type, text, char_count, word_count, density, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (sha256, doc_type, text, char_count, word_count, density, _now()),
            )

    # ---------- 维护 ----------

    def clear_all(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM supervisors")
            self._conn.execute("DELETE FROM pdf_cache")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
