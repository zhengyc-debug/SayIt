/* SayIt 通话页
 * 职责：拿麦克风 → 20ms 一包发给服务端；把服务端回来的音频排进播放队列。
 * 打断：服务端说 user_speech_started 时，立刻清空播放队列。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  // 界面语言（母语）：只影响文案与复盘解释，不影响练习语言
  const T = (s, v) => (window.I18N ? I18N.t(s, v) : s);
  const UI_EN = () => !!(window.I18N && I18N.lang === 'en');
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const el = {
    status: $('status'), captions: $('captions'), avatar: $('avatar'),
    wrap: $('avatar-wrap'), mute: $('btn-mute'), call: $('btn-call'), timer: $('timer'),
    whoName: $('who-name'), whoSub: $('who-sub'), idle: $('idle'), langHost: $('lang-host'),
    toast: $('toast'), rowScen: $('row-scen'), rowMeta: $('row-meta'), recap: $('recap'),
    rowSpeed: $('row-speed'), theme: $('btn-theme'), wordModal: $('word-modal'),
    rpModal: $('report-modal'), rpTitle: $('rp-title'), rpSub: $('rp-sub'),
    rpBody: $('rp-body'), rpClose: $('rp-close'), rpAgain: $('rp-again'),
  };

  const S = {
    boot: null, ws: null, inCall: false,
    pick: { language: 'en', scenario: 'free_talk', level: 'B1' },
    capCtx: null, capStream: null, capNode: null, capSource: null,
    playCtx: null, playGain: null, playHead: 0, sources: new Set(),
    muted: false, t0: 0, tick: null, sessionId: null, limitMs: 0, warned: false,
    curUser: null, curAsst: null, pendingAudio: [],
    // 语速：档位名（slowest/slow/normal/fast），没有值时用设置里的默认
    speed: 'slow',
    // 回放：把 AI 这一轮的 PCM 攒起来，`response.done` 时挂到字幕气泡上
    rate: 24000, turnPcm: [], turnText: '', replay: null,
    // 续聊：/?continue=<session_id> 进来时带着这一通，接通前先拿它的摘要
    resume: null, resumeFor: null,
  };

  // ---------------------------------------------------------------- 工具
  function toast(msg, isErr = false, ms = 6000) {
    el.toast.textContent = msg;
    el.toast.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.toast.className = 'toast'; }, ms);
  }
  const b64ToBytes = (b64) => {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  };
  const fmtTime = (ms) => {
    const s = Math.floor(ms / 1000);
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  };

  // ---------------------------------------------------------------- 启动
  async function boot() {
    S.boot = await (await fetch('/api/bootstrap')).json();
    const a = S.boot.active;

    // 默认值：设置里的 → 被 URL 参数覆盖（"再练一次"从记录页跳过来）
    S.pick = { language: a.language, scenario: a.scenario, level: a.level };
    const q = new URLSearchParams(location.search);
    for (const k of ['language', 'scenario', 'level']) {
      const v = q.get(k);
      if (v) S.pick[k] = v;
    }
    S.speed = (a.ui && a.ui.speech_speed) || (S.boot.ui && S.boot.ui.speech_speed) || 'slow';
    if (el.langHost && window.I18N) I18N.mountSwitch(el.langHost);
    renderChips();
    loadRecap();
    // 从「通话记录」点「继续这通电话」过来：先把这个状态摆出来，
    // 用户一眼看到「会接着上次聊」，而不是一个空白的通话页
    await setupResume(q.get('continue'));
    if (window.I18N) {
      // 切了母语：场景名要跟着变，复盘也要按新语言重新出
      I18N.onChange(() => { renderChips(); renderRecap(); S.reportLang = I18N.lang; });
    }
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  }

  function setChipsEnabled(on) {
    document.querySelectorAll('.chip').forEach(c => { c.disabled = !on; });
  }

  function currentLang() {
    return S.boot.languages.find(l => l.code === S.pick.language) || S.boot.languages[0];
  }

  function scenLabel(s) {
    if (!s) return '';
    return UI_EN() ? (s.label || s.label_zh) : (s.label_zh || s.label);
  }

  function chip(label, on, onClick, extra) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chip' + (extra ? ' ' + extra : '') + (on ? ' on' : '');
    b.textContent = label;
    b.onclick = () => { if (!S.inCall) onClick(); };
    if (S.inCall) b.disabled = true;
    return b;
  }

  function divider() {
    const s = document.createElement('span');
    s.style.cssText = 'flex:0 0 auto;width:1px;height:16px;background:rgba(255,255,255,.13);align-self:center;margin:0 3px';
    return s;
  }

  function renderChips() {
    // 场景一排
    el.rowScen.innerHTML = '';
    for (const s of S.boot.scenarios) {
      el.rowScen.appendChild(chip(`${s.icon} ${scenLabel(s)}`, s.id === S.pick.scenario, () => {
        S.pick.scenario = s.id;
        renderChips();
      }));
    }
    // 语言 + 等级一排
    el.rowMeta.innerHTML = '';
    for (const l of S.boot.languages) {
      el.rowMeta.appendChild(chip(l.label, l.code === S.pick.language, () => {
        S.pick.language = l.code;
        S.pick.level = l.default_level || 'B1';
        renderChips();
      }));
    }
    el.rowMeta.appendChild(divider());
    for (const v of (currentLang().levels || [])) {
      el.rowMeta.appendChild(chip(v, v === S.pick.level, () => {
        S.pick.level = v;
        renderChips();
      }, 'lv'));
    }
    renderSpeed();
    renderWho();
  }

  // 语速：单独一行，放在「语言」下面。通话中也可改 —— 改了立刻重连会话，
  // 因为 speed 是建会话时的参数（服务端没有「运行时调速」事件）。
  function renderSpeed() {
    if (!el.rowSpeed) return;
    const list = (S.boot && S.boot.speech_speeds) || [
      { id: 'slowest', label: '最慢', label_en: 'Slowest' },
      { id: 'slow', label: '慢', label_en: 'Slow' },
      { id: 'normal', label: '正常', label_en: 'Normal' },
      { id: 'fast', label: '快', label_en: 'Fast' },
    ];
    el.rowSpeed.innerHTML = '';
    for (const sp of list) {
      const on = sp.id === S.speed;
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip' + (on ? ' on' : '');
      b.textContent = UI_EN() ? sp.label_en : sp.label;
      b.title = T('AI 的说话速度（整通电话固定）');
      b.onclick = () => pickSpeed(sp.id);
      el.rowSpeed.appendChild(b);
    }
  }

  function pickSpeed(id) {
    if (id === S.speed) return;
    S.speed = id;
    renderSpeed();
    // 记住选择：下次通话、下次打开页面都还是这一档
    fetch('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ui: { speech_speed: id } }),
    }).catch(() => {});
    if (S.inCall) {
      // 建会话时就定死了语速 → 只能重连。对用户表现为「按当前进度接着聊」。
      toast(T('正在切换语速…'), false, 2200);
      reconnect();
    } else {
      toast(T('语速已设为 {v}', { v: speedLabel(id) }), false, 1600);
    }
  }

  function speedLabel(id) {
    const list = (S.boot && S.boot.speech_speeds) || [];
    const r = list.find(x => x.id === id);
    if (!r) return id;
    return UI_EN() ? r.label_en : r.label;
  }

  // 通话对象：完整名字 + 场景/语言/等级，都跟着选择器走
  function aiName() {
    const a = S.boot.active || {};
    const byLang = (S.boot.names || {})[S.pick.language];
    return (a.assistant_name || byLang || 'Emma').trim();
  }

  function renderWho() {
    const lang = currentLang();
    const sc = (S.boot.scenarios || []).find(s => s.id === S.pick.scenario) || {};
    const name = aiName();
    el.avatar.textContent = name.slice(0, 1).toUpperCase();
    el.whoName.textContent = name;
    el.whoSub.textContent = [scenLabel(sc), lang.label, S.pick.level].filter(Boolean).join(' · ');
  }

  // ---------------------------------------------------------------- 续聊
  /* 从「通话记录 → 继续这通电话」进来：
       · 先把 context（场景/语言/等级）对齐到那一通，并按更新时间轴显示
       · 预取一份摘要，顺带把「模型总结」那 2~5 秒的等待遮掉
       · 摘要只用来看一眼 + 失败兜底，真正的上下文注入发生在服务端 /ws/call
     不自动接通 —— 麦克风必须在用户手势里申请，iOS 尤其。 */
  async function setupResume(id) {
    if (!id) return;
    try {
      const d = await (await fetch('/api/sessions/' + id)).json();
      if (!d || d.id !== id) throw new Error(d.detail || 'not found');
      S.resume = { id, scenario: d.scenario, language: d.language, level: d.level };
      S.resumeGreeting = true;
      if (d.scenario) S.pick.scenario = d.scenario;
      if (d.language && S.boot.languages.some(l => l.code === d.language)) S.pick.language = d.language;
      if (d.level) S.pick.level = d.level;
      renderChips();

      const when = new Date(d.started_at * 1000).toLocaleString(
        UI_EN() ? 'en-US' : 'zh-CN',
        { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
      const sc = (S.boot.scenarios || []).find(s => s.id === d.scenario) || {};
      el.whoSub.textContent = `${T('接着 {when} 那通聊', { when })} · ${scenLabel(sc)} · ${S.pick.level}`;
      setStatus(`<span class="resume-dot">⤵</span> ${T('点击下方按钮，接着上次聊')}`);

      const btn = el.call;
      btn.classList.add('resume');
      btn.title = T('继续上次没聊完的话题');
      btn.querySelector('.lbl').textContent = T('继续通话');

      // 预取摘要：失败也不拦着接通（服务端还会自己再算一次）
      fetch('/api/sessions/' + id + '/continue', { method: 'POST' })
        .then(r => r.json())
        .then(g => {
          S.resumeFor = id;
          if (g && g.summary && g.summary.summary) {
            S.resume.summary = g.summary;
            showResumePreview(g.summary);
          }
          if (g && g.status === 'no_speech') {
            btn.disabled = true;
            setStatus(`<span class="err">${T('这通没有对话内容，没法接着聊')}</span>`);
          }
        })
        .catch(() => {});
    } catch (e) {
      S.resume = null;
      toast(T('读不到要续聊的那通电话，就当新的一次开始。'), true, 5000);
    }
  }

  /* 摘要预览：只显示「聊到哪 + 下一步」，不显示 facts —— 那些是给模型的备忘，
     给用户看会像在读内部注释。 */
  function showResumePreview(sum) {
    // 收起「上次通话」条：续聊时信息已经搬到上方，重复显示只挤位置
    const tips = $('idle-tips'); if (tips) tips.classList.add('hide');
    const rec = $('recap'); if (rec) rec.style.display = 'none';
    const box = document.createElement('div');
    box.className = 'resume-box';
    const lines = [];
    if (sum.topic) lines.push(`<b>${esc(sum.topic)}</b>`);
    if (sum.summary) lines.push(esc(sum.summary));
    if (sum.next_step) lines.push(`<span class="nx">${T('接着聊')}：${esc(sum.next_step)}</span>`);
    box.innerHTML = `<div class="rh">${T('上次聊到这儿')}</div>${lines.map(x => `<p>${x}</p>`).join('')}`;
    // 接力提示板本来就在「待机」区里（接通后整块自动隐藏），
    // 放进 captions 是不行的：那个容器只在通话中才显示。
    const idle = el.idle || $('idle');
    const host = idle || el.captions;
    const old = host.querySelector('.resume-box');
    if (old) old.remove();
    host.insertBefore(box, host.firstChild);
  }

  // 上次通话：把中间的空白填成有用的信息，同时充当"历史入口"
  async function loadRecap() {
    try {
      const [ss, pf] = await Promise.all([
        fetch('/api/sessions?limit=1').then(r => r.json()),
        fetch('/api/profile').then(r => r.json()).catch(() => null),
      ]);
      const last = (ss.sessions || [])[0];
      if (!last) return;
      S.recapData = { last, focus: pf && pf.last_focus };
      renderRecap();
    } catch (e) { /* 没记录就不显示 */ }
  }

  // 单独抽出来：切母语时也要跟着重画（不然英文界面里剩一行中文）
  function renderRecap() {
    const d = S.recapData;
    if (!d) return;
    const { last, focus } = d;
    const when = new Date(last.started_at * 1000).toLocaleString(
      UI_EN() ? 'en-US' : 'zh-CN',
      { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
    const line = focus || T('点这里看逐句记录和复盘');
    el.recap.href = '/history?session=' + last.id;
    el.recap.innerHTML =
      `<span class="ico">🗂</span>
       <span class="txt"><b>${T('上次通话')} · ${when} · ${T('{n} 轮', { n: last.turns || 0 })}</b><span>${line}</span></span>
       <span class="go">›</span>`;
    el.recap.style.display = 'flex';
  }

  // ---------------------------------------------------------------- 播放
  function ensurePlayCtx() {
    if (S.playCtx) return S.playCtx;
    S.playCtx = new (window.AudioContext || window.webkitAudioContext)();
    S.playGain = S.playCtx.createGain();
    S.playGain.gain.value = 1;
    S.playGain.connect(S.playCtx.destination);
    return S.playCtx;
  }

  /** Int16 PCM → AudioBuffer（实时播放和「重听」共用） */
  function pcmToBuffer(int16, rate) {
    const ctx = ensurePlayCtx();
    const f = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) f[i] = int16[i] / 32768;
    const buf = ctx.createBuffer(1, f.length, rate);
    buf.copyToChannel(f, 0);
    return buf;
  }

  function playPcm(int16, rate) {
    const ctx = ensurePlayCtx();
    if (ctx.state === 'suspended') ctx.resume();
    const buf = pcmToBuffer(int16, rate);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(S.playGain);
    const now = ctx.currentTime;
    if (S.playHead < now + 0.06) S.playHead = now + 0.06; /* 留点缓冲防爆音 */
    src.start(S.playHead);
    S.playHead += buf.duration;
    S.sources.add(src);
    src.onended = () => S.sources.delete(src);
  }

  function stopAllPlayback() {
    for (const s of S.sources) { try { s.stop(); } catch (e) {} }
    S.sources.clear();
    if (S.playCtx) { S.playHead = S.playCtx.currentTime; }
  }

  // ---------------------------------------------------------------- 字幕
  // 服务端每次都下发"到目前为止的完整文本"，所以这里直接覆盖，不做拼接。
  function setCaption(role, text, final) {
    const key = role === 'user' ? 'curUser' : 'curAsst';
    if (!S[key]) {
      const d = document.createElement('div');
      d.className = 'cap ' + role;
      const who = document.createElement('span');
      who.className = 'who';
      who.textContent = role === 'user' ? T('你') : 'AI';
      const body = document.createElement('span');
      d.append(who, body);
      el.captions.appendChild(d);
      S[key] = { el: d, body };
      while (el.captions.children.length > 40) el.captions.firstChild.remove();
    }
    S[key].body.textContent = text;
    if (final) S[key] = null;
    el.captions.scrollTop = el.captions.scrollHeight;
  }

  /* AI 这一轮说完（response.done）时，把攒下来的音频挂到这条字幕上：
     点一下就原样重放，不需要再问模型一遍（省 token，也没有重新生成的延迟）。 */
  function attachReplay() {
    const pcm = S.turnPcm;
    const text = S.turnText;
    S.turnPcm = [];
    S.turnText = '';
    if (!pcm.length) return;

    // 这一轮的 AI 气泡：优先拿还没收尾的那个；已经 final 了就拿最后一个 assistant 气泡
    let cap = S.curAsst && S.curAsst.el;
    if (!cap) {
      const all = el.captions.querySelectorAll('.cap.assistant');
      cap = all[all.length - 1];
    }
    if (!cap) return;
    if (cap.querySelector('.cap-replay')) return;

    const bytes = concatBytes(pcm);
    const i16 = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
    const rate = S.rate || 24000;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cap-replay';
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><path d="M4 9.5V5h4.5"/>
        <path d="M4.6 9.2A8 8 0 1 1 4 13"/></svg><span>${T('重听')}</span>`;
    btn.title = T('重新播放这一段');
    btn.onclick = () => replay(btn, i16, rate, text);
    cap.appendChild(btn);
    el.captions.scrollTop = el.captions.scrollHeight;
  }

  function concatBytes(chunks) {
    if (chunks.length === 1) return chunks[0];
    let n = 0;
    for (const c of chunks) n += c.byteLength;
    const out = new Uint8Array(n);
    let off = 0;
    for (const c of chunks) { out.set(new Uint8Array(c), off); off += c.byteLength; }
    return out;
  }

  function setReplayLabel(btn, txt) {
    const s = btn.querySelector('span');
    if (s) s.textContent = txt;
   }

  function replay(btn, i16, rate, text) {
    // 正在重听同一条 → 停止
    if (S.replay && S.replay.btn === btn) { stopReplay(); return; }
    stopReplay();
    try {
      const ctx = ensurePlayCtx();
      if (ctx.state === 'suspended') ctx.resume();
      const buf = pcmToBuffer(i16, rate);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(S.playGain);
      src.start();
      S.replay = { btn, src };
      btn.classList.add('playing');
      setReplayLabel(btn, T('播放中…'));
      src.onended = () => { if (S.replay && S.replay.src === src) stopReplay(); };
    } catch (e) {
      toast(T('播放失败') + '：' + e.message, true, 4000);
    }
  }

  function stopReplay() {
    const r = S.replay;
    S.replay = null;
    if (!r) return;
    try { r.src.stop(); } catch (e) {}
    r.btn.classList.remove('playing');
    setReplayLabel(r.btn, T('重听'));
  }

  // 开场白：服务端预生成的、和对话同音色的录音（见 tools/gen_greetings.py）
  // 有音频时缓存成 AudioBuffer，这样开场白也能「重听」。
  async function playGreeting(g) {
    if (!g || !g.text) return;
    setCaption('assistant', g.text, true);
    if (!g.audio_url) return;
    try {
      const buf = await (await fetch(g.audio_url)).arrayBuffer();
      const ctx = ensurePlayCtx();
      const audio = await ctx.decodeAudioData(buf);
      const src = ctx.createBufferSource();
      src.buffer = audio;
      src.connect(S.playGain);
      if (S.playHead < ctx.currentTime + 0.06) S.playHead = ctx.currentTime + 0.06;
      src.start(S.playHead);
      S.playHead += audio.duration;
      S.sources.add(src);
      src.onended = () => S.sources.delete(src);
      el.avatar.classList.add('speaking');
      el.wrap.classList.add('active');
      setTimeout(() => {
        el.avatar.classList.remove('speaking');
        el.wrap.classList.remove('active');
      }, audio.duration * 1000);
      attachGreetingReplay(audio);
    } catch (e) {
      console.warn('开场白播放失败', e);
    }
  }

  // 开场白结束后也给一个重听：刚接通没听清第一句是最常见的情况
  function attachGreetingReplay(audio) {
    const cap = el.captions.lastElementChild;
    if (!cap || !cap.classList.contains('assistant') || cap.querySelector('.cap-replay')) return;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cap-replay';
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><path d="M4 9.5V5h4.5"/>
        <path d="M4.6 9.2A8 8 0 1 1 4 13"/></svg><span>${T('重听')}</span>`;
    btn.title = T('重新播放这一段');
    btn.onclick = () => {
      if (S.replay && S.replay.btn === btn) { stopReplay(); return; }
      stopReplay();
      try {
        const ctx = ensurePlayCtx();
        if (ctx.state === 'suspended') ctx.resume();
        const src = ctx.createBufferSource();
        src.buffer = audio;
        src.connect(S.playGain);
        src.start();
        S.replay = { btn, src };
        btn.classList.add('playing');
        setReplayLabel(btn, T('播放中…'));
        src.onended = () => { if (S.replay && S.replay.src === src) stopReplay(); };
      } catch (e) { toast(T('播放失败') + '：' + e.message, true, 4000); }
    };
    cap.appendChild(btn);
  }

  // ---------------------------------------------------------------- 采集
  async function startCapture() {
    S.capStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    S.capCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    if (S.capCtx.state === 'suspended') await S.capCtx.resume();
    await S.capCtx.audioWorklet.addModule('/static/audio-worklet.js');
    S.capSource = S.capCtx.createMediaStreamSource(S.capStream);
    S.capNode = new AudioWorkletNode(S.capCtx, 'capture-processor');
    S.capNode.port.onmessage = (e) => {
      if (!S.inCall || S.muted || !S.ws || S.ws.readyState !== 1) return;
      S.ws.send(e.data);           // ArrayBuffer = 640 bytes PCM16 16k
    };
    S.capSource.connect(S.capNode);
    // 不连 destination，避免把自己声音放出来（回声）
  }

  function stopCapture() {
    try { if (S.capSource) S.capSource.disconnect(); } catch (e) {}
    try { if (S.capNode) S.capNode.disconnect(); } catch (e) {}
    try { if (S.capStream) S.capStream.getTracks().forEach(t => t.stop()); } catch (e) {}
    try { if (S.capCtx) { const c = S.capCtx.close(); if (c && c.catch) c.catch(() => {}); } } catch (e) {}
    S.capCtx = S.capStream = S.capNode = S.capSource = null;
  }

  // ---------------------------------------------------------------- 通话
  function setStatus(html) { el.status.innerHTML = html; }

  async function startCall(opt = {}) {
    el.call.disabled = true;
    if (!opt.resume) {
      el.captions.innerHTML = '';               // 上一通的字幕不带到这一通
      S.reportLang = UI_EN() ? 'en' : 'zh';     // 挂断后复盘用哪个语言解释
    }
    S.curUser = S.curAsst = null;
    S.turnPcm = [];
    S.turnText = '';
    setStatus(opt.resume ? T('正在切换语速…') : T('正在接通…'));
    try {
      await startCapture();                        // 先要麦克风权限（必须用户手势）
    } catch (e) {
      el.call.disabled = false;
      setStatus(`<span class="err">${T('拿不到麦克风权限')}</span>`);
      toast(T('浏览器拒绝了麦克风权限，或当前页面不是 https / localhost。'), true);
      return;
    }

    ensurePlayCtx();
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    S.ws = new WebSocket(`${proto}://${location.host}/ws/call`);
    S.ws.binaryType = 'arraybuffer';

    const ws = S.ws;
    ws.onopen = () => {
      const msg = {
        type: 'start',
        language: S.pick.language,
        scenario: S.pick.scenario,
        level: S.pick.level,
        provider: S.boot.active.provider,
        speed: S.speed,              // 语速是会话级参数，建会话时定下
      };
      if (S.sessionId) {
        // 这一通已经有记录了 → 这是「切语速」的重连：同一通电话，不要新记录
        msg.parent_session = S.sessionId;
        msg.mode = 'speed_switch';
      } else if (S.resume && S.resume.id) {
        // 新通话，但接着上次那通聊：服务端会注入它的摘要
        msg.parent_session = S.resume.id;
      }
      ws.send(JSON.stringify(msg));
    };

    ws.onmessage = (e) => {
      let m; try { m = JSON.parse(e.data); } catch { return; }
      switch (m.type) {
        case 'ready': {
          const wasInCall = S.inCall;
          S.inCall = true; S.muted = false;
          el.call.disabled = false;
          el.call.classList.add('hangup');
          el.call.title = T('挂断');
          el.call.querySelector('.lbl').textContent = T('挂断');
          el.mute.classList.remove('muted');
          document.body.classList.add('in-call');
          setStatus(`<span class="live">●</span> ${T('通话中')}`);
          // 重连（切语速）时不重放开场白、不重置计时和 sessionId —— 用户还在同一通电话里
          if (!wasInCall) {
            if (m.greeting && m.greeting.resume_hint) {
              // 续聊：AI 要接着上次的最后一句话往下说。说一句字幕提示，
              // 免得接通后那两秒静默看起来像没连上。
              setCaption('assistant', T('（接着上次聊到的地方…）'), true);
            } else {
              playGreeting(m.greeting);
            }
          }
          if (m.session_id && !S.sessionId) S.sessionId = m.session_id;
          if (!wasInCall) {
            S.t0 = Date.now();
            S.warned = false;
            // 时长上限只由服务端配置决定（默认 60 分钟，见 app/store/settings.py），
            // 页面上不提供修改：这是产品规则，不是用户选项。
            // 到点挂断后可以在记录里「继续这通电话」，上下文不丢。
            const cap = Number((S.boot.ui && S.boot.ui.call_time_limit_min) || 0);
            S.limitMs = cap > 0 ? cap * 60000 : 0;
            clearInterval(S.tick);
            S.tick = setInterval(tick, 500);
          }
          setChipsEnabled(false);
          const tips = $('idle-tips'); if (tips) tips.classList.add('hide');
          if (typeof m.speed === 'string' && m.speed !== S.speed) {
            S.speed = m.speed;
            renderSpeed();
          }
          break;
        }

        case 'audio': {
          const bytes = b64ToBytes(m.pcm);
          const i16 = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
          S.rate = m.rate || 24000;
          // 攒下这一轮的原始 PCM：用来做「重听」，不占后端带宽也不调模型
          if (S.turnPcm.length < 3000) S.turnPcm.push(bytes.slice());
          playPcm(i16, S.rate);
          el.wrap.classList.add('active');
          el.avatar.classList.add('speaking');
          clearTimeout(S._spk);
          S._spk = setTimeout(() => {
            el.avatar.classList.remove('speaking');
            el.wrap.classList.remove('active');
          }, 700);
          break;
        }

        case 'user_speech_started':
          stopAllPlayback();          // 打断：立刻静音，用户感知必须即时
          stopReplay();
          el.avatar.classList.remove('speaking');
          break;

        case 'caption':
          setCaption(m.role, m.text, !!m.final);
          if (m.role === 'assistant') S.turnText = m.text || S.turnText;
          break;

        case 'turn_end':
          // AI 这一轮说完 → 字幕气泡上挂「重听」
          if (m.role === 'assistant') attachReplay();
          break;

        case 'error':
          toast(m.message || '出错了', true, 12000);
          if (m.code) console.warn('provider error', m.code, m.detail);
          if (m.fatal) endCall(false);
          break;

        case 'closed':
          endCall(false);
          break;
      }
    };

    let wsOpened = false;
    ws.onopen = ((orig) => () => { wsOpened = true; orig(); })(ws.onopen);

    ws.onerror = () => {
      setStatus(`<span class="err">${T('连接失败')}</span>`);
      // 手机上报「连接服务器失败」时别只说检查服务：这里把真实原因分清楚，
      // 否则证书不匹配（IP 访问 + 域名证书）会被误当成服务挂了。
      const ipLike = /^(\d{1,3}\.){3}\d{1,3}$/.test(location.hostname);
      const hint = location.protocol !== 'https:'
        ? '当前不是 https，浏览器会拦住 wss 和麦克风。'
        : (ipLike ? '你正用 IP 访问，而服务器证书是给域名签的 —— 手机会直接拒绝 wss 连接（PC 上点“继续访问”能过，手机不行）。请改用证书对应的域名访问。' : '');
      el.toast.innerHTML = `连接失败。${hint || '检查服务是否在跑、网络是否正常；若刚重启过服务，刷新页面重新登录。'}`;
      el.toast.className = 'toast show err';
      el.toast.style.pointerEvents = 'auto';
      clearTimeout(toast._t);
      toast._t = setTimeout(() => { el.toast.className = 'toast'; el.toast.style.pointerEvents = 'none'; }, 60000);
    };

    ws.onclose = (ev) => {
      if (!wsOpened) {
        // 4401 = 服务端判定未登录（cookie 过期或换了 SECRET_KEY）
        if (ev.code === 4401) {
          location.replace('/login?next=' + encodeURIComponent(location.pathname));
          return;
        }
        // 从没 open 过 = 握手就没成，把 code 打出来方便定位
        setStatus(`<span class="err">连不上服务（close code ${ev.code}）</span>`);
        if (S.inCall) endCall(false);
        return;
      }
      if (S.inCall) endCall(false);
    };
  }

  function endCall(sendStop = true) {
    if (!S.inCall && !S.ws) return;
    S.inCall = false;

    // ---- 先复位界面：无论后面清理出什么错，UI 都不能卡在"通话中" ----
    clearTimeout(S._spk);
    clearInterval(S.tick);
    el.avatar.classList.remove('speaking');
    el.wrap.classList.remove('active');
    el.call.classList.remove('hangup');
    el.call.querySelector('.lbl').textContent = T('开始通话');
    el.call.disabled = false;
    el.call.title = T('开始通话');
    el.mute.classList.remove('muted');
    document.body.classList.remove('in-call');
    setStatus(T('已结束'));
    setChipsEnabled(true);
    const tips = $('idle-tips'); if (tips) tips.classList.remove('hide');
    // 续聊只对「这一次」有效：挂了就是新的一通。不重置的话，再点开始会继续
    // 注入上一通的摘要，且按切语速的模式复用旧 session_id —— 记录会错乱。
    S.resume = null;
    S.resumeFor = null;
    el.call.classList.remove('resume');
    el.call.title = T('开始通话');
    el.call.querySelector('.lbl').textContent = T('开始通话');

    // ---- 再拆资源，每步独立兜底 ----
    try {
      if (sendStop && S.ws && S.ws.readyState === 1) {
        S.ws.send(JSON.stringify({ type: 'stop' }));
      }
    } catch (e) {}
    try { if (S.ws) S.ws.close(); } catch (e) {}
    S.ws = null;
    try { stopAllPlayback(); } catch (e) { console.warn('停播失败', e); }
    try { stopCapture(); } catch (e) { console.warn('停采集失败', e); }

    // 有语音的通话才值得复盘（太短的直接跳过，省 token 也省等待）
    const sid = S.sessionId;
    S.sessionId = null;
    if (sid) setTimeout(() => openReport(sid), 400);
  }

  /* 语速是建会话时的参数（豆包 session.create.audio.output.speed）：
     通话中改语速必须重建会话，否则不会生效 —— 这正是「让它说慢点，
     过一会儿又恢复」的根源（原来只会在 prompt 里求它）。
     重连对用户是可接受的：字幕保留，AI 会接着往下聊。 */
  async function reconnect() {
    if (!S.inCall) return;
    const keepCaps = el.captions.childElementCount;
    // 保留已显示的字幕：不清空 captions，只是换一条 WS
    S.curUser = S.curAsst = null;
    try { if (S.ws) S.ws.close(); } catch (e) {}
    S.ws = null;
    stopAllPlayback();
    stopReplay();
    try { stopCapture(); } catch (e) {}
    await startCall({ resume: true, keepCaps });
  }

  function tick() {
    const used = Date.now() - S.t0;
    if (S.inCall) {
      setStatus(`<span class="live">●</span> <span class="t">${fmtTime(used)}</span>`);
    }
    // limitMs = 0 表示不限时（设置里填 0 / 留空）
    if (!S.limitMs) return;
    const left = S.limitMs - used;
    if (left <= 60000 && left > 0 && !S.warned) {
      S.warned = true;
      toast(T('还剩 1 分钟，准备收尾'), false, 4000);
    }
    if (left <= 0 && S.inCall) {
      toast(T('时间到，结束这通电话'), false, 3000);
      endCall(true);
    }
  }

  // ---------------------------------------------------------------- 复盘
  const TYPE_LABEL = {
    grammar: '语法', tense: '时态', word: '用词', order: '语序', pronunciation: '发音',
  };
  // esc 已在文件顶部定义（续聊预览和复盘共用）

  function showReportLoading() {
    el.rpSub.textContent = T('正在分析你的表达…');
    el.rpBody.innerHTML = `<div class="rp-loading"><span class="spinner"></span> ${T('AI 正在逐句看你的表达，通常 10~20 秒')}</div>`;
  }

  function renderReport(rep, sid) {
    const sc = rep.score || {};
    const bar = (v) => `<div class="bar"><i style="width:${Math.max(0, Math.min(100, (v || 0) * 10))}%"></i></div>`;
    const scoreCard = (k, v) => `<div class="score"><div class="k">${k}</div><div class="v">${v}<span style="font-size:12px;color:var(--fg-dim)">/10</span></div>${bar(v)}</div>`;

    const errs = (rep.errors || []).map((e, i) => `
      <div class="err-item">
        <div class="err-line">
          <span class="tag">${T('你说')}</span>
          <span class="err-said">${esc(e.you_said)}</span>
          <span class="type-badge">${T(TYPE_LABEL[e.type] || e.type)}</span>
        </div>
        <div class="err-line"><span class="tag">${T('地道')}</span><span class="err-better">${esc(e.better)}</span></div>
        ${e.why ? `<div class="err-why">${esc(e.why)}</div>` : ''}
      </div>`).join('');

    // 生词可点开：音标 / 释义 / 例句 / 词形变化（见 cards.js）
    const words = (rep.new_words || []).map(w =>
      `<button type="button" class="word-chip" data-word="${esc(w.word)}"
               data-meaning="${esc(w.meaning)}" title="${T('点开看音标、例句和词形变化')}">
         ${esc(w.word)}<span class="zh">${esc(w.meaning)}</span></button>`).join('');

    el.rpSub.textContent = `${rep.level ? T('估计水平') + ' ' + rep.level + ' · ' : ''}${(rep.errors || []).length} ${T('处可改进')}`;
    el.rpBody.innerHTML = `
      <p class="rp-overall">${esc(rep.overall)}</p>
      <div class="scores">
        ${scoreCard(T('语法'), sc.grammar)}
        ${scoreCard(T('流利度'), sc.fluency)}
        ${scoreCard(T('词汇'), sc.vocabulary)}
      </div>
      ${errs ? `<div class="rp-sec-title">${T('可以更地道的地方')}</div>${errs}`
             : `<div class="rp-sec-title">${T('可以更地道的地方')}</div><p class="rp-overall" style="color:var(--ok)">${T('这通没挑出明显错误，继续保持。')}</p>`}
      ${words ? `<div class="rp-sec-title">${T('顺手记住')}</div><div class="words">${words}</div>` : ''}
      ${rep.next_focus ? `<div class="rp-sec-title">${T('下一通的重点')}</div><div class="focus-box">${esc(rep.next_focus)}</div>` : ''}
    `;
    el.rpModal.classList.add('open');
  }

  async function openReport(sid) {
    if (!sid) return;
    el.rpModal.classList.add('open');
    showReportLoading();
    try {
      const uiLang = S.reportLang || (UI_EN() ? 'en' : 'zh');
      const r = await fetch(`/api/sessions/${sid}/report`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ui_lang: uiLang }),
      }).then(x => x.json());
      if (!r.ok) throw new Error(r.message || '生成失败');
      renderReport(r.report, sid);
    } catch (e) {
      el.rpSub.textContent = T('生成失败');
      el.rpBody.innerHTML =
        `<div class="rp-err">${esc(e.message)}</div>` +
        `<div class="row" style="margin-top:14px">
           <button class="btn" id="rp-retry">重新生成</button>
           <a class="btn" href="/stats">去错题本看看</a>
         </div>`;
      const b = $('rp-retry');
      if (b) b.onclick = () => openReport(sid);
    }
  }

  // ---------------------------------------------------------------- 绑定
  el.call.onclick = () => {
    if (S.inCall) endCall(true);
    else {
      ensurePlayCtx();               // 必须在用户手势里创建，否则 iOS 不出声
      if (S.resume) toast(T('正在接着上次的话题…'), false, 2500);
      startCall();
    }
  };

  el.mute.onclick = () => {
    S.muted = !S.muted;
    el.mute.classList.toggle('muted', S.muted);
    if (S.ws?.readyState === 1) S.ws.send(JSON.stringify({ type: 'mute', on: S.muted }));
    toast(S.muted ? T('已静音（AI 还会继续说完）') : T('麦克风已开'), false, 1600);
  };

  if (el.theme) {
    el.theme.onclick = () => {
      if (window.THEME) THEME.set(THEME.effective === 'light' ? 'dark' : 'light');
      else document.documentElement.dataset.theme =
        document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    };
  }

  el.rpClose.onclick = () => el.rpModal.classList.remove('open');
  el.rpAgain.onclick = () => {
    el.rpModal.classList.remove('open');
    el.captions.innerHTML = '';
    el.call.click();
  };
  el.rpModal.addEventListener('click', (e) => {
    if (e.target === el.rpModal) el.rpModal.classList.remove('open');
  });

  window.addEventListener('beforeunload', () => { try { S.ws?.close(); } catch (e) {} });

  // 调试出口：控制台里 __sayit 可以直接看通话状态，__openReport(sid) 看某通电话的复盘
  window.__sayit = S;
  window.__openReport = openReport;
  window.__replay = () => el.captions.querySelectorAll('.cap-replay');

  boot().catch(e => {
    setStatus('<span class="err">加载配置失败</span>');
    toast('读不到 /api/bootstrap，服务端可能没起来：' + e.message, true);
  });
})();
