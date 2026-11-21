/* tworounds.js · 看读译 / 听读写的两轮练习引擎
   两个页面共用本脚本，靠 <body> 上的 data-practice-type 区分行为：

   read-translate（看读译）
     第一轮：屏幕展示单词，同时录音用户朗读
     第二轮：屏幕展示单词，键入中文译文（任一项即可）
   listen-spell（听读写）
     第一轮：默认「边播边录」——麦克风和单词音频同一时刻发起，跟着标准音念；
             每个词录「音频本身的长度 + 设置的第一轮时长」：音频还在放就跟着念，
             音频放完后再走满设置的那几秒，独立把这个词念完整。
             取消勾选就回到以前的顺序：先放完再录，同样是一轮时长。
     第二轮：再播一次音频，限时内键入正确拼写

   单词音频由后端每题随机挑一轨真人朗读（男声 / 女声），前端只看到 word.audio_url
   和 word.voice，把 voice 一起存进作答记录，结果页才播得准「当时听到的那一轨」。

   三段时长——第一轮、第二轮、题目之间的停顿——都从练习设置里的输入框读，
   默认值和 min/max 由 config.py 经 app.py 渲染进输入框，这里只负责按边界夹一遍；
   点「开始练习」时读一次并写进会话 params，中途改框不影响正在跑的这轮。
   「边播边录」这个开关也一起进 params，因为它改变了「我的朗读」里听到的内容。

   结束后把所有作答一次性交给 PracticeStore（服务器模式提交 /api/practice/finish，
   本地模式写本机 IndexedDB），再跳转结果页逐题回看。录音同理走 saveRecording：
   脚本里没有任何一处判断运行模式。 */
(() => {
  const body = document.body;
  const practiceType = body.dataset.practiceType;
  if (practiceType !== "read-translate" && practiceType !== "listen-spell") return;

  const $ = id => document.getElementById(id);

  const CFG = {
    "read-translate": {
      showWordRound1: true,
      showWordRound2: true,
      playAudioRound1: false,
      playAudioRound2: false,
      supportsOverlap: false,
      inputPlaceholder: "输入这个单词的中文译文（任写一项）",
      emptyAnswer: "无回答",
      roundNote: seconds => `展示 ${seconds} 秒，超时未提交记为无回答`,
      judge: judgeChinese
    },
    "listen-spell": {
      showWordRound1: false,
      showWordRound2: false,
      playAudioRound1: true,
      playAudioRound2: true,
      supportsOverlap: true,
      inputPlaceholder: "输入你听到的单词拼写",
      emptyAnswer: "无回答",
      roundNote: seconds => `播放后 ${seconds} 秒内提交，超时记为无回答`,
      judge: (word, answer) => answer.trim().toLowerCase() === word.word.toLowerCase()
    }
  };
  const cfg = CFG[practiceType];

  // 输入框不在的时候（比如别处复用这段脚本）退回 config 里那三个默认值。
  // 这三个数在 config.py 的 ROUND1_SECONDS / ROUND2_SECONDS / ROUND_GAP_SECONDS 里也有，
  // 改默认时长记得两边一起改（1.5 秒就是 2026-11-17 从 1 秒调上来的那次）。
  const FALLBACK_SECONDS = { round1_seconds: 1.5, round2_seconds: 5, round_gap: 0.2 };
  let round1Ms = 1500;
  let round2Ms = 5000;
  let gapMs = 200;
  let overlap = false;         // 听读写第一轮是否边播边录，点「开始练习」时定死

  let sessionId = null;
  let words = [];
  let records = [];
  let recordings = {};        // seq → {path, url}
  let stats = { right: 0, wrong: 0 };
  let recorder = null;
  let micBroken = false;
  let recordingWarned = false;   // 本地模式保存失败只提醒一次，别逐题弹
  let currentWord = null;

  /** 读一个秒数输入框：清空或写非数字都用默认值兜底，出格的值夹到 min/max。 */
  function readSeconds(id) {
    const el = $(id);
    if (!el) return FALLBACK_SECONDS[id];
    const raw = String(el.value).trim();
    const parsed = Number(raw);
    let seconds = raw === "" || !Number.isFinite(parsed) ? Number(el.getAttribute("value")) : parsed;
    if (!Number.isFinite(seconds)) seconds = FALLBACK_SECONDS[id];
    const lo = Number(el.min), hi = Number(el.max);
    if (Number.isFinite(lo)) seconds = Math.max(lo, seconds);
    if (Number.isFinite(hi)) seconds = Math.min(hi, seconds);
    return seconds;
  }

  // ---------------------------------------------------------------- 开始

  $("btn-start").addEventListener("click", async () => {
    // 第一行就做，而且要同步：手机上「用户手势里播过一次」的那条音轨才允许之后被脚本自动播放
    Common.unlockAudio();
    const count = Number($("count").value) || 20;
    const seconds = {
      round1: readSeconds("round1_seconds"),
      round2: readSeconds("round2_seconds"),
      gap: readSeconds("round_gap")
    };
    round1Ms = Math.round(seconds.round1 * 1000);
    round2Ms = Math.round(seconds.round2 * 1000);
    gapMs = Math.round(seconds.gap * 1000);
    // 看读译第一轮不放音频，没有「同时」可言，所以只有听读写认这个开关；
    // 输入框不在（比如别处复用这段脚本）就按以前的「先听后录」走。
    const overlapBox = $("overlap_record");
    overlap = !!(cfg.supportsOverlap && overlapBox && overlapBox.checked);
    const note = $("round2-note");
    if (note && cfg.roundNote) note.textContent = cfg.roundNote(seconds.round2);
    $("btn-start").disabled = true;
    try {
      const params = {
        count,
        round1_ms: round1Ms,
        round2_ms: round2Ms,
        round_gap_ms: gapMs
      };
      if (cfg.supportsOverlap) {
        // 边播边录会把标准音一起录进「我的朗读」，结果页得说清这一轮是怎么录的
        params.overlap_record = overlap;
      }
      const sess = await PracticeStore.createSession(practiceType, params);
      sessionId = sess.sessionId;

      // 带上 session_id：服务器模式下后端把这批词记到会话上，交卷时据此判分
      const data = await Common.api("/api/words", { count, session_id: sessionId });
      words = data.words;
      if (!words.length) throw new Error("词库里没有单词，请先运行 scripts/init_db.py");
      if (data.actual < data.requested) {
        // 以前这里会把用户选的 100 题静默变成 30 题，现在如实说明
        Common.toast(`词库里只有 ${data.bank_size} 个单词，本次按 ${data.actual} 题进行`, "warning", 4200);
      }

      // 浏览器语音列表是异步加载的，不预热的话第一个单词的合成发音完全无声
      await Common.warmUpSpeech();

      if (overlap) {
        // 麦克风提前到「开始练习」这一次点击里申请：权限弹窗只会出现一次，
        // 之后每个词起录只剩几毫秒，标准音的开头不会被 getUserMedia 的延迟吃掉。
        // iOS Safari 也要求 AudioContext 在用户手势中建好，这里一并办掉。
        try {
          await Recorder.prime();
        } catch (err) {
          micBroken = true;
          Common.toast(`无法录音（${err.message}），本次练习只放音频、跳过跟读`, "warning", 5000);
        }
      }

      if (window.WordAudio) {
        WordAudio.setWordList(words);
        await WordAudio.refresh().catch(() => {});
      }
      warnIfNoAudioSource();

      $("setup").classList.add("d-none");
      $("stage").classList.remove("d-none");
      await runRound1();
    } catch (err) {
      Common.toast(err.message, "danger");
      $("btn-start").disabled = false;
      releaseMic();
    }
  });

  // ---------------------------------------------------------------- 第一轮：录音

  async function runRound1() {
    const seconds = secondsOf(round1Ms);
    setStageTitle(overlap
      ? `第一轮 · 边听边跟读（每词 = 标准音 + ${seconds} 秒，共 ${words.length} 个词）`
      : `第一轮 · 朗读（每词 ${seconds} 秒，共 ${words.length} 个词）`);
    setPane("round1");
    // 第一轮不显示作答框，先把它们置为不可用，避免"看不见但仍可输入"的状态
    $("answer-input").disabled = true;
    $("answer-submit").disabled = true;
    for (let i = 0; i < words.length; i++) {
      const word = words[i];
      const seq = i + 1;
      setProgress(seq, words.length, overlap ? "请跟着标准音大声念" : "请大声朗读");
      showWord(word, cfg.showWordRound1);
      setTimerBar(0);

      let blobInfo = null;
      if (overlap) {
        blobInfo = await playAndRecord(word);
      } else {
        if (cfg.playAudioRound1) await playStandard(word);
        blobInfo = await recordFor(round1Ms);
      }
      if (blobInfo) {
        try {
          const saved = await PracticeStore.saveRecording(blobInfo.blob, blobInfo.ext, {
            session_id: sessionId, seq
          });
          recordings[seq] = { path: saved.path, url: saved.url };
        } catch (err) {
          /* 保存录音失败不影响判分，结果页会显示「没有录到音」。
             但两种模式的后果不一样：服务器模式只是这一题没传上去，
             本地模式是这台设备上真的没有了，没有第二份备份。
             所以本地模式第一次失败就当场说清楚，并把后面题目的录音停掉 ——
             存储出问题（多半是配额满）通常是连续的，逐题再录 20 次只是白等。 */
          if (PracticeStore.local && !recordingWarned) {
            recordingWarned = true;
            micBroken = true;
            Common.toast(`本地保存录音失败（${err.message}）：本次练习后续不再录音，` +
              "已有的作答照常判分。请到「我的数据」看存储空间，必要时先导出备份。", "warning", 8000);
          }
        }
      }
      await Common.wait(gapMs);
    }
    await runRound2();
  }

  /** 边播边录：起录和起播在同一拍发起，每词的录音长度 = 标准音本身 + 设置的第一轮时长。
      先把标准音放完（这段时间麦克风一直开着，跟着念），音频结束后再走满填的秒数，
      让他独立把这个词念完整——真人音频普遍 0.5~2 秒，只取「音频与设置值的较大者」会把
      跟读挤到音频尾巴上，念完最后一个音节时录制正好切断。
      录下来的音频里会混进标准音——桌面 Chrome/Edge 的回声消除能压掉大半，戴耳机最干净，
      所以首页建议插耳机。判分不受影响：听读写看的是第二轮键入的拼写。 */
  async function playAndRecord(word) {
    if (micBroken || !Recorder.supported()) {
      // 录不了也不能把标准音省掉，那这一题就什么都听不到了
      if (!micBroken) markMicBroken("麦克风不可用，本次练习跳过录音，只判答题目");
      await playStandard(word);
      await timedBar(round1Ms);
      return null;
    }

    let stream = null;
    try {
      stream = await Recorder.openStream();
    } catch (err) {
      markMicBroken(`无法录音（${err.message}），后续题目跳过录音`);
      await playStandard(word);
      await timedBar(round1Ms);
      return null;
    }

    const tail = secondsOf(round1Ms);
    const rec = new Recorder();
    const micOn = rec.start(stream);          // 不 await：两个动作同一时刻发起
    const play = trackPlayback(playStandard(word));
    setRecordingDot(true);
    try {
      await micOn;
      // 第一段：标准音播放中。麦克风已经在录了，进度条先顶着满格，倒计时从第二段才开始，
      //         这样「你填的秒数」就是音频放完之后实打实留给嘴巴的时间。
      setTimerBar(1);
      if (!play.finished()) {
        $("hint").textContent = `正在放标准音，跟着念…（放完再给你 ${tail} 秒）`;
        // 没有音频或播放失败时这里会 reject：那一段本来就不存在，直接进第二段
        await play.promise.catch(() => {});
      }
      // 第二段：音频放完，再走满用户填的时长
      $("hint").textContent = `标准音已放完，自己再念一遍（${tail} 秒）`;
      await timedBar(round1Ms);
      const result = await rec.stop();
      return result.blob && result.blob.size > 0 ? result : null;
    } catch (err) {
      markMicBroken(`无法录音（${err.message}），后续题目跳过录音`);
      await play.promise.catch(() => {});
      return null;
    } finally {
      setRecordingDot(false);
    }
  }

  /** 包一层播放 Promise，好知道「播完了没有」——边播边录要等这一段放完才开始倒计时 */
  function trackPlayback(promise) {
    let done = false;
    const wrapped = promise.then(
      value => { done = true; return value; },
      err => { done = true; throw err; }
    );
    return { promise: wrapped, finished: () => done };
  }

  function markMicBroken(message) {
    micBroken = true;
    Common.toast(message, "warning");
  }

  function setRecordingDot(on) {
    const dot = $("rec-dot");
    if (!dot) return;
    dot.classList.toggle("d-none", !on);
    dot.classList.toggle("recording", on);
  }

  /** 麦克风整场共用，练习一结束就关掉，别让用户以为还在录 */
  function releaseMic() {
    if (window.Recorder && Recorder.releaseStream) Recorder.releaseStream();
  }

  async function recordFor(ms) {
    if (micBroken || !Recorder.supported()) {
      if (!micBroken) markMicBroken("麦克风不可用，本次练习跳过录音，只判答题目");
      await timedBar(ms);
      return null;
    }
    try {
      recorder = new Recorder();
      await recorder.start();
      setRecordingDot(true);
      await timedBar(ms);
      const result = await recorder.stop();
      return result.blob && result.blob.size > 0 ? result : null;
    } catch (err) {
      markMicBroken(`无法录音（${err.message}），后续题目跳过录音`);
      await timedBar(ms);
      return null;
    } finally {
      setRecordingDot(false);
    }
  }

  // ---------------------------------------------------------------- 第二轮：作答

  async function runRound2() {
    setStageTitle(`第二轮 · 作答（每题 ${round2Ms / 1000} 秒，共 ${words.length} 个词）`);
    setPane("round2");
    for (let i = 0; i < words.length; i++) {
      const word = words[i];
      const seq = i + 1;
      setProgress(seq, words.length, "输入答案");
      showWord(word, cfg.showWordRound2);
      $("answer-input").value = "";
      $("answer-input").placeholder = cfg.inputPlaceholder;
      $("answer-input").disabled = false;
      $("answer-submit").disabled = false;
      $("answer-input").focus();   // 手机上脚本 focus 不会弹键盘，靠下面的 focus 监听兜住

      let submitted = null;
      let done = false;
      let timer = null;
      const finishEarly = () => {
        if (done) return;
        done = true;
        submitted = $("answer-input").value;
        if (timer) timer.stop(true);
      };
      $("answer-submit").onclick = finishEarly;
      $("answer-input").onkeydown = e => { if (e.key === "Enter") finishEarly(); };

      if (cfg.playAudioRound2) await playStandard(word);

      const bar = $("round2-timer");
      timer = Common.startCountdown(round2Ms / 1000, left => {
        const ratio = Math.max(0, Math.min(1, left / (round2Ms / 1000)));
        bar.style.width = `${ratio * 100}%`;
        bar.classList.toggle("urgent", ratio < 0.3);
      });
      if (done) timer.stop(true);   // 用户在播放音频期间就已提交，不再等满整段限时
      await timer.promise;

      const answer = (submitted === null ? "" : submitted).trim();
      const correct = answer !== "" && cfg.judge(word, answer);
      if (correct) stats.right++; else stats.wrong++;
      setProgress(seq, words.length, correct ? "✓ 正确" : "✗ 错误");

      records.push({
        seq,
        type: practiceType,
        question: {
          type: practiceType,
          word: word.word,
          phonetic: word.phonetic,
          meaning_cn: word.meaning_cn,
          example: word.example,
          audio_url: word.audio_url || null,
          // 这条朗读用的是男声还是女声（词库音频每题随机），结果页据此标注；
          // 只有教材 3A 单音频或浏览器本地导入的音频没有性别，后端给 null
          voice: word.voice || null
        },
        user_answer: answer || cfg.emptyAnswer,
        is_correct: correct,
        recording_path: (recordings[seq] || {}).path || null
      });

      // 一题判完就让输入框和提交按钮失去响应：第二轮中间的 250ms 空隙、
      // 以及回跳到第一轮时，看不见的输入框不应该还能被键盘敲进去
      $("answer-submit").onclick = null;
      $("answer-input").onkeydown = null;
      // 手机上还要把键盘收起来：焦点留在已经 disabled 的输入框上，键盘会一直压着下一题
      if (document.activeElement === $("answer-input")) $("answer-input").blur();
      $("answer-input").disabled = true;
      $("answer-submit").disabled = true;
      await Common.wait(gapMs);
    }
    await finish();
  }

  // 这里的判分只管作答当下的「✓ 正确」和计分板，以及本地模式的成绩。
  // 服务器模式的成绩由后端按同一套规则重算（services/grading.py），改规则时两边一起改。
  // 释义有两种写法：教材种子词是「放弃；抛弃」，Quiz3000 那批来自 ECDICT，
  // 带词性前缀和领域标签，如「n. 扫除, 打扫；vt. 掸去」「[计] 黑客」「给…穿衣」。
  // 以前只按标点切、不去前缀，又用双向 includes，结果「n.」「.」「的」都能命中
  // 某个义项（4665 个带前缀的词输一个句点就判对），「我不知道放弃」也因为含「放弃」判对。
  const POS_PREFIX = /(^|[\s；;，,、])(?:n|vt|vi|v|a|adj|adv|ad|pron|prep|num|conj|interj|int|aux|art|abbr|pl|pref)\.\s*/gi;

  /** 去掉作答和义项里不承载意思的部分：省略号、波浪线、空白、标点。 */
  function normalizeSense(text) {
    return text.replace(/…+|\.{2,}|[～~]/g, "")
      .replace(/[\s\p{P}]/gu, "");
  }

  function chineseSenses(meaning) {
    return String(meaning || "")
      .replace(/\[[^\]]*\]/g, " ")           // [计] [口] 这类领域标签
      .replace(/（[^）]*）|\([^)]*\)/g, " ")  // 括号里的补充说明
      .replace(POS_PREFIX, "$1")
      .split(/[；;／/，,、]/)
      .map(normalizeSense)
      .filter(Boolean);
  }

  /** 中文译文「任写一项即可」：与某个义项相同，或只差一点点（「满意的」对「满意」、
   *  「放弃了」对「放弃」）。包含关系要求短的一方至少两个字、且不短于长的一方一半，
   *  这样一个字、一个助词、或者把义项夹在一长句里，都不再算对。 */
  function judgeChinese(word, answer) {
    const text = normalizeSense(String(answer || ""));
    if (!text) return false;
    return chineseSenses(word.meaning_cn).some(sense => {
      if (sense === text) return true;
      const [shorter, longer] = sense.length <= text.length ? [sense, text] : [text, sense];
      return shorter.length >= 2 && longer.includes(shorter) && shorter.length * 2 >= longer.length;
    });
  }

  // ---------------------------------------------------------------- 辅助

  /** 听读写全程不显示拼写，一旦三种发音来源都不可用，用户听到的就是静音 */
  function warnIfNoAudioSource() {
    if (!(cfg.playAudioRound1 || cfg.playAudioRound2)) return;
    const hasServerAudio = words.some(w => w.audio_url);
    const hasLocalAudio = window.WordAudio && WordAudio.known.size > 0;
    if (hasServerAudio || hasLocalAudio || Common.speechReady()) return;
    Common.toast(
      "这台设备上既没有这些单词的音频，浏览器也没有可用的英文语音，听读写会听不到声音。" +
      "请用下方「音频导入」上传单词音频，或换一台装有英文语音的设备。", "danger", 9000);
  }

  async function playStandard(word) {
    try {
      if (window.WordAudio) await WordAudio.play(word);
      else if (word.audio_url) await Common.playUrl(word.audio_url);
      else Common.speak(word.word);
    } catch (err) {
      Common.speak(word.word);
    }
  }

  function showWord(word, visible) {
    currentWord = word;
    if (window.WordAudio && WordAudio.paintBadge) WordAudio.paintBadge();

    const box = $("word-box");
    if (!visible) {
      box.textContent = "🔒 不显示拼写";
      box.classList.add("text-muted");
      $("phonetic-box").textContent = "";
      return;
    }
    box.classList.remove("text-muted");
    box.textContent = word.word;
    $("phonetic-box").textContent = word.phonetic || "";
  }

  function setStageTitle(text) { $("stage-title").textContent = text; }

  function setPane(name) {
    document.querySelectorAll("[data-pane]").forEach(el => {
      el.classList.toggle("d-none", el.dataset.pane !== name);
    });
  }

  function setProgress(seq, total, hint) {
    $("progress").textContent = `第 ${seq} / ${total} 题`;
    $("score").textContent = `对 ${stats.right} · 错 ${stats.wrong}`;
    $("hint").textContent = hint;
  }

  function setTimerBar(ratio) {
    $("round1-bar").style.width = `${ratio * 100}%`;
  }

  /** 毫秒念成人话：0.3 秒不能写成 0.30000000000000004，也不能写成 0.30 */
  function secondsOf(ms) {
    return String(Math.round(ms / 100) / 10);
  }

  /** 触控设备上的两件小事：脚本 focus() 不会弹键盘，而键盘一弹就把输入框顶出屏幕。
      所以「拿到焦点」和「可视区域变小（键盘开合）」两个时机都把正在作答的那一行拉回中间。 */
  function revealAnswer() {
    const el = $("answer-input");
    if (!el || document.activeElement !== el) return;
    const vv = window.visualViewport;
    // 键盘上沿所在的可视高度：Chrome/Android 与 iOS Safari 都会缩小 visualViewport
    const visibleBottom = vv ? vv.height + vv.offsetTop : window.innerHeight;
    const box = el.getBoundingClientRect();
    if (box.bottom > visibleBottom - 16 || box.top < 0) {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  const answerInput = $("answer-input");
  if (answerInput) {
    answerInput.addEventListener("focus", () => setTimeout(revealAnswer, 260));
  }
  if (window.visualViewport) {
    // 键盘弹出/收起都算一次 resize，260ms 是留给键盘动画的时间
    window.visualViewport.addEventListener("resize", () => setTimeout(revealAnswer, 260));
  }

  async function timedBar(ms) {
    const started = Date.now();
    return new Promise(resolve => {
      // 用 setInterval 而不是 requestAnimationFrame：标签页切到后台时 rAF 会完全停摆，
      // 练习就卡在正在录音的那一个单词上。
      const id = setInterval(() => {
        const left = ms - (Date.now() - started);
        setTimerBar(Math.max(0, left / ms));
        if (left <= 0) {
          clearInterval(id);
          resolve();
        }
      }, 50);
    });
  }

  // ---------------------------------------------------------------- 结束

  async function finish() {
    $("stage").classList.add("d-none");
    releaseMic();          // 跳转结果页之前先把录音灯关掉
    // 失败时不退回设置面板（旧记录会和下一场混在一起），见 Common.submitResults
    await Common.submitResults(() => PracticeStore.finish(sessionId, records));
  }

  // 音频导入面板：词库里的词现在都有服务器上的真人朗读了，按「服务器 → 本地 → 合成」
  // 的优先级，这里导入的音频只在某个词还没配音频时才会被播到。
  // 听读写不能把单词拼写画进面板（面板挂在题目下方，写了就等于泄答案），看读译不受影响。
  if (window.WordAudio && $("audio-input")) {
    bindWordAudio(() => currentWord, { revealWord: practiceType !== "listen-spell" });
    Common.getJson("/api/words/all")
      .then(data => WordAudio.setWordList(data.words))
      .catch(() => {});
  }

  setStageTitle("准备开始");
})();
