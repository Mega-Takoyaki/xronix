"use strict";
// ログイン（Amazon Cognito）。メールアドレスがログインID。パスワード忘れは、メールの確認コードで再設定する。
// Cognitoの公開API（InitiateAuth など）を、このページから直接呼ぶ。サーバーの秘密情報は使わない。
// トークンは、このブラウザの localStorage に保存する。取得した投稿の本文は、textContent で描画している（innerHTML は使わない）。
(function () {
  const KEY = "xronix.auth";
  let cfg = { enabled: false };
  let tok = null; // { id, refresh, exp(ms), email }
  let onReady = null;

  const store = {
    load() { try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch { return null; } },
    save(v) { try { v ? localStorage.setItem(KEY, JSON.stringify(v)) : localStorage.removeItem(KEY); } catch {} },
  };

  const MESSAGES = {
    NotAuthorizedException: "メールアドレスかパスワードが違います。",
    UserNotFoundException: "メールアドレスかパスワードが違います。",
    UserNotConfirmedException: "このユーザーは、まだ有効になっていません。",
    PasswordResetRequiredException: "パスワードの再設定が必要です。「パスワードを忘れた場合」から進んでください。",
    CodeMismatchException: "確認コードが違います。",
    ExpiredCodeException: "確認コードの期限が切れています。もう一度、コードを送ってください。",
    InvalidPasswordException: "パスワードが条件を満たしていません（12文字以上で、英大文字・英小文字・数字を含めてください）。",
    LimitExceededException: "試行回数が多すぎます。しばらく待ってから、やり直してください。",
    TooManyRequestsException: "リクエストが多すぎます。しばらく待ってから、やり直してください。",
    TooManyFailedAttemptsException: "失敗が続いたため、一時的に止めています。しばらく待ってください。",
    InvalidParameterException: "入力を確認してください。このユーザーは、まだパスワードを再設定できない状態かもしれません（初回は、メールで届いた仮パスワードでログインしてください）。",
    CodeDeliveryFailureException: "確認コードのメールを送れませんでした。",
  };

  async function idp(target, body) {
    let res;
    try {
      res = await fetch(`https://cognito-idp.${cfg.region}.amazonaws.com/`, {
        method: "POST",
        headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}` },
        body: JSON.stringify(body),
      });
    } catch { throw Object.assign(new Error("通信できませんでした。ネットワークを確認してください。"), { code: "Network" }); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const code = String(data.__type || "").split("#").pop();
      throw Object.assign(new Error(MESSAGES[code] || `エラーが発生しました（${code || "HTTP " + res.status}）`), { code });
    }
    return data;
  }

  function adopt(result, email, keepRefresh) {
    tok = { id: result.IdToken, refresh: result.RefreshToken || keepRefresh, exp: Date.now() + (result.ExpiresIn || 3600) * 1000, email };
    store.save(tok);
  }

  async function refresh() {
    if (!tok || !tok.refresh) throw new Error("ログインが必要です。");
    const r = await idp("InitiateAuth", { AuthFlow: "REFRESH_TOKEN_AUTH", ClientId: cfg.client_id, AuthParameters: { REFRESH_TOKEN: tok.refresh } });
    adopt(r.AuthenticationResult, tok.email, tok.refresh);
  }

  const A = {
    get enabled() { return cfg.enabled; },
    get email() { return tok ? tok.email : ""; },
    // APIに付けるヘッダー。期限が近ければ更新する。ログインできていなければ null
    async header() {
      if (!cfg.enabled) return {};
      if (tok && tok.exp - 60000 < Date.now()) { try { await refresh(); } catch { tok = null; store.save(null); } }
      return tok ? { Authorization: tok.id } : null;
    },
    // APIが401を返したとき。更新できれば true（呼び出し側が再試行する）
    async recover() {
      if (!cfg.enabled) return false;
      try { await refresh(); return true; } catch { tok = null; store.save(null); A.showLogin(); return false; }
    },
    async logout() {
      const t = tok; tok = null; store.save(null);
      if (t && t.refresh) { try { await idp("RevokeToken", { ClientId: cfg.client_id, Token: t.refresh }); } catch {} }
      location.reload();
    },
    async login(email, password) {
      const r = await idp("InitiateAuth", { AuthFlow: "USER_PASSWORD_AUTH", ClientId: cfg.client_id, AuthParameters: { USERNAME: email, PASSWORD: password } });
      if (r.ChallengeName === "NEW_PASSWORD_REQUIRED") return { challenge: "new_password", session: r.Session };
      if (r.ChallengeName) throw new Error("この認証方法には対応していません。");
      adopt(r.AuthenticationResult, email);
      return {};
    },
    async setNewPassword(email, session, password) {
      const r = await idp("RespondToAuthChallenge", { ChallengeName: "NEW_PASSWORD_REQUIRED", ClientId: cfg.client_id, Session: session, ChallengeResponses: { USERNAME: email, NEW_PASSWORD: password } });
      adopt(r.AuthenticationResult, email);
    },
    forgot(email) { return idp("ForgotPassword", { ClientId: cfg.client_id, Username: email }); },
    confirmForgot(email, code, password) { return idp("ConfirmForgotPassword", { ClientId: cfg.client_id, Username: email, ConfirmationCode: code, Password: password }); },
  };

  // ---------- ログイン画面 ----------
  function el(tag, attrs = {}, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else if (k === "class") e.className = v; else e.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null) e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return e;
  }

  A.showLogin = function (start = "login") {
    document.body.classList.add("logged-out");
    const box = document.getElementById("login");
    box.hidden = false;
    let email = "", session = "";
    const msg = el("div", { class: "login-msg", role: "alert" });
    const say = (t, ok = false) => { msg.textContent = t || ""; msg.className = "login-msg" + (ok ? " ok" : ""); };
    const field = (label, attrs) => { const i = el("input", attrs); return [el("label", {}, label, i), i]; };
    const busy = async (btn, fn) => {
      btn.disabled = true; say("");
      try { await fn(); } catch (e) { say(e.message); } finally { btn.disabled = false; }
    };
    const frame = (title, ...kids) => box.replaceChildren(el("form", { class: "login-card", onsubmit: (e) => e.preventDefault() },
      el("div", { class: "login-brand" }, "XroniX"), el("h1", {}, title), kids, msg));
    const done = () => { box.hidden = true; document.body.classList.remove("logged-out"); if (onReady) { const f = onReady; onReady = null; f(); } else location.reload(); };

    const loginView = () => {
      const [l1, i1] = field("メールアドレス（ログインID）", { type: "email", autocomplete: "username", required: true, value: email });
      const [l2, i2] = field("パスワード", { type: "password", autocomplete: "current-password", required: true });
      const go = el("button", { class: "primary", type: "submit" }, "ログイン");
      frame("ログイン", l1, l2, go,
        el("a", { href: "#", class: "login-link", onclick: (e) => { e.preventDefault(); email = i1.value.trim(); forgotView(); } }, "パスワードを忘れた場合"));
      go.addEventListener("click", () => { if (!i1.value || !i2.value) return say("メールアドレスとパスワードを入力してください。"); busy(go, async () => {
        email = i1.value.trim();
        const r = await A.login(email, i2.value);
        if (r.challenge === "new_password") { session = r.session; return newPasswordView(); }
        done();
      }); });
      i1.focus();
    };

    const newPasswordView = () => {
      const [l1, i1] = field("新しいパスワード（12文字以上。英大文字・英小文字・数字を含む）", { type: "password", autocomplete: "new-password", required: true, minlength: 12 });
      const [l2, i2] = field("新しいパスワード（確認）", { type: "password", autocomplete: "new-password", required: true });
      const go = el("button", { class: "primary", type: "submit" }, "パスワードを設定してログイン");
      frame("初回のパスワード設定", el("p", { class: "muted" }, "メールで届いた仮パスワードでログインできました。新しいパスワードを決めてください。"), l1, l2, go);
      go.addEventListener("click", () => { if (i1.value !== i2.value) return say("確認用のパスワードが一致しません。"); busy(go, async () => { await A.setNewPassword(email, session, i1.value); done(); }); });
      i1.focus();
    };

    const forgotView = () => {
      const [l1, i1] = field("メールアドレス（ログインID）", { type: "email", autocomplete: "username", required: true, value: email });
      const go = el("button", { class: "primary", type: "submit" }, "確認コードをメールで送る");
      frame("パスワードの再設定", el("p", { class: "muted" }, "登録のメールアドレスに、確認コード（6桁）を送ります。"), l1, go,
        el("a", { href: "#", class: "login-link", onclick: (e) => { e.preventDefault(); email = i1.value.trim(); loginView(); } }, "ログインに戻る"));
      go.addEventListener("click", () => { if (!i1.value) return say("メールアドレスを入力してください。"); busy(go, async () => { email = i1.value.trim(); await A.forgot(email); confirmView(); }); });
      i1.focus();
    };

    const confirmView = () => {
      const [l1, i1] = field("確認コード（メールに届いた6桁）", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", required: true });
      const [l2, i2] = field("新しいパスワード（12文字以上。英大文字・英小文字・数字を含む）", { type: "password", autocomplete: "new-password", required: true, minlength: 12 });
      const [l3, i3] = field("新しいパスワード（確認）", { type: "password", autocomplete: "new-password", required: true });
      const go = el("button", { class: "primary", type: "submit" }, "パスワードを再設定する");
      frame("確認コードの入力", el("p", { class: "muted" }, "届かない場合は、迷惑メールのフォルダを確認してください。メールが無ければ、少し待ってから、もう一度送ってください。"), l1, l2, l3, go,
        el("a", { href: "#", class: "login-link", onclick: (e) => { e.preventDefault(); forgotView(); } }, "コードを送り直す"));
      go.addEventListener("click", () => { if (i2.value !== i3.value) return say("確認用のパスワードが一致しません。"); busy(go, async () => {
        await A.confirmForgot(email, i1.value.trim(), i2.value);
        loginView(); say("パスワードを再設定しました。新しいパスワードでログインしてください。", true);
      }); });
      i1.focus();
    };

    ({ login: loginView, forgot: forgotView })[start]();
  };

  // 起動。ログイン不要の環境（ローカル）や、ログイン済みなら、すぐ解決する
  A.ready = async function () {
    try { cfg = await (await fetch("/api/auth-config")).json(); } catch { cfg = { enabled: false }; }
    if (!cfg.enabled) return;
    tok = store.load();
    if (tok && tok.exp - 60000 > Date.now()) return;
    if (tok && tok.refresh) { try { await refresh(); return; } catch { tok = null; store.save(null); } }
    await new Promise((resolve) => { onReady = resolve; A.showLogin(); });
  };

  window.XAuth = A;
})();
