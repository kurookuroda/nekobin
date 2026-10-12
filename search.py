"""検索・絞り込みの条件を、SQLの WHERE 句に組み立てる。

項目(話し合って決めたもの):
  q       全文検索(プロンプト・応答・メモの本文)
  rating  評価: good(😺のみ) / bad(😿のみ) / none(未評価のみ)
  level   強さ: rating が good / bad のとき「n以上」(良さ3以上、悪さ3以上 など)
  model   モデル名(完全一致)
  since / until  期間(日付、両端を含む。表示タイムゾーンでの日付)

全文検索の方針:
- 空白区切りの語は、すべて含むこと(AND)。1語ごとに「プロンプト / 応答 / メモ」のどこかにあればよい。
- 3文字以上の語は FTS5(trigram)の部分一致、2文字以下の語は trigram では引けないので LIKE で探す。
- FTS5 の構文(AND, NEAR, *, : など)は使わせない。入力は必ず「文字列」として扱う。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo

from ..db import utc_iso

MAX_QUERY_CHARS = 200
MAX_TERMS = 8
RATING_KINDS = ("good", "bad", "none")


def split_terms(q: str) -> list[str]:
    """空白(全角を含む)で区切る。大文字小文字だけが違う重複は除く。"""
    seen: set[str] = set()
    terms: list[str] = []
    for t in q.split():
        key = t.casefold()
        if key not in seen:
            seen.add(key)
            terms.append(t)
    return terms[:MAX_TERMS]


@dataclass
class Filters:
    q: str = ""
    rating: str = ""            # "" | good | bad | none
    level: int | None = None    # 1..5(good / bad のときだけ有効)
    model: str = ""
    since: str = ""             # YYYY-MM-DD
    until: str = ""
    notices: list[str] = field(default_factory=list)

    @property
    def terms(self) -> list[str]:
        return split_terms(self.q)

    @property
    def active(self) -> bool:
        return bool(self.terms or self.rating or self.model or self.since or self.until)

    def as_params(self) -> dict[str, str]:
        """URLに戻すための、空でない項目だけ。"""
        d = {
            "q": self.q,
            "rating": self.rating,
            "level": str(self.level) if self.level else "",
            "model": self.model,
            "since": self.since,
            "until": self.until,
        }
        return {k: v for k, v in d.items() if v}


def _valid_date(value: str, label: str, notices: list[str]) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError:
        notices.append(f"{label}の形式が正しくないので、無視しました")
        return ""
    return value


def parse_filters(
    q: str = "", rating: str = "", level: str = "", model: str = "", since: str = "", until: str = ""
) -> Filters:
    notices: list[str] = []
    rating = rating if rating in RATING_KINDS else ""

    lv: int | None = None
    if rating in ("good", "bad"):
        try:
            n = int((level or "").strip())
        except ValueError:
            n = 0
        lv = n if 1 <= n <= 5 else None

    since = _valid_date(since, "開始日", notices)
    until = _valid_date(until, "終了日", notices)
    if since and until and since > until:
        since, until = until, since
        notices.append("開始日が終了日より後だったので、入れ替えました")

    return Filters(
        q=(q or "").strip()[:MAX_QUERY_CHARS],
        rating=rating,
        level=lv,
        model=(model or "").strip(),
        since=since,
        until=until,
        notices=notices,
    )


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _fts_phrase(term: str) -> str:
    """FTS5 に渡す「文字列としての語」。引用符で囲み、中の引用符は二重にする。"""
    return '"' + term.replace('"', '""') + '"'


def _day_start_utc(day: str, tz: tzinfo, plus_days: int = 0) -> str:
    d = date.fromisoformat(day) + timedelta(days=plus_days)
    return utc_iso(datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc))


def build_conditions(f: Filters, tz: tzinfo) -> tuple[list[str], list]:
    """turns を `t` として参照する WHERE の条件と、そのパラメータ。"""
    conds: list[str] = []
    args: list = []

    for term in f.terms:
        if len(term) >= 3:
            conds.append(
                "(t.id IN (SELECT rowid FROM turns_fts WHERE turns_fts MATCH ?)"
                " OR t.id IN (SELECT turn_id FROM notes WHERE id IN"
                " (SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?)))"
            )
            args += [_fts_phrase(term)] * 2
        else:  # trigram では引けない短い語
            conds.append(
                "(t.prompt LIKE ? ESCAPE '\\' OR t.response LIKE ? ESCAPE '\\'"
                " OR EXISTS (SELECT 1 FROM notes n WHERE n.turn_id = t.id AND n.body LIKE ? ESCAPE '\\'))"
            )
            args += [_like_pattern(term)] * 3

    if f.rating == "none":
        conds.append("t.rating IS NULL")
    elif f.rating == "good":
        conds.append("t.rating >= ?")
        args.append(f.level or 1)
    elif f.rating == "bad":
        conds.append("t.rating <= ?")
        args.append(-(f.level or 1))

    if f.model:
        conds.append("t.model = ?")
        args.append(f.model)

    if f.since:
        conds.append("t.created_at >= ?")
        args.append(_day_start_utc(f.since, tz))
    if f.until:
        conds.append("t.created_at < ?")
        args.append(_day_start_utc(f.until, tz, plus_days=1))   # 終了日の翌日0時まで(終了日を含む)

    return conds, args
