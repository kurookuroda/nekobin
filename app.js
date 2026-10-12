// Nekobin — 画面の振る舞い(JSが無くても内容はすべて読める。コピーボタンだけJS専用)
(() => {
  document.documentElement.classList.add('js');

  // ------------------------------------------------------------ 表示まわり
  // 狭い画面では、セッション一覧をたたんでおく
  const sidebar = document.querySelector('details.sidebar');
  if (sidebar && window.matchMedia('(max-width: 899px)').matches) sidebar.open = false;

  // 長いプロンプト/応答は折りたたんで「続きを表示」ボタンを付ける
  document.querySelectorAll('.body[data-clamp]').forEach((el) => {
    el.classList.add('is-clamped');
    if (el.scrollHeight <= el.clientHeight + 24) {  // 少しの超過は折りたたまない
      el.classList.remove('is-clamped');
      return;
    }
    // 検索のヒット箇所が、折りたたみで隠れてしまう位置にあるときは、全文を見せる
    // (冒頭に見えているヒットだけなら、折りたたんだままにする)
    const top = el.getBoundingClientRect().top;
    const hidden = Array.from(el.querySelectorAll('mark')).some(
      (m) => m.getBoundingClientRect().bottom - top > el.clientHeight,
    );
    if (hidden) {
      el.classList.remove('is-clamped');
      return;
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'expand';
    btn.textContent = '続きを表示 ▾';
    btn.setAttribute('aria-expanded', 'false');
    btn.addEventListener('click', () => {
      const expanded = el.classList.toggle('is-clamped') === false;
      btn.textContent = expanded ? '折りたたむ ▴' : '続きを表示 ▾';
      btn.setAttribute('aria-expanded', String(expanded));
    });
    el.after(btn);
  });

  // 固定リンク(#turn-N)で開いたカードは、全文を見せる
  if (location.hash.startsWith('#turn-')) {
    const target = document.querySelector(location.hash);
    if (target) target.querySelectorAll('.expand').forEach((b) => b.click());
  }

  // ------------------------------------------------------------ コピー
  // コピーする文字列は、サーバーの .md / .txt を取得したもの(= エクスポートと同一)。
  // コードブロックだけは、画面上のコード本文をそのままコピーする。
  const DONE_TEXT = '🎣コピーしました';
  const FAIL_TEXT = '⚠ コピーできませんでした';

  const fetchText = async (url) => {
    const res = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.text();
  };

  const legacyCopy = (text) => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  };

  // source: 文字列、またはそれを返すPromise。
  // Safari はクリック直後でないとコピーできないため、取得中のPromiseをそのまま ClipboardItem に渡す。
  const copyText = async (source) => {
    const pending = Promise.resolve(source);
    pending.catch(() => {});  // 下の分岐で扱うので、未処理エラー扱いにしない
    if (navigator.clipboard && window.ClipboardItem && window.isSecureContext) {
      try {
        await navigator.clipboard.write([
          new ClipboardItem({ 'text/plain': pending.then((t) => new Blob([t], { type: 'text/plain' })) }),
        ]);
        return true;
      } catch (_) { /* 下のフォールバックへ */ }
    }
    try {
      const text = await pending;
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
      return legacyCopy(text);
    } catch (_) {
      return false;
    }
  };

  const live = document.getElementById('live');
  const timers = new WeakMap();

  // 押したボタン(メニューの項目なら、メニューの見出し)の文字を一時的に差し替える
  const showResult = (btn, ok) => {
    const menu = btn.closest('details.copymenu');
    if (menu) menu.open = false;
    const target = menu ? menu.querySelector('summary') : btn;
    if (!timers.has(target)) target.dataset.label = target.textContent;
    clearTimeout(timers.get(target));
    const message = ok ? DONE_TEXT : FAIL_TEXT;
    target.textContent = message;
    target.classList.remove('done', 'failed');
    target.classList.add(ok ? 'done' : 'failed');
    if (live) live.textContent = message;
    timers.set(target, setTimeout(() => {
      target.textContent = target.dataset.label;
      target.classList.remove('done', 'failed');
      timers.delete(target);
      if (live) live.textContent = '';
    }, 1600));
  };

  document.addEventListener('click', async (e) => {
    // メニューの外をクリックしたら閉じる
    document.querySelectorAll('details.copymenu[open]').forEach((d) => {
      if (!d.contains(e.target)) d.open = false;
    });

    const btn = e.target.closest('button[data-copy-url], button.copy-code');
    if (!btn) return;
    const source = btn.classList.contains('copy-code')
      ? btn.closest('.codeblock').querySelector('code').textContent.replace(/\n$/, '')
      : fetchText(btn.dataset.copyUrl);
    showResult(btn, await copyText(source));
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('details.copymenu[open]').forEach((d) => { d.open = false; });
  });

  // ------------------------------------------------------------ 書き込み(評価・メモ)
  // ブラウザからの書き込みは、ログインCookie + 同一サイトからの送信(Originヘッダー)で通る。
  const api = async (method, url, body) => {
    const init = { method, credentials: 'same-origin' };
    if (body !== undefined) {
      init.headers = { 'Content-Type': 'application/json' };
      init.body = JSON.stringify(body);
    }
    const res = await fetch(url, init);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.status === 204 ? null : res.json();
  };

  // ---- 評価メーター: 😿(悪さ1〜5) | 😺(良さ1〜5)。同じ猫をもう一度押すと未評価に戻る
  const RATING_CLASS = /\brating-(?:good|bad)-\d\b/g;

  const applyRating = (card, rating, view, short) => {
    const meter = card.querySelector('.meter');
    meter.dataset.rating = rating == null ? '' : String(rating);
    meter.querySelectorAll('.cat').forEach((cat) => {
      const v = Number(cat.dataset.value);
      cat.classList.toggle('on', rating != null && Math.sign(v) === Math.sign(rating) && Math.abs(v) <= Math.abs(rating));
    });
    meter.querySelector('.meter-text').textContent = short;

    card.className = card.className.replace(RATING_CLASS, '').replace(/\s+/g, ' ').trim();
    const cls = rating == null ? '' : `rating-${rating > 0 ? 'good' : 'bad'}-${Math.abs(rating)}`;
    if (cls) card.classList.add(cls);

    // カード上部の表示
    let badge = card.querySelector('.card-head .rating');
    if (rating == null) {
      if (badge) badge.remove();
    } else {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'rating';
        card.querySelector('.head-actions').append(badge);
      }
      badge.textContent = view;
      badge.title = `${view} (${short})`;
    }

    // セッション画面の目次
    const link = document.querySelector(`.toc a[href="#${card.id}"]`);
    if (link) {
      let t = link.querySelector('.toc-rating');
      if (rating == null) {
        if (t) t.remove();
      } else {
        if (!t) {
          t = document.createElement('span');
          link.insertBefore(t, link.querySelector('.toc-snip'));
        }
        t.className = `toc-rating ${cls}`;
        t.textContent = view;
      }
    }
  };

  const clearPreview = () => document.querySelectorAll('.cat.preview').forEach((c) => c.classList.remove('preview'));

  document.addEventListener('mouseover', (e) => {
    clearPreview();
    const cat = e.target.closest('.cat');
    if (!cat) return;
    const v = Math.abs(Number(cat.dataset.value));
    cat.closest('.meter-side').querySelectorAll('.cat').forEach((c) => {
      if (Math.abs(Number(c.dataset.value)) <= v) c.classList.add('preview');
    });
  });
  document.addEventListener('mouseout', (e) => { if (e.target.closest('.cat')) clearPreview(); });

  const onCatClick = async (cat) => {
    const meter = cat.closest('.meter');
    const card = cat.closest('.card');
    if (meter.classList.contains('busy')) return;
    const value = Number(cat.dataset.value);
    const current = meter.dataset.rating === '' ? null : Number(meter.dataset.rating);
    const next = current === value ? null : value;
    const text = meter.querySelector('.meter-text');
    const before = text.textContent;
    meter.classList.add('busy');
    try {
      const r = await api('PUT', `/api/turns/${meter.dataset.turn}/rating`, { rating: next });
      applyRating(card, r.rating, r.rating_view, r.rating_short);
    } catch (_) {
      meter.classList.add('error');
      text.textContent = '⚠ 保存できませんでした';
      setTimeout(() => { meter.classList.remove('error'); text.textContent = before; }, 2200);
    } finally {
      meter.classList.remove('busy');
    }
  };

  // ---- メモ: 追加 / 編集 / 削除(表示は textContent で組み立てるので、本文はHTMLとして解釈されない)
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  const buildNote = (n) => {
    const root = el('div', `note ${n.author}`);
    root.dataset.noteId = String(n.id);
    const meta = el('div', 'note-meta');
    meta.append(el('span', 'note-who', `${n.author === 'bot' ? '🤖' : '🧑'} ${n.author} · ${n.created_display}`));
    const actions = el('span', 'note-actions');
    const edit = el('button', 'note-edit', '編集');
    const del = el('button', 'note-del', '削除');
    edit.type = del.type = 'button';
    actions.append(edit, del);
    meta.append(actions);
    root.append(meta, el('div', 'note-body', n.body));
    return root;
  };

  // 入力フォーム。onSubmit(text) が例外を投げたら、フォーム内にエラーを出す。
  const buildForm = ({ initial = '', onSubmit, onCancel }) => {
    const form = el('form', 'note-form');
    const ta = el('textarea');
    ta.value = initial;
    ta.maxLength = 20000;
    ta.rows = 3;
    ta.placeholder = 'メモ(例: この通りにやったら失敗した。v2のAPIだった)';
    const actions = el('div', 'note-form-actions');
    const err = el('span', 'form-error');
    const cancel = el('button', 'note-cancel', 'キャンセル');
    const save = el('button', 'note-save', '保存');
    cancel.type = 'button';
    save.type = 'submit';
    actions.append(el('span', 'hint', 'Ctrl/⌘ + Enter で保存'), err, cancel, save);
    form.append(ta, actions);

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!ta.value.trim()) { err.textContent = 'メモが空です'; return; }
      save.disabled = true;
      err.textContent = '';
      try {
        await onSubmit(ta.value);
      } catch (_) {
        err.textContent = '⚠ 保存できませんでした';
        save.disabled = false;
      }
    });
    cancel.addEventListener('click', onCancel);
    // Esc / Ctrl+Enter はフォーム内のどこにフォーカスがあっても効くようにする(ボタン上でも)
    form.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); form.requestSubmit(); }
      else if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
    });
    return form;
  };

  const onNoteAdd = (button) => {
    const card = button.closest('.card');
    const open = card.querySelector(':scope > .note-form');
    if (open) { open.querySelector('textarea').focus(); return; }
    const form = buildForm({
      onCancel: () => form.remove(),
      onSubmit: async (text) => {
        const n = await api('POST', `/api/turns/${card.id.replace('turn-', '')}/notes`, { body: text });
        const section = card.querySelector('.block.notes');
        section.hidden = false;
        section.querySelector('.note-list').append(buildNote(n));
        form.remove();
      },
    });
    card.querySelector('.card-foot').before(form);
    form.querySelector('textarea').focus();
  };

  const onNoteEdit = (button) => {
    const note = button.closest('.note');
    if (note.querySelector('.note-form')) return;
    const body = note.querySelector('.note-body');
    const form = buildForm({
      initial: body.textContent,
      onCancel: () => { form.remove(); body.hidden = false; },
      onSubmit: async (text) => {
        const n = await api('PATCH', `/api/notes/${note.dataset.noteId}`, { body: text });
        body.textContent = n.body;
        form.remove();
        body.hidden = false;
      },
    });
    body.hidden = true;
    body.after(form);
    form.querySelector('textarea').focus();
  };

  const onNoteDelete = async (button) => {
    const note = button.closest('.note');
    if (!window.confirm('このメモを削除しますか?')) return;
    try {
      await api('DELETE', `/api/notes/${note.dataset.noteId}`);
    } catch (_) {
      window.alert('削除できませんでした');
      return;
    }
    const section = note.closest('.block.notes');
    note.remove();
    if (!section.querySelector('.note')) section.hidden = true;
  };

  document.addEventListener('click', (e) => {
    const cat = e.target.closest('button.cat');
    if (cat) return onCatClick(cat);
    const add = e.target.closest('button.note-add');
    if (add) return onNoteAdd(add);
    const edit = e.target.closest('button.note-edit');
    if (edit) return onNoteEdit(edit);
    const del = e.target.closest('button.note-del');
    if (del) return onNoteDelete(del);
  });

  // ------------------------------------------------------------ 絞り込みバー
  const filters = document.querySelector('form.filters');
  if (filters) {
    const kind = filters.querySelector('select[name="rating"]');
    const level = filters.querySelector('select[name="level"]');
    // 「強さ」は、😺 / 😿 を選んだときだけ使える
    const syncLevel = () => { level.disabled = !['good', 'bad'].includes(kind.value); };
    kind.addEventListener('change', syncLevel);
    syncLevel();

    // セレクトと日付は、変えたらすぐ反映する(検索語は「検索」ボタンか Enter)
    filters.querySelectorAll('select, input[type="date"]').forEach((c) => {
      c.addEventListener('change', () => filters.requestSubmit());
    });
    // 空の項目はURLに出さない
    filters.addEventListener('submit', () => {
      filters.querySelectorAll('input, select').forEach((c) => {
        if (!c.value) c.disabled = true;
      });
    });
  }
})();
