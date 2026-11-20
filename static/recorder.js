const MAX_BATCH_BYTES = 2 * 1024 * 1024;

async function writeChunk(onChunk, buffer, startOffset) {
  const delays = [250, 1000, 2000];
  for (let attempt = 0; ; attempt++) {
    const controller = new AbortController();
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new Error("音声の保存がタイムアウトしました。ローカルサーバーを確認してください。"));
      }, 20000);
    });
    let retry = false;
    let endOffset;
    try {
      endOffset = await Promise.race([onChunk(buffer, controller.signal, startOffset), timeout]);
    } catch (error) {
      // 同じ位置・同じデータで再送する。応答だけが失われても二重に書き込まれない。
      const retryable = controller.signal.aborted || error instanceof TypeError ||
        error?.status === 408 || error?.status === 429 || error?.status >= 500;
      if (!retryable || attempt >= delays.length) throw error;
      retry = true;
    } finally {
      clearTimeout(timer);
    }
    if (retry) {
      await new Promise(resolve => setTimeout(resolve, delays[attempt]));
      continue;
    }
    if (endOffset !== startOffset + buffer.byteLength) throw new Error("録音データの保存位置を確認できません。");
    return endOffset;
  }
}

function nextBatch(queue) {
  let count = 1;
  let size = queue[0].byteLength;
  while (count < queue.length && size + queue[count].byteLength <= MAX_BATCH_BYTES) {
    size += queue[count++].byteLength;
  }
  if (count === 1) return queue.shift();
  const batch = new Uint8Array(size);
  let offset = 0;
  for (const block of queue.splice(0, count)) {
    batch.set(new Uint8Array(block), offset);
    offset += block.byteLength;
  }
  return batch.buffer;
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

  async start(onStart, onChunk, onSamples) {
    if (this.phase !== "idle") throw new Error("録音の開始または停止処理が進行中です。");
    this.phase = "starting";
    this.failure = null;
    this.writeFailed = false;
    this.queue = [];
    this.writes = null;
    this.savedBytes = 0;
    this.starting = this.open(onStart, onChunk, onSamples);
    try {
      await this.starting;
    } catch (error) {
      await this.release();
      this.phase = "idle";
      throw error;
    }
  }

  async open(onStart, onChunk, onSamples) {
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
      else if (data === "overrun") this.fail(new Error("未保存の音声が一時バッファの上限に達したため、マイク録音を中断しました。保存済みの範囲は会議履歴から確認できます。"));
      else if (data instanceof ArrayBuffer && !this.writeFailed) {
        this.queue.push(data);
        if (!this.writes) this.writes = this.drain(onChunk);
        onSamples?.(data);
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

  async drain(onChunk) {
    try {
      while (this.queue.length) {
        const buffer = nextBatch(this.queue);
        this.savedBytes = await writeChunk(onChunk, buffer, this.savedBytes);
        this.node.port.postMessage({savedBytes: this.savedBytes});
      }
    } catch (error) {
      this.writeFailed = true;
      this.queue.length = 0;
      this.fail(error);
    } finally {
      this.writes = null;
    }
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
