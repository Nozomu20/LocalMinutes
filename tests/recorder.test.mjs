import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import vm from "node:vm";

const workletSource = readFileSync(new URL("../static/pcm-worklet.js", import.meta.url), "utf8");

// Chromium と同じ process/MessagePort を駆動し、実時間を待たずに保存の遅延を再現する。
function makeWorklet(sampleRate) {
  const messages = [];
  class AudioWorkletProcessor {
    port = {postMessage: value => messages.push(value)};
  }
  let Processor;
  vm.runInNewContext(workletSource, {
    AudioWorkletProcessor, sampleRate,
    registerProcessor(name, implementation) {
      assert.equal(name, "local-minutes-pcm");
      Processor = implementation;
    },
  });
  const processor = new Processor();
  return {
    messages,
    record(samples) {
      for (let i = 0; i < samples.length; i += 128) {
        processor.process([[samples.subarray(i, i + 128)]]);
      }
    },
    post(data) {processor.port.onmessage({data});},
  };
}

test("保存確認が20秒遅れても、最後の端数までPCMの順番と値を維持する", () => {
  const rate = 8000, mic = makeWorklet(rate);
  const input = Float32Array.from({length: rate * 20 + 131}, (_, i) => (i % 71 - 35) / 35);
  mic.record(input);
  assert.ok(!mic.messages.includes("overrun"));
  mic.post("flush");
  assert.equal(mic.messages.at(-1), "flushed");
  const pcm = Buffer.concat(mic.messages.slice(0, -1).map(bytes => Buffer.from(bytes)));
  assert.equal(pcm.length, input.length * 2);
  for (let i = 0; i < input.length; i++) {
    const value = input[i];
    assert.equal(pcm.readInt16LE(i * 2), Math.round(value * (value < 0 ? 32768 : 32767)));
  }
});

test("保存確認が届く限り長く録音でき、途絶えたときはバッファを制限する", () => {
  const rate = 8000, mic = makeWorklet(rate);
  const second = new Float32Array(rate);
  let saved = 0;
  for (let seconds = 0; seconds < 135; seconds++) {
    mic.record(second);
    for (const buffer of mic.messages.splice(0)) {
      assert.notEqual(buffer, "overrun");
      saved += buffer.byteLength;
    }
    mic.post({savedBytes: saved});
  }
  assert.equal(saved, rate * 135 * 2);

  for (let seconds = 0; seconds < 135; seconds++) mic.record(second);
  assert.equal(mic.messages.filter(m => m === "overrun").length, 1);
  const buffered = mic.messages.reduce((total, message) => total + (message.byteLength || 0), 0);
  assert.ok(buffered > rate * 10 * 2 && buffered <= rate * 120 * 2);
});
