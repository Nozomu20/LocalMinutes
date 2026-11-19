import { Recorder } from "./recorder.js";
import { speakerEditor, speakerLabel } from "./speakers.js";
const el = id => document.getElementById(id);
const token = location.hash.slice(1) || sessionStorage.getItem("localminutes.session");
if (location.hash) {
  sessionStorage.setItem("localminutes.session", token);
  history.replaceState(null, "", "/");
}
let state, selected = null, recId = null, busy = false, started = 0;
let result = null, resultKey = "", tab = "summary", refreshing = false;
let historyKey = "", startingError = null;
const samples = new Array(75).fill(0);
const phases = { preparing:"録音を準備中", recording:"録音中", importing:"音声を取り込み中", processing:"議事録を作成中", ready:"音声を保存しました", done:"議事録ができました", error:"処理を確認してください" };
const recorder = new Recorder(error => {
  if (busy) startingError = error;
  else finish(error);
});
const stamp = seconds => {
  const s = Math.max(0, Math.floor(seconds));
  return [Math.floor(s/3600), Math.floor(s/60)%60, s%60].slice(s >= 3600 ? 0 : 1).map(n => String(n).padStart(2,"0")).join(":");
};
const day = date => new Date(date).toLocaleDateString("ja-JP", {year:"numeric",month:"long",day:"numeric",weekday:"short"});
function notify(message="") {
  el("notice").textContent = message;
  el("notice").hidden = !message;
}
async function api(path, options={}) {
  const response = await fetch("/api/"+path, {...options, cache:"no-store",
    headers:{Authorization:"Bearer "+token, ...options.headers}});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "端末内のサーバーで処理できませんでした。");
  return data;
}
const post = (path,data) => api(path,{method:"POST",body:JSON.stringify(data)});
function node(tag, text, cls="") {
  const item = document.createElement(tag);
  item.textContent = text;
  if (cls) item.className = cls;
  return item;
}
function draw() {
  const canvas = el("wave"), width = canvas.clientWidth;
  if (!width) return;
  canvas.width = width * devicePixelRatio;
  canvas.height = 70 * devicePixelRatio;
  const c = canvas.getContext("2d");
  c.scale(devicePixelRatio,devicePixelRatio);
  c.strokeStyle="#628d6a"; c.lineWidth=2; c.lineCap="round"; c.beginPath();
  samples.forEach((v,i) => { const x=(i+.5)*width/samples.length, a=Math.min(30,1+v*90);
    c.moveTo(x,35-a); c.lineTo(x,35+a); });
  c.stroke();
}
function addSamples(buffer) {
  const data = new Int16Array(buffer);
  let peak = 0;
  for (let n=0;n<5;n++) {
    const a=Math.floor(n*data.length/5), b=Math.floor((n+1)*data.length/5);
    let sq=0;
    for (let i=a;i<b;i++) {
      const amplitude = data[i]/32768;
      sq += amplitude**2;
      peak = Math.max(peak, Math.abs(amplitude));
    }
    samples.shift(); samples.push(Math.sqrt(sq/Math.max(1,b-a)));
  }
  el("micLevel").value=Math.sqrt(peak);
  draw();
}
function viewResult() {
  el("tabSummary").setAttribute("aria-pressed",String(tab==="summary"));
  el("tabText").setAttribute("aria-pressed",String(tab==="text"));
  const area=el("content"); area.replaceChildren();
  const actions=el("speakerActions"); actions.replaceChildren();
  if (result?.segments.length && state?.active!==selected) {
    const key=selected, revision=result.revision;
    actions.append(speakerEditor(result.segments,result.names,async edits=>{
      await post("speakers",{id:key,revision,...edits});
      resultKey="";
      await refresh();
    }));
  }
  el("export").hidden = !result;
  if (!result) {
    area.append(node("p","作成した議事録はここに表示されます。","empty"));
  } else if (tab==="text") {
    if (!result.segments.length) area.append(node("p","認識できた発言はありませんでした。"));
    for (const s of result.segments) {
      const row=node("div","","segment");
      const seek=node("button",stamp(s.start),"seek");
      seek.title="この発言から再生";
      seek.onclick=async()=>{
        const player=el("player");
        try {player.currentTime=s.start;await player.play();}
        catch {notify("このブラウザで音声を再生できません。元の音声は会議の保存先にあります。");}
      };
      const body=node("div","");
      body.append(node("strong",speakerLabel(s.speaker,result.names),"attribution"),node("span",s.text));
      row.append(seek,body); area.append(row);
    }
  } else {
    for (const line of (result.summary || "要約を作成しています。").split("\n")) {
      if (!line.trim()) continue;
      if (line.startsWith("### ")) area.append(node("h3",line.slice(4)));
      else if (line.startsWith("## ")) area.append(node("h2",line.slice(3)));
      else if (line.startsWith("- ")) area.append(node("p","• "+line.slice(2),"bullet"));
      else area.append(node("p",line,"md"));
    }
  }
}
function render() {
  if (!state) return;
  const item = state.meetings.find(m=>m.id===selected);
  const player=el("player");
  if (item?.playback !== (player.getAttribute("src") || undefined)) {
    player.pause();
    if (item?.playback) player.src=item.playback;
    else player.removeAttribute("src");
    player.load();
  }
  player.hidden=!item?.playback;
  const key = JSON.stringify([selected,state.meetings.map(m=>[m.id,m.title,m.status])]);
  if (historyKey !== key) {
    historyKey = key;
    const nav=el("history"); nav.replaceChildren();
    if (!state.meetings.length) nav.append(node("p","保存した会議はありません","empty"));
    for (const m of state.meetings) {
      const button=node("button","","history"); button.setAttribute("aria-current",String(m.id===selected));
      button.append(node("b",m.title),node("small",new Date(m.date).toLocaleDateString("ja-JP")+"　·　"+phases[m.status]));
      button.onclick=()=>{if (!busy && !recId) select(m.id);}; nav.append(button);
    }
  }
  el("heading").textContent=item?.title || "新しい会議";
  el("date").textContent=day(item?.date || new Date());
  el("compose").hidden=Boolean(item); el("meeting").hidden=!item;
  el("new").disabled=busy || Boolean(recId);
  el("import").disabled=busy || Boolean(state.active) || Boolean(recId);
  el("title").disabled=busy || Boolean(recId);
  el("count").disabled=busy || Boolean(recId);
  el("mode").disabled=busy || Boolean(recId);
  el("record").disabled=busy || Boolean(state.active && !recId);
  el("record").textContent=recId?"■　録音を停止":"●　録音を開始";
  const online=el("mode").value==="online";
  el("inputs").hidden=!online;
  el("wave").hidden=online;
  const capture=state.capture;
  if (online) {
    el("pcLevel").value=recId && capture ? Math.sqrt(capture.peak) : 0;
    if (!recId) el("micLevel").value=0;
    const status=recId && capture
      ? capture.error || (capture.heard ? "PC音声の入力を確認しました" : "相手の声が聞こえたら、メーターが動くことを確認してください") + "　·　"+capture.device
      : "イヤホンに流れる会議音声を録ります。PCの通知音なども録音に含まれます。";
    if (el("pcInfo").textContent!==status) el("pcInfo").textContent=status;
  }
  el("micStatus").textContent=online ? (recId?"オンライン会議を録音中":"オンライン会議の録音") : (recId?"マイク録音中":"マイク録音");
  el("recHint").textContent=recId?"この画面を開いたまま録音してください":(online && busy?"音声入力の準備中です。OSの許可画面が出た場合は確認してください。":"停止すると議事録を作成します");
  if (item) {
    el("phase").textContent=phases[item.status]+(item.duration?"　·　"+stamp(item.duration):"");
    el("detail").textContent=[item.detail,item.captureWarning,item.speakerWarning].filter(Boolean).join(" ");
    el("retry").hidden=Boolean(state.active) || item.captureFailed || !["error","ready"].includes(item.status);
  }
}
async function refresh() {
  if (refreshing) return;
  refreshing=true;
  try {
    state=await api("state");
    render();
    const m=state.meetings.find(v=>v.id===selected);
    const key=m && (m.hasTranscript||m.hasSummary)?[m.id,m.hasTranscript,m.hasSummary,m.revision||0,m.status].join("/"):"";
    if (key && key!==resultKey) {
      const data=await api("result/"+m.id);
      if (selected===m.id) { result=data; resultKey=key; viewResult(); }
    }
  } catch(error) { notify("ローカルサーバーに接続できません。"+error.message); }
  finally {refreshing=false;}
}
function select(id) {
  selected=id; result=null; resultKey=""; tab="summary"; notify();
  viewResult(); render(); refresh();
  if (!id) {el("title").value=""; el("title").focus(); draw();}
}
async function begin() {
  busy=true; startingError=null; notify(); render(); samples.fill(0); draw();
  try {
    await recorder.start(async rate => {
      el("recHint").textContent=el("mode").value==="online"?"PC音声の入力を準備中です。OSの許可画面が出た場合は確認してください。":"マイクを準備しています";
      recId=(await post("start",{title:el("title").value,rate,count:Number(el("count").value),mode:el("mode").value})).id;
      started=Date.now();
    },async (buffer,signal) => {
      addSamples(buffer);
      await api("chunk/"+recId,{method:"POST",body:buffer,signal});
    });
    if (startingError) throw startingError;
  } catch(error) {
    if (recId) {
      try { await recorder.stop(); } catch {}
      try { await post("stop",{id:recId,interrupted:true}); } catch {}
      recId=null;
    }
    notify(error.message);
  } finally {busy=false;render();refresh();}
}
async function finish(error) {
  if (busy || !recId) return;
  busy=true; render();
  const id=recId;
  try {await recorder.stop();} catch(e) {error=e;}
  try {await post("stop",{id,interrupted:Boolean(error)});} catch(e){error=error||e;}
  recId=null; started=0; busy=false; el("timer").textContent="00:00";
  select(id); if(error) notify(error.message);
}
el("record").onclick=()=>recId?finish():begin();
el("mode").onchange=()=>{render();draw();};
el("new").onclick=()=>select(null);
el("import").onclick=()=>el("file").click();
el("file").onchange=async e=>{
  const file=e.target.files[0]; if(!file)return;
  e.target.value="";
  if (!file.size || file.size>2*1024**3) {notify("空のファイル、または2 GiBを超えるファイルです。");return;}
  const ext=file.name.match(/\.[^.]+$/)?.[0].toLowerCase() || "";
  const title=el("title").value.trim() || file.name.slice(0,file.name.length-ext.length);
  notify("音声をこの端末内に取り込んでいます。");busy=true;render();
  try{
    const m=await api("import",{method:"POST",headers:{"X-Extension":ext,"X-Title":encodeURIComponent(title.slice(0,120)),"X-Speaker-Count":el("count").value},body:file});
    select(m.id);
  }catch(e){notify(e.message);}
  finally{busy=false;render();refresh();}
};
el("retry").onclick=async()=>{
  el("retry").disabled=true;
  try {await post("process",{id:selected}); await refresh();}catch(e){notify(e.message);}
  finally{el("retry").disabled=false;}
};
el("tabText").onclick=()=>{tab="text";viewResult();};
el("tabSummary").onclick=()=>{tab="summary";viewResult();};
el("export").onclick=()=>{
  const m=state.meetings.find(v=>v.id===selected);
  if(!m||!result)return;
  const escapeText=value=>value.replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replace(/[\\*_\[\]`|~]/g,"\\$&");
  const text=["# "+escapeText(m.title), day(m.date),result.summary,"## 文字起こし",
    ...result.segments.map(s=>"- ("+stamp(s.start)+")【"+escapeText(speakerLabel(s.speaker,result.names))+"】 "+escapeText(s.text))].join("\n\n");
  const url=URL.createObjectURL(new Blob([text],{type:"text/markdown;charset=utf-8"}));
  const a=document.createElement("a");a.href=url;
  a.download=m.title.replace(/[<>:"/\\|?*\x00-\x1f]/g,"_").slice(0,80)+".md";
  a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);
};
el("settings").onclick=()=>{
  if(!state)return;
  el("model").value=state.settings.model;el("gguf").value=state.settings.gguf;
  el("storage").textContent=state.settings.storage;el("settingsError").textContent="";
  el("dialog").showModal();
};
el("cancelSettings").onclick=()=>el("dialog").close();
el("saveSettings").onclick=async()=>{
  try{await post("settings",{model:el("model").value.trim(),gguf:el("gguf").value.trim()});el("dialog").close();notify();await refresh();}
  catch(e){el("settingsError").textContent=e.message;}
};
window.addEventListener("beforeunload",e=>{if(recId||busy){e.preventDefault();e.returnValue="";}});
window.addEventListener("pagehide",()=>{
  if(recId) api("stop",{method:"POST",keepalive:true,body:JSON.stringify({id:recId,interrupted:true})}).catch(()=>{});
});
window.addEventListener("resize",draw);
await refresh();viewResult();draw();
if(state&&!state.settings.ready)notify("Whisperモデルの準備が必要です。ローカル設定で .pt ファイルを指定してください。");
setInterval(refresh,1500);
setInterval(()=>{if(started)el("timer").textContent=stamp((Date.now()-started)/1000);},250);
