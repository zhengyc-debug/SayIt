/* SayIt · 词卡（音标 / 释义 / 例句 / 词形变化）
 *
 * 三处共用这一份：通话页复盘弹层、记录页详情、生词本页面。
 * 数据来自 GET /api/words/card?word=…&ui_lang=…，第一次会调文本模型生成并缓存，
 * 之后再点开直接命中缓存（0 token）。
 *
 * 页面里需要有一个 <div class="modal" id="word-modal">，里面
 *   #wc-sub 副标题、#wc-body 内容、#wc-close 关闭按钮。
 * 如果页面没有这个词卡弹层，WORDS.card() 会自己造一个挂到 body 上。
 */
(() => {
  const T = (s, v) => (window.I18N ? I18N.t(s, v) : s);
  const uiLang = () => (window.I18N ? I18N.lang : 'zh');
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function ensureModal() {
    let m = document.getElementById('word-modal');
    if (m) return m;
    m = document.createElement('div');
    m.className = 'modal';
    m.id = 'word-modal';
    m.innerHTML = `
      <div class="modal-card">
        <div class="modal-head">
          <div><h3>词卡</h3><div class="sub" id="wc-sub"></div></div>
          <button class="icon-btn" id="wc-close" title="关闭">✕</button>
        </div>
        <div class="modal-body" id="wc-body"></div>
      </div>`;
    document.body.appendChild(m);
    m.addEventListener('click', (e) => { if (e.target === m) close(); });
    return m;
  }

  function close() {
    const m = document.getElementById('word-modal');
    if (m) m.classList.remove('open');
    SPEAK.stop();
  }

  /** 用浏览器自带的朗读念一遍（不额外调 TTS，省 token 也省延迟） */
  const SPEAK = {
    cur: null,
    stop() {
      if ('speechSynthesis' in window) window.speechSynthesis.cancel();
      this.cur = null;
    },
    say(text, lang) {
      if (!('speechSynthesis' in window)) return false;
      this.stop();
      const u = new SpeechSynthesisUtterance(text);
      u.lang = lang || 'en-US';
      u.rate = 0.85;
      this.cur = u;
      window.speechSynthesis.speak(u);
      return true;
    },
  };

  // 数据来源：让用户能判断这张卡靠不靠谱（有道词典 / 开源词典 / 模型兜底）
  const SOURCE_LABEL = {
    youdao: '有道词典',
    dictapi: 'Wiktionary · dictionaryapi.dev',
    llm: '大模型兜底',
  };

  function sourceNote(src) {
    if (!src) return '';
    const label = SOURCE_LABEL[src] || src;
    const hint = src === 'youdao' ? T('免费词典，不消耗 token')
      : src === 'llm' ? T('词典没收录才用模型，会消耗少量 token')
        : T('免费开源词典');
    return `<div class="wc-source">${T('数据来源')}：${label} · ${hint}</div>`;
  }

  function renderCard(data) {
    const c = data.card;
    const word = data.word || c.word;
    const ipa = (label, v) => v
      ? `<div><b>${label}</b><span class="ipa">${esc(v)}</span></div>` : '';

    const phon = (c.phonetic_us || c.phonetic_uk)
      ? `<div class="wc-phon">${ipa('US', c.phonetic_us)}${ipa('UK', c.phonetic_uk)}</div>`
      : '';

    const senses = (c.senses && c.senses.length)
      ? `<div class="wc-sec"><div class="wc-sec-t">${T('释义')}</div>${c.senses.map(x =>
          `<div class="wc-def">${x.pos ? `<span class="p">${esc(x.pos)}</span>` : ''}${esc(x.def)}</div>`
        ).join('')}</div>`
      : (c.meaning
        ? `<div class="wc-sec"><div class="wc-sec-t">${T('释义')}</div><div class="wc-def">${esc(c.meaning)}</div></div>`
        : '');

    const forms = c.forms
      ? `<div class="wc-sec"><div class="wc-sec-t">${T('词形变化')}</div>
           <div class="wc-forms">${esc(c.forms)}</div></div>`
      : '';

    const exs = (c.examples && c.examples.length)
      ? `<div class="wc-sec"><div class="wc-sec-t">${T('例句')}</div>${c.examples.map(x =>
          `<div class="wc-ex"><div class="en">${esc(x.en)}</div>${x.zh ? `<div class="zh">${esc(x.zh)}</div>` : ''}</div>`
        ).join('')}</div>`
      : '';

    return `
      <div class="wc-head">
        <h2 class="wc-word">${esc(word)}</h2>
        ${c.pos ? `<span class="wc-pos">${esc(c.pos)}</span>` : ''}
      </div>
      ${phon}${senses}${forms}${exs}
      <div class="wc-actions">
        <button class="btn sm" id="wc-speak-word">🔊 ${T('读单词')}</button>
        <button class="btn sm" id="wc-speak-ex">🗣 ${T('读例句')}</button>
        <button class="btn sm" id="wc-regen">${T('换个来源重查')}</button>
      </div>
      ${sourceNote(c.source)}`;
  }

  // 词典给的真人发音（有道 dictvoice / Wiktionary mp3）；没有才退回浏览器 TTS
  let AUDIO = null;
  function playAudio(url) {
    if (!url) return false;
    try {
      if (!AUDIO) AUDIO = new Audio();
      AUDIO.pause();
      AUDIO.src = url;
      AUDIO.play().catch(() => {});   // 播放失败时调用方会退回浏览器 TTS
      return true;
    } catch (e) { return false; }
  }

  function bindActions(data) {
    const c = data.card || {};
    const w = document.getElementById('wc-speak-word');
    const ex = document.getElementById('wc-speak-ex');
    const rg = document.getElementById('wc-regen');
    if (w) w.onclick = () => {
      if (!(c.audio && playAudio(c.audio))) SPEAK.say(data.word || c.word, 'en-US');
    };
    if (ex) ex.onclick = () => {
      const s = (c.examples || [])[0];
      if (s) SPEAK.say(s.en, 'en-US');
    };
    if (rg) rg.onclick = () => open(data.word, { force: true });
  }

  let inflight = null;

  /**
   * 打开某个词的词卡。
   * @param {string} word
   * @param {{force?:boolean, fallback?:string}} opt fallback=复盘里已有的释义，
   *        模型不可用时至少有东西可看
   */
  async function open(word, opt = {}) {
    word = (word || '').trim();
    if (!word) return;
    const m = ensureModal();
    const sub = document.getElementById('wc-sub');
    const body = document.getElementById('wc-body');
    m.classList.add('open');
    if (sub) sub.textContent = '';
    body.innerHTML = `<div class="wc-loading"><span class="spinner"></span> ${T('正在生成词卡…')}</div>`;

    const key = word + '|' + uiLang() + '|' + (opt.force ? 1 : 0);
    if (inflight && inflight.key === key) return;
    inflight = { key };

    try {
      const url = `/api/words/card?word=${encodeURIComponent(word)}`
        + `&ui_lang=${encodeURIComponent(uiLang())}` + (opt.force ? '&force=true' : '');
      const r = await (await fetch(url)).json();
      inflight = null;
      if (!r.ok || !r.card) {
        // 降级：只显示复盘里的意思，并说明为什么没有音标和例句
        body.innerHTML = `
          <div class="wc-head"><h2 class="wc-word">${esc(word)}</h2></div>
          ${opt.fallback ? `<div class="wc-sec"><div class="wc-sec-t">${T('释义')}</div>
            <div class="wc-def">${esc(opt.fallback)}</div></div>` : ''}
          <p class="hint" style="margin-top:16px;color:var(--warn)">${esc(r.message || T('词卡生成失败'))}</p>
          <div class="wc-actions"><button class="btn sm" id="wc-retry">${T('重试')}</button></div>`;
        const b = document.getElementById('wc-retry');
        if (b) b.onclick = () => open(word, { ...opt, force: true });
        return;
      }
      if (sub) {
        sub.textContent = [r.card.pos, r.meaning || ''].filter(Boolean).join(' · ');
      }
      const src = r.source || (r.card && r.card.source) || '';
      body.innerHTML = renderCard({ ...r, word, card: { ...r.card, source: src } });
      bindActions({ ...r, word });
      if (window.I18N) I18N.apply(body);
    } catch (e) {
      inflight = null;
      body.innerHTML = `<div class="wc-loading" style="color:var(--err-ink)">${esc(e.message)}</div>`;
    }
  }

  // 事件委托：任何带 data-word 的元素都能点开词卡
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-word]');
    if (!t) return;
    e.preventDefault();
    open(t.dataset.word, { fallback: t.dataset.meaning || '' });
  });

  document.addEventListener('click', (e) => {
    if (e.target.closest('#wc-close')) close();
  });

  window.WORDS = { open, close, speak: SPEAK };
})();
