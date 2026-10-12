"""人間向けのHTML画面: タイムライン(/)、セッション(/s/{id})、ターン(/t/{id})、ログイン。

注意: main.py では export ルーター(/t/{id}.md など)より後に登録すること。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, tzinfo
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .. import auth
from ..config import get_settings
from ..db import get_db, get_notes_for_turns, get_session, get_session_turns, get_turn
from ..services import exporter
from ..services.highlight import highlight_html, highlight_text
from ..services.render import render_markdown
from ..services.search import Filters, build_conditions, parse_filters

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

PAGE_SIZE = 30
SIDEBAR_SESSIONS = 50
WEEKDAYS = "月火水木金土日"

router = APIRouter(dependencies=[Depends(auth.require_view_access)])
login_router = APIRouter()


# ------------------------------------------------------------------ 表示用データの整形

def _local(iso: str, tz: tzinfo) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)


def _date_label(dt: datetime) -> str:
    return f"{dt:%Y-%m-%d}({WEEKDAYS[dt.weekday()]})"


def _rating_class(rating: int | None) -> str:
    if not rating:
        return ""
    return f"rating-{'good' if rating > 0 else 'bad'}-{abs(rating)}"


def _prepare(
    turn: dict, notes: list[dict], session_title: str | None, tz: tzinfo, terms: list[str] | None = None
) -> dict:
    terms = terms or []
    dt = _local(turn["created_at"], tz)
    rating = turn["rating"]
    return {
        **turn,
        "session_title": session_title or turn["session_id"],
        "date_label": _date_label(dt),
        "time_short": f"{dt:%H:%M}",
        "time_full": exporter.fmt_time(turn["created_at"], tz),
        # どちらも表示用。生HTMLは無効化済み(原文は変更しない)
        "prompt_html": highlight_text(turn["prompt"], terms),
        "response_html": Markup(highlight_html(render_markdown(turn["response"]), terms)),
        "rating_view": exporter.rating_view(rating),
        "rating_title": exporter.rating_phrase(rating),
        "rating_short": exporter.rating_short(rating),
        "rating_class": _rating_class(rating),
        "notes": [
            {**n, "time_full": exporter.fmt_time(n["created_at"], tz), "body_html": highlight_text(n["body"], terms)}
            for n in notes
        ],
    }


def _group_by_date(cards: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: list[tuple[str, list[dict]]] = []
    for c in cards:
        if not groups or groups[-1][0] != c["date_label"]:
            groups.append((c["date_label"], []))
        groups[-1][1].append(c)
    return groups


def _sidebar_sessions(conn: sqlite3.Connection, tz: tzinfo) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.id, s.title, s.updated_at,
               COUNT(t.id) AS n,
               COALESCE(SUM(CASE WHEN t.rating < 0 THEN 1 ELSE 0 END), 0) AS bad,
               COALESCE(SUM(CASE WHEN t.rating > 0 THEN 1 ELSE 0 END), 0) AS good
        FROM sessions s LEFT JOIN turns t ON t.session_id = s.id
        GROUP BY s.id
        ORDER BY s.updated_at DESC
        LIMIT ?
        """,
        (SIDEBAR_SESSIONS,),
    ).fetchall()
    return [
        {**dict(r), "label": r["title"] or r["id"], "updated": exporter.fmt_time(r["updated_at"], tz)}
        for r in rows
    ]


def _render(request: Request, name: str, conn: sqlite3.Connection, tz: tzinfo, **ctx) -> HTMLResponse:
    ctx.setdefault("current_session", None)
    return templates.TemplateResponse(
        request,
        name,
        {
            "sessions": _sidebar_sessions(conn, tz),
            "auth_enabled": bool(get_settings().token),
            **ctx,
        },
    )


# ------------------------------------------------------------------ 画面

@router.get("/", response_class=HTMLResponse)
def timeline(
    request: Request,
    q: str = "",
    rating: str = "",
    level: str = "",
    model: str = "",
    since: str = "",
    until: str = "",
    bt: str | None = Query(default=None, description="この時刻より古いものを表示(ページング用)"),
    bid: int | None = Query(default=None),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    tz = exporter.display_tz()
    f = parse_filters(q, rating, level, model, since, until)
    conds, args = build_conditions(f, tz)

    total = None
    if f.active:
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        total = conn.execute(f"SELECT COUNT(*) FROM turns t {where}", args).fetchone()[0]

    page_conds, page_args = list(conds), list(args)
    if bt is not None and bid is not None:
        page_conds.append("(t.created_at, t.id) < (?, ?)")
        page_args += [bt, bid]
    where = ("WHERE " + " AND ".join(page_conds)) if page_conds else ""
    rows = conn.execute(
        f"""
        SELECT t.*, s.title AS session_title
        FROM turns t JOIN sessions s ON s.id = t.session_id
        {where}
        ORDER BY t.created_at DESC, t.id DESC
        LIMIT ?
        """,
        [*page_args, PAGE_SIZE + 1],
    ).fetchall()

    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    turns, titles = [], {}
    for r in rows:
        d = dict(r)
        titles[d["id"]] = d.pop("session_title")
        d["tags"], d["meta"] = json.loads(d["tags"]), json.loads(d["meta"])
        turns.append(d)

    notes = get_notes_for_turns(conn, [t["id"] for t in turns])
    terms = f.terms
    cards = [_prepare(t, notes.get(t["id"], []), titles[t["id"]], tz, terms) for t in turns]

    older_url = None
    if has_more and turns:
        last = turns[-1]
        older_url = "/?" + urlencode({**f.as_params(), "bt": last["created_at"], "bid": last["id"]})

    models = [
        r["model"]
        for r in conn.execute("SELECT model FROM turns GROUP BY model ORDER BY COUNT(*) DESC, model")
    ]
    if f.model and f.model not in models:
        models.append(f.model)  # URLで指定されたモデルも選択肢に残す

    return _render(
        request, "timeline.html", conn, tz,
        groups=_group_by_date(cards), older_url=older_url,
        f=f, total=total, models=models, page_title="検索結果" if f.active else "タイムライン",
    )


@router.get("/s/{session_id}", response_class=HTMLResponse)
def session_page(
    session_id: str, request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    session = get_session(conn, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    tz = exporter.display_tz()
    turns = get_session_turns(conn, session_id)  # 会話は古い順に読む
    notes = get_notes_for_turns(conn, [t["id"] for t in turns])
    cards = [_prepare(t, notes.get(t["id"], []), session["title"], tz) for t in turns]
    toc = [
        {
            "id": c["id"],
            "seq": c["seq"],
            "rating_view": c["rating_view"],
            "rating_class": c["rating_class"],
            "snippet": " ".join(c["prompt"].split())[:48],
        }
        for c in cards
    ]
    return _render(
        request, "session.html", conn, tz,
        session=session, title=session["title"] or session["id"],
        cards=cards, toc=toc, current_session=session_id,
        page_title=session["title"] or session["id"],
    )


@router.get("/t/{turn_id}", response_class=HTMLResponse)
def turn_page(
    turn_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    turn = get_turn(conn, turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="turn not found")
    session = get_session(conn, turn["session_id"])
    tz = exporter.display_tz()
    notes = get_notes_for_turns(conn, [turn_id]).get(turn_id, [])
    card = _prepare(turn, notes, session["title"], tz)
    return _render(
        request, "turn.html", conn, tz,
        tc=card, current_session=turn["session_id"],
        page_title=f"Turn {turn['seq']} · {card['session_title']}",
    )


# ------------------------------------------------------------------ ログイン(暫定)

@login_router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/") -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", {"next": auth.safe_next(next), "error": False, "page_title": "ログイン"}
    )


@login_router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, token: str = Form(...), next: str = Form("/")):
    target = auth.safe_next(next)
    if not auth.token_matches(token):
        return templates.TemplateResponse(
            request, "login.html",
            {"next": target, "error": True, "page_title": "ログイン"},
            status_code=401,
        )
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        auth.COOKIE_NAME,
        auth.session_cookie_value(get_settings().token),
        max_age=auth.COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@login_router.get("/logout")
def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp
