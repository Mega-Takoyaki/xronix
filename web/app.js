"use strict";
// 画面は日本語。機能の説明は長文にせず、ⓘのツールチップに集約する。
// 取得したツイート本文は信頼できないため、必ず textContent で描画する（innerHTML は使わない）。

const $ = (s, r = document) => r.querySelector(s);
const view = $("#view");

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
const tip = (text) => h("i", { class: "tip", tabindex: "0", "data-tip": text, "aria-label": text }, "ⓘ");
// ⓘの説明: 画面の端で切れないよう、位置を計算して表示する（マウスのホバー、キーボードのフォーカス、スマホのタップ）
{
  const place = (el) => {
    const r = el.getBoundingClientRect(), vw = document.documentElement.clientWidth, w = Math.min(280, vw - 16);
    el.style.setProperty("--tip-left", `${Math.max(8, Math.min(vw - w - 8, r.left + r.width / 2 - w / 2))}px`);
    if (r.top > 150) { el.style.setProperty("--tip-bottom", `${window.innerHeight - r.top + 6}px`); el.style.setProperty("--tip-top", "auto"); }
    else { el.style.setProperty("--tip-top", `${r.bottom + 6}px`); el.style.setProperty("--tip-bottom", "auto"); }
  };
  const hide = () => document.querySelectorAll(".tip.on").forEach((e) => e.classList.remove("on"));
  const show = (el) => { hide(); place(el); el.classList.add("on"); el.dataset.at = String(Date.now()); };
  document.addEventListener("mouseover", (e) => { const t = e.target.closest && e.target.closest(".tip"); if (t) show(t); });
  document.addEventListener("mouseout", (e) => { if (e.target.closest && e.target.closest(".tip")) hide(); });
  document.addEventListener("focusin", (e) => { const t = e.target.closest && e.target.closest(".tip"); if (t) show(t); });
  document.addEventListener("focusout", hide);
  document.addEventListener("click", (e) => { const t = e.target.closest && e.target.closest(".tip"); if (t) { e.preventDefault(); e.stopPropagation(); if (t.classList.contains("on") && Date.now() - Number(t.dataset.at || 0) > 600) hide(); else if (!t.classList.contains("on")) show(t); } else hide(); }, true);  // タップでは、マウスのイベントと続けて起きるため、直後のクリックでは閉じない
  window.addEventListener("scroll", hide, { passive: true });
}
const lbl = (text, tipText) => h("label", {}, text, tipText ? tip(tipText) : null);

async function api(method, path, body, retried = false) {
  const auth = await XAuth.header();
  if (auth === null) { XAuth.showLogin(); throw Object.assign(new Error("ログインが必要です。"), { status: 401 }); }
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Requested-With": "xronix", ...auth },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401 && !retried && (await XAuth.recover())) return api(method, path, body, true);  // トークンの期限切れは、更新して1回だけ再試行する
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `エラー（HTTP ${res.status}）`), { status: res.status });
  return data;
}
// トースト通知（画面の上部）。レベルで色を分ける: success=薄緑、info=薄青、warn=薄黄、error=薄ピンク
const TOAST_ICON = { success: "✓", info: "ℹ", warn: "⚠", error: "✕" };
function toast(msg, level = "success") {
  if (level === true) level = "error";                      // 古い呼び方（true=エラー）との互換
  if (!TOAST_ICON[level]) level = "success";
  if (!msg) return;
  const box = $("#toasts");
  const el = h("div", { class: `toast ${level}`, role: level === "error" || level === "warn" ? "alert" : "status", title: "クリックで閉じる", onclick: () => el.remove() },
    h("span", { class: "ic" }, TOAST_ICON[level]), h("span", {}, msg));
  box.append(el);
  while (box.children.length > 4) box.firstChild.remove();
  setTimeout(() => el.remove(), { error: 7000, warn: 5500 }[level] || 3000);
}
async function act(fn, okMsg, okLevel = "success") {
  try { const r = await fn(); if (okMsg) toast(okMsg, okLevel); return r; }
  catch (e) { toast(e.message, e.status && e.status < 500 ? "warn" : "error"); }  // 入力や状態の問題（4xx）は警告、サーバー・外部サービスの失敗は、エラー
}
const fmtSec = (iso) => (iso ? new Date(iso).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—");
const fmt = (iso) => (iso ? new Date(iso).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
function weighted(s) { let n = 0; for (const ch of s) n += /[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹯＀-｠￠-￦]/.test(ch) || ch.codePointAt(0) > 0x1F000 ? 2 : 1; return n; }
const hasUrl = (s) => /(https?:\/\/|www\.|\b[a-z0-9-]+\.(com|net|org|jp|co|io|me|ly|to|app|xyz|info|biz|gl|cc|tv|fm)\b)/i.test(s);

// ---------- ヘッダー ----------
let status = {};
async function refreshStatus() {
  try { status = await api("GET", "/api/status"); } catch { return; }
  $("#mode").textContent = status.mode === "mock" ? "モック" : "";
  const banner = $("#banner");
  banner.hidden = status.mode !== "mock" && !status.auth_error;
  banner.textContent = status.auth_error ? "Xの認可が切れています。設定でXと再接続してください。" : "モックモードです。Xへは何も送信されません。";
  $("#chips").replaceChildren(...[
    h("span", { class: "chip " + (status.x_connected ? "good" : "bad") }, status.x_connected ? `X: @${status.username}` : "X: 未接続"),
    h("span", { class: "chip" }, `本日の送信 ${status.sent_today}/${status.daily_limit}`),
    h("span", { class: "chip " + (status.job_enabled ? "good" : "") }, status.job_enabled ? `定期処理: 次回 ${fmtSec(status.next_due)}` : "定期処理: 停止中"),
    status.unknown ? h("span", { class: "chip bad" }, `結果不明 ${status.unknown}件`) : null,
  ].filter(Boolean));
  $("#n-queue").textContent = status.pending || "";
  const k = $("#kill");
  k.textContent = status.kill_switch ? "緊急停止中（解除）" : "緊急停止";
  k.className = "kill" + (status.kill_switch ? " on" : "");
}
$("#kill").addEventListener("click", async () => {
  if (!status.kill_switch && !confirm("取得と送信をすべて止めます。よろしいですか？")) return;
  await act(() => api("POST", "/api/kill", { on: !status.kill_switch }), status.kill_switch ? "解除しました" : "停止しました", status.kill_switch ? "success" : "warn");
  await refreshStatus(); render();
});

// ---------- 承認待ち・履歴 ----------
const STATE = { pending: "承認待ち", review: "要確認", failed: "送信失敗", unknown: "結果不明", sending: "送信中", send_queued: "送信待ち（キュー）", sent: "送信済み", rejected: "却下", spam: "スパム", blocked: "ブロック対象", done: "対応済み（自動）" };

function actionsView(m) {
  if (!m.actions || !m.actions.length) return null;
  return h("div", { class: "muted", style: "margin:4px 0" }, m.rule_name ? `ルール「${m.rule_name}」: ` : "",
    ...m.actions.map((a) => h("span", { class: a.ok ? "ok" : "err", style: "margin-right:10px" }, `${a.ok ? "✓" : "✗"} ${a.label}（${a.detail}）`)));
}

function mentionCard(m, queue) {
  const card = h("div", { class: "card" });
  const head = h("div", { class: "row" },
    m.metric ? null : h("span", { class: `tag ${m.category}` }, m.label), h("span", { class: "tag" }, STATE[m.state] || m.state),
    m.kind_label ? h("span", { class: "tag" }, m.kind_label) : null,
    h("span", { class: "muted grow" }, m.metric ? `${m.metric.label} ${m.metric.value}件（基準 ${m.metric.threshold}件）・ ${fmt(m.fetched_at)}` : `ユーザーID ${m.author_id} ・ ${fmt(m.created_at || m.fetched_at)}${m.source ? ` ・ 取得元: ${m.source}` : ""}`),
    h("a", { href: `https://x.com/i/web/status/${m.target_tweet_id || m.id}`, target: "_blank", rel: "noopener noreferrer" }, "Xで開く"),
  );
  card.append(head, h("div", { class: "muted" }, m.metric ? "あなたの投稿" : null), h("div", { class: "quote" }, m.text), actionsView(m));
  if (m.state === "unknown") {
    card.append(h("div", { class: "warn" }, "送信結果が確認できませんでした。Xで返信が付いているか確認してください。", tip("通信が途切れた場合、Xが投稿を受け付けた可能性があります。二重投稿を避けるため、自動では再送しません。")),
      h("div", { class: "row end" },
        h("button", { onclick: () => resolve(m.id, "sent") }, "送信済みだった"),
        h("button", { onclick: () => resolve(m.id, "not_sent") }, "送信されていなかった")));
    return card;
  }
  if (m.state === "send_queued") {
    card.append(h("div", { class: "muted" }, `送信待ちの返信（${m.queue_auto ? "自動送信" : "承認済み"}。送信キューの間隔をあけて、順に送ります）`), h("div", { class: "quote" }, m.queued_text || ""),
      h("div", { class: "row end" }, h("button", { onclick: async () => { await act(() => api("POST", "/api/sendq/cancel", { id: m.id }), "キューから外しました（承認待ちに戻しました）", "info"); await refreshStatus(); render(); } }, "キューから外す")));
    return card;
  }
  if (!queue) {
    if (m.final_text) card.append(h("div", { class: "muted" }, "送信した返信"), h("div", { class: "quote" }, m.final_text));
    if (m.error) card.append(h("div", { class: "err" }, m.error));
    return card;
  }
  if (m.error) card.append(h("div", { class: "err" }, m.error));
  const ta = h("textarea", { placeholder: m.state === "review" ? "返信文を入力（空のままなら、送信せず却下できます）" : "" }, m.draft_text);
  ta.value = m.draft_text;
  const counter = h("span", { class: "muted" });
  const send = h("button", { class: "primary" }, "承認して送信");
  const check = () => {
    const t = ta.value.trim(), n = weighted(t);
    const problem = !t ? "返信文が空です" : hasUrl(t) ? "リンクは入れられません" : n > 280 ? "文字数が多すぎます" : "";
    counter.textContent = `${n}/280` + (problem ? `　${problem}` : "");
    counter.className = problem ? "err" : "muted";
    send.disabled = !!problem || status.kill_switch;
  };
  ta.addEventListener("input", check); check();
  send.addEventListener("click", async () => {
    send.disabled = true;
    const r = await act(() => api("POST", `/api/mentions/${m.id}/reply`, { text: ta.value }), "送信しました");
    await refreshStatus(); if (r) render(); else check();
  });
  card.append(h("div", { class: "row" }, lbl("返信案", "定型文から選んだ返信案です。そのまま送るか、書き換えてください。全角は2文字として数え、リンクは入れられません。"), counter), ta,
    h("div", { class: "row end" },
      h("button", { onclick: () => redraft(m) }, "別の定型文にする"),
      h("button", { onclick: () => block(m) }, "ブロック対象に追加"),
      h("button", { class: "danger", onclick: () => reject(m) }, "却下"),
      send));
  return card;
}
async function reject(m) { await act(() => api("POST", `/api/mentions/${m.id}/reject`), "却下しました"); await refreshStatus(); render(); }
async function redraft(m) { await act(() => api("POST", `/api/mentions/${m.id}/redraft`, {}), "差し替えました"); render(); }
async function block(m) {
  if (!confirm("このユーザーを返信対象から外し、他の承認待ちも外します。よろしいですか？")) return;
  await act(() => api("POST", `/api/mentions/${m.id}/block`, {}), "ブロック対象に追加しました"); await refreshStatus(); render();
}
async function resolve(id, outcome) { await act(() => api("POST", `/api/mentions/${id}/resolve`, { outcome }), "記録しました"); await refreshStatus(); render(); }

async function renderQueue(queue) {
  const { items } = await api("GET", `/api/mentions?view=${queue ? "queue" : "history"}`);
  view.replaceChildren(...(items.length ? items.map((m) => mentionCard(m, queue)) : [h("div", { class: "empty" }, queue ? "承認待ちはありません" : "履歴はまだありません")]));
}

// ---------- 定型文 ----------
async function renderTemplates() {
  const { items, categories } = await api("GET", "/api/templates");
  const catLabel = Object.fromEntries(categories.map((c) => [c.key, c.label]));
  const newText = h("textarea", { placeholder: "新しい定型文" });
  const newCat = h("select", {}, categories.map((c) => h("option", { value: c.key }, c.label)));
  const form = h("div", { class: "card" }, h("h3", {}, "定型文を追加", tip("分類ごとに、最近選ばれていないものから順に返信案へ使います。複数用意すると、同じ文面が続きません。")),
    h("div", { class: "row" }, newCat), newText,
    h("div", { class: "row end" }, h("button", { class: "primary", onclick: async () => {
      if (await act(() => api("POST", "/api/templates", { category: newCat.value, text: newText.value }), "追加しました")) renderTemplates();
    } }, "追加")));
  const rows = items.map((t) => {
    const ta = h("textarea", {}, t.text); ta.value = t.text;
    return h("div", { class: "card" },
      h("div", { class: "row" }, h("span", { class: `tag ${t.category}` }, catLabel[t.category]),
        h("span", { class: "muted grow" }, `送信 ${t.use_count}回 ・ 最終使用 ${fmt(t.last_used_at)}`),
        h("label", { class: "inline" }, h("input", { type: "checkbox", checked: t.enabled, onchange: async (e) => { await act(() => api("PUT", `/api/templates/${t.id}`, { enabled: e.target.checked })); } }), "有効")),
      ta, h("div", { class: "row end" },
        h("button", { onclick: async () => { await act(() => api("PUT", `/api/templates/${t.id}`, { text: ta.value }), "保存しました"); } }, "保存"),
        h("button", { class: "danger", onclick: async () => { if (confirm("削除しますか？")) { await act(() => api("DELETE", `/api/templates/${t.id}`), "削除しました"); renderTemplates(); } } }, "削除")));
  });
  view.replaceChildren(form, ...rows);
}

// ---------- ジョブ ----------
const TRIGGER = { manual: "手動", schedule: "定期", watchdog: "見守り" };
async function renderJobs() {
  const d = await api("GET", "/api/jobs");
  const s = d.settings;
  const enabled = h("input", { type: "checkbox", checked: s.enabled });
  const interval = h("input", { type: "number", min: 1, max: 999, step: 1 }); interval.value = s.interval_minutes;
  const jitter = h("input", { type: "number", min: 0, max: 99, step: 1 }); jitter.value = s.jitter_minutes;
  const pages = h("input", { type: "number", min: 1, max: 10, value: s.max_pages }); pages.value = s.max_pages;
  const mInt = h("input", { type: "number", min: 5, max: 1440 }); mInt.value = s.metrics_interval_minutes;
  const mDays = h("input", { type: "number", min: 1, max: 30 }); mDays.value = s.metrics_window_days;
  const mMax = h("input", { type: "number", min: 5, max: 100 }); mMax.value = s.metrics_max_posts;
  const wide = h("input", { type: "checkbox", checked: s.wide_fetch });
  const verbose = h("input", { type: "checkbox", checked: s.verbose_log });
  const initial = h("select", {}, [["from_now", "現在地点から始める（過去分は取り込まない）"], ["backfill", "過去のメンションも取り込む"]].map(([v, t]) => h("option", { value: v, selected: v === s.initial_mode }, t)));
  const preview = h("div", { class: "muted" });
  const updatePreview = () => {
    const i = Number(interval.value), j = Number(jitter.value);
    preview.textContent = !Number.isInteger(i) || !Number.isInteger(j) ? "" : j > 0
      ? `${i}分ごと（前回の開始から ${Math.max(i - j, 0.25)}〜${i + j}分後のあいだで、秒までランダムにずらします）`
      : `${i}分ごと（前回の開始から、ちょうど${i}分後）`;
  };
  interval.addEventListener("input", updatePreview); jitter.addEventListener("input", updatePreview); updatePreview();
  const presets = [30, 45, 60].map((n) => h("button", { type: "button", onclick: () => { interval.value = n; updatePreview(); } }, `${n}分`));
  const ctl = h("div", { class: "card" }, h("h3", {}, "定期処理"),
    h("label", { class: "inline" }, enabled, "定期処理を有効にする", tip("有効にすると、設定した間隔で新着を取得し、分類して返信案を作ります。送信は必ず承認が必要です。有効にした直後に、最初の1回が動きます。")),
    h("div", { class: "row" },
      h("div", { class: "grow" }, lbl("実行間隔（分）", "1〜999分を、好きな分数で入力できます。短くするほど、Xの読み取り件数が増えます。新着が無ければ、料金はほぼかかりません。"), h("div", { class: "row" }, h("div", { class: "grow" }, interval), ...presets)),
      h("div", { class: "grow" }, lbl("ランダムなずらし幅（分）", "0〜99分。指定した分数以内で、前後にランダムにずらします。0ならずらしません。毎回、秒の単位までランダムになるので、00秒に揃いません。間隔より大きいずらし幅の場合、最短15秒後になります。"), jitter)),
    preview,
    h("div", { class: "row" },
      h("div", { class: "grow" }, lbl("1回の最大ページ数", "1ページは最大100件です。新着が多いときの上限で、残りは次回に取得します。"), pages),
      h("div", { class: "grow" }, lbl("初回の動作", "初回の実行だけに使います。現在地点から始めると、過去のメンションへの返信案は作られません。"), initial)),
    h("label", { class: "inline", style: "margin-top:10px" }, wide, "取得範囲を広げる（自分の@が無い返信と、引用も取る）", tip("メンション一覧には、自分の@が無い返信や、引用が出ません。オンにすると、検索で自分宛ての返信を補い、引用が増えた自分の投稿だけ引用を取りに行きます。ルールで「自分の投稿への返信」「自分の返信への返信」「自分の投稿の引用」を使うときに必要です。他人の投稿の読み取りになり、約$0.005／件がかかります。")),
    h("label", { class: "inline", style: "margin-top:6px" }, verbose, "詳細ログを残す", tip("ジョブの各段階を、統計・ログに「詳細」として記録します（取得件数、取得位置、取り込んだ／スキップした理由、使ったルールなど）。投稿の本文は記録しません。7日で削除されます。取りこぼしの調査に使います。")),
    h("h3", { style: "margin-top:14px" }, "自投稿の数値の確認", tip("「いいね数が基準以上」などのルールがあるときだけ、自分の投稿を読みに行きます。読み取りは1投稿あたり約$0.001です。例: 6時間ごとに20投稿なら、1日約$0.08です。")),
    h("div", { class: "row" },
      h("div", { class: "grow" }, lbl("確認の間隔（分）", "5〜1440分。定期処理の間隔とは別に、この間隔ごとに確認します。"), mInt),
      h("div", { class: "grow" }, lbl("見る投稿の期間（日）", "1〜30日。この期間に投稿したものが対象です。"), mDays),
      h("div", { class: "grow" }, lbl("見る投稿の最大数", "5〜100件。多いほど、読み取り料金が増えます。"), mMax)),
    h("div", { class: "row end", style: "margin-top:12px" },
      h("button", { onclick: async () => {
        await act(() => api("PUT", "/api/settings/job", { enabled: enabled.checked, interval_minutes: Number(interval.value), jitter_minutes: Number(jitter.value), max_pages: Number(pages.value), initial_mode: initial.value, wide_fetch: wide.checked, verbose_log: verbose.checked,
          metrics_interval_minutes: Number(mInt.value), metrics_window_days: Number(mDays.value), metrics_max_posts: Number(mMax.value) }), "保存しました");
        await refreshStatus(); renderJobs();
      } }, "保存"),
      h("button", { class: "primary", onclick: async (e) => { e.target.disabled = true; const r = await act(() => api("POST", "/api/jobs/run"), null); if (r) toast(r.status === "ok" ? `完了しました（新規${r.new}件）` : r.message, r.status === "ok" ? "success" : r.status === "skipped" ? "warn" : "error"); await refreshStatus(); renderJobs(); } }, "今すぐ実行")),
    h("div", { class: "muted" }, `次回の実行予定: ${s.enabled ? fmtSec(d.next_due) : "停止中"}`));
  const reset = h("div", { class: "card" }, h("h3", {}, "取得位置のリセット", tip("通常は不要です。取得位置（どこまで読んだか）を消し、次の実行を「初回」として扱います。履歴・統計・取り込み済みの反応は消えません。")),
    h("div", { class: "muted" }, d.cursor_set ? "取得位置は、記録されています。" : "取得位置は、まだ記録されていません（次の実行が初回です）。"),
    h("div", { class: "row", style: "margin-top:8px" },
      h("button", { onclick: async () => {
        if (!confirm("取得位置をリセットし、次の実行を「現在地点から始める」にします。過去分は取り込まれません。よろしいですか？")) return;
        if (await act(() => api("POST", "/api/jobs/reset-cursor", { mode: "from_now" }), "リセットしました（次回は現在地点から）", "warn")) renderJobs();
      } }, "現在地点からやり直す"),
      h("button", { class: "danger", onclick: async () => {
        if (!confirm("取得位置をリセットし、次の実行で過去のメンションも取り込みます。\n\n・取り込み件数が多くなり、Xの読み取り費用が増えます（約$0.001／件）\n・自動送信のルールが有効だと、まとめて送信される恐れがあります\n\n本当に実行しますか？")) return;
        if (await act(() => api("POST", "/api/jobs/reset-cursor", { mode: "backfill" }), "リセットしました（次回は過去分も取り込みます）", "warn")) renderJobs();
      } }, "過去分も取り込むようにやり直す")));
  const sq = await api("GET", "/api/sendq");
  const qInt = h("input", { type: "number", min: 10, max: 3600 }); qInt.value = sq.settings.queue_interval_seconds;
  const qJit = h("input", { type: "number", min: 0, max: 1800 }); qJit.value = sq.settings.queue_jitter_seconds;
  const qManual = h("input", { type: "checkbox", checked: sq.settings.manual_via_queue });
  const sendq = h("div", { class: "card" }, h("h3", {}, "送信キュー", tip("自動送信（と、設定すれば承認からの送信）は、すぐには送らず、キューに入れて、間隔をあけて1件ずつ送ります。短時間に続けて送ると、スパムとみなされたり、表示が制限されたりする恐れがあるためです。間隔は、設定した秒数に、ランダムな幅（秒）を加えて決めます。")),
    h("div", { class: "row" },
      h("div", { class: "grow" }, lbl("送信の間隔（秒）", "10〜3600秒。前の送信から、この秒数あとに、次の1件を送ります。"), qInt),
      h("div", { class: "grow" }, lbl("ランダムな幅（秒）", "0〜1800秒。間隔を、この秒数以内で、前後にランダムにずらします（秒まで）。0ならずらしません。"), qJit)),
    h("label", { class: "inline", style: "margin-top:8px" }, qManual, "承認からの送信も、キューに入れる", tip("オフ（既定）では、画面やTelegramで承認した返信は、すぐ送ります。オンにすると、承認した返信も、キューに入れて、間隔をあけて送ります。")),
    h("div", { class: "row end", style: "margin:8px 0" }, h("button", { onclick: async () => { await act(() => api("PUT", "/api/settings/send", { queue_interval_seconds: Number(qInt.value), queue_jitter_seconds: Number(qJit.value), manual_via_queue: qManual.checked }), "保存しました"); renderJobs(); } }, "保存")),
    sq.queued.length ? h("div", {}, h("div", { class: "muted" }, `送信待ち ${sq.queued.length}件。次の送信予定: ${fmtSec(sq.next_at)}${sq.last_sent_at ? `（前回の送信: ${fmtSec(sq.last_sent_at)}）` : ""}`),
      h("table", {}, sq.queued.map((q, i) => h("tr", {}, h("td", {}, `${i + 1}`), h("td", { style: "word-break:break-all" }, q.text), h("td", {}, q.auto ? "自動" : "承認済み"),
        h("td", {}, h("button", { class: "danger", onclick: async () => { await act(() => api("POST", "/api/sendq/cancel", { id: q.id }), "キューから外しました（承認待ちに戻しました）", "info"); renderJobs(); } }, "外す")))))) : h("div", { class: "muted" }, `送信待ちはありません${sq.last_sent_at ? `（前回の送信: ${fmtSec(sq.last_sent_at)}）` : ""}`));
  const diag = h("div", { class: "card" }, h("h3", {}, "取得の診断", tip("取りこぼしの調査用です。投稿のURL（またはID）を入れると、その投稿をXから読み、返信先・本文の@・取り込み状況・取得位置との関係から、「なぜ取り込まれないか」を表示します。読み取りの費用は、1回約$0.005〜0.01です。「取り込む」を押すと、通常の取り込みと同じ処理（ルールも動く）で、承認待ちか履歴に入れます。")),
    h("div", { class: "row" }, h("div", { class: "grow" }, h("input", { type: "text", id: "diag-input", placeholder: "https://x.com/…/status/… または 投稿のID" })),
      h("button", { onclick: async (e) => { e.target.disabled = true; await runDiag(false); e.target.disabled = false; } }, "診断する"),
      h("button", { title: "自分の投稿ごとに、Xが数える返信数・引用数と、取り込み済みの件数を並べます（約$0.001／投稿）", onclick: async (e) => {
        e.target.disabled = true;
        const r = await act(() => api("POST", "/api/diagnose/posts"), null);
        if (r) showPosts(r);
        e.target.disabled = false;
      } }, "自投稿の返信数を確認")),
    h("div", { id: "diag-out", style: "margin-top:10px" }));
  const diagOut = () => $("#diag-out");
  const runDiag = async (ingest) => {
    const v = $("#diag-input").value.trim();
    if (!v) return toast("投稿のURLかIDを入力してください", "warn");
    const r = await act(() => api("POST", "/api/diagnose/tweet", { input: v, ingest }), null);
    if (r) showTweet(r);
  };
  const row = (k, v) => h("tr", {}, h("th", { style: "white-space:nowrap;text-align:left;vertical-align:top;padding-right:12px" }, k), h("td", { style: "word-break:break-all" }, v));
  const showTweet = (r) => {
    const rows = [row("投稿", `${r.id}（${r.posted_at}）`)];
    if (r.found) {
      rows.push(row("種類", r.kind_label), row("投稿者ID", r.author_id), row("返信先のユーザー", r.in_reply_to_user_id ? `${r.in_reply_to_user_id}${r.directed_at_me ? "（自分）" : ""}` : "なし"),
        row("本文の自分の@", r.mentions_me ? "あり" : "なし"), row("本文（先頭）", `${r.preview}${r.text_len > 60 ? "…" : ""}（${r.text_len}文字）`),
        row("取り込み", r.ingested ? `済み（状態: ${r.ingested.state}、取得元: ${r.ingested.source}）` : "まだ"));
      if (r.mentions_cursor && r.mentions_cursor.at) rows.push(row("メンション一覧の取得位置", r.mentions_cursor.at));
    }
    const verdict = h("ul", {}, r.verdict.map((t) => h("li", {}, t)));
    const probes = r.probes && r.probes.length ? h("table", { style: "margin-top:8px" }, h("thead", {}, h("tr", {}, ["検索の確認", "検索の式", "結果"].map((c) => h("th", {}, c)))),
      r.probes.map((p) => h("tr", {}, h("td", {}, p.label), h("td", { style: "word-break:break-all" }, p.query), h("td", { class: p.error ? "err" : p.contains ? "ok" : "warn" }, p.error ? p.error : `${p.count}件${p.contains ? "（この投稿を含む）" : "（この投稿を含まない）"}`)))) : null;
    const canIngest = r.found && !r.ingested && !r.own_post;
    diagOut().replaceChildren(...[h("table", {}, rows), h("div", { style: "margin-top:6px" }, h("b", {}, "診断")), verdict, probes,
      canIngest ? h("button", { class: "primary", onclick: async () => { if (confirm("この投稿を取り込みます。ルールが動きます（いいね・自動送信などを設定している場合は、実行されます）。よろしいですか？")) await runDiag(true); } }, "この投稿を取り込む") : null,
      h("div", { class: "muted" }, `読み取り ${r.reads}件`)].filter(Boolean));
  };
  const showPosts = (r) => {
    diagOut().replaceChildren(
      h("div", { class: "muted" }, `直近${r.window_days}日の自分の投稿（読み取り ${r.reads}件）。「Xの数」が「取り込み済み」より多い投稿は、返信や引用を取りこぼしている目安です（返信数には、他人同士の返信も含まれます）。`),
      h("table", {}, h("thead", {}, h("tr", {}, ["投稿", "日時", "Xの返信", "取り込み済み", "Xの引用", "取り込み済み"].map((c) => h("th", {}, c)))),
        r.posts.map((p) => h("tr", {}, h("td", {}, `${p.preview || p.id}`), h("td", {}, p.posted_at.slice(5, 16)), h("td", {}, p.x_replies),
          h("td", { class: p.x_replies > p.got_replies ? "warn" : "" }, p.got_replies), h("td", {}, p.x_quotes), h("td", { class: p.x_quotes > p.got_quotes ? "warn" : "" }, p.got_quotes)))));
  };
  const dev = status.mode === "mock" ? h("div", { class: "card" }, h("h3", {}, "動作確認（モック専用）", tip("モックの取得キューへ、サンプルのメンションを追加します。「今すぐ実行」で取り込まれます。")),
    h("div", { class: "row" },
      h("button", { onclick: async () => { await act(() => api("POST", "/api/dev/mock-mentions", { count: 4 }), "サンプルを4件追加しました"); } }, "サンプルのメンションを追加"),
      h("button", { onclick: async () => { await act(() => api("POST", "/api/dev/mock-posts", { likes: 120 }), "いいね120件の自投稿を追加しました"); } }, "サンプルの自投稿（いいね120件）を追加"))) : null;
  const runs = h("div", { class: "card" }, h("h3", {}, "実行履歴"), d.runs.length ? h("table", {}, h("tr", {}, ["開始", "起動", "結果", "取得", "新規", "返信案", "概算費用", "メッセージ"].map((c) => h("th", {}, c))),
    d.runs.map((r) => h("tr", {}, h("td", {}, fmtSec(r.started_at)), h("td", {}, TRIGGER[r.trigger] || r.trigger), h("td", { class: r.status === "ok" ? "ok" : "err" }, r.status === "ok" ? "成功" : "失敗"),
      h("td", {}, r.fetched), h("td", {}, r.new), h("td", {}, r.drafted), h("td", {}, `$${r.est_cost_usd}`), h("td", {}, r.message)))) : h("div", { class: "muted" }, "まだ実行されていません"));
  view.replaceChildren(...[ctl, sendq, diag, reset, dev, runs].filter(Boolean));
}

// ---------- 統計・ログ ----------
const KIND = { job: "ジョブ", send: "送信", token: "トークン", x: "X接続", settings: "設定", ai: "AI", notify: "通知", safety: "安全装置", blocklist: "ブロック対象" };
async function renderStats() {
  const [s, l] = await Promise.all([api("GET", "/api/stats"), api("GET", "/api/logs")]);
  const max = Math.max(1, ...s.received, ...s.sent);
  const tiles = h("div", { class: "tiles" },
    [["受信（累計）", s.total_mentions], ["今月の送信", s.month.sent], ["今月の読み取り", s.month.reads], [`今月の概算費用`, `$${s.month.est_cost_usd}`, "X APIの従量課金の概算です。返信の料金区分が未確認のため、投稿作成と同額（$0.015）で計算しています。"], ["ジョブ成功/全体", `${s.runs.ok}/${s.runs.total}`]]
      .map(([t, v, tp]) => h("div", { class: "tile" }, h("span", { class: "muted" }, t, tp ? tip(tp) : null), h("b", {}, v))));
  const bars = h("div", { class: "card" }, h("h3", {}, "日別の受信と送信（直近14日）"),
    h("div", { class: "bars" }, s.days.map((d, i) => h("div", { class: "bar", title: `${d} 受信${s.received[i]} 送信${s.sent[i]}` },
      h("div", { class: "pair" }, h("div", {}, h("i", { style: `height:${(s.received[i] / max) * 100}%` })), h("div", {}, h("i", { class: "s", style: `height:${(s.sent[i] / max) * 100}%` }))), d.slice(5)))),
    h("div", { class: "legend" }, h("span", {}, "受信"), h("span", { class: "s" }, "送信")));
  const cats = h("div", { class: "card" }, h("h3", {}, "分類の内訳"), s.by_category.length ? h("table", {}, s.by_category.map((c) => h("tr", {}, h("td", {}, c.label), h("td", {}, c.count)))) : h("div", { class: "muted" }, "データがありません"));
  const lvl = h("select", { onchange: async (e) => { const r = await api("GET", `/api/logs?level=${e.target.value}`); fill(r.items); } },
    [["", "情報・警告・エラー"], ["all", "詳細も含めてすべて"], ["warn", "警告"], ["error", "エラー"], ["debug", "詳細のみ"]].map(([v, t]) => h("option", { value: v }, t)));
  const tbody = h("tbody");
  const fill = (items) => tbody.replaceChildren(...items.map((r) => h("tr", {}, h("td", {}, fmt(r.ts)), h("td", { class: r.level === "error" ? "err" : r.level === "warn" ? "warn" : r.level === "debug" ? "muted" : "" }, { debug: "詳細", info: "情報", warn: "警告", error: "エラー" }[r.level]), h("td", {}, KIND[r.kind] || r.kind), h("td", { class: r.level === "debug" ? "muted" : "", style: "white-space:pre-wrap;word-break:break-all" }, r.message))));
  fill(l.items);
  const logs = h("div", { class: "card" }, h("div", { class: "row" }, h("h3", { class: "grow" }, "ログ", tip("操作とジョブの記録です。「詳細」は、ジョブの各段階（何件取れたか、なぜ取り込まなかったか）の記録で、7日で削除されます。それ以外は60日で削除されます。トークンや投稿の本文は記録しません。")), lvl),
    h("table", {}, h("thead", {}, h("tr", {}, ["日時", "レベル", "種類", "内容"].map((c) => h("th", {}, c)))), tbody));
  view.replaceChildren(tiles, bars, cats, logs);
}

// ---------- 設定 ----------
function secretField(name, label, tipText, secrets, onChange) {
  const input = h("input", { type: "password", autocomplete: "off", placeholder: secrets[name] ? "設定済み（変更する場合のみ入力）" : "未設定" });
  return h("div", {}, lbl(label, tipText), h("div", { class: "row" }, h("div", { class: "grow" }, input),
    h("button", { onclick: async () => { if (!input.value.trim()) return toast("値を入力してください", "warn"); await act(() => api("PUT", `/api/secrets/${name}`, { value: input.value }), "保存しました"); onChange(); } }, "保存"),
    secrets[name] ? h("button", { class: "danger", onclick: async () => { await act(() => api("DELETE", `/api/secrets/${name}`), "削除しました"); onChange(); } }, "削除") : null));
}

async function renderSettings() {
  const s = await api("GET", "/api/settings"), reload = async () => { await refreshStatus(); renderSettings(); };
  const params = new URLSearchParams(location.search);
  if (params.get("x")) { toast({ ok: "Xと接続しました", denied: "Xでの認可が拒否されました", invalid: "認可の期限が切れました。もう一度お試しください", failed: "接続に失敗しました。ログを確認してください" }[params.get("x")] || "", { ok: "success", denied: "warn", invalid: "warn" }[params.get("x")] || "error"); history.replaceState(null, "", "/"); }

  // X接続
  const cid = h("input", { type: "text", value: s.x.client_id, autocomplete: "off" }); cid.value = s.x.client_id;
  const redir = h("input", { type: "text" }); redir.value = s.x.redirect_uri || `${location.origin}/api/x/callback`;
  const xcard = h("div", { class: "card" }, h("h3", {}, "X との接続", tip("XroniX専用のXアプリを使います。他のアプリとトークンを共有すると、互いのトークンが無効になります。")),
    h("div", { class: "row" }, h("span", { class: "tag " + (status.x_connected ? "fan" : "caution") }, status.x_connected ? `接続中: @${status.username}` : "未接続"),
      status.mode === "mock" ? h("span", { class: "muted" }, "モックモード（実際のXには接続しません）") : null),
    status.mode === "mock" ? null : h("div", {}, lbl("Client ID"), cid, lbl("コールバックURL", "Xの開発者コンソールに登録したものと完全に同じ値を入力してください。ローカルでは http://127.0.0.1:ポート/api/x/callback です。"), redir,
      h("div", { class: "row end", style: "margin:8px 0" }, h("button", { onclick: async () => { await act(() => api("PUT", "/api/settings/x", { client_id: cid.value, redirect_uri: redir.value }), "保存しました"); reload(); } }, "保存")),
      secretField("x_client_secret", "Client Secret", "画面には表示されません。保存済みの値は確認できず、上書きのみできます。", s.secrets, reload)),
    h("div", { class: "row end", style: "margin-top:10px" },
      status.x_connected ? h("button", { onclick: async () => { const r = await act(() => api("POST", "/api/x/test")); if (r) toast(`接続OK: @${r.username}`); } }, "接続テスト") : null,
      status.x_connected ? h("button", { class: "danger", onclick: async () => { if (confirm("接続を解除しますか？")) { await act(() => api("POST", "/api/x/disconnect"), "解除しました"); reload(); } } }, "接続を解除") : null,
      h("button", { class: "primary", onclick: async () => { const r = await act(() => api("POST", "/api/x/connect")); if (!r) return; if (r.url) location.href = r.url; else { toast("接続しました"); reload(); } } }, status.x_connected ? "再接続" : "Xと接続")));

  // AI接続
  const prov = h("select", {}, [["bedrock", "Amazon Bedrock（既定）"], ["openai", "OpenAI"], ["rule", "AIを使わない（ルールのみ）"]].map(([v, t]) => h("option", { value: v, selected: v === s.ai.provider }, t)));
  const bModel = h("select", {}, h("option", { value: s.ai.bedrock_model }, s.ai.bedrock_model || "自動（安価なモデルを選ぶ）"));
  const bManual = h("input", { type: "text", placeholder: "一覧に無い場合は、ここに直接入力（任意）" });
  const bRegion = h("input", { type: "text" }); bRegion.value = s.ai.bedrock_region;
  const modelInfo = h("div", { class: "muted" });
  async function loadModels(refresh) {
    modelInfo.textContent = "モデルの一覧を取得しています…";
    try {
      const r = await api("GET", "/api/ai/models" + (refresh ? "?refresh=1" : ""));
      const groups = [["モデル", "model"], ["推論プロファイル（リージョン横断）", "profile"]];
      bModel.replaceChildren(h("option", { value: "" }, `自動（安価なモデルを選ぶ）${r.default ? " → 現在: " + r.default : ""}`),
        ...groups.map(([t, k]) => { const its = r.items.filter((i) => i.kind === k); return its.length ? h("optgroup", { label: t }, its.map((i) => h("option", { value: i.id, disabled: i.available === false }, `${i.available === true ? "✓ 利用可　" : i.available === false ? "✗ 利用不可　" : ""}${i.label}（${i.id}）${i.available === false && i.reason ? " — " + i.reason : ""}`))) : null; }).filter(Boolean));
      if (r.selected && !r.items.some((i) => i.id === r.selected)) bModel.append(h("option", { value: r.selected }, `${r.selected}（一覧に無い）`));
      bModel.value = r.selected || "";
      const ok = r.items.filter((i) => i.available === true).length, ng = r.items.filter((i) => i.available === false).length;
      const sel = r.items.find((i) => i.id === r.selected);
      modelInfo.textContent = `${r.items.length}件（利用可 ${ok}件、利用不可 ${ng}件）${r.sample ? "（モックのサンプル）" : r.cached ? "（1時間以内に取得した一覧）" : ""}。` +
        (sel && sel.available === false ? "選択中のモデルは、このアカウントでは使えません。別のモデルを選ぶか、「自動」に戻してください。" : "「利用不可」は、選べません。実際に使えるかは、「接続テスト」で確認できます。");
    } catch (e) { modelInfo.textContent = `一覧を取得できませんでした: ${e.message}（保存済みのモデルは、そのまま使えます）`; }
  }
  const oModel = h("input", { type: "text" }); oModel.value = s.ai.openai_model;
  const oBase = h("input", { type: "text" }); oBase.value = s.ai.openai_base_url;
  const bedrockBox = h("div", {}, h("div", { class: "row" },
    h("div", { class: "grow" }, lbl("モデル", "このアカウントで使えるかを確認した、モデルの一覧です。「自動」なら、使えるモデルの中から、名前で安価な小型モデルを選びます（GPT-6 Luna、nano、mini、Haiku、Nova Micro などの順）。実行時に拒否されたら、自動で次の候補に切り替えます。選んだモデルは、分類にも、AIで作る返信文にも使います。"), bModel),
    h("div", { style: "align-self:flex-end" }, h("button", { onclick: () => loadModels(true) }, "一覧を更新"))),
    bManual,
    h("div", { class: "row", style: "margin-top:6px" }, h("div", { class: "grow" }, lbl("リージョン", "Bedrockを呼び出すリージョンです。変えたら、保存してから「一覧を更新」を押してください。"), bRegion)),
    modelInfo, h("div", { class: "muted" }, "AWSの権限（IAM）で認証するため、APIキーは不要です。"));
  const openaiBox = h("div", {}, h("div", { class: "row" },
    h("div", { class: "grow" }, lbl("モデル名"), oModel),
    h("div", { class: "grow" }, lbl("接続先URL", "OpenAI互換のAPI（/chat/completions）です。"), oBase)),
    secretField("openai_api_key", "APIキー", "画面には表示されません。保存済みの値は確認できず、上書きのみできます。", s.secrets, reload));
  const showProvider = () => { bedrockBox.hidden = prov.value !== "bedrock"; openaiBox.hidden = prov.value !== "openai"; };
  prov.addEventListener("change", showProvider); showProvider();
  const aicard = h("div", { class: "card" }, h("h3", {}, "AI との接続", tip("スパム・要注意以外のメンション本文が、選んだAIへ送られます（分類のため）。返信文をAIで作るかどうかは、「ルール」のアクションで決めます。本アプリでは、成人向けの内容は扱わない方針です。")),
    status.mode === "mock" ? h("div", { class: "muted" }, "モックモードでは、外部のAIを呼びません（モデルの一覧は、サンプルです）。") : null,
    h("div", { class: "row" }, h("div", { class: "grow" }, lbl("方式"), prov)),
    bedrockBox, openaiBox,
    h("div", { class: "row end", style: "margin-top:10px" },
      h("button", { onclick: async () => { await act(() => api("PUT", "/api/settings/ai", { provider: prov.value, bedrock_model: bManual.value.trim() || bModel.value, bedrock_region: bRegion.value, openai_model: oModel.value, openai_base_url: oBase.value }), "保存しました"); if (bManual.value.trim()) { bManual.value = ""; loadModels(false); } } }, "保存"),
      h("button", { onclick: async (e) => { e.target.disabled = true; const r = await act(() => api("POST", "/api/ai/test")); e.target.disabled = false; if (r) toast(`接続OK（${r.model}／試験文の判定: ${r.label || "不明"}）`); } }, "接続テスト（保存後に押す）")));
  if (s.ai.provider === "bedrock") loadModels(false);
  prov.addEventListener("change", () => { if (prov.value === "bedrock") loadModels(false); });

  // 通知
  const tgOn = h("input", { type: "checkbox", checked: s.notify.telegram_enabled });
  const tgChat = h("input", { type: "text", placeholder: "例: 123456789" }); tgChat.value = s.notify.telegram_chat_id;
  const chatList = h("div", { style: "flex:1" });
  const siteUrl = h("input", { type: "text", placeholder: "https://…（任意。承認ボタンには必須）" }); siteUrl.value = s.notify.site_url;
  const tgUser = h("input", { type: "text", placeholder: "例: 123456789（グループのときだけ）" }); tgUser.value = s.notify.telegram_user_id || "";
  const ncard = h("div", { class: "card" }, h("h3", {}, "通知（Telegram）", tip("承認待ちの件数だけを送ります。返信の本文は送りません。")),
    h("label", { class: "inline" }, tgOn, "新着があったら通知する"),
    secretField("telegram_bot_token", "ボットトークン", "Telegramの BotFather でボットを作ると発行されます。画面には表示されません。", s.secrets, reload),
    h("div", { class: "row" },
      h("div", { class: "grow" }, lbl("チャットID", "個人とのチャットでは、あなたのユーザーIDと同じ数字です。分かっていれば、そのまま入力できます。分からなければ、ボットに何かメッセージを送ってから、「自動で取得」を押してください。ボットに、先に「開始」を押していないと、通知は届きません。"), tgChat,
        h("div", { class: "row", style: "margin-top:6px" }, h("button", { onclick: async () => {
          const r = await act(() => api("POST", "/api/notify/detect-chat"));
          if (!r) return;
          if (!r.chats.length) return toast("ボットにメッセージが届いていません。ボットに何か送ってから、もう一度押してください", "warn");
          const pick = (c) => { tgChat.value = c.id; chatList.replaceChildren(h("div", { class: "ok" }, `入力しました: ${c.name || c.username || c.id}（${c.id}）。「保存」を押してください。`)); };
          if (r.chats.length === 1) return pick(r.chats[0]);
          chatList.replaceChildren(h("div", { class: "muted" }, "複数見つかりました。通知を送るチャットを選んでください。"),
            ...r.chats.map((c) => h("div", { class: "row" }, h("button", { onclick: () => pick(c) }, "選ぶ"), h("span", {}, `${c.name || c.username || "（名前なし）"}　${c.type}　${c.id}`))));
        } }, "自動で取得"), chatList)),
      h("div", { class: "grow" }, lbl("画面のURL（任意）", "入力すると、通知にこのURLを添えます。承認ボタンを使うときは、TelegramのWebhookの宛先にもなるので、必須です（https://…cloudfront.net のような、このアプリのURL）。"), siteUrl)),
    h("div", { class: "row", style: "margin-top:10px" },
      h("div", { class: "grow" }, lbl("許可するユーザーID（任意）", "承認ボタンを押せる人を、あなたのTelegramのユーザーIDに限ります。個人とのチャットなら、チャットIDがユーザーIDと同じなので、空のままで構いません。グループのチャットを使う場合だけ、入力してください。"), tgUser)),
    h("div", { class: "row", style: "margin-top:10px;align-items:center" },
      h("div", { class: s.notify.telegram_actions ? "ok" : "muted" }, s.notify.telegram_actions ? "承認ボタン: 有効（本文の全文を、Telegramに送ります）" : "承認ボタン: 無効（件数だけを通知します）", tip("有効にすると、承認待ちが、相手の投稿と返信案の全文つきで、「承認して送信」「却下」のボタンと一緒に、Telegramに届きます。ボタンの結果は、そのメッセージ上に表示されます。ボタンを押せるのは、許可したユーザーIDだけです。承認は、画面の承認と同じ検査（上限・ブロック・緊急停止・リンク禁止）を通ります。返信案の書き換えは、画面で行います。本文がTelegramのサーバーを通る点に、ご注意ください。有効にするとTelegramのWebhookを登録するため、「自動で取得」（チャットの検出）は使えなくなります。")),
      s.notify.telegram_actions
        ? h("button", { onclick: async () => { if (!confirm("承認ボタンを無効にします（Webhookを削除）。よろしいですか？")) return; await act(() => api("POST", "/api/telegram/disable"), "無効にしました", "info"); renderSettings(); } }, "承認ボタンを無効にする")
        : h("button", { class: "primary", onclick: async () => { if (!confirm("承認待ちの本文（相手の投稿と返信案の全文）を、Telegramに送るようにします。よろしいですか？\n\n先に「保存」を押して、ボットトークン・チャットID・画面のURLを保存してください。")) return; await act(() => api("POST", "/api/telegram/enable"), "承認ボタンを有効にしました"); renderSettings(); } }, "承認ボタンを有効にする")),
    h("div", { class: "row end", style: "margin-top:10px" },
      h("button", { onclick: async () => { await act(() => api("PUT", "/api/settings/notify", { telegram_enabled: tgOn.checked, telegram_chat_id: tgChat.value, site_url: siteUrl.value, telegram_user_id: tgUser.value }), "保存しました"); } }, "保存"),
      h("button", { onclick: async () => { const r = await act(() => api("POST", "/api/notify/test")); if (r) toast(r.sent ? "送信しました" : `送信に失敗しました: ${r.reason}`, r.sent ? "success" : "error"); } }, "テスト送信（保存後に押す）")));

  // 安全装置
  const lim = h("input", { type: "number", min: 0, max: 100 }); lim.value = s.safety.daily_limit;
  const likeLim = h("input", { type: "number", min: 0, max: 100 }); likeLim.value = s.safety.like_daily_limit;
  const autoLim = h("input", { type: "number", min: 0, max: 100 }); autoLim.value = s.safety.auto_daily_limit;
  const convLim = h("input", { type: "number", min: 1, max: 50 }); convLim.value = s.safety.conversation_reply_limit;
  const authorLim = h("input", { type: "number", min: 1, max: 50 }); authorLim.value = s.safety.author_daily_limit;
  const { items: blocks } = await api("GET", "/api/blocklist");
  const bid = h("input", { type: "text", placeholder: "ユーザーID（数字）" }), bnote = h("input", { type: "text", placeholder: "メモ（任意）" });
  const scard = h("div", { class: "card" }, h("h3", {}, "安全装置"),
    h("div", { class: "row" },
      h("div", { class: "grow" }, lbl("1日の送信上限", "承認した返信と、自動送信を合わせた、1日の上限です。日本時間の0時に数え直します。超える送信は拒否されます。"), lim),
      h("div", { class: "grow" }, lbl("1日の自動送信の上限", "ルールで「承認なしで送信」したものの上限です。超えた分は、承認待ちに入ります。0なら、自動送信を止めます。"), autoLim),
      h("div", { class: "grow" }, lbl("1日のいいね上限", "ルールで付ける、いいねの上限です。大量のいいねは、スパム判定の原因になります。0なら、いいねを止めます。"), likeLim)),
    h("div", { class: "row" },
      h("div", { class: "grow" }, lbl("同じ相手との会話の返信上限（回）", "同じ相手に、同じ会話の中で返信した回数の上限です。超えると、ルールによる返信案づくりと自動送信を止めます（人が承認する送信は止めません）。相手が返信を繰り返す場合や、相手も自動返信の場合に、会話が終わらなくなることを防ぎます。"), convLim),
      h("div", { class: "grow" }, lbl("同じ相手への1日の返信上限（回）", "同じ相手に、1日（日本時間）に返信した回数の上限です。会話をまたいで数えます。超えると、ルールによる返信案づくりと自動送信を止めます。"), authorLim)),
    h("div", { class: "row end", style: "margin:8px 0" }, h("button", { onclick: async () => { await act(() => api("PUT", "/api/settings/safety", { daily_limit: Number(lim.value), auto_daily_limit: Number(autoLim.value), like_daily_limit: Number(likeLim.value), conversation_reply_limit: Number(convLim.value), author_daily_limit: Number(authorLim.value) }), "保存しました"); refreshStatus(); } }, "保存")),
    lbl("ブロック対象", "ここに入れたユーザーには、ルールを実行せず、送信もできません。Xのブロックとは別の、このアプリ内の一覧です。"),
    h("div", { class: "row" }, h("div", { class: "grow" }, bid), h("div", { class: "grow" }, bnote), h("button", { onclick: async () => { if (await act(() => api("POST", "/api/blocklist", { author_id: bid.value, note: bnote.value }), "追加しました")) renderSettings(); } }, "追加")),
    blocks.length ? h("table", {}, blocks.map((b) => h("tr", {}, h("td", {}, b.id), h("td", {}, b.note), h("td", {}, fmt(b.created_at)), h("td", {}, h("button", { class: "danger", onclick: async () => { await act(() => api("DELETE", `/api/blocklist/${b.id}`)); renderSettings(); } }, "解除"))))) : h("div", { class: "muted", style: "margin-top:8px" }, "登録はありません"));
  view.replaceChildren(xcard, aicard, ncard, scard);
}

// ---------- ルール（反応ごとのアクション） ----------
const SOURCE_LABEL = { template: "定型文から選ぶ", ai: "AIで作る（指示を書く）", manual: "空欄にする（自分で書く）" };
const CONTEXT_LABEL = { single: "この投稿だけを渡す", thread: "スレッドの流れも渡す（遡った件数ぶん、読み取り料金がかかる）" };
const TPL_LABEL = { auto: "分類に合わせる（ポジティブ反応／質問）", fan: "ポジティブ反応", question: "質問" };
const ACTION_TIP = {
  ignore: "何もせず、履歴に記録だけ残します。スパムなどに使います。",
  notify: "Telegramに、反応の種類だけを通知します（本文は送りません）。通知の設定が必要です。",
  like: "相手の投稿に、いいねを付けます。権限 like.write が必要です（Xと再接続すると付きます）。1日の上限があります。",
  queue_reply: "返信案を作り、「承認待ち」に入れます。あなたが確認して送ります。",
  send_reply: "返信を作り、あなたの承認なしで、そのまま投稿します。1日の自動送信の上限と、全体の送信上限が効きます。作れなかったときは、送りません。",
  blocklist: "相手を、このアプリのブロック対象に追加します（Xのブロックとは別）。以後、このユーザーには、ルールを実行しません。",
};

function ruleCard(rule, meta, reload) {
  const d = JSON.parse(JSON.stringify(rule));  // 編集中のコピー
  const card = h("div", { class: "card" });
  const isMetric = () => (meta.events.find((e) => e.key === d.events[0]) || {}).metric;
  const replyDefaults = (type) => ({ type, source: "template", template_category: "auto", prompt: "あなたは、親しみやすく丁寧なSNSアカウントの運用者です。受け取った投稿に、短い日本語で、感謝や共感を伝える返信を書いてください。相手を傷つけず、約束や個人情報には触れないでください。", context: "single", thread_depth: 3 });
  const sel = (options, value, onchange) => { const el = h("select", { onchange: (e) => onchange(e.target.value) }, options.map(([v, t]) => h("option", { value: v, selected: v === value }, t))); el.value = value; return el; };
  const num = (value, min, max, onchange) => { const el = h("input", { type: "number", min, max, step: 1, oninput: (e) => onchange(Number(e.target.value)) }); el.value = value; return el; };
  const draw = () => card.replaceChildren(...build());

  function actionRow(a, i) {
    const metric = isMetric();
    const types = meta.actions.filter((x) => !metric || x.metric_ok).map((x) => [x.key, x.label]);
    const row = h("div", { class: "card", style: "background:var(--bg);margin:8px 0" });
    const top = h("div", { class: "row" },
      h("div", { class: "grow" }, sel(types, a.type, (v) => { d.actions[i] = ["queue_reply", "send_reply"].includes(v) ? replyDefaults(v) : { type: v }; if (v === "send_reply" && d.actions[i].source === "manual") d.actions[i].source = "template"; draw(); })),
      tip(ACTION_TIP[a.type]),
      h("button", { class: "danger", onclick: () => { d.actions.splice(i, 1); draw(); } }, "削除"));
    row.append(top);
    if (a.type === "queue_reply" || a.type === "send_reply") {
      const sources = meta.sources[a.type];
      row.append(h("div", { class: "row" }, h("div", { class: "grow" }, lbl("返信文の作り方"), sel(sources.map((x) => [x, SOURCE_LABEL[x]]), a.source, (v) => { a.source = v; draw(); }))));
      if (a.source === "template") row.append(lbl("使う定型文の分類", "「分類に合わせる」は、ポジティブ反応→ポジティブ反応、質問→質問、それ以外→ポジティブ反応の定型文を使います。"), sel(Object.entries(TPL_LABEL), a.template_category, (v) => (a.template_category = v)));
      if (a.source === "ai") {
        const ta = h("textarea", { oninput: (e) => (a.prompt = e.target.value) }); ta.value = a.prompt;
        row.append(lbl("AIへの指示（プロンプト）", "返信文を作るときの、役割や口調の指示です。出力の決まり（短く、リンクなし、日本語）は、自動で付きます。"), ta,
          h("div", { class: "row" },
            h("div", { class: "grow" }, lbl("文脈", "「スレッドの流れも渡す」と、返信先をたどった投稿も、AIに渡します。会話の流れに沿った返信になります。たどった投稿の読み取り料金（1件あたり最大約$0.005）がかかります。"), sel(Object.entries(CONTEXT_LABEL), a.context, (v) => { a.context = v; draw(); })),
            a.context === "thread" ? h("div", {}, lbl("遡る件数", "1〜10件。返信先を、この件数まで遡ります。"), num(a.thread_depth, 1, 10, (v) => (a.thread_depth = v))) : null));
      }
      if (a.type === "send_reply") row.append(h("div", { class: "warn" }, "承認なしで、実際に投稿されます。1日の自動送信の上限が効きます（設定の「安全装置」）。"));
    }
    return row;
  }

  function build() {
    const metric = isMetric();
    const evs = meta.events;
    const head = h("div", { class: "row" },
      h("label", { class: "inline" }, h("input", { type: "checkbox", checked: d.enabled, onchange: (e) => (d.enabled = e.target.checked) }), "有効"),
      h("input", { type: "text", value: d.name, style: "flex:1;min-width:200px", oninput: (e) => (d.name = e.target.value) }),
      h("button", { onclick: async () => { await act(() => api("POST", `/api/rules/${rule.id}/move`, { direction: "up" })); reload(); } }, "↑"),
      h("button", { onclick: async () => { await act(() => api("POST", `/api/rules/${rule.id}/move`, { direction: "down" })); reload(); } }, "↓"));
    const kind = sel([["reaction", "反応（返信・引用・メンション）"], ["metric", "自投稿の数値（いいね数など）"]], metric ? "metric" : "reaction", (v) => {
      d.events = v === "metric" ? ["metric_likes"] : ["reply_to_post", "reply_to_reply"];
      if (v === "metric") { d.threshold = d.threshold || 100; d.categories = []; d.actions = d.actions.filter((a) => ["ignore", "notify", "queue_reply", "send_reply"].includes(a.type)); }
      draw();
    });
    const when = [lbl("対象", "「反応」は、他の人の返信・引用・メンションに対して動きます。「自投稿の数値」は、自分の投稿のいいね数などが基準を超えたときに、1回だけ動きます。"), kind];
    if (!metric) {
      when.push(lbl("反応の種類", "自分の投稿への返信／自分の返信への返信（スレッドの続き）／自分の投稿の引用／その他のメンション。引用は、メンションに含まれるものだけを拾います。"),
        h("div", { class: "row" }, ...evs.filter((e) => !e.metric).map((e) => h("label", { class: "inline" }, h("input", { type: "checkbox", checked: d.events.includes(e.key), onchange: (ev) => { d.events = ev.target.checked ? [...d.events, e.key] : d.events.filter((x) => x !== e.key); } }), e.label))),
        lbl("分類（何も選ばないと、すべて）", "メンションの本文から、ルールまたはAIで判定した分類です。スパム・要注意は、ルールで判定できたものはAIに送りません。"),
        h("div", { class: "row" }, ...meta.categories.map((c) => h("label", { class: "inline" }, h("input", { type: "checkbox", checked: d.categories.includes(c.key), onchange: (ev) => { d.categories = ev.target.checked ? [...d.categories, c.key] : d.categories.filter((x) => x !== c.key); } }), c.label))));
    } else {
      when.push(h("div", { class: "row" },
        h("div", { class: "grow" }, lbl("数値の種類"), sel(evs.filter((e) => e.metric).map((e) => [e.key, e.label]), d.events[0], (v) => (d.events = [v]))),
        h("div", { class: "grow" }, lbl("基準（以上）", "この数以上になったとき、各投稿で1回だけ動きます。基準を変えても、すでに動いた投稿には、動きません。"), num(d.threshold, 1, 100000000, (v) => (d.threshold = v)))));
    }
    const acts = [h("div", { class: "muted", style: "margin-top:8px" }, "アクション（上から順に実行）", tip("1つのルールに、最大5個まで。返信のアクションは1つまでです。1つが失敗しても、残りは続けます。"))];
    d.actions.forEach((a, i) => acts.push(actionRow(a, i)));
    if (d.actions.length < 5) acts.push(h("button", { onclick: () => { d.actions.push({ type: "ignore" }); draw(); } }, "＋ アクションを追加"));
    const foot = h("div", { class: "row end", style: "margin-top:10px" },
      h("button", { class: "danger", onclick: async () => { if (confirm("このルールを削除しますか？")) { await act(() => api("DELETE", `/api/rules/${rule.id}`), "削除しました"); reload(); } } }, "削除"),
      h("button", { class: "primary", onclick: async () => { if (await act(() => api("PUT", `/api/rules/${rule.id}`, d), "保存しました")) reload(); } }, "保存"));
    return [head, ...when, ...acts, foot];
  }
  draw();
  return card;
}

async function renderRules() {
  const { items, meta } = await api("GET", "/api/rules");
  const reload = () => renderRules();
  const ev = h("select", {}, meta.events.filter((e) => !e.metric).map((e) => h("option", { value: e.key }, e.label)));
  const cat = h("select", {}, meta.categories.map((c) => h("option", { value: c.key }, c.label)));
  const out = h("div", { class: "muted" });
  const intro = h("div", { class: "card" }, h("h3", {}, "ルール（反応ごとのアクション）", tip("反応の種類と分類に合ったルールを、上から順に探し、最初に合った1件だけを実行します。自投稿の数値のルールは、ルールごとに独立して動きます。何も合わなければ、人が確認する「承認待ち」に入ります。")),
    h("div", { class: "muted" }, "反応の種類（返信・引用など）ごとに、いいね、AIの返信案、通知などを設定します。"),
    h("div", { class: "row", style: "margin-top:10px" }, h("div", { class: "grow" }, lbl("確認: どのルールが動くか", "反応の種類と分類を選ぶと、動くルールを表示します（何も実行しません）。"), ev), h("div", { class: "grow" }, lbl("分類"), cat),
      h("button", { style: "align-self:flex-end", onclick: async () => { const r = await act(() => api("POST", "/api/rules/simulate", { event: ev.value, category: cat.value })); if (r) out.textContent = r.rule ? `「${r.rule.name}」→ ${r.rule.actions.join(" → ")}` : "合うルールがありません（人が確認します）"; } }, "確認")),
    out,
    h("div", { class: "row end" }, h("button", { onclick: async () => { await act(() => api("POST", "/api/rules", { name: "新しいルール", enabled: false, events: ["reply_to_post"], categories: [], actions: [{ type: "ignore" }] }), "追加しました"); reload(); } }, "＋ ルールを追加")));
  view.replaceChildren(intro, ...items.map((r) => ruleCard(r, meta, reload)));
}

// ---------- ルーティング ----------
let tab = new URLSearchParams(location.search).get("tab") || localStorage.getItem("xronix.tab") || "queue";
async function render() {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  try { localStorage.setItem("xronix.tab", tab); } catch {}
  $("#hint").replaceChildren(h("a", { href: `manual.html#${tab}`, target: "_blank", rel: "noopener" }, "この画面の使い方 ↗"));
  try {
    await ({ queue: () => renderQueue(true), history: () => renderQueue(false), rules: renderRules, templates: renderTemplates, jobs: renderJobs, stats: renderStats, settings: renderSettings }[tab] || (() => renderQueue(true)))();
  } catch (e) { view.replaceChildren(h("div", { class: "empty err" }, e.message)); }
}
$("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { tab = b.dataset.tab; render(); } });
XAuth.ready().then(() => {
  if (XAuth.enabled) { const b = $("#logout"); b.hidden = false; b.title = `${XAuth.email} をログアウトする`; b.addEventListener("click", () => XAuth.logout()); }
  refreshStatus().then(render);
  setInterval(refreshStatus, 30000);
});
