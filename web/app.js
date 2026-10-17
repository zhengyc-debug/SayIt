/* SayIt 通话页
 * 职责：拿麦克风 → 20ms 一包发给服务端；把服务端回来的音频排进播放队列。
 * 打断：服务端说 user_speech_started 时，立刻清空播放队列。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  // 界面语言（母语）：只影响文案与复盘解释，不影响练习语言
  const T = (s, v) => (window.I18N ? I18N.t(s, v) : s);
  const UI_EN = () => !!(window.I18N && I18N.lang === 'en');
  const el = {
    status: $('status'), captions: $('captions'), avatar: $('avatar'),
    wrap: $('avatar-wrap'), mute: $('btn-mute'), call: $('btn-call'), timer: $('timer'),
    whoName: $('who-name'), whoSub: $('who-sub'), idle: $('idle'), langHost: $('lang-host'),
    toast: $('toast'), rowScen: $('row-scen'), rowMeta: $('row-meta'), recap: $('recap'),
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
    if (el.langHost && window.I18N) I18N.mountSwitch(el.langHost);
    renderChips();
    loadRecap();
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
    renderWho();
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

  function playPcm(int16, rate) {
    const ctx = ensurePlayCtx();
    if (ctx.state === 'suspended') ctx.resume();
    const f = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) f[i] = int16[i] / 32768;
    const buf = ctx.createBuffer(1, f.length, rate);
    buf.copyToChannel(f, 0);
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

  // 开场白：服务端预生成的、和对话同音色的录音（见 tools/gen_greetings.py）
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
    } catch (e) {
      console.warn('开场白播放失败', e);
    }
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

  async function startCall() {
    el.call.disabled = true;
    el.captions.innerHTML = '';                 // 上一通的字幕不带到这一通
    S.curUser = S.curAsst = null;
    S.reportLang = UI_EN() ? 'en' : 'zh';       // 挂断后复盘用哪个语言解释
    setStatus(T('正在接通…'));
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
      ws.send(JSON.stringify({
        type: 'start',
        language: S.pick.language,
        scenario: S.pick.scenario,
        level: S.pick.level,
        provider: S.boot.active.provider,
      }));
    };

    ws.onmessage = (e) => {
      let m; try { m = JSON.parse(e.data); } catch { return; }
      switch (m.type) {
        case 'ready':
          S.inCall = true; S.muted = false;
          el.call.disabled = false;
          el.call.classList.add('hangup');
          el.call.title = T('挂断');
          el.call.querySelector('.lbl').textContent = T('挂断');
          el.mute.classList.remove('muted');
          document.body.classList.add('in-call');
          setStatus(`<span class="live">●</span> ${T('通话中')}`);
          playGreeting(m.greeting);
          S.sessionId = m.session_id;
          S.t0 = Date.now();
          S.warned = false;
          S.limitMs = ((S.boot.ui && S.boot.ui.call_time_limit_min) || 15) * 60000;
          S.tick = setInterval(tick, 500);
          setChipsEnabled(false);
          const tips = $('idle-tips'); if (tips) tips.classList.add('hide');
          break;

        case 'audio': {
          const bytes = b64ToBytes(m.pcm);
          const i16 = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
          playPcm(i16, m.rate || 24000);
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
          el.avatar.classList.remove('speaking');
          break;

        case 'caption':
          setCaption(m.role, m.text, !!m.final);
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
      const isHttps = location.protocol === 'https:';
      const hint = isHttps
        ? `WebSocket 连接失败。如果你是用<b>手机 + 自签证书</b>访问的：<br>` +
          `浏览器会拒绝为「不受信任的证书」建立 WebSocket（页面能点"继续前往"，WebSocket 没有这个按钮）。<br>` +
          `解决：在手机浏览器里打开 <a href="/ca.cer" style="color:#6ea8fe">${location.origin}/ca.cer</a> ` +
          `安装证书后再刷新本页。`
        : '连接服务端失败，检查服务是否还在跑。';
      setStatus(`<span class="err">${T('连接失败')}</span>`);
      el.toast.innerHTML = hint;
      el.toast.className = 'toast show err';
      el.toast.style.pointerEvents = 'auto';
      clearTimeout(toast._t);
      toast._t = setTimeout(() => { el.toast.className = 'toast'; el.toast.style.pointerEvents = 'none'; }, 60000);
    };

    ws.onclose = (ev) => {
      if (!wsOpened) {
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

  function tick() {
    const used = Date.now() - S.t0;
    if (S.inCall) {
      setStatus(`<span class="live">●</span> <span class="t">${fmtTime(used)}</span>`);
    }
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
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

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

    const words = (rep.new_words || []).map(w =>
      `<div class="word-chip">${esc(w.word)}<span>${esc(w.meaning)}</span></div>`).join('');

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
      startCall();
    }
  };

  el.mute.onclick = () => {
    S.muted = !S.muted;
    el.mute.classList.toggle('muted', S.muted);
    if (S.ws?.readyState === 1) S.ws.send(JSON.stringify({ type: 'mute', on: S.muted }));
    toast(S.muted ? T('已静音（AI 还会继续说完）') : T('麦克风已开'), false, 1600);
  };

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

  boot().catch(e => {
    setStatus('<span class="err">加载配置失败</span>');
    toast('读不到 /api/bootstrap，服务端可能没起来：' + e.message, true);
  });
})();
