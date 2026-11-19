// 0.5秒単位でモノラルPCMを渡す。録音データをスピーカーへ出力しない。
class PcmRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.blockSize = Math.round(sampleRate / 2);
    this.buffer = new Int16Array(this.blockSize);
    this.used = 0;
    this.pending = 0;
    this.recording = true;
    this.port.onmessage = ({ data }) => {
      if (data === "ack") this.pending--;
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
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    this.pending++;
    this.buffer = new Int16Array(this.blockSize);
    this.used = 0;
  }

  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!this.recording || !samples) return true;
    for (const sample of samples) {
      const value = Math.max(-1, Math.min(1, sample));
      this.buffer[this.used++] = Math.round(value * (value < 0 ? 32768 : 32767));
      if (this.used === this.blockSize) {
        this.sendBlock();
        // 10秒分の未保存が続いたら停止し、受信側で既に届けた範囲を保存する。
        if (this.pending >= 20) {
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
