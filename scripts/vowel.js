/* vowel.js · 近似音区分练习
   流程按设计文档：屏幕标出两个选项 1/2 → 随机播放其中一个 → 限时内点卡片（或按键盘 1/2）
   作答 → 立刻给出对错并进入下一题；结束后一次性提交全部作答，跳转到结果页逐题回看。
   单词练习播的是 3B 的单词音频，句子练习播的是 4A 的整句，选项只有句中变化的那两个词。
   作答入口有两个：卡片本身是按钮，手机上点一下就算选；键盘 1/2 留给用电脑的人。
   成绩落库走 PracticeStore：服务器模式提交给后端，本地模式写本机 IndexedDB，
   这个脚本本身不判断模式（模式判断集中在 practice-store.js 那一层）。 */
(() => {
  const $ = id => document.getElementById(id);

  const VOICE_LABEL = { f: "女声", m: "男声" };
  const MODE_HINT_DEFAULT = "句子练习放的是含该词的整句，屏幕上仍然只给句中变化的那两个词。";
  const esc = text => String(text).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

  let sessionId = null;
  let questions = [];
  let cursor = 0;
  let records = [];
  let stats = { right: 0, wrong: 0 };
  let timeLimit = 1;
  let timer = null;
  let picked = null;
  let answered = false;
  let running = false;
  let mode = "word";

  function readParams() {
    return {
      group_key: $("group_key").value,
      count: Number($("count").value) || 10,
      mode: (document.querySelector('input[name="mode"]:checked') || {}).value || "word",
      // 性别不再由用户选：不传 voice，后端按每题随机抽男声或女声
      time_limit: Number($("time_limit").value) || 1
    };
  }

  // 句子音频只覆盖了部分近似音组，选到没有句子的组时提前说清楚，别等点开始才报错
  function refreshModeHint() {
    const box = $("mode-hint");
    if (!box) return;
    const opt = $("group_key").selectedOptions[0];
    const params = readParams();
    if (params.mode !== "sentence") {
      box.className = "form-text";
      box.textContent = MODE_HINT_DEFAULT;
      return;
    }
    const sentencePairs = Number((opt && opt.dataset.sentencePairs) || 0);
    const wordPairs = Number((opt && opt.dataset.wordPairs) || 0);
    if (!sentencePairs) {
      box.className = "form-text text-danger";
      box.textContent = `${opt.value} 这组还没有句子音频（单词题有 ${wordPairs} 组），` +
        "先换一组，或跑 scripts/import_audio_package.py --only sentence 补。";
    } else {
      box.className = "form-text";
      box.textContent = `${opt.value} 有 ${sentencePairs} 组句子音频，` +
        "每题播放整句，屏幕上只给句中变化的那两个词。";
    }
  }

  $("btn-start").addEventListener("click", async () => {
    // 同步执行，还在用户手势里：手机上先把共用的那条音轨解锁，后面每题的自动播放才被放行
    Common.unlockAudio();
    const params = readParams();
    $("btn-start").disabled = true;
    try {
      // 会话落在哪儿由 PracticeStore 决定：服务器模式写库，本地模式写 IndexedDB
      const sess = await PracticeStore.createSession("vowel", params);
      sessionId = sess.sessionId;

      // 带上 session_id：服务器模式下后端把这批题和标准答案记到会话上，交卷时据此判分
      const data = await Common.api("/api/vowel/questions", { ...params, session_id: sessionId });
      questions = data.questions;
      mode = data.mode || params.mode;
      timeLimit = params.time_limit;
      if (!questions.length) throw new Error("没有抽到题目");

      $("setup").classList.add("d-none");
      $("stage").classList.remove("d-none");
      running = true;
      document.addEventListener("keydown", onKey);
      nextQuestion();
    } catch (err) {
      Common.toast(err.message, "danger");
      $("btn-start").disabled = false;
    }
  });

  ["change"].forEach(evt => {
    $("group_key").addEventListener(evt, refreshModeHint);
    document.querySelectorAll('input[name="mode"]').forEach(radio => radio.addEventListener(evt, refreshModeHint));
  });
  refreshModeHint();

  // 手机上「刚那声到底读的哪个」不容易自查，给一个重播按钮。
  // 只在倒计时开始后能按：正在播第一遍时按下去会抢掉同一个 <audio>，
  // 第一遍的 onended 就被顶掉了，那一题会永远卡在「播放中」；判完成绩也禁用，
  // 免得 700ms 的空档里按出下一题的声音泄题。
  const replay = $("btn-replay");

  function setReplayEnabled(on) {
    if (replay) replay.disabled = !on;
  }

  if (replay) {
    replay.addEventListener("click", async () => {
      const question = questions[cursor];
      if (!question) return;
      setReplayEnabled(false);
      try {
        await Common.playUrl(question.audio_url);
      } catch (err) {
        Common.toast("这题没有可播放的音频", "warning");
      } finally {
        if (running && !answered) setReplayEnabled(true);
      }
    });
  }

  function onKey(e) {
    if (!running || answered) return;
    if (e.key === "1" || e.key === "2") {
      e.preventDefault();
      choose(Number(e.key));
    }
  }

  /** 键盘和点卡片共用这一个入口：倒计时已开始就立刻结束，还在播音频时先记下选择 */
  function choose(index) {
    if (!running || answered) return;
    picked = index;
    if (timer) timer.stop(true);
  }

  function resetTimerUi() {
    const fill = $("timer-fill");
    fill.style.width = "100%";
    fill.classList.remove("urgent");
    $("timer-text").textContent = "播放音频中…";
  }

  /** 选项卡是 <button>：手机上点一下就作答，也顺便能被键盘 Tab 到（Tab 后再按 1/2 或直接回车） */
  function renderOptions(question) {
    const box = $("options");
    box.innerHTML = question.options.map(opt => `
      <button type="button" class="option-chip" data-index="${opt.index}"
              aria-label="${opt.index} 号：${esc(opt.word)}">
        <span class="num">${opt.index}</span>
        <span class="txt">${esc(opt.word)}</span>
      </button>`).join("");
    box.querySelectorAll(".option-chip").forEach(chip => {
      chip.addEventListener("click", () => choose(Number(chip.dataset.index)));
    });
  }

  /** 已经判完的这题不能再点：把卡片置成 disabled，避免手指连点把下一题的选择带跑 */
  function lockOptions() {
    document.querySelectorAll(".option-chip").forEach(chip => { chip.disabled = true; });
  }

  async function nextQuestion() {
    if (cursor >= questions.length) return finish();

    const question = questions[cursor];
    picked = null;
    answered = false;
    timer = null;

    $("progress").textContent = `第 ${cursor + 1} / ${questions.length} 题`;
    $("score").textContent = `对 ${stats.right} · 错 ${stats.wrong}`;
    $("feedback").innerHTML = "";
    renderOptions(question);
    $("hint").innerHTML = `正在播放（${VOICE_LABEL[question.voice] || "随机音色"}）…` +
      "请点下面的卡片作答";
    resetTimerUi();
    setReplayEnabled(false);

    // 先播完音频再开始限时：单词录音普遍 1.0~1.4 秒，
    // 和倒计时同时起跑的话默认 1 秒限时会在听完之前判成「无回答」。
    try {
      await Common.playUrl(question.audio_url);
    } catch (err) {
      $("hint").textContent = "音频播放失败，可以点下面的卡片猜一个";
    }
    setReplayEnabled(true);

    // 播放期间就已经按下编号（听得快），判完立刻进入下一题
    if (picked !== null) {
      answered = true;
      grade(question, picked);
      return;
    }

    timer = Common.startCountdown(timeLimit, left => {
      const ratio = Math.max(0, Math.min(1, left / timeLimit));
      $("timer-fill").style.width = `${ratio * 100}%`;
      $("timer-fill").classList.toggle("urgent", ratio < 0.3);
      $("timer-text").textContent = `剩余 ${left.toFixed(1)} 秒`;
    });

    const timedOut = !(await timer.promise);
    if (answered) return;      // 避免与提前作答重复判分
    answered = true;
    grade(question, timedOut ? null : picked);
  }

  function grade(question, index) {
    const correct = index !== null && index === question.answer;
    if (correct) stats.right++; else stats.wrong++;

    document.querySelectorAll(".option-chip").forEach(chip => {
      const chipIndex = Number(chip.dataset.index);
      if (chipIndex === question.answer) chip.classList.add("is-answer");
      if (chipIndex === index) chip.classList.add("picked");
    });
    lockOptions();

    const played = question.options[question.answer - 1];
    // 整句要等答完才给：作答时看到句子就等于白送答案
    const sentence = played.sentence
      ? `<div class="small text-muted mt-1">原句：${esc(played.sentence)}</div>` : "";

    if (index === null) {
      $("feedback").innerHTML =
        `<div class="result-wrong"><i class="bi bi-hourglass-split me-2"></i>无回答` +
        ` —— 正确的是 <b>${question.answer}</b>（${esc(played.word)}）</div>` + sentence;
    } else if (correct) {
      $("feedback").innerHTML =
        `<div class="result-correct"><i class="bi bi-check-circle-fill me-2"></i>答对了` +
        ` —— 播放的是 <b>${esc(played.word)}</b></div>` + sentence;
    } else {
      $("feedback").innerHTML =
        `<div class="result-wrong"><i class="bi bi-x-circle-fill me-2"></i>答错了` +
        ` —— 你选了 ${index}，播放的是 <b>${esc(played.word)}</b></div>` + sentence;
    }

    $("hint").textContent = "即将进入下一题…";
    setReplayEnabled(false);

    records.push({
      seq: question.seq,
      type: "vowel",
      question: {
        type: "vowel",
        mode: mode,
        group_key: question.group_key,
        options: question.options,
        answer: question.answer,
        voice: question.voice,
        audio_url: question.audio_url
      },
      user_answer: index,
      is_correct: correct
    });

    cursor++;
    setTimeout(nextQuestion, 700);
  }

  async function finish() {
    running = false;
    document.removeEventListener("keydown", onKey);
    $("stage").classList.add("d-none");
    // 失败时不退回设置面板（旧记录会和下一场混在一起），见 Common.submitResults
    await Common.submitResults(() => PracticeStore.finish(sessionId, records));
  }
})();
