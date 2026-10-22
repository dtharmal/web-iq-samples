// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

// Area averaging preserves fractional sample boundaries across render quanta.
class PCMResampler {
  constructor(inputRate, outputRate = 24000) {
    this.ratio = inputRate / outputRate;
    this.weight = 0;
    this.sum = 0;
  }
  process(input) {
    const output = [];
    for (const sample of input) {
      let remaining = 1;
      while (remaining > 1e-9) {
        const take = Math.min(remaining, this.ratio - this.weight);
        this.sum += sample * take;
        this.weight += take;
        remaining -= take;
        if (this.weight >= this.ratio - 1e-9) {
          const value = Math.max(-1, Math.min(1, this.sum / this.ratio));
          output.push(Math.round(value * (value < 0 ? 32768 : 32767)));
          this.sum = 0;
          this.weight = 0;
        }
      }
    }
    return output;
  }
}
if (typeof registerProcessor !== "undefined") {
  class PCMCapture extends AudioWorkletProcessor {
    constructor() {
      super();
      this.resampler = new PCMResampler(sampleRate);
      this.frame = new Int16Array(480);
      this.offset = 0;
      this.muted = false;
      this.port.onmessage = (event) => {
        this.muted = Boolean(event.data.muted);
        this.offset = 0;
        this.resampler = new PCMResampler(sampleRate);
      };
    }
    process(inputs) {
      const channel = inputs[0]?.[0];
      if (!channel || this.muted) return true;
      for (const value of this.resampler.process(channel)) {
        this.frame[this.offset++] = value;
        if (this.offset === this.frame.length) {
          this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
          this.frame = new Int16Array(480);
          this.offset = 0;
        }
      }
      return true;
    }
  }
  registerProcessor("pcm-capture", PCMCapture);
}
export { PCMResampler };
