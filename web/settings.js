/* 设置页：所有可选项都来自服务端注册表，前端不硬编码任何供应商/语言。 */
(() => {
  const $ = (id) => document.getElementById(id);
  const T = (s, v) => (window.I18N ? I18N.t(s, v) : s);
  let boot = null;

  function toast(msg, isErr = false, ms = 4000) {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.className = 'toast'; }, ms);
  }

  function currentLang() {
    return boot.languages.find(l => l.code === $('s-language').value) || boot.languages[0];
  }

  // 主题选择器（深色 / 浅色 / 跟随系统）：纯前端偏好，不进服务端配置
  function renderTheme() {
    const host = $('theme-switch');
    if (!host || !window.THEME) return;
    host.innerHTML = '';
    THEME.mountSwitch(host);
  }

  function renderSpeeds(cur) {
    const sel = $('s-speed');
    if (!sel) return;
    const list = boot.speech_speeds || [];
    const en = !!(window.I18N && I18N.lang === 'en');
    sel.innerHTML = list.map(s =>
      `<option value="${s.id}">${en ? s.label_en : s.label}</option>`).join('');
    const want = cur || (boot.ui && boot.ui.speech_speed) || 'slow';
    sel.value = list.some(s => s.id === want) ? want : (list[0] || {}).id || '';
  }

  function renderCommon() {
    const a = boot.active;
    const en = !!(window.I18N && I18N.lang === 'en');
    $('s-language').innerHTML = boot.languages.map(l =>
      `<option value="${l.code}">${en ? l.label : `${l.label_zh} ${l.label}`}</option>`).join('');
    $('s-scenario').innerHTML = boot.scenarios.map(s =>
      `<option value="${s.id}">${s.icon} ${en ? (s.label || s.label_zh) : s.label_zh}</option>`).join('');
    $('s-language').value = a.language;
    $('s-scenario').value = a.scenario;
    $('s-name').value = a.assistant_name || '';
    renderLevels(a.level);
    renderSpeeds();
    renderTheme();
    renderWordCard();
  }

  function renderWordCard() {
    const sel = $('s-wc-llm');
    if (!sel) return;
    sel.value = String((boot.ui && boot.ui.word_card_llm) !== false);
  }

  function renderLevels(pick) {
    const lang = currentLang();
    $('s-level').innerHTML = (lang.levels || []).map(v => `<option>${v}</option>`).join('');
    $('s-level').value = pick && lang.levels?.includes(pick) ? pick : (lang.default_level || 'B1');
  }

  function voiceOptions(pid) {
    const lang = currentLang();
    return (lang.voices?.[pid]) || [];
  }

  function renderLlm() {
    const c = boot.llm || {};
    document.querySelectorAll('[data-llm]').forEach(inp => {
      const f = inp.dataset.llm;
      if (f === 'api_key') {
        inp.placeholder = c.api_key_masked || 'sk-...';
      } else if (f === 'enabled') {
        inp.value = String(c.enabled !== false);
      } else {
        inp.value = c[f] || '';
      }
    });
    const b = $('llm-badge');
    b.innerHTML = c.api_key_set
      ? `<span class="badge ok">${T('Key 已配置')}</span>`
      : `<span class="badge warn">${T('未配 Key')}</span>`;
  }

  function collectLlm() {
    const out = {};
    document.querySelectorAll('[data-llm]').forEach(inp => {
      const f = inp.dataset.llm;
      if (f === 'api_key') { if (inp.value.trim()) out.api_key = inp.value.trim(); return; }
      if (f === 'enabled') { out.enabled = inp.value === 'true'; return; }
      out[f] = inp.value.trim();
    });
    return out;
  }

  function renderProviderSummary() {
    const el = $('provider-summary');
    if (!el) return;
    const parts = boot.providers.map(p => {
      const conf = boot.provider_settings[p.id] || {};
      const mark = conf.api_key_set ? '✓' : '✗';
      return `${p.label.split(' · ')[0]} ${mark}`;
    });
    const activeConf = boot.provider_settings[boot.active.provider] || {};
    const head = activeConf.api_key_set
      ? `${T('当前语音模型')}：已配 Key（${activeConf.api_key_masked || ''}）`
      : `${T('当前语音模型')}：未配 Key，无法通话`;
    el.innerHTML = `${head}　|　${parts.join('　')}`;
  }

  function renderProviders() {
    const host = $('providers');
    host.innerHTML = '';
    const activeId = boot.active.provider;
    renderProviderSummary();

    for (const p of boot.providers) {
      const conf = boot.provider_settings[p.id] || {};
      const card = document.createElement('div');
      card.className = 'card' + (p.id === activeId ? ' sel' : '');
      card.dataset.pid = p.id;

      const badges = [];
      if (conf.api_key_set) badges.push(`<span class="badge ok">${T('Key 已配置')}</span>`);
      else badges.push(`<span class="badge warn">${T('未配 Key')}</span>`);
      if (!p.implemented) badges.push(`<span class="badge warn">${T('未联调')}</span>`);

      const voices = voiceOptions(p.id);
      card.innerHTML = `
        <div class="card-head">
          <input type="radio" name="provider" value="${p.id}" ${p.id === activeId ? 'checked' : ''} style="width:auto" />
          <span class="name">${p.label}</span>
          ${badges.join('')}
        </div>
        <p class="hint" style="margin:8px 0 0">${p.notes || ''}</p>
        <div class="grid">
          <label class="field">API Key
            <input type="password" data-f="api_key" autocomplete="off"
                   placeholder="${conf.api_key_masked || '粘贴 API Key'}" />
          </label>
          <label class="field">模型
            <select data-f="model">
              ${(p.models || []).map(m => `<option ${m === conf.model ? 'selected' : ''}>${m}</option>`).join('')}
            </select>
          </label>
          <label class="field">音色
            <select data-f="voice">
              ${voices.map((v, i) => {
                const sel = conf.voice ? v.id === conf.voice : i === 0;
                return `<option value="${v.id}" ${sel ? 'selected' : ''}>${v.label}</option>`;
              }).join('') || '<option value="">（该语言暂无音色）</option>'}
            </select>
          </label>
          <label class="field">端点（一般不用改）
            <input data-f="endpoint" value="${conf.endpoint || ''}" />
          </label>
        </div>
        <div class="row">
          <button class="btn" data-act="test">测试连接</button>
          <span class="result" data-res></span>
        </div>
      `;
      host.appendChild(card);
    }
    if (window.I18N) I18N.apply(host);

    host.querySelectorAll('input[name=provider]').forEach(r => {
      r.onchange = () => {
        host.querySelectorAll('.card').forEach(c =>
          c.classList.toggle('sel', c.dataset.pid === r.value));
      };
    });

    host.querySelectorAll('[data-act=test]').forEach(btn => {
      btn.onclick = () => testProvider(btn.closest('.card'), btn);
    });
  }

  async function testProvider(card, btn) {
    const pid = card.dataset.pid;
    const res = card.querySelector('[data-res]');
    btn.disabled = true;
    res.className = 'result';
    res.textContent = T('连接中…');
    try {
      // 先把当前表单值落库，否则测的是旧 Key
      await save(true);
      const r = await fetch('/api/settings/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: pid, language: $('s-language').value }),
      });
      const d = await r.json();
      if (d.ok) {
        res.className = 'result ok';
        res.textContent = `✓ 连接成功 ${d.connect_ms}ms`;
      } else {
        res.className = 'result err';
        res.textContent = `✗ ${d.message || '失败'}${d.code ? ` (${d.code})` : ''}`;
      }
    } catch (e) {
      res.className = 'result err';
      res.textContent = '✗ ' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  function collect() {
    const providers = {};
    document.querySelectorAll('.card[data-pid]').forEach(card => {
      const pid = card.dataset.pid;
      const conf = {};
      card.querySelectorAll('[data-f]').forEach(inp => {
        const f = inp.dataset.f;
        if (f === 'api_key' && !inp.value.trim()) return;  // 留空 = 不改
        conf[f] = inp.value;
      });
      if (Object.keys(conf).length) providers[pid] = conf;
    });
    const sel = document.querySelector('input[name=provider]:checked');
    return {
      llm: collectLlm(),
      active: {
        provider: sel ? sel.value : boot.active.provider,
        language: $('s-language').value,
        scenario: $('s-scenario').value,
        level: $('s-level').value,
        assistant_name: $('s-name').value.trim(),
      },
      // 语速：默认通话语速（通话页那一排的默认值，也是通话中不选时用的档）
      // word_card_llm：生词卡在词典都没收录时是否用文本模型兜底
      ui: {
        speech_speed: $('s-speed') ? $('s-speed').value : undefined,
        word_card_llm: $('s-wc-llm') ? $('s-wc-llm').value === 'true' : undefined,
      },
      providers,
    };
  }

  async function save(quiet = false) {
    const body = collect();
    const r = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    boot = await (await fetch('/api/bootstrap')).json();
    if (!quiet) {
      $('save-hint').textContent = T('已保存') + ' ' + new Date().toLocaleTimeString();
      toast(T('设置已保存'));
      renderProviders();
      renderLlm();
      renderCommon();
      // 保存成功后回「我的」：设置改完就是回主流程，别让人停在设置页
      setTimeout(() => location.assign('/me'), 600);
    }
    return boot;
  }

  $('btn-test-llm').onclick = async () => {
    const res = $('llm-res');
    const btn = $('btn-test-llm');
    btn.disabled = true; res.className = 'result'; res.textContent = T('请求中…');
    try {
      await save(true);
      const d = await (await fetch('/api/llm/test', { method: 'POST' })).json();
      if (d.ok) { res.className = 'result ok'; res.textContent = `✓ ${d.ms}ms · ${d.reply}`; }
      else { res.className = 'result err'; res.textContent = '✗ ' + d.message; }
    } catch (e) { res.className = 'result err'; res.textContent = '✗ ' + e.message; }
    finally { btn.disabled = false; }
  };

  $('s-language').onchange = () => { renderLevels(); renderProviders(); };
  $('btn-save').onclick = () => save(false).catch(e => toast(e.message, true));
  $('btn-reload').onclick = () => {
    // 放弃修改 = 丢掉未保存的编辑并回「我的」。不做「重载数据留在本页」那种半吊子行为，
    // 免得以为点了之后改动还在。
    if (confirm(T('放弃未保存的修改并返回？'))) {
      location.assign('/me');
    }
  };

  if (window.I18N) I18N.mountSwitch($('lang-host'));

  (async () => {
    boot = await (await fetch('/api/bootstrap')).json();
    renderCommon();
    renderProviders();
    renderLlm();
  })().catch(e => toast('加载失败: ' + e.message, true));
})();
