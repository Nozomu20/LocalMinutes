async function writeChunk(onChunk, buffer) {
  const controller = new AbortController();
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error("音声の保存がタイムアウトしました。ローカルサーバーを確認してください。"));
    }, 15000);
  });
  try {
    await Promise.race([onChunk(buffer, controller.signal), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

async function openMicrophone() {
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    throw new Error("マイク入力を利用できません。起動時に表示されたローカルURLを対応ブラウザで開いてください。");
  }
  try {
    return await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 }, video: false });
  } catch (error) {
    if (error.name === "NotAllowedError") {
      throw new Error("マイクの使用が許可されていません。ブラウザとOSのマイク設定を確認してください。");
    }
    if (error.name === "NotFoundError") throw new Error("マイクが見つかりません。入力デバイスを接続してください。");
    throw new Error("マイクを開けませんでした。入力デバイスの状態を確認してください。" + error.message);
  }
}

export class Recorder {
  constructor(onFailure) {
    this.onFailure = onFailure;
    this.phase = "idle";
  }

  async start(onStart, onChunk) {
    if (this.phase !== "idle") throw new Error("録音の開始または停止処理が進行中です。");
    this.phase = "starting";
    this.failure = null;
    this.writeFailed = false;
    this.writes = Promise.resolve();
    this.starting = this.open(onStart, onChunk);
    try {
      await this.starting;
    } catch (error) {
      await this.release();
      this.phase = "idle";
      throw error;
    }
  }

  async open(onStart, onChunk) {
    if (!window.AudioContext) throw new Error("このブラウザは録音に対応していません。");
    this.context = new AudioContext();
    // ユーザーのクリック中にAudioContextを再開する。
    const resumed = this.context.resume();
    resumed.catch(() => {});
    this.stream = await openMicrophone();
    await resumed;
    await this.context.audioWorklet.addModule(new URL("./pcm-worklet.js", import.meta.url));
    this.source = this.context.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.context, "local-minutes-pcm", {
      channelCount: 1, channelCountMode: "explicit", outputChannelCount: [1],
    });
    this.node.port.onmessage = ({ data }) => {
      if (data === "flushed") this.flushed?.();
      else if (data === "overrun") this.fail(new Error("音声の保存が追いつかず、録音を中断しました。"));
      else if (data instanceof ArrayBuffer) {
        this.writes = this.writes.then(async () => {
          if (this.writeFailed) return;
          try {
            await writeChunk(onChunk, data);
            this.node?.port.postMessage("ack");
          } catch (error) {
            this.writeFailed = true;
            this.fail(error);
          }
        });
      }
    };
    const track = this.stream.getAudioTracks()[0];
    if (track.readyState !== "live") throw new Error("マイクの接続が失われました。");
    this.node.onprocessorerror = () => this.fail(new Error("ブラウザの音声処理が停止しました。"));
    track.onended = () => this.fail(new Error("マイクが切断されました。"));
    this.context.onstatechange = () => {
      if (this.phase === "recording" && this.context.state !== "running") {
        this.fail(new Error("ブラウザのマイク入力が中断されました。"));
      }
    };
    await onStart(this.context.sampleRate);
    this.phase = "recording";
    if (this.failure) throw this.failure;
    this.node.connect(this.context.destination);
    this.source.connect(this.node);
  }

  fail(error) {
    if (this.failure) return;
    this.failure = error instanceof Error ? error : new Error(String(error));
    if (this.phase === "recording") this.onFailure(this.failure);
  }

  async stop() {
    if (this.phase === "starting") await this.starting;
    if (this.phase === "stopping") return this.stopping;
    if (this.phase !== "recording") return;
    this.phase = "stopping";
    this.stopping = this.finish();
    return this.stopping;
  }

  async finish() {
    const flushed = new Promise((resolve) => { this.flushed = resolve; });
    const timer = setTimeout(() => {
      this.fail(new Error("録音の末尾を取得できませんでした。"));
      this.flushed();
    }, 3000);
    try {
      this.node.port.postMessage("flush");
      await flushed;
      clearTimeout(timer);
      this.stream.getTracks().forEach((track) => track.stop());
      await this.writes;
    } finally {
      clearTimeout(timer);
      this.flushed = null;
      await this.release();
      this.phase = "idle";
    }
    if (this.failure) throw this.failure;
  }

  async release() {
    this.stream?.getTracks().forEach((track) => {
      track.onended = null;
      track.stop();
    });
    this.source?.disconnect();
    this.node?.disconnect();
    this.node?.port.close();
    if (this.context) {
      this.context.onstatechange = null;
      await this.context.close().catch(() => {});
    }
    this.source = this.node = this.stream = this.context = null;
  }
}
