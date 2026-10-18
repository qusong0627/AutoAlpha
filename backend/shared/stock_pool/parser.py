"""股票池文件解析引擎。

把用户上传的 CSV / TXT（内容格式随意）解析成规范股票池：

```
上传内容 → 提取候选 token → 与 data/stocks/stocks_index.json 对比 → 匹配报告
```

设计要点
--------
1. **全单元格扫描**：不假设用户把代码放在第几列。逐行把所有单元格依次尝试匹配，
   第一个命中的单元格即为该行的股票（因此 "600519,贵州茅台" 与
   "贵州茅台,600519" 都能正确解析）。可用 `column` 参数强制指定列。
2. **代码与名称双通道**：代码支持 `600519` / `SH600519` / `600519.SH` / `sh600519`；
   名称支持中文简称（全角字母与空白会被归一化，故 "万 科Ａ" 可匹配到 "万科A"）。
3. **格式合法但索引里没有** 会单独归类（`not_in_index`），用于解释北交所/新股/退市
   —— `stocks_index.json` 目前只覆盖沪深两市，没有 BJ 标的。
4. **名称宽松兜底**：去掉 `*` / `ST` 前缀后再试一次；因本地索引名称唯一，
   仅当唯一命中时才接受，并标记 `name_loose` 交由用户复核。
5. 纯函数 + 进程内缓存（按文件 mtime 失效），不依赖数据库与 engine。
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Sequence

from .normalize import normalize_market, to_api_symbol, to_storage_symbol

logger = logging.getLogger(__name__)

# 索引文件路径（优先级从高到低）
_ENV_PATH = "STOCK_INDEX_JSON_PATH"


def _candidate_paths() -> list[Path]:
    """索引文件候选路径。

    `./data:/data` 是 compose 的挂载口径 → 容器内为 `/data/stocks/...`；
    `parents[3]` 指向仓库根，保证本地开发（Windows）也能命中。
    """
    here = Path(__file__).resolve()
    repo_root = here.parents[3] if len(here.parents) > 3 else here.parent
    return [
        Path("/data/stocks/stocks_index.json"),
        Path("/app/data/stocks/stocks_index.json"),
        repo_root / "data" / "stocks" / "stocks_index.json",
    ]


def resolve_index_path() -> Path | None:
    override = os.getenv(_ENV_PATH, "").strip()
    if override:
        p = Path(override)
        return p if p.exists() else None
    for path in _candidate_paths():
        if path.exists():
            return path
    return None


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    """名称归一化：全角→半角、去除所有空白、统一大写。"""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name))
    s = re.sub(r"\s+", "", s)
    return s.upper()


def _loose_name(name: str) -> str:
    """宽松键：去掉前导 `*` 与 `ST`（含 `*ST` / `ST` / `ＳＴ`）。"""
    s = normalize_name(name)
    s = s.lstrip("*")
    if s.startswith("ST"):
        s = s[2:]
    return s.lstrip("*")


@dataclass(frozen=True)
class IndexEntry:
    symbol: str  # 600036.SH
    code: str  # 600036
    exchange: str  # SH
    name: str  # 招商银行
    name_norm: str  # 招商银行（归一化）

    @property
    def api_symbol(self) -> str:
        return to_api_symbol(self.symbol, "CN")


class StockIndex:
    """stocks_index.json 的内存索引（按代码 / 名称 / 前缀多路查询）。"""

    def __init__(self, entries: Sequence[IndexEntry], source: Path | None = None):
        self.entries: list[IndexEntry] = list(entries)
        self.source = source
        self.by_symbol: dict[str, IndexEntry] = {}
        self.by_code: dict[str, IndexEntry] = {}
        self.by_prefix: dict[str, IndexEntry] = {}
        self.by_name: dict[str, IndexEntry] = {}
        self.by_loose_name: dict[str, list[IndexEntry]] = {}
        for e in self.entries:
            self.by_symbol.setdefault(e.symbol, e)
            self.by_code.setdefault(e.code, e)
            self.by_prefix.setdefault(e.api_symbol, e)
            if e.name_norm:
                self.by_name.setdefault(e.name_norm, e)
                self.by_loose_name.setdefault(_loose_name(e.name), []).append(e)

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def exchanges(self) -> set[str]:
        return {e.exchange for e in self.entries}


_index_cache: tuple[float, StockIndex] | None = None


def load_stock_index(
    path: str | Path | None = None, *, force: bool = False
) -> StockIndex:
    """加载（并缓存）股票索引。文件缺失时返回空索引并告警，不抛异常。"""
    global _index_cache

    resolved = Path(path) if path else resolve_index_path()
    if resolved is None or not Path(resolved).exists():
        logger.warning(
            "股票索引文件未找到（检查 %s 或 %s）",
            _ENV_PATH,
            [str(p) for p in _candidate_paths()],
        )
        return StockIndex([], None)

    fp = Path(resolved)
    try:
        mtime = fp.stat().st_mtime
    except OSError:
        mtime = 0.0

    if not force and _index_cache is not None and _index_cache[0] == mtime:
        return _index_cache[1]

    entries: list[IndexEntry] = []
    try:
        import json

        with fp.open(encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("items", []) or []:
            symbol = str(item.get("symbol") or "").strip()
            code = str(item.get("code") or "").strip()
            exchange = str(item.get("exchange") or "").strip().upper()
            name = str(item.get("name") or "").strip()
            if not symbol or not code:
                continue
            entries.append(
                IndexEntry(
                    symbol=symbol,
                    code=code,
                    exchange=exchange,
                    name=name,
                    name_norm=normalize_name(name),
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("股票索引加载失败 %s: %s", fp, exc)
        return StockIndex([], fp)

    index = StockIndex(entries, fp)
    _index_cache = (mtime, index)
    logger.info("股票索引已加载: %d 条 (%s)", len(index), fp)
    return index


# ---------------------------------------------------------------------------
# 解析报告
# ---------------------------------------------------------------------------
MATCH_CODE = "code"
MATCH_SYMBOL = "symbol"
MATCH_PREFIX = "prefix"
MATCH_EXCHANGE_FIXED = "exchange_fixed"
MATCH_NAME = "name"
MATCH_NAME_LOOSE = "name_loose"
MATCH_NONE = "none"

REASON_NOT_IN_INDEX = "not_in_index"
REASON_UNRECOGNIZED = "unrecognized"
REASON_EMPTY = "empty"

_CODE_RE = re.compile(r"^\d{6}$")
_SUFFIX_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_PREFIX_RE = re.compile(r"^(SH|SZ|BJ)\d{6}$")
_QLIB_RE = re.compile(r"^(sh|sz|bj)\d{6}$")


@dataclass
class ParseRow:
    row_index: int  # 原文件行号（从 1 开始，便于用户对回文件）
    raw: str  # 整行原文（截断展示）
    token: str  # 命中的候选单元
    status: str  # matched | not_in_index | unrecognized
    match_type: str = MATCH_NONE
    symbol: str | None = None  # 后缀式
    api_symbol: str | None = None
    code: str | None = None
    name: str | None = None
    duplicate: bool = False
    reason: str | None = None


@dataclass
class ParseReport:
    total: int = 0
    matched: int = 0
    unmatched: int = 0
    duplicates: int = 0
    not_in_index: int = 0
    rows: list[ParseRow] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)  # 去重后的后缀式
    members: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    encoding: str | None = None
    index_source: str | None = None
    index_size: int = 0

    def as_dict(self, row_limit: int | None = None) -> dict[str, Any]:
        rows = self.rows if row_limit is None else self.rows[:row_limit]
        return {
            "summary": {
                "total": self.total,
                "matched": self.matched,
                "unmatched": self.unmatched,
                "duplicates": self.duplicates,
                "not_in_index": self.not_in_index,
                "unique_symbols": len(self.symbols),
            },
            "rows": [r.__dict__ for r in rows],
            "symbols": self.symbols,
            "members": self.members,
            "warnings": self.warnings,
            "encoding": self.encoding,
            "index_source": self.index_source,
            "index_size": self.index_size,
            "truncated": row_limit is not None and len(self.rows) > len(rows),
        }


# ---------------------------------------------------------------------------
# 候选提取
# ---------------------------------------------------------------------------
SPLIT_RE = re.compile(r"[,;\t|，、；]+")
_HEADER_HINTS = {
    "symbol",
    "code",
    "ticker",
    "stock",
    "stock_code",
    "股票代码",
    "证券代码",
    "代码",
    "股票",
    "名称",
    "股票名称",
    "证券简称",
    "简称",
}


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """按常见中文编码顺序解码（Excel 导出的 CSV 常为 GBK）。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def _cells_of_line(line: str) -> list[str]:
    return [c.strip().strip('"').strip("'").strip() for c in SPLIT_RE.split(line)]


def extract_token_rows(
    content: str, fmt: str | None = None, *, has_header: bool = True
):
    """产出 (行号, 整行原文, 候选单元列表)。

    候选单元按出现顺序排列，由调用方逐个尝试匹配。
    """
    text = (content or "").replace("\ufeff", "")
    looks_csv = fmt == "csv" or (
        fmt is None and any(ch in text.split("\n")[0] for ch in ",;\t")
    )

    out: list[tuple[int, str, list[str]]] = []

    if looks_csv:
        reader = csv.reader(io.StringIO(text))
        for idx, row in enumerate(reader, start=1):
            raw = ",".join(row)
            if idx == 1 and has_header:
                joined = "".join(c.strip().lower() for c in row)
                if joined in _HEADER_HINTS or any(
                    c.strip().lower() in _HEADER_HINTS for c in row
                ):
                    continue
            cells = [c.strip() for c in row if c and c.strip()]
            if cells:
                out.append((idx, raw, cells))
    else:
        for idx, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                continue
            cells = _cells_of_line(stripped)
            if cells:
                out.append((idx, stripped, cells))

    return out


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------
def match_token(
    token: str, index: StockIndex
) -> tuple[IndexEntry | None, str, str | None]:
    """单个 token → (命中条目, match_type, reason)。

    口径优先级：后缀式 > 前缀式 > 纯代码 > Qlib 小写式 > 名称。
    """
    raw = (token or "").strip()
    if not raw:
        return None, MATCH_NONE, REASON_EMPTY

    upper = raw.upper()

    # 1) 后缀式 600036.SH
    if _SUFFIX_RE.match(upper):
        entry = index.by_symbol.get(upper)
        if entry:
            return entry, MATCH_SYMBOL, None
        # 交易所写错（如 sh300750 / 300750.SH）时按代码纠正，避免误判"不存在"
        fixed = index.by_code.get(upper[:6])
        if fixed:
            return fixed, MATCH_EXCHANGE_FIXED, None
        return None, MATCH_NONE, REASON_NOT_IN_INDEX

    # 2) 前缀式 SH600036
    if _PREFIX_RE.match(upper):
        entry = index.by_prefix.get(upper)
        if entry:
            return entry, MATCH_PREFIX, None
        fixed = index.by_code.get(upper[2:])
        if fixed:
            return fixed, MATCH_EXCHANGE_FIXED, None
        return None, MATCH_NONE, REASON_NOT_IN_INDEX

    # 3) Qlib 小写式 sh600036
    if _QLIB_RE.match(raw):
        entry = index.by_prefix.get(f"{raw[:2].upper()}{raw[2:]}")
        if entry:
            return entry, MATCH_PREFIX, None
        fixed = index.by_code.get(raw[2:])
        if fixed:
            return fixed, MATCH_EXCHANGE_FIXED, None
        return None, MATCH_NONE, REASON_NOT_IN_INDEX

    # 4) 纯 6 位代码
    if _CODE_RE.match(raw):
        entry = index.by_code.get(raw)
        if entry:
            return entry, MATCH_CODE, None
        return None, MATCH_NONE, REASON_NOT_IN_INDEX

    # 5) 名称（严格）
    norm = normalize_name(raw)
    entry = index.by_name.get(norm)
    if entry:
        return entry, MATCH_NAME, None

    # 6) 名称（宽松：去 * / ST 前缀，仅唯一命中时接受）
    candidates = index.by_loose_name.get(_loose_name(raw)) or []
    if len(candidates) == 1:
        return candidates[0], MATCH_NAME_LOOSE, None
    if len(candidates) > 1:
        return None, MATCH_NONE, REASON_UNRECOGNIZED

    return None, MATCH_NONE, REASON_UNRECOGNIZED


def _format_code_like(token: str) -> str | None:
    """判断 token 是否"像"股票代码（用于区分 not_in_index 与 unrecognized）。"""
    upper = (token or "").strip().upper()
    if _CODE_RE.match(upper) or _SUFFIX_RE.match(upper) or _PREFIX_RE.match(upper):
        return upper
    if _QLIB_RE.match((token or "").strip()):
        return (token or "").strip().upper()
    return None


def parse_stock_list(
    content: str,
    *,
    fmt: str | None = None,
    has_header: bool = True,
    column: str | None = None,
    index: StockIndex | None = None,
    encoding: str | None = None,
) -> ParseReport:
    """解析上传的 CSV/TXT 内容，产出匹配报告（不落库）。"""
    idx = index if index is not None else load_stock_index()

    report = ParseReport(
        encoding=encoding,
        index_source=str(idx.source) if idx.source else None,
        index_size=len(idx),
    )
    if len(idx) == 0:
        report.warnings.append(
            "股票索引为空，无法校验代码；请检查 data/stocks/stocks_index.json "
            f"是否存在（可用 {_ENV_PATH} 覆盖路径）"
        )

    # 强制列模式：仅取该列
    forced_idx: int | None = None
    rows = extract_token_rows(content, fmt, has_header=has_header)
    if column:
        header = None
        first_line = (content or "").lstrip("\ufeff").splitlines()
        if first_line:
            header = [c.strip().lower() for c in _cells_of_line(first_line[0])]
        if header and column.strip().lower() in header:
            forced_idx = header.index(column.strip().lower())
        elif column.strip().isdigit():
            forced_idx = int(column.strip())

    seen: set[str] = set()

    for row_idx, raw_line, cells in rows:
        candidates: Iterable[str]
        if forced_idx is not None:
            candidates = [cells[forced_idx]] if forced_idx < len(cells) else []
        else:
            # 跳过重复表头行（非首行也可能再出现一次表头）
            if cells and all(c.strip().lower() in _HEADER_HINTS for c in cells):
                continue
            candidates = cells

        report.total += 1

        matched_entry: IndexEntry | None = None
        matched_type = MATCH_NONE
        matched_token = ""
        code_like: str | None = None

        for cand in candidates:
            entry, mtype, _reason = match_token(cand, idx)
            if entry is not None:
                matched_entry, matched_type, matched_token = entry, mtype, cand
                break
            if code_like is None and _format_code_like(cand):
                code_like = cand

        if matched_entry is None:
            report.unmatched += 1
            if code_like:
                report.not_in_index += 1
                status, reason = REASON_NOT_IN_INDEX, REASON_NOT_IN_INDEX
                report.warnings.append(
                    f"第 {row_idx} 行 {code_like} 格式合法但不在本地索引中"
                    "（索引仅含沪深两市，北交所/新股/退市标的会落在这里）"
                )
            else:
                status, reason = "unrecognized", REASON_UNRECOGNIZED
            report.rows.append(
                ParseRow(
                    row_index=row_idx,
                    raw=raw_line[:200],
                    token=code_like or (cells[0] if cells else ""),
                    status=status,
                    match_type=MATCH_NONE,
                    reason=reason,
                )
            )
            continue

        duplicate = matched_entry.symbol in seen
        if duplicate:
            report.duplicates += 1
        else:
            seen.add(matched_entry.symbol)
            report.matched += 1
            report.symbols.append(matched_entry.symbol)
            report.members.append(
                {
                    "symbol": matched_entry.symbol,
                    "api_symbol": matched_entry.api_symbol,
                    "name": matched_entry.name,
                    "meta": {"source_token": matched_token, "match_type": matched_type},
                }
            )

        if matched_type == MATCH_EXCHANGE_FIXED:
            report.warnings.append(
                f"第 {row_idx} 行 {matched_token} 的交易所前缀有误，"
                f"已按代码纠正为 {matched_entry.symbol}"
            )

        report.rows.append(
            ParseRow(
                row_index=row_idx,
                raw=raw_line[:200],
                token=matched_token,
                status="matched",
                match_type=matched_type,
                symbol=matched_entry.symbol,
                api_symbol=matched_entry.api_symbol,
                code=matched_entry.code,
                name=matched_entry.name,
                duplicate=duplicate,
            )
        )

    # 去重告警合并（同因只留一条）
    report.warnings = _dedupe_keep_order(report.warnings)
    return report


def _dedupe_keep_order(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.split("（")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------
def parse_upload(
    raw: bytes | str,
    *,
    fmt: str | None = None,
    filename: str | None = None,
    has_header: bool = True,
    column: str | None = None,
) -> ParseReport:
    """从上传的原始字节/字符串解析（自动探测编码与格式）。"""
    encoding: str | None = None
    if isinstance(raw, bytes):
        content, encoding = decode_bytes(raw)
    else:
        content = raw

    if fmt is None and filename:
        suffix = Path(filename).suffix.lower().lstrip(".")
        if suffix in ("csv", "txt", "tsv"):
            fmt = "csv" if suffix == "csv" else "txt"

    return parse_stock_list(
        content, fmt=fmt, has_header=has_header, column=column, encoding=encoding
    )


def validate_symbols(
    symbols: Sequence[str], index: StockIndex | None = None
) -> list[str]:
    """把任意代码列表归一到索引内的后缀式（丢弃无法匹配的）。"""
    idx = index if index is not None else load_stock_index()
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols or []:
        entry, _t, _r = match_token(s, idx)
        if entry is None or entry.symbol in seen:
            continue
        seen.add(entry.symbol)
        out.append(entry.symbol)
    return out


def symbol_to_name(symbol: str, index: StockIndex | None = None) -> str | None:
    idx = index if index is not None else load_stock_index()
    entry = idx.by_symbol.get(to_storage_symbol(symbol, normalize_market("CN")))
    return entry.name if entry else None
