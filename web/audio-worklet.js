/**
 * 麦克风采集 worklet。
 * 目标：输出 PCM16 小端 / 单声道 / 16000Hz / 每包 640 字节（20ms）。
 *
 * 为什么不用 ScriptProcessorNode：
 *   它在主线程跑，页面一卡就断音、爆音；AudioWorklet 在音频线程，稳定。
 *
 * 重采样：浏览器 AudioContext 若是 48000/44100，这里用"块内均值抽稀"降到 16k。
 * 语音场景够用（不需要高保真抗混叠），而且比任何 JS 库都省事。
 */
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;   // sampleRate 是 worklet 全局变量
    this.acc = 0;
    this.accN = 0;
    this.out = new Int16Array(320);    // 320 samples = 640 bytes = 20ms@16k
    this.fill = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const ch = input[0];

    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i];
      this.accN++;
      if (this.accN >= this.ratio) {
        let s = this.acc / this.accN;
        this.acc = 0;
        this.accN = 0;
        if (s > 1) s = 1; else if (s < -1) s = -1;
        this.out[this.fill++] = s < 0 ? (s * 0x8000) | 0 : (s * 0x7fff) | 0;
        if (this.fill === this.out.length) {
          const copy = this.out.slice(0);
          this.port.postMessage(copy.buffer, [copy.buffer]);
          this.fill = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('capture-processor', CaptureProcessor);
