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
const lbl = (text, tipText) => h("label", {}, text, tipText ? tip(tipText) : null);

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Requested-With": "xronix" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `エラー（HTTP ${res.status}）`);
  return data;
}
let toastTimer;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), isErr ? 5000 : 2500);
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, true); }
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
  await act(() => api("POST", "/api/kill", { on: !status.kill_switch }), status.kill_switch ? "解除しました" : "停止しました");
  await refreshStatus(); render();
});

// ---------- 承認待ち・履歴 ----------
const STATE = { pending: "承認待ち", review: "要確認", failed: "送信失敗", unknown: "結果不明", sending: "送信中", sent: "送信済み", rejected: "却下", spam: "スパム", blocked: "ブロック対象", done: "対応済み（自動）" };

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
    h("span", { class: "muted grow" }, m.metric ? `${m.metric.label} ${m.metric.value}件（基準 ${m.metric.threshold}件）・ ${fmt(m.fetched_at)}` : `ユーザーID ${m.author_id} ・ ${fmt(m.created_at || m.fetched_at)}`),
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
    h("h3", { style: "margin-top:14px" }, "自投稿の数値の確認", tip("「いいね数が基準以上」などのルールがあるときだけ、自分の投稿を読みに行きます。読み取りは1投稿あたり約$0.001です。例: 6時間ごとに20投稿なら、1日約$0.08です。")),
    h("div", { class: "row" },
      h("div", { class: "grow" }, lbl("確認の間隔（分）", "5〜1440分。定期処理の間隔とは別に、この間隔ごとに確認します。"), mInt),
      h("div", { class: "grow" }, lbl("見る投稿の期間（日）", "1〜30日。この期間に投稿したものが対象です。"), mDays),
      h("div", { class: "grow" }, lbl("見る投稿の最大数", "5〜100件。多いほど、読み取り料金が増えます。"), mMax)),
    h("div", { class: "row end", style: "margin-top:12px" },
      h("button", { onclick: async () => {
        await act(() => api("PUT", "/api/settings/job", { enabled: enabled.checked, interval_minutes: Number(interval.value), jitter_minutes: Number(jitter.value), max_pages: Number(pages.value), initial_mode: initial.value,
          metrics_interval_minutes: Number(mInt.value), metrics_window_days: Number(mDays.value), metrics_max_posts: Number(mMax.value) }), "保存しました");
        await refreshStatus(); renderJobs();
      } }, "保存"),
      h("button", { class: "primary", onclick: async (e) => { e.target.disabled = true; const r = await act(() => api("POST", "/api/jobs/run"), null); if (r) toast(r.status === "ok" ? `完了しました（新規${r.new}件）` : r.message, r.status !== "ok"); await refreshStatus(); renderJobs(); } }, "今すぐ実行")),
    h("div", { class: "muted" }, `次回の実行予定: ${s.enabled ? fmtSec(d.next_due) : "停止中"}`));
  const dev = status.mode === "mock" ? h("div", { class: "card" }, h("h3", {}, "動作確認（モック専用）", tip("モックの取得キューへ、サンプルのメンションを追加します。「今すぐ実行」で取り込まれます。")),
    h("div", { class: "row" },
      h("button", { onclick: async () => { await act(() => api("POST", "/api/dev/mock-mentions", { count: 4 }), "サンプルを4件追加しました"); } }, "サンプルのメンションを追加"),
      h("button", { onclick: async () => { await act(() => api("POST", "/api/dev/mock-posts", { likes: 120 }), "いいね120件の自投稿を追加しました"); } }, "サンプルの自投稿（いいね120件）を追加"))) : null;
  const runs = h("div", { class: "card" }, h("h3", {}, "実行履歴"), d.runs.length ? h("table", {}, h("tr", {}, ["開始", "起動", "結果", "取得", "新規", "返信案", "概算費用", "メッセージ"].map((c) => h("th", {}, c))),
    d.runs.map((r) => h("tr", {}, h("td", {}, fmtSec(r.started_at)), h("td", {}, TRIGGER[r.trigger] || r.trigger), h("td", { class: r.status === "ok" ? "ok" : "err" }, r.status === "ok" ? "成功" : "失敗"),
      h("td", {}, r.fetched), h("td", {}, r.new), h("td", {}, r.drafted), h("td", {}, `$${r.est_cost_usd}`), h("td", {}, r.message)))) : h("div", { class: "muted" }, "まだ実行されていません"));
  view.replaceChildren(ctl, dev, runs);
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
    [["", "すべて"], ["info", "情報"], ["warn", "警告"], ["error", "エラー"]].map(([v, t]) => h("option", { value: v }, t)));
  const tbody = h("tbody");
  const fill = (items) => tbody.replaceChildren(...items.map((r) => h("tr", {}, h("td", {}, fmt(r.ts)), h("td", { class: r.level === "error" ? "err" : r.level === "warn" ? "warn" : "" }, { info: "情報", warn: "警告", error: "エラー" }[r.level]), h("td", {}, KIND[r.kind] || r.kind), h("td", {}, r.message))));
  fill(l.items);
  const logs = h("div", { class: "card" }, h("div", { class: "row" }, h("h3", { class: "grow" }, "ログ", tip("操作とジョブの記録です。60日で自動的に削除されます。トークンや返信の本文は記録しません。")), lvl),
    h("table", {}, h("thead", {}, h("tr", {}, ["日時", "レベル", "種類", "内容"].map((c) => h("th", {}, c)))), tbody));
  view.replaceChildren(tiles, bars, cats, logs);
}

// ---------- 設定 ----------
function secretField(name, label, tipText, secrets, onChange) {
  const input = h("input", { type: "password", autocomplete: "off", placeholder: secrets[name] ? "設定済み（変更する場合のみ入力）" : "未設定" });
  return h("div", {}, lbl(label, tipText), h("div", { class: "row" }, h("div", { class: "grow" }, input),
    h("button", { onclick: async () => { if (!input.value.trim()) return toast("値を入力してください", true); await act(() => api("PUT", `/api/secrets/${name}`, { value: input.value }), "保存しました"); onChange(); } }, "保存"),
    secrets[name] ? h("button", { class: "danger", onclick: async () => { await act(() => api("DELETE", `/api/secrets/${name}`), "削除しました"); onChange(); } }, "削除") : null));
}

async function renderSettings() {
  const s = await api("GET", "/api/settings"), reload = async () => { await refreshStatus(); renderSettings(); };
  const params = new URLSearchParams(location.search);
  if (params.get("x")) { toast({ ok: "Xと接続しました", denied: "Xでの認可が拒否されました", invalid: "認可の期限が切れました。もう一度お試しください", failed: "接続に失敗しました。ログを確認してください" }[params.get("x")] || "", params.get("x") !== "ok"); history.replaceState(null, "", "/"); }

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
        ...groups.map(([t, k]) => { const its = r.items.filter((i) => i.kind === k); return its.length ? h("optgroup", { label: t }, its.map((i) => h("option", { value: i.id }, `${i.label}（${i.id}）`))) : null; }).filter(Boolean));
      if (r.selected && !r.items.some((i) => i.id === r.selected)) bModel.append(h("option", { value: r.selected }, `${r.selected}（一覧に無い）`));
      bModel.value = r.selected || "";
      modelInfo.textContent = `${r.items.length}件${r.sample ? "（モックのサンプル）" : r.cached ? "（1時間以内に取得した一覧）" : ""}。モデルの利用有効化が済んでいるかは、「接続テスト」で確認できます。`;
    } catch (e) { modelInfo.textContent = `一覧を取得できませんでした: ${e.message}（保存済みのモデルは、そのまま使えます）`; }
  }
  const oModel = h("input", { type: "text" }); oModel.value = s.ai.openai_model;
  const oBase = h("input", { type: "text" }); oBase.value = s.ai.openai_base_url;
  const bedrockBox = h("div", {}, h("div", { class: "row" },
    h("div", { class: "grow" }, lbl("モデル", "いま使えるモデルの一覧から選びます。「自動」なら、名前から安価な小型モデルを選びます（GPT-6 Luna、nano、mini、Haiku、Nova Micro などの順）。選んだモデルは、分類にも、AIで作る返信文にも使います。"), bModel),
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
  const siteUrl = h("input", { type: "text", placeholder: "https://…（任意）" }); siteUrl.value = s.notify.site_url;
  const ncard = h("div", { class: "card" }, h("h3", {}, "通知（Telegram）", tip("承認待ちの件数だけを送ります。返信の本文は送りません。")),
    h("label", { class: "inline" }, tgOn, "新着があったら通知する"),
    secretField("telegram_bot_token", "ボットトークン", "Telegramの BotFather でボットを作ると発行されます。画面には表示されません。", s.secrets, reload),
    h("div", { class: "row" },
      h("div", { class: "grow" }, lbl("チャットID", "作ったボットに何かメッセージを送ってから、ブラウザで https://api.telegram.org/bot（トークン）/getUpdates を開くと、chat の id が分かります。グループの場合は -100 から始まる数字です。"), tgChat),
      h("div", { class: "grow" }, lbl("画面のURL（任意）", "入力すると、通知にこのURLを添えます。"), siteUrl)),
    h("div", { class: "row end", style: "margin-top:10px" },
      h("button", { onclick: async () => { await act(() => api("PUT", "/api/settings/notify", { telegram_enabled: tgOn.checked, telegram_chat_id: tgChat.value, site_url: siteUrl.value }), "保存しました"); } }, "保存"),
      h("button", { onclick: async () => { const r = await act(() => api("POST", "/api/notify/test")); if (r) toast(r.sent ? "送信しました" : `送信に失敗しました: ${r.reason}`, !r.sent); } }, "テスト送信（保存後に押す）")));

  // 安全装置
  const lim = h("input", { type: "number", min: 0, max: 100 }); lim.value = s.safety.daily_limit;
  const likeLim = h("input", { type: "number", min: 0, max: 100 }); likeLim.value = s.safety.like_daily_limit;
  const autoLim = h("input", { type: "number", min: 0, max: 100 }); autoLim.value = s.safety.auto_daily_limit;
  const { items: blocks } = await api("GET", "/api/blocklist");
  const bid = h("input", { type: "text", placeholder: "ユーザーID（数字）" }), bnote = h("input", { type: "text", placeholder: "メモ（任意）" });
  const scard = h("div", { class: "card" }, h("h3", {}, "安全装置"),
    h("div", { class: "row" },
      h("div", { class: "grow" }, lbl("1日の送信上限", "承認した返信と、自動送信を合わせた、1日の上限です。日本時間の0時に数え直します。超える送信は拒否されます。"), lim),
      h("div", { class: "grow" }, lbl("1日の自動送信の上限", "ルールで「承認なしで送信」したものの上限です。超えた分は、承認待ちに入ります。0なら、自動送信を止めます。"), autoLim),
      h("div", { class: "grow" }, lbl("1日のいいね上限", "ルールで付ける、いいねの上限です。大量のいいねは、スパム判定の原因になります。0なら、いいねを止めます。"), likeLim)),
    h("div", { class: "row end", style: "margin:8px 0" }, h("button", { onclick: async () => { await act(() => api("PUT", "/api/settings/safety", { daily_limit: Number(lim.value), auto_daily_limit: Number(autoLim.value), like_daily_limit: Number(likeLim.value) }), "保存しました"); refreshStatus(); } }, "保存")),
    lbl("ブロック対象", "ここに入れたユーザーには、ルールを実行せず、送信もできません。Xのブロックとは別の、このアプリ内の一覧です。"),
    h("div", { class: "row" }, h("div", { class: "grow" }, bid), h("div", { class: "grow" }, bnote), h("button", { onclick: async () => { if (await act(() => api("POST", "/api/blocklist", { author_id: bid.value, note: bnote.value }), "追加しました")) renderSettings(); } }, "追加")),
    blocks.length ? h("table", {}, blocks.map((b) => h("tr", {}, h("td", {}, b.id), h("td", {}, b.note), h("td", {}, fmt(b.created_at)), h("td", {}, h("button", { class: "danger", onclick: async () => { await act(() => api("DELETE", `/api/blocklist/${b.id}`)); renderSettings(); } }, "解除"))))) : h("div", { class: "muted", style: "margin-top:8px" }, "登録はありません"));
  view.replaceChildren(xcard, aicard, ncard, scard);
}

// ---------- ルール（反応ごとのアクション） ----------
const SOURCE_LABEL = { template: "定型文から選ぶ", ai: "AIで作る（指示を書く）", manual: "空欄にする（自分で書く）" };
const CONTEXT_LABEL = { single: "この投稿だけを渡す", thread: "スレッドの流れも渡す（遡った件数ぶん、読み取り料金がかかる）" };
const TPL_LABEL = { auto: "分類に合わせる（ファン／質問）", fan: "ファンの感想", question: "質問" };
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
      if (a.source === "template") row.append(lbl("使う定型文の分類", "「分類に合わせる」は、ファン→ファンの感想、質問→質問、それ以外→ファンの感想の定型文を使います。"), sel(Object.entries(TPL_LABEL), a.template_category, (v) => (a.template_category = v)));
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
  try {
    await ({ queue: () => renderQueue(true), history: () => renderQueue(false), rules: renderRules, templates: renderTemplates, jobs: renderJobs, stats: renderStats, settings: renderSettings }[tab] || (() => renderQueue(true)))();
  } catch (e) { view.replaceChildren(h("div", { class: "empty err" }, e.message)); }
}
$("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { tab = b.dataset.tab; render(); } });
refreshStatus().then(render);
setInterval(refreshStatus, 30000);
