"""SQLite 接続・スキーマ・保存処理。

原則:
- prompt / response は受信した原文markdownのまま保存し、後から書き換えない。
- 日時はUTCのISO8601(ミリ秒, 末尾Z)で統一する(文字列比較で時系列順になる)。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS turns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,                -- セッション内の通し番号(1始まり)
    created_at  TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    prompt      TEXT    NOT NULL,
    response    TEXT    NOT NULL,
    -- 評価: -5〜-1(悪さ=😿の数) / 1〜5(良さ=😺の数) / NULL(未評価)。0は使わない。
    rating      INTEGER CHECK (rating IS NULL OR (rating BETWEEN -5 AND 5 AND rating <> 0)),
    tags        TEXT    NOT NULL DEFAULT '[]',   -- JSON配列
    meta        TEXT    NOT NULL DEFAULT '{}',   -- JSONオブジェクト
    UNIQUE (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_turns_created ON turns(created_at);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_turns_rating  ON turns(rating);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id     INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    body        TEXT    NOT NULL,
    author      TEXT    NOT NULL DEFAULT 'human' CHECK (author IN ('human', 'bot')),
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_turn ON notes(turn_id);

-- 全文検索(日本語向けに trigram)。content= 指定で本体は turns を参照する。
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    prompt, response,
    content='turns', content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, prompt, response)
    VALUES (new.id, new.prompt, new.response);
END;

CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, prompt, response)
    VALUES ('delete', old.id, old.prompt, old.response);
END;

CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE OF prompt, response ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, prompt, response)
    VALUES ('delete', old.id, old.prompt, old.response);
    INSERT INTO turns_fts(rowid, prompt, response)
    VALUES (new.id, new.prompt, new.response);
END;

-- メモの全文検索
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    body,
    content='notes', content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE OF body ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO notes_fts(rowid, body) VALUES (new.id, new.body);
END;
"""


def utc_iso(dt: datetime | None = None) -> str:
    """UTCのISO8601(ミリ秒, 末尾Z)。naiveなdatetimeはUTCとして扱う。"""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or get_settings().db_path
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    # isolation_level=None: 自動コミット。必要な箇所で BEGIN/COMMIT を明示する。
    # check_same_thread=False: FastAPI の依存関係とエンドポイントが別スレッドで動きうるため。
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(path: str | None = None) -> None:
    conn = connect(path)
    try:
        # ステップ5までに作られたDBには notes_fts が無い。今回初めて作るときだけ、
        # すでにあるメモを検索対象に取り込む(以降はトリガーが同期する)。
        had_notes_fts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'notes_fts'"
        ).fetchone()
        conn.executescript(SCHEMA)
        if not had_notes_fts:
            conn.execute("INSERT INTO notes_fts(notes_fts) VALUES ('rebuild')")
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 依存関係: リクエストごとに接続を開いて閉じる。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def new_session_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def insert_turn(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    session_title: str | None,
    model: str,
    prompt: str,
    response: str,
    tags: list[str],
    meta: dict,
    created_at: datetime | None,
) -> dict:
    """1ターンを保存する。セッションが無ければ作り、seq を採番する。"""
    now = utc_iso()
    turn_time = utc_iso(created_at) if created_at else now
    session_id = session_id or new_session_id()

    conn.execute("BEGIN IMMEDIATE")  # seq の採番を競合させない
    try:
        exists = conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO sessions(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, session_title, turn_time, now),
            )
        else:
            conn.execute(
                "UPDATE sessions SET updated_at = ?, title = COALESCE(?, title) WHERE id = ?",
                (now, session_title, session_id),
            )

        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM turns WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]

        cur = conn.execute(
            """
            INSERT INTO turns(session_id, seq, created_at, model, prompt, response, tags, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                seq,
                turn_time,
                model,
                prompt,
                response,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(meta, ensure_ascii=False),
            ),
        )
        turn_id = cur.lastrowid
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    return {"turn_id": turn_id, "session_id": session_id, "seq": seq}


def _decode_turn(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    d["meta"] = json.loads(d["meta"])
    return d


def get_turn(conn: sqlite3.Connection, turn_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
    return _decode_turn(row) if row else None


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["meta"] = json.loads(d["meta"])
    return d


def get_session_turns(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM turns WHERE session_id = ? ORDER BY seq", (session_id,)
    ).fetchall()
    return [_decode_turn(r) for r in rows]


def get_notes_for_turns(
    conn: sqlite3.Connection, turn_ids: list[int]
) -> dict[int, list[dict]]:
    """turn_id -> メモ一覧(古い順)。メモの無いターンはキーを持たない。"""
    out: dict[int, list[dict]] = {}
    for i in range(0, len(turn_ids), 500):  # プレースホルダ数の上限対策
        chunk = turn_ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM notes WHERE turn_id IN ({marks}) ORDER BY created_at, id",
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(r["turn_id"], []).append(dict(r))
    return out


# ---------------------------------------------------------------- 評価・メモの書き込み

def set_rating(conn: sqlite3.Connection, turn_id: int, rating: int | None) -> bool:
    """rating: -5..-1 / 1..5 / None(未評価)。ターンが無ければ False。"""
    cur = conn.execute("UPDATE turns SET rating = ? WHERE id = ?", (rating, turn_id))
    return cur.rowcount == 1


def get_note(conn: sqlite3.Connection, note_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row) if row else None


def add_note(conn: sqlite3.Connection, turn_id: int, body: str, author: str) -> dict | None:
    if conn.execute("SELECT 1 FROM turns WHERE id = ?", (turn_id,)).fetchone() is None:
        return None
    now = utc_iso()
    cur = conn.execute(
        "INSERT INTO notes(turn_id, body, author, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (turn_id, body, author, now, now),
    )
    return get_note(conn, cur.lastrowid)


def update_note(conn: sqlite3.Connection, note_id: int, body: str) -> dict | None:
    cur = conn.execute(
        "UPDATE notes SET body = ?, updated_at = ? WHERE id = ?", (body, utc_iso(), note_id)
    )
    return get_note(conn, note_id) if cur.rowcount == 1 else None


def delete_note(conn: sqlite3.Connection, note_id: int) -> bool:
    return conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount == 1
