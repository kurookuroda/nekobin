import html as _html
import re
import sqlite3

import pytest

from .conftest import payload
from .test_export import add_note, make_turn, set_rating


def ids(client, **params):
    """絞り込み後のタイムラインに出ているターンID(新しい順)。"""
    r = client.get("/", params=params)
    assert r.status_code == 200
    return [int(i) for i in re.findall(r'<article class="card[^"]*" id="turn-(\d+)"', r.text)]


def page(client, **params):
    return client.get("/", params=params).text


@pytest.fixture()
def corpus(client, tmp_path):
    """検索用の小さなデータ。ターンID順に 1,2,3,4,5。"""
    make_turn(client, session_id="a", model="gpt-xx", prompt="FastAPIでwebhookを受けたい", response="Bearerトークンで認証します",
              created_at="2026-10-10T03:00:00+00:00")                                   # 1
    make_turn(client, session_id="a", model="claude-x", prompt="ページネーションの書き方", response="OFFSETより最後のIDを覚える方式が速い",
              created_at="2026-10-11T03:00:00+00:00")                                   # 2
    make_turn(client, session_id="b", model="gpt-xx", prompt="今日の天気は?", response="晴れです",
              created_at="2026-10-12T03:00:00+00:00")                                   # 3
    make_turn(client, session_id="b", model="claude-x", prompt="hmac.compare_digest の使い方", response="定数時間で比較します",
              created_at="2026-10-12T16:30:00+00:00")                                   # 4 (JSTでは10/13 1:30)
    make_turn(client, session_id="c", model="gpt-xx", prompt="関係ない話", response="とくに何もありません",
              created_at="2026-10-13T03:00:00+00:00")                                   # 5
    set_rating(tmp_path, 1, -4)
    set_rating(tmp_path, 2, 5)
    set_rating(tmp_path, 3, -2)
    set_rating(tmp_path, 4, 3)
    add_note(tmp_path, 5, "この回答は古い情報だった(v2のAPI)")
    return client


# ------------------------------------------------------------ 全文検索

def test_no_filters_lists_everything_newest_first(corpus):
    assert ids(corpus) == [5, 4, 3, 2, 1]
    assert "条件をクリア" not in page(corpus)                       # 条件が無いときはクリアリンクも件数も出さない
    assert 'class="result-count"' not in page(corpus)


def test_search_matches_prompt_response_and_notes(corpus):
    assert ids(corpus, q="webhook") == [1]                       # プロンプト
    assert ids(corpus, q="OFFSET") == [2]                        # 応答
    assert ids(corpus, q="古い情報") == [5]                      # メモ
    assert ids(corpus, q="webhook".upper()) == [1]               # 大文字小文字は区別しない
    assert ids(corpus, q="存在しない言葉です") == []


def test_search_is_substring_match_for_japanese_and_symbols(corpus):
    assert ids(corpus, q="ネーション") == [2]                    # 語の途中でも当たる
    assert ids(corpus, q="hmac.compare_digest") == [4]           # . や _ を含む語
    assert ids(corpus, q="compare") == [4]


def test_all_terms_must_match_and_may_span_turn_and_notes(corpus):
    assert ids(corpus, q="FastAPI Bearer") == [1]               # プロンプトと応答
    assert ids(corpus, q="FastAPI OFFSET") == []                 # 別々のターンにある語は当たらない
    assert ids(corpus, q="関係ない 古い情報") == [5]             # プロンプトとメモ
    assert ids(corpus, q="webhook　Bearer") == [1]               # 全角スペース区切りも可


def test_short_terms_fall_back_to_like(corpus):
    assert ids(corpus, q="天気") == [3]                          # 2文字(trigramでは引けない)
    assert ids(corpus, q="晴") == [3]                            # 1文字
    assert ids(corpus, q="天気 晴れ") == [3]                     # 短い語 + 3文字未満の語の組み合わせ
    assert ids(corpus, q="天気 webhook") == []                   # 短い語と長い語のAND


def test_like_wildcards_are_literal(client):
    make_turn(client, prompt="割合は5%です", response="x")
    make_turn(client, prompt="普通の文", response="y")
    make_turn(client, prompt="snake_case 名", response="z")
    assert ids(client, q="%") == [1]                             # % は「任意の文字」ではなく、文字そのもの
    assert ids(client, q="_") == [3]


@pytest.mark.parametrize("q", ['"', "'", "AND", "OR", "NOT", "NEAR(a b)", "*", "foo*", "a:b", "-x", "(", ")", '"unclosed', "\\", "^", "{", "}", "col:val OR x"])
def test_fts_syntax_in_queries_never_breaks(corpus, q):
    r = corpus.get("/", params={"q": q})
    assert r.status_code == 200


def test_term_limits(corpus):
    long_q = " ".join(f"語{i}" for i in range(50))
    assert corpus.get("/", params={"q": long_q}).status_code == 200
    assert corpus.get("/", params={"q": "あ" * 5000}).status_code == 200


# ------------------------------------------------------------ 評価

def test_rating_filters(corpus):
    assert ids(corpus, rating="good") == [4, 2]
    assert ids(corpus, rating="bad") == [3, 1]
    assert ids(corpus, rating="none") == [5]
    assert ids(corpus, rating="") == [5, 4, 3, 2, 1]
    assert ids(corpus, rating="bogus") == [5, 4, 3, 2, 1]        # 不正な値は無視


def test_rating_level_means_n_or_more(corpus):
    assert ids(corpus, rating="good", level="4") == [2]
    assert ids(corpus, rating="good", level="3") == [4, 2]
    assert ids(corpus, rating="bad", level="3") == [1]           # 悪さ3以上(-4)
    assert ids(corpus, rating="bad", level="2") == [3, 1]
    assert ids(corpus, rating="bad", level="5") == []
    assert ids(corpus, rating="bad", level="9") == [3, 1]        # 範囲外は「指定なし」
    assert ids(corpus, rating="none", level="3") == [5]          # 強さは 😺/😿 のときだけ有効
    assert ids(corpus, level="3") == [5, 4, 3, 2, 1]


# ------------------------------------------------------------ モデル

def test_model_filter_and_options(corpus):
    assert ids(corpus, model="gpt-xx") == [5, 3, 1]
    assert ids(corpus, model="claude-x") == [4, 2]
    assert ids(corpus, model="gpt") == []                        # 完全一致
    html = page(corpus)
    assert '<option value="gpt-xx">gpt-xx</option>' in html and '<option value="claude-x">claude-x</option>' in html
    assert '<option value="gpt-xx" selected>gpt-xx</option>' in page(corpus, model="gpt-xx")
    assert '<option value="ghost" selected>ghost</option>' in page(corpus, model="ghost")     # 未知の指定も画面に残る


# ------------------------------------------------------------ 期間

def test_period_filter_is_inclusive_and_uses_display_timezone(corpus, monkeypatch):
    assert ids(corpus, since="2026-10-12") == [5, 4, 3]
    assert ids(corpus, until="2026-10-11") == [2, 1]
    assert ids(corpus, since="2026-10-11", until="2026-10-12") == [4, 3, 2]   # 両端を含む(UTC)

    monkeypatch.setenv("NEKOBIN_TZ", "Asia/Tokyo")                            # 4 は JST で 10/13 1:30
    assert ids(corpus, since="2026-10-13", until="2026-10-13") == [5, 4]
    assert ids(corpus, since="2026-10-12", until="2026-10-12") == [3]


def test_period_invalid_and_reversed_dates(corpus):
    html = page(corpus, since="2026-13-45")
    assert "開始日の形式が正しくないので、無視しました" in html and ids(corpus, since="2026-13-45") == [5, 4, 3, 2, 1]
    assert ids(corpus, since="abc", until="2026-10-11") == [2, 1]
    html = page(corpus, since="2026-10-12", until="2026-10-10")
    assert "入れ替えました" in html
    assert ids(corpus, since="2026-10-12", until="2026-10-10") == [4, 3, 2, 1]   # 10/10〜10/12(UTC)として扱う


# ------------------------------------------------------------ 組み合わせ・件数・ページング

def test_combined_filters(corpus):
    assert ids(corpus, q="Bearer", rating="bad", model="gpt-xx") == [1]
    assert ids(corpus, q="Bearer", rating="good") == []
    assert ids(corpus, model="claude-x", rating="good", since="2026-10-11", until="2026-10-11") == [2]


def test_result_count_and_empty_state(corpus):
    assert '<strong>2</strong>件' in page(corpus, rating="good")
    html = page(corpus, q="存在しない言葉です")
    assert "条件に合う記録がありません" in html and "<strong>0</strong>件" in html
    assert "まだ記録がありません" not in html                    # 「データが空」とは区別する


def test_filter_form_keeps_current_values(corpus):
    html = page(corpus, q="webhook 認証", rating="bad", level="3", model="gpt-xx", since="2026-10-01", until="2026-10-31")
    assert 'name="q" value="webhook 認証"' in html
    assert '<option value="bad" selected>' in html and '<option value="3" selected>3以上</option>' in html
    assert 'name="since" value="2026-10-01"' in html and 'name="until" value="2026-10-31"' in html
    assert 'class="filter-clear"' in html


def test_pagination_keeps_filters(client):
    for i in range(35):
        make_turn(client, model="gpt-xx", prompt=f"共通語ABC {i:02d}", created_at=f"2026-10-12T05:{i:02d}:00+00:00")
    make_turn(client, model="other", prompt="共通語ABC 別モデル", created_at="2026-10-12T07:00:00+00:00")
    first = page(client, q="共通語ABC", model="gpt-xx")
    assert "<strong>35</strong>件" in first and len(re.findall(r'id="turn-\d+"', first)) == 30
    href = _html.unescape(re.search(r'class="older"><a href="([^"]+)"', first).group(1))
    assert "q=" in href and "model=gpt-xx" in href and "bt=" in href
    second = client.get(href).text
    assert len(re.findall(r'id="turn-\d+"', second)) == 5 and "別モデル" not in second


def test_filtered_view_still_requires_login(client, monkeypatch):
    make_turn(client)
    monkeypatch.setenv("NEKOBIN_TOKEN", "s3cret")
    r = client.get("/?q=abc", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


# ------------------------------------------------------------ ハイライト

def test_hits_are_highlighted_in_prompt_response_and_notes(corpus):
    html = page(corpus, q="webhook")
    assert "<mark>webhook</mark>" in html
    html = page(corpus, q="Bearer")
    assert "<mark>Bearer</mark>トークン" in html                     # 応答(markdown変換後)
    html = page(corpus, q="古い情報")
    assert "<mark>古い情報</mark>" in html                           # メモ
    assert "<mark>FASTAPI</mark>" not in page(corpus, q="fastapi") and "<mark>FastAPI</mark>" in page(corpus, q="fastapi")   # 表記は原文のまま
    assert "<mark>" not in page(corpus)


def test_highlight_never_touches_tags_attributes_or_entities(client):
    make_turn(client, prompt="質問", response="A & B <tag> と amp の話。\n\n[class](https://example.com/class)\n\n```python\nclass Foo:\n    pass\n```")
    html = page(client, q="class")
    assert 'href="https://example.com/<mark>' not in html              # 属性の中は触らない
    assert 'class="<mark>' not in html and '<mark>codeblock' not in html   # クラス名も壊さない
    assert '<div class="codeblock">' in html and 'class="copy copy-code"' in html
    assert "<mark>class</mark>" in html                              # 本文・コードの文字は対象
    html = page(client, q="amp")
    assert "A &amp; B &lt;tag&gt;" in html                           # &amp; を壊さない
    assert "<mark>amp</mark>" in html and "&<mark>amp</mark>;" not in html


def test_highlight_is_xss_safe(client):
    make_turn(client, prompt="<script>alert(1)</script> を入力", response="<img src=x onerror=alert(2)>")
    html = page(client, q="<script>")
    assert "<script>alert" not in html
    assert "<mark>&lt;script&gt;</mark>" in html
    html = page(client, q="onerror")
    assert "<img src=x" not in html and "<mark>onerror</mark>" in html


def test_script_unclamps_only_when_a_hit_would_be_hidden(client):
    js = client.get("/static/app.js").text
    assert "querySelectorAll('mark')" in js and "getBoundingClientRect" in js


# ------------------------------------------------------------ メモの検索インデックス(同期・既存DBの移行)

def test_note_index_follows_create_edit_delete(client):
    t = make_turn(client)["turn_id"]
    n = client.post(f"/api/turns/{t}/notes", json={"body": "最初のメモ文章"}).json()
    assert ids(client, q="最初のメモ") == [t]
    client.patch(f"/api/notes/{n['id']}", json={"body": "書き換えた内容"})
    assert ids(client, q="最初のメモ") == [] and ids(client, q="書き換えた") == [t]
    client.delete(f"/api/notes/{n['id']}")
    assert ids(client, q="書き換えた") == []


def test_existing_database_without_notes_index_is_migrated(tmp_path):
    """ステップ5までのDB(notes_fts が無い)を開いても、既存のメモが検索できる。"""
    from app.db import SCHEMA, init_db

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA[: SCHEMA.index("-- メモの全文検索")])        # notes_fts より前だけ(=ステップ5までのDB)
    conn.execute("INSERT INTO sessions(id, created_at, updated_at) VALUES ('s', 'x', 'x')")
    conn.execute("INSERT INTO turns(id, session_id, seq, created_at, model, prompt, response) VALUES (1, 's', 1, 'x', 'm', 'p', 'r')")
    conn.execute("INSERT INTO notes(turn_id, body, author, created_at, updated_at) VALUES (1, '移行前に書いたメモ', 'human', 'x', 'x')")
    conn.commit()
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='notes_fts'").fetchone() is None
    conn.close()

    init_db(str(path))
    conn = sqlite3.connect(path)
    hit = conn.execute("SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?", ('"移行前に"',)).fetchall()
    assert hit == [(1,)]
    init_db(str(path))                                              # 2回目以降は何もしない(壊さない)
    assert conn.execute("SELECT COUNT(*) FROM notes_fts WHERE notes_fts MATCH ?", ('"移行前に"',)).fetchone()[0] == 1
