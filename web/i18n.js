/* SayIt · 界面语言（母语）切换
 *
 * 两个语言概念要分清，别混：
 *   1. 练习语言 —— 通话页「语言」那一排（en / ja …），存在服务端 settings，决定 AI 说什么。
 *   2. 界面语言 —— 这里的 ui_lang（zh / en），是"我的母语"，决定页面文案和复盘解释用什么语言。
 *
 * 实现取巧但可靠：以中文原文为 key 做替换。HTML 不用埋一堆 key，
 * 动态渲染出来的片段也能被 apply() 兜住（MutationObserver 自动跟）。
 * 存了原文，切回中文能原样还原。
 */
(() => {
  const LS_KEY = 'sayit.ui_lang';
  const SUPPORTED = ['zh', 'en'];

  // 中文原文 → English
  const EN = {
    // ---------------- 通用 / 导航 ----------------
    '通话': 'Call',
    '记录': 'History',
    '错题本': 'Mistakes',
    '我的': 'Me',
    '设置': 'Settings',
    '关闭': 'Close',
    '保存': 'Save',
    '加载中…': 'Loading…',
    '加载失败：': 'Failed to load: ',

    // ---------------- 通话页 ----------------
    'SayIt · 口语陪练': 'SayIt · Speaking practice',
    '场景': 'Scenario',
    '语言': 'Language',
    '待机': 'Idle',
    '通话中': 'In call',
    '上次通话': 'Last call',
    '通话详情': 'Call details',
    '第 {n} / {m} 页 · 共 {t} 通': 'Page {n} / {m} · {t} calls',
    '{n} 条': '{n} items',
    '{n} 句': '{n} lines',
    '{n} 轮': '{n} turns',
    '{a} 秒': '{a}s',
    '{a} 分 {b} 秒': '{a}m {b}s',
    '{n} 通': '{n} calls',
    '次': 'calls',
    '已保存': 'Saved',
    '刷新': 'Refresh',
    '● 通话中': '● In call',
    '正在接通…': 'Connecting…',
    '拿不到麦克风权限': 'Microphone blocked',
    '连接失败': 'Connection failed',
    '已结束': 'Ended',
    '开始通话': 'Start call',
    '挂断': 'Hang up',
    '直接开口说英文，不用按任何键': 'Just start speaking — no button to press',
    '你一说话，它会立刻停下来听': 'It stops talking as soon as you speak',
    '挂断后自动生成复盘和错题': 'A review and mistake list appear when you hang up',
    '点这里看逐句记录和复盘': 'Tap to see the transcript and review',
    '上次通话': 'Last call',
    '轮': 'turns',
    '这通电话复盘': 'Call review',
    '正在生成…': 'Generating…',
    '正在分析你的表达…': 'Analysing your speech…',
    'AI 正在逐句看你的表达，通常 10~20 秒': 'Reading through your lines — usually 10–20 seconds',
    '看错题本': 'Open mistakes',
    '再打一通': 'Call again',
    '估计水平': 'Estimated level',
    '处可改进': 'things to improve',
    '可以更地道的地方': 'Where you could sound more natural',
    '这通没挑出明显错误，继续保持。': 'No obvious mistakes this time — keep it up.',
    '顺手记住': 'Worth remembering',
    '下一通的重点': 'Focus for next call',
    '语法': 'Grammar',
    '时态': 'Tense',
    '用词': 'Word choice',
    '语序': 'Word order',
    '发音': 'Pronunciation',
    '流利度': 'Fluency',
    '流利': 'Fluency',
    '词汇': 'Vocabulary',
    '你说': 'You said',
    '地道': 'Natural',
    '你': 'You',
    '出错': 'Something went wrong',
    '正在分析你的表达…': 'Analysing your speech…',
    '重新生成': 'Try again',
    '去错题本看看': 'Go to mistakes',

    // ---------------- 续聊 ----------------
    '继续这通电话': 'Continue this call',
    '继续通话': 'Continue call',
    '再练一次': 'Practise again',
    '接着上次没聊完的地方继续说': 'Pick up where you left off',
    '这通电话没有留下对话，没法接着聊': 'This call left no transcript, so there is nothing to continue',
    '这通没有对话内容，没法接着聊': 'No conversation in that call — nothing to continue',
    '上次聊到这儿': 'Where you left off',
    '接着聊': 'Next',
    '接着 {when} 那通聊': 'Continuing the call from {when}',
    '点击下方按钮，接着上次聊': 'Tap the button below to pick up where you left off',
    '正在接着上次的话题…': 'Picking up where you left off…',
    '（接着上次聊到的地方…）': '(Picking up where you left off…)',
    '后来接着聊过 {n} 次': 'Continued {n} time(s) later',
    '看后面那次': 'See the later call',
    '续接的通话': 'Continued from an earlier call',
    '读不到要续聊的那通电话，就当新的一次开始。':
      "Couldn't load that call — starting a fresh one instead.",
    '单次通话时长': 'Call time limit',
    '分钟，0 = 不限时': 'minutes, 0 = no limit',
    '到这里会自动挂断，之后可以在记录里点「继续这通电话」接着聊。':
      'The call ends automatically at this point. You can pick it up later from History.',

    // ---------------- 删除 ----------------
    '删除': 'Delete',
    '清空生词本': 'Clear all words',
    '清空错题本': 'Clear all mistakes',
    '确定删除这条通话记录？': 'Delete this call?',
    '这通的字幕、复盘和错题会一起删掉，删了找不回来。':
      'Its transcript, review and mistakes will be deleted too. This cannot be undone.',
    '这通的复盘和错题也会一起删掉；后面接着聊的那 {n} 次不会删。':
      'Its review and mistakes go with it. The {n} later call(s) that continued from it stay.',
    '确认删除': 'Delete',
    '不删': 'Keep',
    '删除中…': 'Deleting…',
    '删除失败，再试一次': 'Delete failed — try again',
    '清空失败，再试一次': 'Could not clear — try again',
    '从生词本删除': 'Remove from wordbook',
    '移出错题本': 'Remove from mistakes',
    '还没学到规则。': 'No learned rules yet.',
    '已经移出 {n} 条错题，同类再多删一条就能归纳出规则。':
      '{n} correction(s) removed — one more of the same kind and a rule can be learned.',
    '在下面删掉不认同的错题，同类删两次之后这里会出现规则。':
      'Remove corrections you disagree with below; after two of the same kind a rule appears here.',
    '现在就归纳一次': 'Learn from my removals now',
    '归纳中…': 'Learning…',
    '删掉这条规则': 'Delete this rule',
    '已帮你挡掉 {n} 条': 'Blocked {n} correction(s)',
    '规则已生效': 'Rule active',
    '单次通话最长': 'Call limit',

    // 提示 / toast
    '浏览器拒绝了麦克风权限，或当前页面不是 https / localhost。':
      'The browser blocked the microphone, or this page is not served over https / localhost.',
    '已静音（AI 还会继续说完）': 'Muted (the AI will finish its sentence)',
    '麦克风已开': 'Microphone on',
    '还剩 1 分钟，准备收尾': 'One minute left — start wrapping up',
    '时间到，结束这通电话': "Time's up, ending the call",
    '读不到 /api/bootstrap，服务端可能没起来：':
      'Cannot reach /api/bootstrap — the server may be down: ',
    '加载配置失败': 'Failed to load config',
    '正在接通…': 'Connecting…',

    // ---------------- 记录页 ----------------
    'SayIt · 通话记录': 'SayIt · Call history',
    '通话记录': 'Call history',
    '句': 'lines',
    '秒': 's',
    '这通没挑出明显错误。': 'No obvious mistakes in this call.',
    '这通没有识别到你的语音，无法复盘。': 'No speech was recognised, so there is nothing to review.',
    '生成复盘（约 15 秒）': 'Generate review (~15s)',
    '生成中…': 'Generating…',
    '生成失败': 'Generation failed',
    '这通电话没有留下文本。': 'This call left no transcript.',
    '再练一次': 'Practise again',
    '返回列表': 'Back to list',
    '还没有通话记录。': 'No calls yet.',
    '去「通话」页打第一通吧。': 'Head to the Call tab and make your first one.',
    '上一页': 'Prev',
    '下一页': 'Next',
    '共': 'of',
    '通': 'calls',
    '页': '',

    // ---------------- 语速 / 主题 ----------------
    '语速': 'Speed',
    'AI 的说话速度（整通电话固定）': 'How fast the AI talks (fixed for the whole call)',
    '语速已设为 {v}': 'Speed set to {v}',
    '正在切换语速…': 'Switching speed…',
    '最慢': 'Slowest',
    '慢': 'Slow',
    '正常': 'Normal',
    '快': 'Fast',
    '主题': 'Theme',
    '深色': 'Dark',
    '浅色': 'Light',
    '跟随系统': 'System',
    '外观': 'Appearance',
    '深色适合晚上躺在床上练，浅色在白天光线强时更清楚。':
      'Dark for evening practice, light when the room is bright.',
    '切换深浅色': 'Toggle light / dark',
    '语速（整通电话固定）': 'Speed (fixed for the call)',

    // ---------------- 重听 ----------------
    '重听': 'Replay',
    '播放中…': 'Playing…',
    '重新播放这一段': 'Play this part again',
    '播放失败': 'Playback failed',
    '语音已更新为所选语速': 'Voice now uses the selected speed',

    // ---------------- 词卡 / 生词本 ----------------
    'SayIt · 生词本': 'SayIt · Wordbook',
    '生词本': 'Wordbook',
    '顺手记住的词': 'Words worth remembering',
    '顺手记住的词单独一页': 'Saved words now have their own page',
    '每通电话挂断后，复盘里标出的生词会自动进这里。点开一个词看音标、释义、例句和词形变化。':
      'Every call ends with a review; the words it flags land here. Tap one for phonetics, meanings, examples and inflections.',
    '打开生词本': 'Open the wordbook',
    '音标 · 释义 · 例句 · 词形变化': 'Phonetics · meanings · examples · inflections',
    '复盘里「顺手记住」的词，点开看音标 / 例句 / 词形变化':
      'Words saved from reviews — tap one for phonetics, examples and inflections',
    '点开看音标、例句和词形变化': 'Tap for phonetics, examples and word forms',
    '词卡': 'Word card',
    '释义': 'Meaning',
    '例句': 'Examples',
    '词形变化': 'Word forms',
    '读单词': 'Say the word',
    '读例句': 'Say an example',
    '重新生成': 'Regenerate',
    '换个来源重查': 'Look up again',
    '数据来源': 'Source',
    '免费词典，不消耗 token': 'free dictionary, no tokens',
    '免费开源词典': 'free open dictionary',
    '词典没收录才用模型，会消耗少量 token': 'model used only because no dictionary had it',
    '生词卡': 'Word cards',
    '免费词典优先': 'Dictionaries first',
    '词典没收录时用文本模型兜底': 'Fall back to the text model',
    '允许兜底': 'Allowed',
    '不兜底（完全不花 token）': 'Never (no tokens at all)',
    '正在生成词卡…': 'Building the word card…',
    '词卡生成失败': 'Could not build the word card',
    '重试': 'Retry',
    '查一个词，例如 receive / break the ice': 'Look up a word, e.g. receive',
    '查词': 'Look up',
    '{n} 个': '{n} words',
    '生词总数': 'Words',
    '已生成词卡': 'Cards built',
    '待生成': 'Not built yet',
    '生词本还是空的。': 'Your wordbook is empty.',
    '打完一通电话并生成复盘后，复盘里「顺手记住」的词会自动出现在这里。':
      'Finish a call and let the review run — the words it picks show up here.',
    '把「{w}」移出生词本？': 'Remove “{w}” from the wordbook?',
    '移出生词本': 'Remove',
    '完成': 'Done',
    '听一遍': 'Listen',

    // ---------------- 错题本 ----------------
    'SayIt · 错题本': 'SayIt · Mistakes',
    '错误类型分布': 'Mistake types',
    '全部': 'All',
    '你说错过的句子': "Sentences you got wrong",
    '下次通话时，AI 会自然地把这些场景再造一遍，让你把正确的说法真正用出去。':
      'Next call, the AI recreates these situations so you actually use the correct phrasing.',
    '通话次数': 'Calls',
    '累计时长': 'Total time',
    '复盘报告': 'Reviews',
    '累计错误': 'Mistakes',
    '分': 'min',
    '还没有错误记录，打完一通电话并生成复盘后会出现。':
      'No mistakes recorded yet — they appear after a call with a generated review.',
    '错题本是空的。': 'Your mistake list is empty.',
    '条': 'items',

    // ---------------- 我的 ----------------
    'SayIt · 我的': 'SayIt · Me',
    '你的画像': 'Your profile',
    '自动算的，每次通话前会告诉 AI': 'Computed automatically, fed to the AI before every call',
    '关于你自己': 'About you',
    '写点 AI 推不出来的事，比如「我下周有个英语面试」。这句会直接影响它怎么跟你聊。':
      'Write things the AI cannot infer — e.g. "I have an English interview next week." It directly shapes how it talks to you.',
    '更多': 'More',
    '模型与语音设置': 'Models & voice',
    '换供应商 / 音色 / 复盘用的文本模型': 'Switch provider / voice / the text model used for reviews',
    '实测水平': 'Measured level',
    '累计通话': 'Calls',
    '最近常犯': 'Recent weak spots',
    '最近聊过': 'Recent topics',
    '上次留下的重点': 'Last focus',
    '目标': 'target',
    '打几通后评估': 'Estimate after a few calls',
    '还不够数据': 'Not enough data yet',
    '还没积累': 'Nothing yet',
    '已保存，下一通电话生效': 'Saved — applies from your next call',
    '界面语言': 'Interface language',
    '中文': '中文',

    // ---------------- 设置页 ----------------
    'SayIt · 设置': 'SayIt · Settings',
    '默认通话设置': 'Default call settings',
    '水平（CEFR）': 'Level (CEFR)',
    '默认场景': 'Default scenario',
    'AI 名字（留空用默认）': 'AI name (leave empty for default)',
    '语音模型': 'Voice model',
    '选中的就是通话时用的模型。换供应商只改这里，代码不用动。':
      'The selected one is used for calls. Switching providers only touches this screen.',
    '复盘用文本模型': 'Text model for reviews',
    '挂断后生成复盘报告用的 LLM，任何 OpenAI 兼容端点都行（DeepSeek / 方舟 / 百炼 / OpenAI）。':
      'The LLM that writes your review after a call. Any OpenAI-compatible endpoint works.',
    '文本模型': 'Text model',
    '模型': 'Model',
    '启用复盘': 'Enable reviews',
    '开启': 'On',
    '关闭': 'Off',
    '测试文本模型': 'Test text model',
    '没配 Key 也能正常通话、看字幕、记错题，只是挂断后不出复盘报告。':
      'Without a key you can still call, read captions and collect mistakes — you just get no review.',
    '放弃修改': 'Discard changes',
    'Key 已配置': 'Key set',
    '未配 Key': 'No key',
    '未联调': 'Not wired up',
    '（该语言暂无音色）': '(no voice for this language yet)',
    '端点（一般不用改）': 'Endpoint (rarely changed)',
    '粘贴 API Key': 'Paste API key',
    '测试连接': 'Test connection',
    '连接中…': 'Connecting…',
    '请求中…': 'Requesting…',
    '设置已保存': 'Settings saved',
    '已恢复为已保存的配置': 'Reverted to the saved settings',
    '加载失败': 'Failed to load',
    'registry-hint': 'Languages and scenarios are registry-driven: add a language in <code>app/languages.py</code>, a scenario in <code>app/scenarios.py</code>, and the UI picks it up automatically.',
    '音色': 'Voice',
  };

  const ATTRS = ['placeholder', 'title', 'aria-label'];

  function detect() {
    const saved = localStorage.getItem(LS_KEY);
    if (SUPPORTED.includes(saved)) return saved;
    const nav = (navigator.language || '').toLowerCase();
    return nav.startsWith('zh') ? 'zh' : 'en';
  }

  let lang = detect();
  const subs = [];

  /** 取某个中文串在当前界面语言下的写法 */
  function t(zh, vars) {
    let out = lang === 'zh' ? zh : (EN[zh] || zh);
    if (vars) {
      for (const k of Object.keys(vars)) {
        out = out.split('{' + k + '}').join(vars[k]);
      }
    }
    return out;
  }

  const hasCJK = (s) => /[\u4e00-\u9fff]/.test(s);

  /** 把一个子树里的中文文案就地换成当前语言（存原文，可还原） */
  function apply(root) {
    root = root || document;
    if (!root) return;
    // 带 data-i18n 的整段（含 <code> 这类内联标签）单独替换 innerHTML
    const blocks = root.querySelectorAll ? root.querySelectorAll('[data-i18n]') : [];
    for (const el of blocks) {
      const key = el.getAttribute('data-i18n');
      if (el.__zhhtml === undefined) el.__zhhtml = el.innerHTML;
      const want = lang === 'en' ? (EN[key] || el.__zhhtml) : el.__zhhtml;
      if (el.innerHTML !== want) el.innerHTML = want;   // 只在真的变了才写，避免 MutationObserver 自激
    }

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    const nodes = [];
    let n;
    while ((n = walker.nextNode())) nodes.push(n);
    for (const node of nodes) {
      const p = node.parentElement;
      if (!p) continue;
      if (/^(SCRIPT|STYLE|CODE|PRE|TEXTAREA)$/.test(p.tagName)) continue;
      if (p.closest && p.closest('[data-i18n]')) continue;   // 整段已经换过了，别再逐句替换
      if (node.__zh === undefined) node.__zh = node.nodeValue;
      const src = node.__zh;
      const key = (src || '').trim();
      if (!key || !hasCJK(key)) continue;
      node.nodeValue = lang === 'zh' ? src : src.replace(key, EN[key] || key);
    }
    const els = root.querySelectorAll ? root.querySelectorAll('[placeholder],[title],[aria-label]') : [];
    for (const el of els) {
      for (const a of ATTRS) {
        if (!el.hasAttribute(a)) continue;
        const store = '__zh_' + a;
        if (el[store] === undefined) el[store] = el.getAttribute(a);
        const src = el[store] || '';
        const key = src.trim();
        if (!key || !hasCJK(key)) continue;
        el.setAttribute(a, lang === 'zh' ? src : src.replace(key, EN[key] || key));
      }
    }
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
  }

  /** 动态内容也会被自动翻译：监听 DOM 变化，防抖后再跑一遍 */
  let timer = null;
  function watch() {
    if (typeof MutationObserver === 'undefined') return;
    const mo = new MutationObserver(() => {
      if (lang === 'zh') return;
      clearTimeout(timer);
      timer = setTimeout(() => apply(document.body), 40);
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }

  function setLang(l) {
    if (!SUPPORTED.includes(l)) return;
    lang = l;
    localStorage.setItem(LS_KEY, l);
    apply(document);
    for (const fn of subs) { try { fn(lang); } catch (e) { } }
  }

  /** 中 / EN 两段式开关，插到任意容器里 */
  function mountSwitch(host) {
    if (!host) return null;
    const box = document.createElement('div');
    box.className = 'langswitch';
    box.innerHTML = `<button type="button" data-l="zh">中文</button><button type="button" data-l="en">EN</button>`;
    const paint = () => box.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.l === lang));
    box.querySelectorAll('button').forEach(b => {
      b.onclick = () => setLang(b.dataset.l);
    });
    host.appendChild(box);
    paint();
    subs.push(paint);
    apply(box);
    return box;
  }

  window.I18N = {
    get lang() { return lang; },
    t, apply, setLang, mountSwitch,
    onChange(fn) { subs.push(fn); },
  };

  // 尽早跑一次，避免中文闪一下
  const boot = () => { apply(document); watch(); };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
