"use strict";

const statusEl = document.querySelector("#status");
const userText = document.querySelector("#userText");
const assistantText = document.querySelector("#assistantText");
const connectButton = document.querySelector("#connectButton");
const micButton = document.querySelector("#micButton");
const interruptButton = document.querySelector("#interruptButton");
const textForm = document.querySelector("#textForm");
const textInput = document.querySelector("#textInput");
const sendButton = document.querySelector("#sendButton");
const hint = document.querySelector("#hint");
const answerSource = document.querySelector("#answerSource");
const hintReport = document.querySelector("#hintReport");
// Preserve the selected voice's natural pitch and timing.
const VOICE_PLAYBACK_RATE = 1.0;

let socket = null;
let ready = false;
let recording = false;
let audioContext = null;
let microphoneStream = null;
let microphoneSource = null;
let captureProcessor = null;
let silentGain = null;
let pcmPending = [];
let nextPlayTime = 0;
let scheduledSources = new Set();
let activeResponseId = "";
let doubaoAnswer = "";

function setStatus(text, state) {
  statusEl.lastChild.textContent = text;
  statusEl.dataset.state = state;
}

function setText(element, text) {
  element.textContent = text || "…";
  element.classList.toggle("placeholder", !text);
}

const SOURCE_LABELS = { doubao: "豆包实时语音", workflow: "问叮当链路" };

function setSource(source) {
  answerSource.textContent = SOURCE_LABELS[source] || "";
  if (source) answerSource.dataset.source = source;
  else delete answerSource.dataset.source;
}

function showHintReport(argumentsValue) {
  hintReport.textContent = `report_hint → ${JSON.stringify(argumentsValue, null, 2)}`;
  hintReport.hidden = false;
}

function clearAnswer(source) {
  doubaoAnswer = "";
  hintReport.hidden = true;
  hintReport.textContent = "";
  setSource(source);
}

function send(frame) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(frame));
}

function bytesToBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function base64ToBytes(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function downsample(input, inputRate) {
  if (inputRate === 16000) return Array.from(input);
  const ratio = inputRate / 16000;
  const result = [];
  const length = Math.floor(input.length / ratio);
  for (let outputIndex = 0; outputIndex < length; outputIndex += 1) {
    const start = Math.floor(outputIndex * ratio);
    const end = Math.max(start + 1, Math.floor((outputIndex + 1) * ratio));
    let total = 0;
    for (let i = start; i < end && i < input.length; i += 1) total += input[i];
    result.push(total / Math.max(1, end - start));
  }
  return result;
}

function sendPcmFrames(samples) {
  pcmPending.push(...samples);
  while (pcmPending.length >= 320) {
    const frame = pcmPending.splice(0, 320);
    const bytes = new Uint8Array(640);
    const view = new DataView(bytes.buffer);
    frame.forEach((sample, index) => {
      const limited = Math.max(-1, Math.min(1, sample));
      view.setInt16(index * 2, limited < 0 ? limited * 32768 : limited * 32767, true);
    });
    send({ type: "audio", audio: bytesToBase64(bytes) });
  }
}

async function startMicrophone() {
  if (!ready || recording) return;
  audioContext ||= new AudioContext();
  await audioContext.resume();
  microphoneStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  microphoneSource = audioContext.createMediaStreamSource(microphoneStream);
  captureProcessor = audioContext.createScriptProcessor(2048, 1, 1);
  silentGain = audioContext.createGain();
  silentGain.gain.value = 0;
  captureProcessor.onaudioprocess = (event) => {
    if (recording) sendPcmFrames(downsample(event.inputBuffer.getChannelData(0), audioContext.sampleRate));
  };
  microphoneSource.connect(captureProcessor);
  captureProcessor.connect(silentGain);
  silentGain.connect(audioContext.destination);
  pcmPending = [];
  recording = true;
  send({ type: "unmute" });
  micButton.classList.add("recording");
  micButton.querySelector("span:last-child").textContent = "停止说话";
  setStatus("正在聆听", "online");
}

function stopMicrophone({ commit = true } = {}) {
  if (!recording && !microphoneStream) return;
  recording = false;
  if (commit) send({ type: "commit" });
  send({ type: "mute" });
  microphoneStream?.getTracks().forEach((track) => track.stop());
  microphoneSource?.disconnect();
  captureProcessor?.disconnect();
  silentGain?.disconnect();
  microphoneStream = microphoneSource = captureProcessor = silentGain = null;
  pcmPending = [];
  micButton.classList.remove("recording");
  micButton.querySelector("span:last-child").textContent = "开始说话";
  if (ready) setStatus("已连接", "online");
}

async function playPcm(encoded) {
  audioContext ||= new AudioContext();
  await audioContext.resume();
  const bytes = base64ToBytes(encoded);
  if (bytes.byteLength % 2 !== 0) throw new Error("Invalid PCM frame");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const buffer = audioContext.createBuffer(1, bytes.byteLength / 2, 24000);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < channel.length; i += 1) channel[i] = view.getInt16(i * 2, true) / 32768;
  const source = audioContext.createBufferSource();
  source.buffer = buffer;
  source.playbackRate.value = VOICE_PLAYBACK_RATE;
  source.connect(audioContext.destination);
  const startAt = Math.max(audioContext.currentTime + 0.025, nextPlayTime);
  source.start(startAt);
  nextPlayTime = startAt + buffer.duration / VOICE_PLAYBACK_RATE;
  scheduledSources.add(source);
  source.onended = () => scheduledSources.delete(source);
}

function clearPlayback() {
  for (const source of scheduledSources) {
    try { source.stop(); } catch (_) { /* already stopped */ }
  }
  scheduledSources.clear();
  nextPlayTime = audioContext?.currentTime || 0;
}

function setControls(enabled) {
  micButton.disabled = !enabled;
  interruptButton.disabled = !enabled;
  textInput.disabled = !enabled;
  sendButton.disabled = !enabled;
}

function acceptProviderEvent(event) {
  const type = String(event.type || "");
  if (type === "voice.connecting") {
    setStatus("连接豆包中", "busy");
  } else if (type === "voice.ready") {
    ready = true;
    setControls(true);
    connectButton.textContent = "断开连接";
    setStatus("已连接", "online");
    hint.textContent = `课程已授权：task ${event.task_id || "?"} · session ${event.session_id || "?"}。点“开始说话”让豆包直接回答。`;
  } else if (type === "voice.error") {
    hint.textContent = `连接失败：${event.code || "UNKNOWN"}`;
    setStatus("连接失败", "error");
  } else if (type.endsWith("input_audio_transcription.started")) {
    setText(userText, "");
  } else if (type.endsWith("input_audio_transcription.delta")) {
    setText(userText, (userText.classList.contains("placeholder") ? "" : userText.textContent) + (event.text || ""));
  } else if (type.endsWith("input_audio_transcription.completed")) {
    if (event.text) setText(userText, event.text);
    clearAnswer("doubao");
    setText(assistantText, "");
    setStatus("豆包正在回答", "busy");
  } else if (type === "response.output_text.delta") {
    doubaoAnswer += event.text || "";
    setSource("doubao");
    setText(assistantText, doubaoAnswer);
  } else if (type === "response.output_text.done") {
    if (event.text) doubaoAnswer = event.text;
    setSource("doubao");
    setText(assistantText, doubaoAnswer);
    setStatus("已连接", "online");
  } else if (type === "dingdang.hint_report") {
    showHintReport(event.arguments || {});
  } else if (type === "dingdang.thinking") {
    clearPlayback();
    clearAnswer("workflow");
    setText(assistantText, "叮当正在思考…");
    setStatus("正在问叮当", "busy");
  } else if (type === "dingdang.response") {
    setSource("workflow");
    setText(assistantText, event.text || "");
    setStatus("叮当已回答", "online");
  } else if (type === "dingdang.error") {
    setSource("workflow");
    setText(assistantText, `问叮当失败：${event.code || "UNKNOWN"}`);
    setStatus("问叮当失败", "error");
  } else if (type === "response.output_audio.delta" && event.audio) {
    playPcm(event.audio).catch(() => setStatus("音频播放失败", "error"));
  } else if (type === "response.canceled") {
    clearPlayback();
  }
}

function disconnect() {
  stopMicrophone({ commit: false });
  clearPlayback();
  if (socket?.readyState === WebSocket.OPEN) {
    send({ type: "close" });
    socket.close(1000, "demo closed");
  }
  socket = null;
  ready = false;
  activeResponseId = "";
  clearAnswer("");
  setControls(false);
  connectButton.textContent = "连接服务";
  setStatus("未连接", "offline");
}

async function connect() {
  if (socket) {
    disconnect();
    return;
  }
  // Playback needs an AudioContext, connecting does not. Awaiting resume() here
  // let an autoplay-suspended context block the WebSocket with no visible error,
  // so warm it up without gating the connection; playPcm resumes it again.
  try {
    audioContext ||= new AudioContext();
    void audioContext.resume();
  } catch (_) { /* playback stays unavailable; the session still works */ }
  setStatus("连接本机代理", "busy");
  socket = new WebSocket("ws://127.0.0.1:8765/voice");
  socket.onmessage = (message) => {
    try { acceptProviderEvent(JSON.parse(message.data)); }
    catch (_) { setStatus("收到无效消息", "error"); }
  };
  socket.onerror = () => setStatus("无法连接", "error");
  socket.onclose = () => {
    stopMicrophone({ commit: false });
    clearPlayback();
    socket = null;
    ready = false;
    setControls(false);
    connectButton.textContent = "重新连接";
    if (statusEl.dataset.state !== "error") setStatus("连接已关闭", "offline");
  };
}

connectButton.addEventListener("click", () => connect().catch(() => setStatus("初始化失败", "error")));
micButton.addEventListener("click", () => {
  if (recording) stopMicrophone();
  else startMicrophone().catch(() => {
    hint.textContent = "无法使用麦克风，请在浏览器地址栏允许麦克风权限。";
    setStatus("麦克风不可用", "error");
  });
});
interruptButton.addEventListener("click", () => {
  clearPlayback();
  send({ type: "interrupt" });
});
textForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = textInput.value.trim();
  if (!text || !ready) return;
  setText(userText, text);
  clearAnswer("workflow");
  setText(assistantText, "叮当正在思考…");
  activeResponseId = "";
  send({ type: "ask", text });
  textInput.value = "";
});
window.addEventListener("beforeunload", disconnect);
