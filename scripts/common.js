/* common.js · 三个练习页共用的工具
   包含：请求封装、音频播放（整页共用一个 <audio>，移动端要先解锁）、语音合成、
        录音（MediaRecorder + WAV 回退，麦克风整流复用）、轻提示 */

const Common = (() => {
  // 整页共用一个 <audio>：iOS Safari 只放行「在用户手势里播过一次」的那个元素，
  // 每题 new Audio() 的话第二题开始就被当成自动播放直接拒了，手机上等于没声音。
  let sharedAudio = null;

  function ensureAudio() {
    if (!sharedAudio) {
      sharedAudio = new Audio();
      sharedAudio.preload = "auto";
    }
    return sharedAudio;
  }

  /** POST JSON。options.timeoutMs 给了就到点中止请求：练习进行中的请求（逐题判分）
   *  不能在弱网下无限挂着，否则整场练习停在那一题上。 */
  async function api(url, data, options = {}) {
    const controller = options.timeoutMs && window.AbortController ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), options.timeoutMs) : null;
    let res;
    try {
      res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data || {}),
        signal: controller ? controller.signal : undefined
      });
    } catch (err) {
      if (err.name === "AbortError") throw new Error("请求超时");
      throw err;
    } finally {
      if (timer) clearTimeout(timer);
    }
    let json = {};
    try { json = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
    if (!res.ok) {
      const err = new Error(json.error || `请求失败（${res.status}）`);
      err.status = res.status;
      err.data = json;          // 409 之类的拒绝会附带可用信息（如交卷接口的 result_url）
      throw err;
    }
    return json;
  }

  async function getJson(url) {
    const res = await fetch(url);
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(json.error || `请求失败（${res.status}）`);
    return json;
  }

  // 播放看门狗：只听 ended / error 不够。iOS 切到后台会把媒体暂停、回来后不自动续播，
  // 弱网下也可能一直 stalled —— 这两种情况既不 ended 也不 error，await 它的练习就永远停在
  // 「正在播放」。所以元数据到了以后给「时长 + 余量」，元数据一直不来就用上限兜底，
  // 超时按播放失败处理（调用方都有失败分支：近似音提示猜一个，看读译 / 听读写退回合成发音）。
  const PLAY_SLACK_MS = 4000;
  const PLAY_MAX_MS = 30000;
  let playToken = 0;
  let settlePrevious = null;

  function playUrl(url, onended) {
    // 上一段还没放完就被新的一段顶掉：把它的 Promise 收尾（当作没放完），
    // 不然等它的那个调用方会永远挂着。
    if (settlePrevious) { settlePrevious(); settlePrevious = null; }
    return new Promise((resolve, reject) => {
      if (!url) { resolve(false); return; }
      const token = ++playToken;
      const audio = ensureAudio();
      let watchdog = null;
      let done = false;
      const finish = (fn, value) => {
        if (done) return;
        done = true;
        clearTimeout(watchdog);
        if (token === playToken) settlePrevious = null;
        fn(value);
      };
      const arm = ms => {
        clearTimeout(watchdog);
        watchdog = setTimeout(() => {
          // 只有这段还是「当前那段」时才去动共用的 <audio>，免得把下一题的音频停掉
          if (token === playToken) audio.pause();
          finish(reject, new Error("音频播放超时：" + url));
        }, ms);
      };
      settlePrevious = () => finish(resolve, false);

      // 上一题的回调必须先摘掉：换 src 时旧元素的 ended/error 会串到新音频上
      audio.onended = null;
      audio.onerror = null;
      audio.onloadedmetadata = null;
      audio.pause();
      if ("speechSynthesis" in window) speechSynthesis.cancel();   // 别和合成音叠在一起
      audio.muted = false;
      audio.src = url;
      audio.onended = () => {
        if (onended) onended();
        finish(resolve, true);
      };
      audio.onerror = () => finish(reject, new Error("音频播放失败：" + url));
      audio.onloadedmetadata = () => {
        if (Number.isFinite(audio.duration) && audio.duration > 0) {
          arm(audio.duration * 1000 + PLAY_SLACK_MS);
        }
      };
      arm(PLAY_MAX_MS);
      let started;
      try {
        started = audio.play();
      } catch (err) {
        finish(reject, err);
        return;
      }
      if (started && started.catch) started.catch(err => finish(reject, err));
    });
  }

  /** 移动端解锁：Safari 要求音频第一次播放发生在用户手势里，之后同一个元素才允许被脚本唤起。
      练习中每个词都是自动播放，所以在点「开始练习」的那一刻先无声地放一段静音。
      注意不要 play() 完马上 pause()：那样 WebKit 认为「一次都没播起来」，照样不给解锁，
      所以用一段 1 毫秒的静音让它自己放完，muted 保证听不见。 */
  const SILENT_WAV = "data:audio/wav;base64," +
    "UklGRjwAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQgAAAAAAAAAAAAAAA==";

  function unlockAudio() {
    const audio = ensureAudio();
    audio.onended = null;
    audio.onerror = null;
    audio.muted = true;
    audio.src = SILENT_WAV;
    let started;
    try {
      started = audio.play();
    } catch (err) {
      return false;
    }
    if (started && started.catch) started.catch(() => {});   // 桌面浏览器一般直接成功
    return true;
  }

  function stopAudio() {
    if (sharedAudio) {
      sharedAudio.pause();
      sharedAudio.currentTime = 0;
    }
    if ("speechSynthesis" in window) speechSynthesis.cancel();
  }

  function speak(text, lang = "en-US", rate = 0.9) {
    if (!("speechSynthesis" in window)) return;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = lang;
    u.rate = rate;
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  }

  /** 语音列表是异步加载的：浏览器首次 getVoices() 往往返回空数组，
      此时直接 speak() 完全无声。练习开始前先调本方法预热一次。 */
  function warmUpSpeech(timeoutMs = 800) {
    if (!("speechSynthesis" in window)) return Promise.resolve(false);
    if (speechSynthesis.getVoices().length) return Promise.resolve(true);
    return new Promise(resolve => {
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        speechSynthesis.removeEventListener("voiceschanged", done);
        resolve(speechSynthesis.getVoices().length > 0);
      };
      speechSynthesis.addEventListener("voiceschanged", done);
      setTimeout(done, timeoutMs);
    });
  }

  /** 这台设备到底能不能合成英文发音（没有语音包时听读写等于出静音题） */
  function speechReady() {
    if (!("speechSynthesis" in window)) return false;
    return speechSynthesis.getVoices().some(v => /^en/i.test(v.lang || ""));
  }

  function toast(message, variant = "secondary", ms = 2600) {
    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.className = "position-fixed top-0 start-50 translate-middle-x p-3";
      host.style.zIndex = "1090";
      document.body.appendChild(host);
    }
    const box = document.createElement("div");
    box.className = `alert alert-${variant} shadow py-2 px-3`;
    box.textContent = message;
    host.appendChild(box);
    setTimeout(() => box.remove(), ms);
  }

  function wait(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function fmtTime(seconds) {
    return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
  }

  /** 倒计时。返回 {promise, stop}：promise 在限时结束时 resolve(false)，
      调用 stop(true) 表示用户已作答并提前结束。onTick 每 100ms 收到剩余秒数。 */
  function startCountdown(seconds, onTick) {
    const deadline = Date.now() + seconds * 1000;
    let settled = false;
    let resolveFn;
    const promise = new Promise(resolve => { resolveFn = resolve; });
    const stop = answered => {
      if (settled) return;
      settled = true;
      clearInterval(timer);
      resolveFn(Boolean(answered));
    };
    const timer = setInterval(() => {
      const left = (deadline - Date.now()) / 1000;
      if (onTick) onTick(Math.max(0, left));
      if (left <= 0) stop(false);
    }, 100);
    return { promise, stop };
  }

  /** 交卷：成功就跳结果页；失败时留在「正在保存」那块，给「重新保存」和「放弃这场」。
   *
   *  以前失败后退回设置面板，可内存里这一场的作答记录、题目游标都还在，再点「开始练习」
   *  会和新一场混在一起：近似音直接拿旧记录交卷或跳题，看读译 / 听读写记录翻倍、题号重复，
   *  超过 100 题后服务器回 400，永远交不上。现在只留两条路：原样重交，或者整页重来。
   *  @param {() => Promise<{resultUrl: string}>} save 真正去保存的那一步，重试时再调一次 */
  async function submitResults(save) {
    const box = document.getElementById("finishing");
    const original = box ? box.innerHTML : "";
    for (;;) {
      if (box) { box.innerHTML = original; box.classList.remove("d-none"); }
      try {
        const res = await save();
        location.href = res.resultUrl;
        return;
      } catch (err) {
        if (!box) { toast(`保存失败：${err.message}`, "danger"); return; }
        const retry = await askRetry(box, err.message);
        if (!retry) { location.reload(); return; }
      }
    }
  }

  function askRetry(box, message) {
    return new Promise(resolve => {
      box.innerHTML = "";
      const text = document.createElement("div");
      text.className = "alert alert-danger text-start";
      text.textContent = `保存失败：${message}。作答记录还在这个页面里，网络恢复后点「重新保存」；` +
        "放弃的话这一场不会留下记录。";
      const again = document.createElement("button");
      again.className = "btn btn-primary me-2";
      again.textContent = "重新保存";
      again.addEventListener("click", () => resolve(true));
      const drop = document.createElement("button");
      drop.className = "btn btn-outline-secondary";
      drop.textContent = "放弃这场";
      drop.addEventListener("click", () => {
        if (confirm("放弃后这一场的作答都不会保存，确定吗？")) resolve(false);
      });
      box.append(text, again, drop);
    });
  }

  return {
    api, getJson, playUrl, stopAudio, unlockAudio, speak, warmUpSpeech, speechReady,
    toast, wait, fmtTime, startCountdown, submitResults
  };
})();

/** 录音：优先 MediaRecorder，全部编码都不支持时（部分 iOS Safari）用 Web Audio 自己编码 WAV
 *
 *  麦克风是整场练习共用的：每次 getUserMedia() 要 0.1~0.5 秒，逐词重新申请会有两个后果——
 *  「边播边录」时标准音的第一个音节还没进缓冲区就断了，手机浏览器则会反复弹权限。
 *  所以先 Recorder.prime() 拿到一条流（放在用户点击的回调里，权限弹窗只出现一次），
 *  每个词再 Recorder.openStream() 复用同一条流，练习结束 Recorder.releaseStream() 关灯。 */
class Recorder {
  constructor() {
    this.stream = null;
    this.mediaRecorder = null;
    this.chunks = [];
    this.mode = null;          // 'mediarecorder' | 'wav'
    this.audioContext = null;
    this.processor = null;
    this.wavSource = null;
    this.samples = [];
    this.sampleRate = 44100;
    this.ownsStream = true;    // 自建流要在 stop() 里释放，共享流不能动，否则下一题没麦克风
  }

  static mimeTypeCandidates() {
    return [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/mp4",
      "audio/mpeg"
    ];
  }

  static pickMime() {
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
    return Recorder.mimeTypeCandidates().find(m => MediaRecorder.isTypeSupported(m)) || "";
  }

  static supported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  /** 这台设备到底会走哪条录音路径，供帮助文案与调试使用 */
  static mode() {
    if (!Recorder.supported()) return "none";
    return Recorder.pickMime() ? "mediarecorder" : "wav";
  }

  /** 取整场练习共用的麦克风流；已经没了（用户中途在地址栏关掉权限）就重新申请。 */
  static async openStream() {
    const alive = Recorder._sharedStream &&
      Recorder._sharedStream.getAudioTracks().some(t => t.readyState === "live");
    if (alive) return Recorder._sharedStream;
    Recorder.releaseStream();
    Recorder._sharedStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    return Recorder._sharedStream;
  }

  /** 播放 + 录音并发时，麦克风和 AudioContext 都得在用户手势里先建好，
      否则 iOS Safari 会因为「不在手势上下文中」直接拒绝。 */
  static async prime() {
    const stream = await Recorder.openStream();
    if (!Recorder.pickMime()) await Recorder.sharedContext();
    return stream;
  }

  /** Web Audio 兜底用的上下文同样复用：iOS 上 AudioContext 有数量上限，逐词新建会失败 */
  static async sharedContext() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Recorder._sharedContext) Recorder._sharedContext = new Ctx();
    if (Recorder._sharedContext.state === "suspended") {
      await Recorder._sharedContext.resume();
    }
    return Recorder._sharedContext;
  }

  /** 释放麦克风（浏览器那个录音红点）和 Web Audio 上下文 */
  static releaseStream() {
    if (Recorder._sharedStream) {
      Recorder._sharedStream.getTracks().forEach(t => t.stop());
      Recorder._sharedStream = null;
    }
    if (Recorder._sharedContext) {
      Recorder._sharedContext.close();
      Recorder._sharedContext = null;
    }
  }

  /** @param {MediaStream} [stream] 传共享流就只负责录制，不拥有它、结束时也不关掉 */
  async start(stream) {
    this.ownsStream = !stream;
    this.stream = stream || await navigator.mediaDevices.getUserMedia({ audio: true });
    try {
      const mime = Recorder.pickMime();
      if (mime) {
        this.mode = "mediarecorder";
        this.mediaRecorder = new MediaRecorder(this.stream, { mimeType: mime });
        this.chunks = [];
        this.mediaRecorder.ondataavailable = e => {
          if (e.data && e.data.size > 0) this.chunks.push(e.data);
        };
        this.mediaRecorder.start();
      } else {
        this.mode = "wav";
        await this._startWavCapture();
      }
    } catch (err) {
      // 麦克风已经拿到、录音器却没起来：自己申请的那条流要关掉，否则录音红点一直亮到离开页面
      if (this.ownsStream && this.stream) this.stream.getTracks().forEach(t => t.stop());
      this.stream = null;
      this.mode = null;
      throw err;
    }
    return this.mode;
  }

  async _startWavCapture() {
    this.audioContext = await Recorder.sharedContext();
    this.sampleRate = this.audioContext.sampleRate;
    this.wavSource = this.audioContext.createMediaStreamSource(this.stream);
    this.processor = this.audioContext.createScriptProcessor(4096, 1, 1);
    this.samples = [];
    this.processor.onaudioprocess = e => {
      this.samples.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    };
    this.wavSource.connect(this.processor);
    this.processor.connect(this.audioContext.destination);
  }

  stop() {
    return new Promise(resolve => {
      const release = () => {
        // 共享流留给下一题用，只有这次自己申请的才关掉；
        // AudioContext 同理是复用的，close() 交给 releaseStream()
        if (this.ownsStream && this.stream) this.stream.getTracks().forEach(t => t.stop());
        if (this.wavSource) { this.wavSource.disconnect(); this.wavSource = null; }
      };
      if (this.mode === "mediarecorder") {
        let done = false;
        let fallback = null;       // 先声明：下面「已经 inactive」那条路会在设定时器之前就 deliver()
        const deliver = () => {
          if (done) return;
          done = true;
          clearTimeout(fallback);
          const type = this.mediaRecorder.mimeType || "audio/webm";
          const blob = this.chunks.length ? new Blob(this.chunks, { type }) : null;
          release();
          resolve({ blob, ext: blob ? Recorder.extOf(type) : null });
        };
        // 录音器可能已经自己停了：来电、别的 App 抢麦、蓝牙耳机断开、在地址栏收回权限，
        // 麦克风轨道一结束 MediaRecorder 就转成 inactive 并且已经触发过 stop。
        // 这时再调 stop() 按规范是静默返回、不会再来一次 onstop，以前的写法就永远等不到。
        if (this.mediaRecorder.state === "inactive") { deliver(); return; }
        this.mediaRecorder.onstop = deliver;
        // 再兜一层：个别浏览器 stop 之后迟迟不派发 onstop，2 秒后用已收到的数据交差
        fallback = setTimeout(deliver, 2000);
        try {
          this.mediaRecorder.stop();
        } catch (err) {
          deliver();
        }
      } else if (this.mode === "wav") {
        this.processor.disconnect();
        const blob = Recorder.encodeWav(this.samples, this.sampleRate);
        release();
        resolve({ blob, ext: "wav" });
      } else {
        resolve({ blob: null, ext: null });
      }
    });
  }

  static extOf(mime) {
    if (mime.includes("webm")) return "webm";
    if (mime.includes("ogg")) return "ogg";
    if (mime.includes("mp4") || mime.includes("m4a")) return "m4a";
    if (mime.includes("mpeg") || mime.includes("mp3")) return "mp3";
    return "webm";
  }

  /** Float32 采样 → 16 位 PCM WAV，作为 MediaRecorder 不可用时的兜底 */
  static encodeWav(channelData, sampleRate) {
    const length = channelData.reduce((sum, arr) => sum + arr.length, 0);
    const buffer = new ArrayBuffer(44 + length * 2);
    const view = new DataView(buffer);
    const writeString = (offset, str) => {
      for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
    };

    writeString(0, "RIFF");
    view.setUint32(4, 36 + length * 2, true);
    writeString(8, "WAVE");
    writeString(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);          // PCM
    view.setUint16(22, 1, true);          // 单声道
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeString(36, "data");
    view.setUint32(40, length * 2, true);

    let offset = 44;
    for (const chunk of channelData) {
      for (let i = 0; i < chunk.length; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
        offset += 2;
      }
    }
    return new Blob([view], { type: "audio/wav" });
  }
}

// 共享的麦克风流与 AudioContext。写成普通赋值而不是 static 字段：
// static x = 1 这种类字段要 Safari 14.1 之后才认识，而手机端的用户可能还停在更早的版本。
Recorder._sharedStream = null;
Recorder._sharedContext = null;

/** 把录音 Blob 上传到后端，返回 {path, url} */
async function uploadRecording(blob, ext, meta = {}) {
  const form = new FormData();
  form.append("audio", blob, `recording.${ext || "webm"}`);
  Object.entries(meta).forEach(([k, v]) => form.append(k, v));
  const res = await fetch("/api/recording", { method: "POST", body: form });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.error || "录音上传失败");
  return json;
}
