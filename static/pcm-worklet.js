// 1秒単位でモノラルPCMを渡す。出力チャンネルは無音のままにする。
class PcmRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.blockSize = Math.round(sampleRate);
    this.buffer = new Int16Array(this.blockSize);
    this.used = 0;
    this.sentBytes = 0;
    this.savedBytes = 0;
    // タブが一時的に重くなっても入力を保持し、高サンプルレートでのメモリ使用も制限する。
    this.maxPendingBlocks = Math.min(120, Math.floor(16 * 1024 * 1024 / (2 * this.blockSize)));
    this.recording = true;
    this.port.onmessage = ({ data }) => {
      if (Number.isSafeInteger(data?.savedBytes) && data.savedBytes > this.savedBytes &&
          data.savedBytes <= this.sentBytes) this.savedBytes = data.savedBytes;
      if (data === "flush") {
        this.recording = false;
        this.sendBlock();
        this.port.postMessage("flushed");
      }
    };
  }

  sendBlock() {
    if (!this.used) return;
    const pcm = this.used === this.blockSize ? this.buffer : this.buffer.slice(0, this.used);
    this.sentBytes += pcm.byteLength;
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    this.buffer = new Int16Array(this.blockSize);
    this.used = 0;
  }

  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!this.recording || !samples) return true;
    for (let i = 0; i < samples.length; i++) {
      const value = Math.max(-1, Math.min(1, samples[i]));
      this.buffer[this.used++] = Math.round(value * (value < 0 ? 32768 : 32767));
      if (this.used === this.blockSize) {
        this.sendBlock();
        if (this.sentBytes - this.savedBytes >= this.maxPendingBlocks * 2 * this.blockSize) {
          this.recording = false;
          this.port.postMessage("overrun");
          break;
        }
      }
    }
    return true;
  }
}

registerProcessor("local-minutes-pcm", PcmRecorder);
