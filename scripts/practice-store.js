/* practice-store.js · 练习数据往哪儿去的唯一入口
   两种运行模式（config.APP_MODE）共用同一套练习脚本，区别只在于「会话、作答、录音」
   这三件事落到哪里：

     server  调 /api/practice/session、/api/recording、/api/practice/finish，
             数据进服务器的 SQLite 和 data/recordings/（要登录）。
     local   调 LocalStore（IndexedDB），全部留在当前这台设备的浏览器里（不登录）。

   三个练习脚本只认这一层门面，不再各自判断模式：以后加第四种练习，
   只要走 PracticeStore，两种模式自动都成立。门面在两种模式下都会被加载
   （base.html 无条件引这个文件），所以不会出现「服务器模式少了个全局变量」这种白屏。

   约定：返回值里的 sessionId 两种模式都是整数 —— 本地模式用的是 IndexedDB 的
   autoIncrement 主键，因此结果页可以继续用 /result/<int:session_id> 这一条路由，
   后端不用为本地模式开一个字符串 id 的分支。 */

const AppMode = (() => {
  // 模式由后端渲染进 <body data-mode="...">（见 base.html），前端不去 fetch /api/me 猜一遍：
  // 少一次请求，也就少了「脚本已经跑了、模式还没确定」的时间窗。
  // 属性缺失时按 server 处理：最坏情况是往 /api/practice/session 提一次、被本地模式闸门挡成
  // 409 报错，数据不会悄悄落到错误的地方。
  const raw = (document.body.dataset.mode || "server").toLowerCase();
  return {
    mode: raw === "local" ? "local" : "server",
    get local() { return this.mode === "local"; },
  };
})();

const PracticeStore = (() => {
  /** 本地模式该不该走 IndexedDB。脚本没加载成功时说清楚原因，
   *  而不是悄悄退回写服务器 —— 那会被 /api/practice/session 的本地模式闸门挡成 409，
   *  用户只看到一句莫名其妙的报错。 */
  function useLocal() {
    if (!AppMode.local) return false;
    if (!window.LocalStore) {
      throw new Error("本地存储脚本没有加载成功（浏览器可能拦住了站点数据），" +
        "这台设备存不了练习记录。请关掉「阻止跟踪/清理站点数据」后刷新。");
    }
    return true;
  }

  /** @returns {Promise<{sessionId:number, local:boolean}>} */
  async function createSession(practiceType, params) {
    if (useLocal()) {
      return { sessionId: await LocalStore.createSession({ practice_type: practiceType, params }), local: true };
    }
    const sess = await Common.api("/api/practice/session", { practice_type: practiceType, params });
    return { sessionId: sess.session_id, local: false };
  }

  /** 保存一题的朗读。服务器模式返回 {path,url}，本地模式返回 {path,url:""}：
   *  本地的 url 是空的，因为 Blob 只能在结果页从 IndexedDB 现取现建 object URL，
   *  提前生成的 URL 一跳转页面就失效了（local-result.js 负责这件事）。 */
  async function saveRecording(blob, ext, meta = {}) {
    if (useLocal()) {
      const path = await LocalStore.putRecording(meta.session_id, meta.seq, blob, ext);
      return { path, url: "", local: true };
    }
    return uploadRecording(blob, ext, meta);
  }

  /** 一次性交整场成绩。本地模式两次写库放在同一层做完，练习脚本那边不用关心。 */
  async function finish(sessionId, records) {
    if (useLocal()) {
      const correct = records.filter(rec => rec.is_correct).length;
      // 先落逐题、再翻会话标记：中间出问题，会话会停在 finished=0，
      // 数据页能看到「这场没记完」，比反过来「标记完成了但一题都没有」好诊断。
      await LocalStore.saveRecords(sessionId, records);
      await LocalStore.finishSession(sessionId, {
        total: records.length, correct, wrong: records.length - correct,
      });
      return { resultUrl: `/result/${sessionId}`, local: true };
    }
    try {
      const res = await Common.api("/api/practice/finish", { session_id: sessionId, records });
      return { resultUrl: res.result_url, local: false };
    } catch (err) {
      // 409 = 这场已经交过卷。多半是上一次其实存好了、只是响应丢了，
      // 成绩以服务器上那份为准，直接去结果页，不当成失败。
      if (err.status === 409 && err.data && err.data.result_url) {
        return { resultUrl: err.data.result_url, local: false };
      }
      throw err;
    }
  }

  /** 近似音判一题，返回 {correct, answer, choice}（choice 是最终算数的那个选项）。
   *  服务器模式下出题时不下发答案，要把选项交给后端换回对错；后端只认每题第一次提交。
   *  本地模式题目里本来就带着答案（成绩只存这台设备，没有防作弊的必要），就地比对。
   *  请求失败时抛错，调用方照常进入下一题，这一题的对错以结果页为准。 */
  async function checkVowelAnswer(sessionId, question, choice) {
    if (useLocal() || question.answer !== undefined) {
      return { correct: choice !== null && choice === question.answer, answer: question.answer, choice };
    }
    const res = await Common.api("/api/vowel/answer",
      { session_id: sessionId, seq: question.seq, choice }, { timeoutMs: 4000 });
    return { correct: !!res.correct, answer: res.answer, choice: res.choice };
  }

  return {
    createSession, saveRecording, finish, checkVowelAnswer,
    get local() { return AppMode.local; }
  };
})();

/** 本地模式的「设置」：值存在这台设备的 localStorage（见 local-store.js）。
 *  职责有两块 —— 把存过的值回填进练习页/设置页的输入框，以及在本地模式下接管设置表单。 */
const LocalSettings = (() => {
  const NUMERIC = [
    // [元素 id 或 name, 设置里的键]
    ["count", "default_count"],
    ["default_count", "default_count"],
    ["time_limit", "time_limit"],
    ["round1_seconds", "round1_seconds"],
    ["round2_seconds", "round2_seconds"],
    ["round_gap", "round_gap"],
  ];
  const SWITCHES = [["overlap_record", "overlap_record"]];

  /** 数字框：拿输入框自己的 min/max 夹一遍（边界由后端按 config 渲染进来），
   *  这样「存过离谱值」和「以后把上限调小」都不会让练习页跑到边界外面。 */
  function clamp(raw, el, fallback) {
    let value = Number(raw);
    if (!Number.isFinite(value)) value = Number(fallback);
    if (!Number.isFinite(value)) return null;
    const lo = Number(el.min), hi = Number(el.max);
    if (Number.isFinite(lo)) value = Math.max(lo, value);
    if (Number.isFinite(hi)) value = Math.min(hi, value);
    return value;
  }

  function fill(settings) {
    NUMERIC.forEach(([key, field]) => {
      const el = document.getElementById(key) ||
        document.querySelector(`[name="${key}"]`);
      if (!el) return;
      const value = clamp(settings[field], el, settings[field]);
      if (value !== null) el.value = value;
    });
    SWITCHES.forEach(([key, field]) => {
      const el = document.getElementById(key);
      if (el && el.type === "checkbox") el.checked = Boolean(settings[field]);
    });
  }

  function collect() {
    // 以默认值为底：这一项在页面上不存在（比如练习页没有 default_count）就保留原来的值
    const out = { ...LocalStore.readSettings() };
    NUMERIC.forEach(([key, field]) => {
      const el = document.getElementById(key) ||
        document.querySelector(`[name="${key}"]`);
      if (!el) return;
      const value = clamp(el.value, el, out[field]);
      if (value !== null) out[field] = value;
    });
    SWITCHES.forEach(([key, field]) => {
      const el = document.getElementById(key);
      if (el && el.type === "checkbox") out[field] = el.checked;
    });
    return out;
  }

  function showSaved(text) {
    let box = document.getElementById("local-saved");
    if (!box) {
      box = document.createElement("div");
      box.id = "local-saved";
      box.className = "alert alert-success py-2";
      const form = document.getElementById("settings-form");
      (form || document.querySelector(".card")).prepend(box);
    }
    box.innerHTML = `<i class="bi bi-check-circle-fill me-2"></i>${text}`;
  }

  /** /settings 在本地模式下不往服务器发任何东西：表单交给这里接。
   *  注意是 preventDefault 而不是把 form 的 action 改掉 —— 万一 JS 没跑起来，
   *  用户按提交只会拿回同一张表单（后端的本地分支不写库），不会误写数据。 */
  function bindForm() {
    const form = document.getElementById("settings-form");
    if (!form || !AppMode.local) return;
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (!LocalStore.settingsAvailable()) {
        Common.toast("这台设备的浏览器不允许保存站点设置（多为隐私模式），" +
          "本次设置的值只能在这一页用，刷新就没了", "warning", 6000);
        return;
      }
      LocalStore.writeSettings(collect());
      showSaved("设置已保存到这台设备 · 只影响本机，不会上传");
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  function start() {
    if (!AppMode.local || !window.LocalStore) return;
    fill(LocalStore.readSettings());
    bindForm();
    if (!LocalStore.settingsAvailable() && document.getElementById("settings-form")) {
      Common.toast("浏览器当前不允许保存站点设置，可能是隐私模式", "warning", 5000);
    }
  }

  return { fill, collect, bindForm, start, NUMERIC, SWITCHES };
})();

window.AppMode = AppMode;
window.PracticeStore = PracticeStore;
window.LocalSettings = LocalSettings;

// 设置回填要在练习脚本之前跑完：vowel.js / tworounds.js 都是等点「开始练习」才读输入框，
// 但设置页的表单绑定越早挂越好。这里同步执行，避免被人手快先点了一下。
LocalSettings.start();
