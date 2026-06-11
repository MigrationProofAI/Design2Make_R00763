import React, { useState, useEffect, useRef, useCallback } from "react";
import ReactMarkdown from "react-markdown";

const newId = () => (crypto.randomUUID ? crypto.randomUUID().slice(0, 8) : Math.random().toString(36).slice(2, 10));
const ts = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const STRUCT = {
  search: "LlmAgent · read-only tools",
  create_change: "Sequential · Intake → ValidateEnrich → Writer",
  make: "LlmAgent · make tools (PIR/cost/BOM/routing)",
  genesis: "LlmAgent (vision) → run_genesis",
  planning: "LlmAgent → remote MRP (RFC)",
  boardroom: "Conductor → Parallel critics → Chair",
  validate: "Rule engine · deterministic",
};
const blobToB64 = (blob) => new Promise((res) => {
  const r = new FileReader(); r.onloadend = () => res(String(r.result).split(",")[1] || ""); r.readAsDataURL(blob);
});
function parseVerdicts(txt) {
  if (!/\b(ACCEPT|ALTERNATE|ESCALATE)\s*\|/.test(txt)) return [];
  const out = [];
  txt.replace(/\s*(ACCEPT|ALTERNATE|ESCALATE)\s*\|/g, "\n$1 |").split("\n").forEach((line) => {
    const m = line.match(/^(ACCEPT|ALTERNATE|ESCALATE)\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|\s*(.*)$/);
    if (m) out.push({ disp: m[1], component: m[2].trim(), severity: m[3].trim(), issue: m[4].trim(), evidence: m[5].trim() });
  });
  return out;
}
// which tool results become Structured Data CARDS (exploration dumps stay out -- Activity only)
const CARD_KINDS = new Set(["genesis", "pir", "cost", "bom", "routing"]);
const CARD_TOOLS = new Set(["get_material", "build_material_payload", "create_material", "run_mrp",
  "semantic_search", "find_duplicates", "search_materials", "create_demand", "assure_assembly"]);
function isCard(tool, p) {
  if (!p || typeof p !== "object") return false;
  if (!Array.isArray(p) && CARD_KINDS.has(p.kind)) return true;
  return CARD_TOOLS.has(tool);
}
// reconstruct a card from a recorded trace step (live cards travel via "data"; reload/replay use this)
function cardFromStep(s) {
  if (s.card) return { tool: s.tool, payload: s.card };          // persisted @@DATA@@ / big payloads
  if (s.kind === "tool_result" && typeof s.result === "string") {
    const t = s.result.trim();
    if (t[0] === "{" || t[0] === "[") {
      try { const p = JSON.parse(t); if (isCard(s.tool, p)) return { tool: s.tool, payload: p }; } catch (e) {}
    }
  }
  return null;
}

export default function App() {
  const [sessionId, setSessionId] = useState(() => {
    const s = localStorage.getItem("d2m.session") || newId(); localStorage.setItem("d2m.session", s); return s;
  });
  const [theme, setTheme] = useState(() => localStorage.getItem("d2m.theme") || "light");
  const [messages, setMessages] = useState([]);
  const [activity, setActivity] = useState([]);
  const [data, setData] = useState([]);
  const [working, setWorking] = useState(false);
  const [workLabel, setWorkLabel] = useState("working…");
  const [connected, setConnected] = useState(false);
  const [input, setInput] = useState("");
  const [img, setImg] = useState(null);            // {mime, data, preview}
  const [recording, setRecording] = useState(false);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [touring, setTouring] = useState(false);
  const [tourCap, setTourCap] = useState(null);    // {label, text}
  const [hi, setHi] = useState(null);              // highlighted pane id during tour
  const [spot, setSpot] = useState(null);          // spotlighted ELEMENT (data-spot id) during narration
  const [gate, setGate] = useState(null);          // confirm-gate {summary, intent}
  const [demoSess, setDemoSess] = useState(() => localStorage.getItem("d2m.demoSession") || "");  // pinned demo
  const [recScreen, setRecScreen] = useState(false);   // screen recording active
  const [recSecs, setRecSecs] = useState(0);           // elapsed recording seconds

  const wsRef = useRef(null), chatEndRef = useRef(null), reconnectRef = useRef(0), inputRef = useRef(null);
  const mediaRef = useRef(null), tourRef = useRef({ audio: null, resolve: null }), tourActiveRef = useRef(false);
  const screenRef = useRef({ mr: null, chunks: [], stream: null, timer: null });

  const addMsg = useCallback((m) => setMessages((xs) => [...xs, { ...m, ts: ts() }]), []);
  const addStatus = useCallback((text) => setMessages((xs) => [...xs, { role: "status", text, ts: ts() }]), []);

  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem("d2m.theme", theme); }, [theme]);
  // spotlight: glow exactly one tagged element ([data-spot="…"]) — decoupled from where it lives
  useEffect(() => {
    document.querySelectorAll(".spot-on").forEach((el) => el.classList.remove("spot-on"));
    if (spot) { const el = document.querySelector(`[data-spot="${spot}"]`); if (el) el.classList.add("spot-on"); }
  }, [spot]);

  const _resolveTurnRef = useRef(null);
  const connect = useCallback(() => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/${sessionId}`);
    wsRef.current = ws;
    ws.onopen = () => { reconnectRef.current = 0; setConnected(true); };
    ws.onclose = () => { setConnected(false); setWorking(false); if (reconnectRef.current++ < 5) setTimeout(connect, 2500 * reconnectRef.current); };
    ws.onerror = () => setConnected(false);
    ws.onmessage = (ev) => {
      let p; try { p = JSON.parse(ev.data); } catch { return; }
      if (p.type === "status") { setWorking(true); setWorkLabel(p.text || "working…"); }
      else if (p.type === "turn") {
        setWorking(false);
        addMsg({ role: "agent", text: p.answer || "(no answer)", intent: p.intent });
        setActivity((xs) => [...xs, { intent: p.intent, steps: p.trace || [], ts: ts() }]);
        const cards = Array.isArray(p.data) ? p.data : (p.data && p.data.payload != null ? [p.data] : []);
        if (cards.length) setData((xs) => [...xs, ...cards.map((d) => ({ tool: d.tool, payload: d.payload, ts: ts() }))]);
        setGate(p.gate || null);
        playChime((p.trace || []).length);
        _resolveTurnRef.current?.();
      } else if (p.message) { setWorking(false); addMsg({ role: "agent", text: p.message }); _resolveTurnRef.current?.(); }
    };
  }, [sessionId, addMsg]);

  useEffect(() => { connect(); return () => { try { const w = wsRef.current; if (w) { w.onclose = null; w.close(); } } catch (e) {} }; }, [connect]);
  useEffect(() => { loadTranscript(sessionId); /* eslint-disable-next-line */ }, []);
  useEffect(() => { chatEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, working]);

  async function loadTranscript(id) {
    setMessages([]); setActivity([]); setData([]);
    try {
      const turns = await (await fetch("/api/sessions/" + id + "/events")).json();
      setMessages(turns.map((t) => ({ role: t.role === "user" ? "user" : "agent", text: t.text, ts: "" })));
    } catch (e) {}
    try {
      const tr = await (await fetch("/api/sessions/" + id + "/trace")).json();
      const acts = [], dat = [];
      tr.forEach((turn) => {
        acts.push({ intent: turn.intent, steps: turn.steps || [], ts: "" });
        (turn.steps || []).forEach((s) => { const c = cardFromStep(s); if (c) dat.push({ ...c, ts: "" }); });
      });
      setActivity(acts); setData(dat);
    } catch (e) {}
  }
  function switchSession(id) {
    try { const w = wsRef.current; if (w) { w.onclose = null; w.close(); } } catch (e) {}
    localStorage.setItem("d2m.session", id); setSessionId(id); loadTranscript(id); setSessionsOpen(false);
  }

  function send() {
    const text = input.trim();
    if ((!text && !img) || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    const env = { text }; if (img) env.image = { mime: img.mime, data: img.data };
    addMsg({ role: "user", text, image: img?.preview });
    try { wsRef.current.send(JSON.stringify(env)); setWorking(true); setWorkLabel("working…"); } catch (e) {}
    setInput(""); setImg(null);
  }

  function playChime(nSteps) {
    if (!nSteps || nSteps < 2 || localStorage.getItem("d2m.sound") === "off") return;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext; const ctx = new Ctx(); const t0 = ctx.currentTime;
      [880, 1320].forEach((f, i) => { const o = ctx.createOscillator(), g = ctx.createGain(); o.type = "sine"; o.frequency.value = f;
        const t = t0 + i * 0.12; g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.13, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.15);
        o.connect(g); g.connect(ctx.destination); o.start(t); o.stop(t + 0.16); });
    } catch (e) {}
  }
  // ---- confirm gate: the SAME confirmed write whether clicked or spoken; modality is logged ----
  function approveGate(modality) {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    addMsg({ role: "user", text: `✓ confirm creation · ${modality}` });
    wsRef.current.send(JSON.stringify({ text: "confirm creation", modality }));
    setWorking(true); setWorkLabel("creating…"); setGate(null);
  }
  function rejectGate(modality) { addStatus(`Cancelled (${modality}) — nothing was written to SAP.`); setGate(null); }
  function editGate() { setGate(null); inputRef.current?.focus(); }

  async function pickImage(e) {
    const f = e.target.files?.[0]; if (!f) return;
    const data = await blobToB64(f);
    setImg({ mime: f.type || "image/png", data, preview: `data:${f.type};base64,${data}` });
    e.target.value = "";
  }
  async function toggleRecord() {
    if (recording) { try { mediaRef.current?.stop(); } catch (e) {} return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream); const chunks = [];
      mr.ondataavailable = (ev) => chunks.push(ev.data);
      mr.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop()); setRecording(false);
        const blob = new Blob(chunks, { type: mr.mimeType || "audio/webm" });
        const data = await blobToB64(blob);
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          addMsg({ role: "user", text: "🎤 voice message" });
          wsRef.current.send(JSON.stringify({ text: "", audio: { mime: blob.type, data } }));
          setWorking(true); setWorkLabel("transcribing…");
        }
      };
      mediaRef.current = mr; mr.start(); setRecording(true);
    } catch (e) { addStatus("Microphone unavailable: " + e.message); }
  }

  // ---- replay (recorded data, progressive) ----
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function replaySession(id) {
    let turns, trace;
    try { turns = await (await fetch("/api/sessions/" + id + "/events")).json(); trace = await (await fetch("/api/sessions/" + id + "/trace")).json(); }
    catch (e) { addStatus("Replay: could not load that session."); return; }
    if (!turns?.length) { addStatus("Replay: nothing recorded."); return; }
    setSessionsOpen(false); setMessages([]); setActivity([]); setData([]);
    addStatus("▶ Replaying " + id + " — recorded data, not a live run");
    let ti = 0;
    for (const t of turns) {
      if (t.role === "user") { addMsg({ role: "user", text: t.text }); setWorking(true); setWorkLabel("replaying…"); await sleep(700); }
      else {
        const tr = trace[ti++] || {};
        setActivity((xs) => [...xs, { intent: tr.intent, steps: tr.steps || [], ts: ts() }]);
        (tr.steps || []).forEach((s) => { const c = cardFromStep(s); if (c) setData((xs) => [...xs, { ...c, ts: ts() }]); });
        await sleep(400); setWorking(false); addMsg({ role: "agent", text: t.text, intent: tr.intent }); await sleep(850);
      }
    }
    addStatus("▶ Replay complete.");
  }

  // ---- spoken product tour ----
  // base64 mp3 -> an object URL with the CORRECT mime (audio/mpeg, not the bogus audio/mp3 that
  // some browsers -- Edge especially -- refuse to decode). Object URLs play far more reliably than
  // giant data: URIs.
  function b64ToAudioUrl(b64) {
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return URL.createObjectURL(new Blob([bytes], { type: "audio/mpeg" }));
  }
  function noVoiceOnce() {                                   // one quiet hint, not silent confusion
    if (tourRef.current.warned) return;
    tourRef.current.warned = true;
    addStatus("🔇 Voice unavailable — captions only. If you just updated, RESTART the server (so /api/tts is live).");
  }
  // Caption + highlight are already on screen; here we fetch this line's audio (cached server-side)
  // and play it. A single slow/failed clip just falls back to reading-time pacing for THAT line --
  // we only give up on audio after 3 misses IN A ROW (and recover on any success), so one transient
  // blip on a long demo never makes "the voice run out".
  function tourStep(text) {
    return new Promise((resolve) => {
      const ms = Math.max(2800, (text || "").length * 55);
      const done = () => { if (tourRef.current.resolve) { tourRef.current.resolve = null; if (tourRef.current.url) { try { URL.revokeObjectURL(tourRef.current.url); } catch (e) {} tourRef.current.url = null; } tourRef.current.audio = null; resolve(); } };
      tourRef.current.resolve = resolve;
      const playPaced = () => setTimeout(done, ms);
      const miss = () => { tourRef.current.misses = (tourRef.current.misses || 0) + 1; if (tourRef.current.misses >= 3) tourRef.current.audioDead = true; noVoiceOnce(); playPaced(); };
      if (tourRef.current.audioDead) return void playPaced();   // 3 misses in a row -> captions only
      (async () => {
        let b64 = "";
        try {
          const ctrl = new AbortController();
          const t = setTimeout(() => ctrl.abort(), 16000);     // > server's retry budget
          const r = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }), signal: ctrl.signal });
          clearTimeout(t);
          if (r.ok) b64 = (await r.json()).audio_b64 || "";
        } catch (e) {}
        if (!tourRef.current.resolve) return;                   // stopped during the fetch
        if (!b64) return void miss();
        tourRef.current.misses = 0;                             // recovered
        try {
          const url = b64ToAudioUrl(b64); tourRef.current.url = url;
          const a = new Audio(url); tourRef.current.audio = a;
          a.onended = done; a.onerror = miss; a.play().catch(miss);
        } catch (e) { playPaced(); }
      })();
    });
  }
  function stopTour() {
    tourActiveRef.current = false; setTouring(false); setTourCap(null); setHi(null); setSpot(null);
    try { tourRef.current.audio?.pause(); } catch (e) {}
    const r = tourRef.current.resolve; tourRef.current.resolve = null; tourRef.current.audio = null; r?.();
  }
  async function startTour() {
    setTouring(true); tourActiveRef.current = true; tourRef.current.audioDead = false; tourRef.current.warned = false; tourRef.current.misses = 0;
    setTourCap({ label: "Tour", text: "Preparing the tour…" });   // instant feedback during the LLM call
    let sections = [];
    try { sections = (await (await fetch("/api/explain_app", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ focus: data.length ? "the structured data card on the right" : "" }) })).json()).sections || []; } catch (e) {}
    if (!tourActiveRef.current) return;                            // stopped during prep
    if (!sections.length) { setTouring(false); tourActiveRef.current = false; setTourCap(null); addStatus("Tour unavailable."); return; }
    const paneFor = { multimodal: "chat", genesis: "activity", planning: "activity", board: "activity", correction: "data", discipline: "activity" };
    // element to spotlight while each section is narrated (deterministic, by section id)
    const spotFor = { intro: "brand", multimodal: "inputs", genesis: "h-activity", planning: "h-activity", board: "h-activity", correction: "h-data", discipline: "h-activity", panels: "h-chat", cta: "composer" };
    for (const s of sections) {
      if (!tourActiveRef.current) break;
      setHi(paneFor[s.id] || null); setSpot(spotFor[s.id] || null); setTourCap({ label: s.label, text: s.text });
      await tourStep(s.text);
    }
    setHi(null); setSpot(null); setTourCap(null); setTouring(false); tourActiveRef.current = false;
  }

  // ---- GUIDED DEMO: a narrated, step-by-step walk through a REAL recorded session ----
  // For each turn it speaks the actual ask, renders the agent's activity + structured results,
  // then narrates (via /api/explain -> /api/tts) what it did and what came back. Pane highlights
  // follow chat -> activity -> data so the eye lands where the action is. The product, narrating
  // its own real work. Shares the Tour's audio machinery + Stop button.
  async function demoSession(id) {
    let turns, trace;
    try { turns = await (await fetch("/api/sessions/" + id + "/events")).json(); trace = await (await fetch("/api/sessions/" + id + "/trace")).json(); }
    catch (e) { addStatus("Demo: could not load that session."); return; }
    if (!turns?.length) { addStatus("Demo: nothing recorded in " + id + "."); return; }
    setSessionsOpen(false); setMessages([]); setActivity([]); setData([]);
    setTouring(true); tourActiveRef.current = true; tourRef.current.audioDead = false; tourRef.current.warned = false; tourRef.current.misses = 0;
    addStatus("🎬 Guided demo of " + id + " — recorded data, narrated live.");
    setHi("chat"); setTourCap({ label: "Guided demo", text: "A real session of mine — the actual ask, what I did, and what came back." });
    await tourStep("Let me walk you through one of my real sessions, step by step. You'll see the actual request, every action I took, and what came back from SAP.");
    let ti = 0, step = 0;
    for (const t of turns) {
      if (!tourActiveRef.current) break;
      if (t.role === "user") {
        step++; setHi("chat"); setSpot("h-chat"); addMsg({ role: "user", text: t.text });
        setTourCap({ label: `Step ${step} · the ask`, text: t.text });
        await tourStep(t.text.length > 240 ? t.text.slice(0, 237) + "…" : t.text);
      } else {
        const tr = trace[ti++] || {};
        setHi("activity"); setSpot("h-activity");
        setActivity((xs) => [...xs, { intent: tr.intent, steps: tr.steps || [], ts: ts() }]);
        let added = 0;
        (tr.steps || []).forEach((s) => { const c = cardFromStep(s); if (c) { setData((xs) => [...xs, { ...c, ts: ts() }]); added++; } });
        setTourCap({ label: "What I did", text: "Reading back my actions for this step…" });
        let expl = "";
        try { expl = (await (await fetch("/api/explain", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ steps: tr.steps || [], intent: tr.intent }) })).json()).explanation || ""; } catch (e) {}
        if (!tourActiveRef.current) break;
        if (added) { setHi("data"); setSpot("h-data"); }
        setTourCap({ label: added ? "What I did & found" : "What I did", text: expl || t.text });
        await tourStep(expl || "Here is what came back.");
        if (!tourActiveRef.current) break;
        setHi("chat"); setSpot("h-chat"); addMsg({ role: "agent", text: t.text, intent: tr.intent }); await sleep(350);
      }
    }
    const completed = tourActiveRef.current;
    setHi(null); setSpot(null); setTourCap(null); setTouring(false); tourActiveRef.current = false;
    addStatus(completed ? "🎬 Guided demo complete." : "🎬 Demo stopped.");
  }
  function pinDemo(id) {                              // make the showcase deterministic: always walk THIS one
    setDemoSess(id); localStorage.setItem("d2m.demoSession", id);
    addStatus("📌 Pinned " + id + " as the demo session — 🎬 Demo will always walk it.");
  }
  async function startDemo() {                        // pinned session -> current -> richest
    let pick = demoSess || sessionId;
    try {
      const list = await (await fetch("/api/sessions")).json();
      const ids = new Set((list || []).map((s) => s.id));
      if (demoSess && ids.has(demoSess)) { demoSession(demoSess); return; }
      const cur = (list || []).find((s) => s.id === sessionId);
      if (!cur || (cur.turns || 0) < 2) {
        const rich = (list || []).filter((s) => (s.turns || 0) >= 2).sort((a, b) => (b.turns || 0) - (a.turns || 0))[0];
        if (rich) pick = rich.id;
      }
    } catch (e) {}
    demoSession(pick);
  }

  // ---- screen recording: capture the session (tab + narration audio) to a downloadable .webm ----
  async function startScreenRec() {
    if (!navigator.mediaDevices?.getDisplayMedia) { addStatus("Screen recording isn't supported in this browser."); return; }
    let stream;
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 30 }, audio: true });
    } catch (e) { addStatus("Screen recording cancelled."); return; }
    const mime = ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"]
      .find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "video/webm";
    let mr;
    try { mr = new MediaRecorder(stream, { mimeType: mime }); }
    catch (e) { stream.getTracks().forEach((t) => t.stop()); addStatus("Couldn't start the recorder."); return; }
    const chunks = [];
    screenRef.current = { mr, chunks, stream, timer: null };
    mr.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    mr.onstop = () => {
      if (screenRef.current.timer) clearInterval(screenRef.current.timer);
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `design2make-${sessionId}-${ts().replace(/[: ]/g, "")}.webm`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      setRecScreen(false); setRecSecs(0);
      addStatus("⏹ Recording saved — check your downloads (.webm).");
    };
    stream.getVideoTracks()[0].addEventListener("ended", () => { try { mr.stop(); } catch (e) {} });  // native "Stop sharing"
    mr.start(1000);
    setRecScreen(true); setRecSecs(0);
    screenRef.current.timer = setInterval(() => setRecSecs((s) => s + 1), 1000);
    addStatus("⏺ Recording — narrate a tour/demo, then ⏹ Stop to save the video.");
  }
  function stopScreenRec() { try { screenRef.current.mr?.stop(); } catch (e) {} }
  const recClock = `${String(Math.floor(recSecs / 60)).padStart(2, "0")}:${String(recSecs % 60).padStart(2, "0")}`;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand" data-spot="brand"><span className="dot" /> Design2Make <span className="sub">· SAP master-data &amp; planning</span></div>
        <div className="spacer" />
        <span className="sess-code">{sessionId}</span>
        <button className="mini" onClick={() => setSessionsOpen(true)}>🗂 Sessions</button>
        {touring
          ? <button className="mini stop" onClick={stopTour}>⏹ Stop</button>
          : <>
              <button className="mini" onClick={startTour} title="Spoken tour of what Design2Make does">🎙 Tour</button>
              <button className="mini" onClick={startDemo} title="Narrated walk-through of a real recorded session">🎬 Demo</button>
            </>}
        {recScreen
          ? <button className="mini rec-live" onClick={stopScreenRec} title="Stop & save the recording"><span className="rec-dot" /> Rec {recClock} · Stop</button>
          : <button className="mini" onClick={startScreenRec} title="Record the screen (with narration) to a video file">⏺ Record</button>}
        <button className="mini" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} title="Toggle theme">{theme === "dark" ? "☀️" : "🌙"}</button>
        <span className={"chip-conn" + (connected ? " on" : "")}><span className="led" />{connected ? "connected" : "offline"}</span>
      </header>

      <div className="cols">
        <section className={"pane" + (hi === "chat" ? " tour-hi" : "")}>
          <div className="pane-h" data-spot="h-chat">Conversation</div>
          <div className="pane-body">
            {messages.length === 0 && <div className="empty-hint">Ask to search, create, plan, or convene the board.</div>}
            {messages.map((m, i) => <ChatMsg key={i} m={m} />)}
            {gate && <GateCard gate={gate} onApprove={approveGate} onReject={rejectGate} onEdit={editGate} />}
            {working && <div className="thinking"><span className="spin" /> {workLabel}</div>}
            <div ref={chatEndRef} />
          </div>
          <div className="composer">
            {img && <div className="stage-thumb"><img src={img.preview} alt="" /><button onClick={() => setImg(null)}>✕</button></div>}
            <span className="compose-inputs" data-spot="inputs">
              <label className="icon-btn" title="Attach image">🖼<input type="file" accept="image/*" hidden onChange={pickImage} /></label>
              <button className={"icon-btn" + (recording ? " rec" : "")} title="Voice" onClick={toggleRecord}>{recording ? "⏹" : "🎤"}</button>
            </span>
            <input ref={inputRef} data-spot="composer" value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()} placeholder="Message Design2Make…" />
            <button className="send" onClick={send} disabled={!connected}>Send</button>
          </div>
        </section>

        <section className={"pane" + (hi === "activity" ? " tour-hi" : "")}>
          <div className="pane-h" data-spot="h-activity">🔭 Agent Activity</div>
          <div className="pane-body">
            {activity.length === 0 && <div className="empty-hint">The agents' steps appear here.</div>}
            {activity.map((t, i) => <ActivityTurn key={i} turn={t} />)}
          </div>
        </section>

        <section className={"pane" + (hi === "data" ? " tour-hi" : "")}>
          <div className="pane-h" data-spot="h-data">📦 Structured Data</div>
          <div className="pane-body">
            {data.length === 0 && <div className="empty-hint">Typed result cards appear here.</div>}
            {data.map((d, i) => <DataCard key={i} d={d} />)}
          </div>
        </section>
      </div>

      {tourCap && <div className="tour-cap"><b>{tourCap.label}</b> {tourCap.text}</div>}
      {sessionsOpen && <SessionsModal onClose={() => setSessionsOpen(false)} cur={sessionId} pinned={demoSess} onSwitch={switchSession} onReplay={replaySession} onDemo={demoSession} onPin={pinDemo} onNew={() => switchSession(newId())} />}
    </div>
  );
}

function ChatMsg({ m }) {
  if (m.role === "status") return <div className="status-line">{m.text}</div>;
  return (
    <div className={"msg " + m.role}>
      {m.role === "agent" && m.intent && <span className="intent-chip" style={{ background: `var(--route-${m.intent}, #475569)` }}>{m.intent}</span>}
      {m.image && <img className="msg-img" src={m.image} alt="" />}
      <div className="bubble">{m.role === "agent" ? <ReactMarkdown>{m.text}</ReactMarkdown> : m.text}</div>
      {m.ts && <div className="msg-ts">{m.ts}</div>}
    </div>
  );
}

function ActivityTurn({ turn }) {
  const [explain, setExplain] = useState(null);
  const [busy, setBusy] = useState(false);
  async function doExplain() {
    setBusy(true);
    try { const d = await (await fetch("/api/explain", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ intent: turn.intent, steps: turn.steps || [] }) })).json(); setExplain(d.explanation || "(no explanation)"); }
    catch (e) { setExplain("(could not explain)"); } finally { setBusy(false); }
  }
  return (
    <div className="act-turn">
      <span className="act-route" style={{ background: `var(--route-${turn.intent}, #475569)` }}>🧭 {turn.intent || "session"}</span>
      <div className="act-struct">🧠 {STRUCT[turn.intent] || "agent"}</div>
      {(turn.steps || []).map((s, i) => <Step key={i} s={s} />)}
      {(!turn.steps || !turn.steps.length) && <div className="act-text">(no tool calls)</div>}
      {explain && <div className="act-explain">💡 {explain}</div>}
      {!explain && <button className="explain-btn" disabled={busy} onClick={doExplain}>{busy ? "💡 …" : "💡 Explain"}</button>}
    </div>
  );
}

function Step({ s }) {
  if (s.kind === "tool_call") {
    const args = Object.entries(s.args || {}).map(([k, v]) => `${k}=${v}`).join(", ");
    return <div className="act-call"><span className="who">{s.author}</span><code>{s.tool}({args})</code></div>;
  }
  if (s.kind === "tool_result") {
    if (s.lines && s.lines.length)
      return <div><div className="act-result">↳ <b>{s.tool}</b>{s.dur != null ? ` · ${s.dur}s` : ""}</div>{s.lines.map((ln, i) => <div key={i} className="act-sub">• {ln}</div>)}</div>;
    return <div className="act-result">↳ {String(s.result || "")}{s.dur != null ? ` · ${s.dur}s` : ""}</div>;
  }
  if (s.kind === "text") {
    const t = (s.text || "").trim();
    const verdicts = parseVerdicts(t);
    if (verdicts.length) return <div>{s.author && <div className="act-text"><span className="who">{s.author}</span></div>}{verdicts.map((v, i) => (
      <div key={i} className={"verdict v-" + v.disp.toLowerCase()}><span className="v-badge">{v.disp}</span><span className="v-comp">{v.component}</span><span className="v-sev">{v.severity}</span>{v.issue}{v.evidence && <span className="v-ev"> — {v.evidence}</span>}</div>
    ))}</div>;
    if (t.startsWith("💭")) return <div className="act-thought">{t}</div>;
    if (t.startsWith("✦")) return <div className="act-reflect">{t}</div>;
    return <div className="act-text">{s.author && <span className="who">{s.author}</span>}{t}</div>;
  }
  return null;
}

function DataCard({ d }) {
  const typed = renderTyped(d.tool, d.payload);
  return <div className="data-card"><div className="data-tool">{d.tool}{d.ts ? " · " + d.ts : ""}</div>{typed || <pre className="data-json">{JSON.stringify(d.payload, null, 2)}</pre>}</div>;
}
function renderTyped(tool, p) {
  if (!p || typeof p !== "object") return null;
  if (p.kind === "genesis") return <GenesisCard p={p} />;
  if (p.kind === "pir") return <PirCard p={p} />;
  if (p.kind === "cost") return <CostCard p={p} />;
  if (p.kind === "bom") return <BomCard p={p} />;
  if (p.kind === "routing") return <RoutingCard p={p} />;
  if (tool === "assure_assembly" && p.facts) return <AssuranceCard p={p} />;
  if (tool === "find_duplicates") return <DupCard p={p} />;
  if (tool === "semantic_search" && Array.isArray(p.results)) return <SemanticCard p={p} />;
  if (tool === "search_materials" && (p.materials || p.applied_filters)) return <SearchCard p={p} />;
  if (tool === "run_mrp" || p.planned_orders || p.purchase_reqs) return <MrpCard p={p} />;
  if (tool === "get_material" && p.d) return <MaterialCard d={p.d} />;
  if (tool === "build_material_payload" && p.fields) return <MaterialDraftCard p={p} />;
  if (tool === "create_demand" && p.sales_order) return <DemandCard p={p} />;
  return null;
}
const fmtDate = (s) => { if (!s) return ""; const m = String(s).match(/^(\d{4})(\d{2})(\d{2})/); return m ? `${m[1]}-${m[2]}-${m[3]}` : String(s); };
const sapDate = (v) => { const m = String(v || "").match(/\d{13}/); if (!m) return ""; try { return new Date(+m[0]).toISOString().slice(0, 10); } catch (e) { return ""; } };
const isBought = (t) => /HAWA|ROH|VERP/.test(t || "");
const pct = (s) => Math.max(0, Math.min(100, Math.round((s || 0) * 100)));
function ScoreRow({ product, desc, score, dupAt = 0.92 }) {
  const dup = score >= dupAt;
  return (
    <div className="tc-row score">
      <span className="tc-mat">{product}</span>
      <span className="tc-desc">{desc}</span>
      <span className="bar"><span className={"bar-fill" + (dup ? " dup" : "")} style={{ width: pct(score) + "%" }} /></span>
      <span className={"score-n" + (dup ? " dup" : "")}>{pct(score)}%</span>
    </div>
  );
}
function ActTag({ a }) {
  const label = a === "dedup-reuse" ? "reuse·dup" : a;
  return <span className={"g-act " + (a || "")}>{label}</span>;
}
function DupPill({ dd }) {
  const top = dd && dd.candidates && dd.candidates[0];
  if (!top) return null;
  return <span className={"dup-pill" + (dd.is_duplicate ? " hot" : "")} title={top.Description}>{top.Product} · {pct(top.score)}%</span>;
}
function GenesisCard({ p }) {
  const comps = p.components || [];
  const created = comps.filter((c) => c.action === "created").length + (p.parent?.action === "created" ? 1 : 0);
  const reused = comps.filter((c) => c.action === "dedup-reuse" || c.action === "exists").length;
  const dups = comps.filter((c) => c.action === "dedup-reuse" || (c.dedup && c.dedup.is_duplicate)).length + (p.parent?.action === "dedup-reuse" ? 1 : 0);
  const disc = p.discipline || {};
  return (
    <div className="tc">
      <div className={"tc-banner " + (p.mode === "preview" ? "rev" : "ok")}>
        {p.mode === "preview" ? "Genesis preview — nothing written yet" : `Genesis complete · ${created} created · ${reused} reused`}
      </div>
      <div className="tc-mline"><b>🧬 {p.parent ? p.parent.description : "components-only run"}</b>{p.parent && p.parent.material ? ` · ${p.parent.material}` : ""} · plant {p.plant} · {comps.length} components</div>
      <div className="tc-tiles">
        <div className="tc-tile"><div className="n">{created}</div><div className="l">create</div></div>
        <div className="tc-tile rev"><div className="n">{reused}</div><div className="l">reuse</div></div>
        <div className="tc-tile warn"><div className="n">{dups}</div><div className="l">dups caught</div></div>
        <div className="tc-tile"><div className="n">{disc.confidence != null ? disc.confidence : "—"}</div><div className="l">confidence</div></div>
      </div>
      {p.parent && <><div className="tc-sec">Parent (FERT)</div>
        <div className="tc-rows"><div className="tc-row">
          <span className="tc-mat">{p.parent.material || "new"}</span>
          <span className="tc-desc">{p.parent.description}</span>
          <ActTag a={p.parent.action} /><DupPill dd={p.parent.dedup} />
        </div></div></>}
      <div className="tc-sec">Components</div>
      <div className="tc-rows">{comps.map((c, i) => (
        <div key={i} className={"tc-row" + (c.action === "failed" ? " exc" : "")}>
          <span className="tc-mat">{c.material || "new"}</span>
          <span className="tc-desc">{c.description}{c.quantity != null ? ` ×${c.quantity}` : ""}</span>
          <span className={"tc-tag " + (isBought(c.type) ? "bought" : "made")}>{c.type}</span>
          <ActTag a={c.action} /><DupPill dd={c.dedup} />
          {c.pir && <span className={"chip-st " + c.pir.status}>PIR</span>}
          {c.cost && <span className={"chip-st " + c.cost.status}>cost{c.price != null ? ` ${c.price}` : ""}</span>}
        </div>))}</div>
      {(p.bom || p.routing || p.production_version) && <><div className="tc-sec">Assembly</div>
        <div className="tc-rows">
          {p.bom && <div className="tc-row"><span className="tc-mat">BOM</span><span className="tc-desc">{p.bom.components} components</span><span className={"chip-st " + p.bom.status}>{p.bom.status}</span></div>}
          {p.routing && <div className="tc-row"><span className="tc-mat">Routing</span><span className="tc-desc">{(p.routing.operations || []).map((o) => o.work_center).join(" → ")}</span><span className={"chip-st " + p.routing.status}>{p.routing.status}</span></div>}
          {p.production_version && <div className="tc-row"><span className="tc-mat">Prod Ver {p.production_version.version}</span><span className="tc-desc">BOM alt {p.production_version.bom_alt}/use {p.production_version.bom_usage} · lot {p.production_version.lot}</span><span className={"chip-st " + p.production_version.status}>{p.production_version.status}</span></div>}
        </div></>}
      {disc.verdict && <div className={"tc-disc" + (disc.verdict === "ESCALATE" ? " exc" : "")}>🛡 {disc.verdict} · conf {disc.confidence} · {disc.writes} writes{disc.escalations && disc.escalations.length ? ` · ⚠ ${disc.escalations.length} escalation(s)` : ""}</div>}
    </div>
  );
}
function SemanticCard({ p }) {
  const res = p.results || [], sc = p.scope || {};
  const scoped = sc.from_material || sc.to_material;
  return (
    <div className="tc">
      <div className="tc-banner ok">Semantic · “{p.query}” · {res.length} by meaning</div>
      {scoped && <div className="tc-mline">scope {sc.from_material || "…"}–{sc.to_material || "…"}</div>}
      <div className="tc-rows">{res.map((r, i) => <ScoreRow key={i} product={r.Product} desc={r.Description} score={r.score} />)}</div>
    </div>
  );
}
function DupCard({ p }) {
  const cands = p.candidates || [];
  return (
    <div className="tc">
      <div className={"tc-banner " + (p.is_duplicate ? "warn" : "ok")}>
        {p.is_duplicate ? `Duplicate — ${p.match.Product} · ${pct(p.match.score)}%` : "No duplicate above threshold — clear to create"}
      </div>
      <div className="tc-mline">“{p.description}” · threshold {pct(p.threshold)}%</div>
      <div className="tc-rows">{cands.map((r, i) => <ScoreRow key={i} product={r.Product} desc={r.Description} score={r.score} dupAt={p.threshold || 0.92} />)}</div>
    </div>
  );
}
function SearchCard({ p }) {
  const mats = p.materials || [], f = p.applied_filters || {};
  return (
    <div className="tc">
      <div className="tc-banner ok">Search · {p.total_matches != null ? p.total_matches : mats.length} match{p.total_matches === 1 ? "" : "es"}{p.returned != null ? ` · ${p.returned} shown` : ""}</div>
      <div className="tc-chips">{Object.entries(f).map(([k, v]) => v ? <span key={k} className="f-chip">{k}: {String(v)}</span> : null)}</div>
      {mats.length > 0 ? <div className="tc-rows">{mats.slice(0, 30).map((m, i) => (
        <div key={i} className="tc-row">
          <span className="tc-mat">{m.Product || m.Material || m.product}</span>
          <span className="tc-desc">{m.Description || m.ProductDescription || m.description || ""}</span>
          {m.ProductType && <span className={"tc-tag " + (isBought(m.ProductType) ? "bought" : "made")}>{m.ProductType}</span>}
        </div>))}</div> : <div className="tc-empty">No materials matched.</div>}
      {(p.warnings || []).length > 0 && <div className="tc-warn">⚠ {p.warnings[0]}</div>}
    </div>
  );
}
function MrpCard({ p }) {
  const po = p.planned_orders || [], pr = p.purchase_reqs || [], msg = (p.return || {}).message || "";
  return (
    <div className="tc">
      <div className="tc-banner ok">MRP · {po.length} planned order{po.length === 1 ? "" : "s"} · {pr.length} purchase req{pr.length === 1 ? "" : "s"}</div>
      {msg && <div className="tc-mline">{msg}</div>}
      {po.length > 0 && <><div className="tc-sec">Planned orders</div>
        <div className="tc-rows">{po.slice(0, 20).map((o, i) => (
          <div key={i} className="tc-row"><span className="tc-mat">{o.PLNUM}</span><span className="tc-desc">qty {o.GSMNG} {o.MEINS}</span><span className="tc-date">{fmtDate(o.PSTTR)}→{fmtDate(o.PEDTR)}</span></div>))}</div></>}
      {pr.length > 0 && <><div className="tc-sec">Purchase requisitions</div>
        <div className="tc-rows">{pr.slice(0, 20).map((r, i) => (
          <div key={i} className="tc-row"><span className="tc-mat">{r.BANFN || r.PreqNo || "PR"}</span><span className="tc-desc">{r.MATNR || r.Material || ""} qty {r.MENGE || r.Quantity || ""}</span><span className="tc-date">{fmtDate(r.LFDAT || r.DeliveryDate)}</span></div>))}</div></>}
    </div>
  );
}
function MaterialCard({ d }) {
  const desc = ((d.to_Description || {}).results || [{}])[0].ProductDescription;
  const wt = (v, u) => (v && +v ? `${(+v).toFixed(3)} ${u || ""}`.trim() : null);
  const yn = (v) => (v === true || v === "X" ? "yes" : v === false || v === "" ? "no" : null);
  return (
    <div className="tc">
      <div className="tc-banner ok">Material {d.Product} · {d.ProductType}</div>
      <KV rows={[
        ["Description", desc],
        ["Type", `${d.ProductType}${d.BaseUnit ? ` · ${d.BaseUnit}` : ""}`],
        ["Group", [d.ProductGroup && `grp ${d.ProductGroup}`, d.Division && `div ${d.Division}`, d.ItemCategoryGroup].filter(Boolean).join(" · ")],
        ["Industry", d.IndustrySector],
        ["Gross / Net wt", [wt(d.GrossWeight, d.WeightUnit), wt(d.NetWeight, d.WeightUnit)].filter(Boolean).join(" / ")],
        ["Volume", wt(d.MaterialVolume, d.VolumeUnit)],
        ["Configurable", yn(d.IsConfigurableProduct)],
        ["Batch managed", yn(d.IsBatchManagementRequired)],
        ["Deletion flag", yn(d.IsMarkedForDeletion)],
        ["Created", d.CreatedByUser ? `${d.CreatedByUser}${sapDate(d.CreationDate) ? ` · ${sapDate(d.CreationDate)}` : ""}` : null],
      ]} />
    </div>
  );
}
function MaterialDraftCard({ p }) {
  const f = p.fields || {}, desc = ((f.to_Description || {}).results || [{}])[0].ProductDescription;
  return (
    <div className="tc">
      <div className="tc-banner rev">Material to create · {f.ProductType}</div>
      <div className="tc-rows">
        {desc && <div className="tc-row"><span className="tc-mat">Desc</span><span className="tc-desc">{desc}</span></div>}
        <div className="tc-row"><span className="tc-mat">Spec</span><span className="tc-desc">{f.ProductType}{f.BaseUnit ? ` · ${f.BaseUnit}` : ""}{f.ProductGroup ? ` · grp ${f.ProductGroup}` : ""}{f.Division ? ` · div ${f.Division}` : ""}</span></div>
      </div>
    </div>
  );
}
function DemandCard({ p }) {
  return <div className="tc"><div className="tc-banner ok">Demand created · sales order {p.sales_order}</div></div>;
}
function MakeHead({ status, children }) {
  const label = status === "written" ? "written to SAP" : status === "read" ? "current" : "preview — not written";
  return <div className={"tc-banner " + (status === "preview" ? "rev" : "ok")}>{children} · {label}</div>;
}
function KV({ rows }) {
  return <div className="tc-rows">{rows.filter((r) => r[1] != null && r[1] !== "").map((r, i) => (
    <div key={i} className="tc-row"><span className="tc-mat">{r[0]}</span><span className="tc-desc">{r[1]}</span></div>))}</div>;
}
function PirCard({ p }) {
  const src = p.sources || [];
  return (
    <div className="tc">
      <MakeHead status={p.status}>PIR{p.number ? ` ${p.number}` : ""}</MakeHead>
      <KV rows={[["Material", p.material], ["Supplier", p.supplier],
        ["Price", p.price != null ? `${p.price} ${p.currency || ""}`.trim() : null],
        ["Purch. org", p.purch_org], ["Lead time", p.lead_time_days != null ? `${p.lead_time_days} days` : null],
        ["Min order qty", p.min_qty]]} />
      {src.length > 1 && <><div className="tc-sec">Sources ({src.length})</div>
        <div className="tc-rows">{src.map((s, i) => (
          <div key={i} className="tc-row"><span className="tc-mat">{s.supplier}</span>
            <span className="tc-desc">{s.price} {s.currency || ""}{s.lead_time_days ? ` · ${s.lead_time_days}d lead` : ""}</span>
            {s.number && <span className="tc-tag made">{s.number}</span>}</div>))}</div></>}
    </div>
  );
}
function CostCard({ p }) {
  return (
    <div className="tc">
      <MakeHead status={p.status}>Cost condition{p.number ? ` ${p.number}` : ""}</MakeHead>
      <KV rows={[["Material", p.material], ["Supplier", p.supplier], ["Price", `${p.price} ${p.currency || ""}`.trim()],
        ["Scales", p.scales || null]]} />
    </div>
  );
}
function BomCard({ p }) {
  const comps = p.components || [];
  const label = p.status === "written" ? (p.items != null ? `${p.items}/${p.of} items written` : "written to SAP")
    : p.status === "read" ? "current structure" : "preview — not written";
  return (
    <div className="tc">
      <div className={"tc-banner " + (p.status === "preview" ? "rev" : "ok")}>BOM{p.number ? ` ${p.number}` : ""} · {label}</div>
      <div className="tc-mline">{p.material} @ plant {p.plant}{p.alternative ? ` · alt ${p.alternative}` : ""}{p.usage ? ` · usage ${p.usage}` : ""}{p.base_quantity ? ` · base ${p.base_quantity} ${p.base_unit || ""}` : ""} · {comps.length} component{comps.length === 1 ? "" : "s"}</div>
      {comps.length > 0 ? <div className="tc-rows">{comps.map((c, i) => (
        <div key={i} className="tc-row"><span className="tc-mat">{c.component}</span><span className="tc-desc">qty {c.quantity}{c.unit ? ` ${c.unit}` : ""}</span>{c.category && <span className="tc-tag made">{c.category}</span>}</div>))}</div>
        : <div className="tc-empty">No components.</div>}
    </div>
  );
}
function RoutingCard({ p }) {
  const ops = p.operations || [];
  const times = (o) => [o.setup ? `setup ${o.setup}` : null, o.run ? `run ${o.run}${o.unit ? " " + o.unit : ""}` : null].filter(Boolean).join(" · ");
  return (
    <div className="tc">
      <MakeHead status={p.status}>Routing{p.number ? ` ${p.number}` : ""}{p.status === "written" ? " · released" : ""}</MakeHead>
      <div className="tc-mline">{p.material} @ plant {p.plant} · {ops.length} operation{ops.length === 1 ? "" : "s"}</div>
      {ops.length > 0 ? <div className="tc-rows">{ops.map((o, i) => (
        <div key={i} className="tc-row"><span className="tc-mat">{o.operation}</span>
          <span className="tc-desc">{o.text}{times(o) ? ` · ${times(o)}` : ""}</span>
          {o.work_center && <span className="tc-tag made">{o.work_center}</span>}</div>))}</div>
        : <div className="tc-empty">No operations.</div>}
    </div>
  );
}
function AssuranceCard({ p }) {
  const sum = p.summary || {}, comps = (p.facts && p.facts.components) || [];
  const findings = (p.findings || []).filter((f) => f.verdict !== "pass");
  const errs = +sum.error || 0, warns = +sum.warning || 0, revs = +sum.review || 0, total = +sum.total_findings || 0;
  return (
    <div className="tc">
      <div className={"tc-banner " + (errs ? "err" : total ? "warn" : "ok")}>
        {errs ? `${errs} error${errs === 1 ? "" : "s"} — escalate to human` : total ? `${total} finding${total === 1 ? "" : "s"}` : "assurance clean — no findings"}
      </div>
      <div className="tc-mline"><b>🛡 {p.material}</b> · plant {p.plant} · {comps.length} components</div>
      <div className="tc-tiles">
        <div className="tc-tile err"><div className="n">{errs}</div><div className="l">errors</div></div>
        <div className="tc-tile warn"><div className="n">{warns}</div><div className="l">warnings</div></div>
        <div className="tc-tile rev"><div className="n">{revs}</div><div className="l">reviews</div></div>
        <div className="tc-tile"><div className="n">{total}</div><div className="l">findings</div></div>
      </div>
      {comps.length > 0 && <><div className="tc-sec">BOM composition</div>
        <div className="tc-rows">{comps.map((c, i) => (
          <div key={i} className="tc-row">
            <span className="tc-mat">{c.Product}</span>
            <span className="tc-desc">{c.description}{c.bom_quantity != null ? ` ×${c.bom_quantity}` : ""}</span>
            <span className={"tc-tag " + (/HAWA|ROH|VERP/.test(c.ProductType || "") ? "bought" : "made")}>{c.ProductType}</span>
            <span className="tc-coo">{c.CountryOfOrigin || "—"}</span>
          </div>))}</div></>}
      {findings.length > 0 && <><div className="tc-sec">Findings</div>
        <div className="tc-rows">{findings.slice(0, 30).map((f, i) => (
          <div key={i} className={"tc-row" + (f.severity === "error" ? " exc" : "")}>
            <span className={"tc-sev " + f.severity}>{f.severity}</span>
            <span className="tc-mat">{f.object}</span>
            <span className="tc-desc">{f.fact}</span>
            <span className="tc-against">{f.against}</span>
          </div>))}</div></>}
    </div>
  );
}
function GateCard({ gate, onApprove, onReject, onEdit }) {
  const [listening, setListening] = useState(false);
  function voice() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { alert("Voice recognition isn't available in this browser — use the buttons."); return; }
    const rec = new SR(); rec.lang = "en-US"; rec.interimResults = false; rec.maxAlternatives = 4; setListening(true);
    rec.onresult = (e) => {
      const txt = Array.from(e.results).map((r) => r[0].transcript).join(" ").toLowerCase();
      if (/confirm creation|confirm create|confirm it|approve/.test(txt)) onApprove("spoken");
      else if (/cancel|reject|abort|stop/.test(txt)) onReject("spoken");
    };
    rec.onend = () => setListening(false); rec.onerror = () => setListening(false);
    try { rec.start(); } catch (e) { setListening(false); }
  }
  return (
    <div className="gate">
      <div className="gate-h">Approval Review — Calibrated Gate</div>
      <div className="gate-body"><ReactMarkdown>{gate.summary || "Confirm to proceed."}</ReactMarkdown></div>
      <div className="gate-actions">
        <button className="g-approve" onClick={() => onApprove("clicked")}>✓ Confirm &amp; Create</button>
        <button className="g-reject" onClick={() => onReject("clicked")}>✕ Cancel</button>
        <button className="g-edit" onClick={onEdit}>✎ Edit spec</button>
        <button className={"g-voice" + (listening ? " on" : "")} onClick={voice}>{listening ? "🎙 listening…" : "🎤 Voice"}</button>
      </div>
      <div className="gate-foot">Click <b>Confirm &amp; Create</b> or say <b>“confirm creation”</b>. No casual “yes” writes to SAP.</div>
    </div>
  );
}

function SessionsModal({ onClose, cur, pinned, onSwitch, onReplay, onDemo, onPin, onNew }) {
  const [items, setItems] = useState(null);
  useEffect(() => { (async () => { try { setItems(await (await fetch("/api/sessions")).json()); } catch (e) { setItems([]); } })(); }, []);
  return (
    <div className="modal-bg" onClick={(e) => e.target.classList.contains("modal-bg") && onClose()}>
      <div className="modal">
        <div className="modal-h"><b>Sessions</b><div className="spacer" /><button className="mini" onClick={onNew}>+ New</button><button className="mini" onClick={onClose}>✕</button></div>
        <div className="sess-list">
          {items === null && <div className="empty-hint">Loading…</div>}
          {items && !items.length && <div className="empty-hint">No sessions yet.</div>}
          {items && items.map((s) => (
            <div key={s.id} className={"sess-row" + (s.id === cur ? " cur" : "")} onClick={() => s.id !== cur && onSwitch(s.id)}>
              <div className="sess-title">{s.title || "(no text yet)"}</div>
              <div className="sess-meta">{s.turns} turn{s.turns === 1 ? "" : "s"} · {s.id}{s.id === cur ? " · current" : ""}{s.id === pinned ? " · 📌 demo" : ""}</div>
              <div className="sess-actions">
                <button className={"rerun-btn pin" + (s.id === pinned ? " on" : "")} title="Pin as the demo session (🎬 Demo always walks it)" onClick={(e) => { e.stopPropagation(); onPin(s.id); }}>📌</button>
                <button className="rerun-btn" title="Replay recorded data" onClick={(e) => { e.stopPropagation(); onReplay(s.id); }}>▶ Replay</button>
                <button className="rerun-btn demo" title="Narrated, step-by-step walk-through" onClick={(e) => { e.stopPropagation(); onDemo(s.id); }}>🎬 Demo</button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

