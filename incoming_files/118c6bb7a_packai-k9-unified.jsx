import { useState, useEffect, useRef, useCallback } from "react";

// ─────────────────────────────────────────────────────────────────────────────
//  PackAI K-9 UNIFIED MODULE
//  K-9 Financial Literacy OS  ×  SCOUT AI Tutor
//  Full integration: shared state, cross-module awareness, productivity exports
// ─────────────────────────────────────────────────────────────────────────────

// ── CONSTANTS ────────────────────────────────────────────────────────────────
const WEEKLY_COL = 225;
const WEEKLY_TARGET = 255;

const REWARDS = [
  { id: "wwe1",  label: "WWE Event #1",  cost: 600,  icon: "🏆", category: "events"  },
  { id: "wwe2",  label: "WWE Event #2",  cost: 600,  icon: "🏆", category: "events"  },
  { id: "game1", label: "Game Drop #1",  cost: 70,   icon: "🎮", category: "games"   },
  { id: "game2", label: "Game Drop #2",  cost: 70,   icon: "🎮", category: "games"   },
  { id: "game3", label: "Game Drop #3",  cost: 70,   icon: "🎮", category: "games"   },
  { id: "game4", label: "Game Drop #4",  cost: 70,   icon: "🎮", category: "games"   },
  { id: "cash",  label: "Cash Savings",  cost: 1000, icon: "💰", category: "savings" },
];

const ACADEMIC_TASKS = [
  { id: "gpa",   label: "GPA Maintained (3.0+)",  value: 100, desc: "Primary 9-to-5",       subject: "math"    },
  { id: "hw",    label: "Homework On Time",         value: 25,  desc: "Weekly deliverable",   subject: "reading" },
  { id: "study", label: "Extra Study (30 min+)",    value: 25,  desc: "Skill mastery bonus",  subject: "science" },
];

const CHORE_TASKS = [
  { id: "lawn",     label: "Lawn / Heavy Maintenance",   value: 40, desc: "Per occurrence" },
  { id: "bathroom", label: "Bathroom & Kitchen Detail",  value: 30, desc: "Weekly clean"   },
  { id: "trash",    label: "Trash & Dish Rotation",       value: 35, desc: "Weekly ops"     },
];

const SUBJECTS = [
  { id: "math",    label: "Math",    icon: "🔢", color: "#f59e0b" },
  { id: "science", label: "Science", icon: "🔬", color: "#22d3ee" },
  { id: "reading", label: "Reading", icon: "📚", color: "#a78bfa" },
  { id: "history", label: "History", icon: "🏛️", color: "#fb923c" },
  { id: "coding",  label: "Coding",  icon: "💻", color: "#4ade80" },
  { id: "finance", label: "Finance", icon: "💹", color: "#f43f5e" },
];

const VIDEO_TOPICS = ["fractions", "photosynthesis", "US history", "JavaScript basics", "budgeting for teens", "solar system", "essay writing"];

const fmt = (n) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(n);

const calcGross = (week) => {
  const a = ACADEMIC_TASKS.filter((t) => week.academic[t.id]).reduce((s, t) => s + t.value, 0);
  const c = CHORE_TASKS.filter((t) => week.chores[t.id]).reduce((s, t) => s + t.value, 0);
  return a + c;
};

// ── AI CALLS ─────────────────────────────────────────────────────────────────

const SCOUT_SYSTEM = (ctx) => `You are SCOUT, a friendly AI learning companion embedded inside K-9 — a real financial literacy & rewards system a teen uses to earn money and unlock rewards.

SCOUT's personality: curious, encouraging, adventurous. Like a wise explorer who discovers things together with the student.

FINANCIAL CONTEXT (use this to make responses relevant):
- This week's reward fund balance: ${fmt(ctx.rewardFund)}
- This week gross earned: ${fmt(ctx.gross)} / ${fmt(WEEKLY_TARGET)} target
- Net reward pay this week: ${fmt(ctx.net)}
- Academic tasks completed: ${ctx.completedAcademic.join(", ") || "none yet"}
- Current subject focus: ${ctx.subject || "general"}
- Rewards they're working toward: ${ctx.nearRewards.join(", ") || "none yet"}

CORE TUTORING RULES:
1. NEVER give direct answers. Guide with questions and strategic hints only.
2. Use the Socratic method: "What do you already know about this?" / "What might happen if...?"
3. Break problems into steps. Celebrate each one.
4. If stuck: give a CLUE, not the answer. "Here's a hint: think about what multiplication really means..."
5. Connect learning to their financial system when natural: "Understanding percentages will help you calculate how close you are to your WWE reward!"
6. Encourage real productivity tools: suggest Excel, Word, Snipping Tool, note-taking.
7. Keep responses SHORT (3-5 sentences). Match language to their level.
8. End every response with a guiding question or challenge.
9. For financial literacy questions: teach the concept behind the number, don't just explain it.

You are SCOUT. Be warm, brief, and always end with a question.`;

async function callScout(messages, ctx) {
  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "claude-sonnet-4-20250514",
      max_tokens: 1000,
      system: SCOUT_SYSTEM(ctx),
      messages,
    }),
  });
  const data = await res.json();
  return data.content?.find((b) => b.type === "text")?.text || "Let me think about that... 🤔 What do YOU think the first step is?";
}

async function callFinanceCoach(week, gross, net, rewardFund) {
  const completedA = ACADEMIC_TASKS.filter((t) => week.academic[t.id]).map((t) => t.label);
  const completedC = CHORE_TASKS.filter((t) => week.chores[t.id]).map((t) => t.label);
  const prompt = `You are a sharp financial literacy coach for a teenager using a gamified "Total Compensation Package" system.

This week: Gross $${gross}, CoL deduction $${WEEKLY_COL}, Net reward pay $${net}.
Cumulative reward fund: $${rewardFund}.
Academic tasks: ${completedA.join(", ") || "none"}.
Operations tasks: ${completedC.join(", ") || "none"}.

Give a SHORT (2-3 sentence), motivating, real-world financial insight tied to their exact numbers. Reference one real career/finance concept. Be direct and specific. One emoji at the very start only.`;

  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "claude-sonnet-4-20250514",
      max_tokens: 1000,
      messages: [{ role: "user", content: prompt }],
    }),
  });
  const data = await res.json();
  return data.content?.[0]?.text || "Keep pushing — every dollar earned builds the habit.";
}

// ── SHARED COMPONENTS ─────────────────────────────────────────────────────────

function ProgressBar({ value, max, color = "#f59e0b", height = 8 }) {
  const pct = Math.min(100, Math.max(0, (value / max) * 100));
  return (
    <div style={{ background: "#1a1a2e", borderRadius: 4, height, overflow: "hidden", width: "100%" }}>
      <div style={{
        width: `${pct}%`, height: "100%",
        background: `linear-gradient(90deg, ${color}, ${color}bb)`,
        borderRadius: 4, transition: "width 0.7s cubic-bezier(0.4,0,0.2,1)",
        boxShadow: `0 0 8px ${color}55`,
      }} />
    </div>
  );
}

function TaskRow({ task, checked, onToggle, onStudy, category }) {
  const accent = category === "academic" ? "#22d3ee" : "#a78bfa";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <div
        onClick={onToggle}
        style={{
          flex: 1, display: "flex", alignItems: "center", gap: 12,
          padding: "10px 14px", borderRadius: 8, cursor: "pointer",
          background: checked ? `${accent}11` : "transparent",
          border: `1px solid ${checked ? accent + "44" : "#ffffff0f"}`,
          transition: "all 0.2s", userSelect: "none",
        }}
      >
        <div style={{
          width: 20, height: 20, borderRadius: 4, flexShrink: 0,
          border: `2px solid ${checked ? accent : "#ffffff33"}`,
          background: checked ? accent : "transparent",
          display: "flex", alignItems: "center", justifyContent: "center",
          transition: "all 0.2s",
        }}>
          {checked && <span style={{ color: "#0f0f23", fontSize: 12, fontWeight: 900 }}>✓</span>}
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: checked ? "#fff" : "#94a3b8" }}>{task.label}</div>
          <div style={{ fontSize: 11, color: "#475569", marginTop: 1 }}>{task.desc}</div>
        </div>
        <div style={{ fontFamily: "'Courier New', monospace", fontSize: 14, fontWeight: 700, color: checked ? accent : "#475569" }}>
          +{fmt(task.value)}
        </div>
      </div>
      {category === "academic" && onStudy && (
        <button
          onClick={() => onStudy(task)}
          title="Open SCOUT tutor for this subject"
          style={{
            width: 34, height: 34, borderRadius: 8, flexShrink: 0,
            background: "rgba(34,211,238,0.1)", border: "1px solid rgba(34,211,238,0.3)",
            color: "#22d3ee", fontSize: 14, cursor: "pointer",
            display: "flex", alignItems: "center", justifyContent: "center",
            transition: "all 0.15s",
          }}
        >🛸</button>
      )}
    </div>
  );
}

function PaycheckModal({ gross, net, onClose }) {
  return (
    <div
      style={{ position: "fixed", inset: 0, background: "#000000cc", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 200, padding: 20 }}
      onClick={onClose}
    >
      <div onClick={(e) => e.stopPropagation()} style={{
        background: "#0f0f23", border: "1px solid #f59e0b44", borderRadius: 16,
        padding: 32, maxWidth: 400, width: "100%", boxShadow: "0 0 60px #f59e0b22",
        animation: "popIn 0.3s cubic-bezier(0.175,0.885,0.32,1.275)",
      }}>
        <div style={{ textAlign: "center", marginBottom: 24 }}>
          <div style={{ fontSize: 11, letterSpacing: 4, color: "#f59e0b", fontFamily: "monospace" }}>K-9 FINANCIAL SYSTEM</div>
          <div style={{ fontSize: 22, fontWeight: 800, color: "#fff", marginTop: 4 }}>WEEKLY PAY STUB</div>
          <div style={{ fontSize: 12, color: "#475569", fontFamily: "monospace", marginTop: 2 }}>
            {new Date().toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "short", day: "numeric" })}
          </div>
        </div>
        <div style={{ fontFamily: "'Courier New', monospace", fontSize: 13 }}>
          {[
            { label: "GROSS EARNINGS",              value: gross,          color: "#22d3ee" },
            { label: "──────────────────────────",  value: null },
            { label: "CoL — Rent Share",             value: -(500 / 4),    color: "#ef4444" },
            { label: "CoL — Food Share",             value: -(300 / 4),    color: "#ef4444" },
            { label: "CoL — Utilities",              value: -(100 / 4),    color: "#ef4444" },
            { label: "──────────────────────────",  value: null },
            { label: "NET REWARD PAY",               value: net,            color: net >= 0 ? "#22c55e" : "#ef4444" },
          ].map((l, i) =>
            l.value === null ? (
              <div key={i} style={{ color: "#334155", margin: "8px 0" }}>{l.label}</div>
            ) : (
              <div key={i} style={{ display: "flex", justifyContent: "space-between", margin: "6px 0" }}>
                <span style={{ color: "#94a3b8" }}>{l.label}</span>
                <span style={{ color: l.color, fontWeight: 700 }}>{l.value >= 0 ? "+" : ""}{fmt(l.value)}</span>
              </div>
            )
          )}
        </div>
        {net < 0 && (
          <div style={{ marginTop: 14, padding: "10px 14px", background: "#ef444411", border: "1px solid #ef444433", borderRadius: 8, fontSize: 12, color: "#fca5a5" }}>
            ⚠ This week's earnings didn't cover Cost of Living. No reward progress — consistency is the skill.
          </div>
        )}
        <button onClick={onClose} style={{
          marginTop: 20, width: "100%", padding: 12, background: "#f59e0b",
          color: "#0f0f23", border: "none", borderRadius: 8, fontWeight: 800,
          fontSize: 14, cursor: "pointer", letterSpacing: 1,
        }}>CLOSE STUB</button>
      </div>
    </div>
  );
}

// ── MAIN COMPONENT ────────────────────────────────────────────────────────────
export default function K9UnifiedModule() {
  // ── Financial state
  const [week, setWeek]                   = useState({ academic: {}, chores: {}, note: "" });
  const [history, setHistory]             = useState([]);
  const [rewardFund, setRewardFund]       = useState(0);
  const [unlockedRewards, setUnlockedRewards] = useState([]);
  const [showPaycheck, setShowPaycheck]   = useState(false);
  const [coachMsg, setCoachMsg]           = useState("");
  const [coachLoading, setCoachLoading]   = useState(false);

  // ── SCOUT Tutor state
  const [scoutMessages, setScoutMessages] = useState([]);
  const [scoutInput, setScoutInput]       = useState("");
  const [scoutLoading, setScoutLoading]   = useState(false);
  const [selectedSubject, setSelectedSubject] = useState(null);
  const [uploadedImage, setUploadedImage] = useState(null);
  const [imageBase64, setImageBase64]     = useState(null);
  const [isListening, setIsListening]     = useState(false);
  const [skillLevel, setSkillLevel]       = useState("middle");

  // ── Video state
  const [videoQuery, setVideoQuery]       = useState("");
  const [videoResults, setVideoResults]   = useState([]);
  const [selectedVideo, setSelectedVideo] = useState(null);

  // ── UI state
  const [activeTab, setActiveTab]         = useState("dashboard");
  const [animIn, setAnimIn]               = useState(false);
  const [exportNotice, setExportNotice]   = useState("");
  const [sessionStats, setSessionStats]   = useState({ questions: 0, hints: 0 });

  const messagesEndRef  = useRef(null);
  const fileInputRef    = useRef(null);
  const recognitionRef  = useRef(null);
  const conversationRef = useRef([]);

  const gross = calcGross(week);
  const net   = gross - WEEKLY_COL;
  const totalRewardGoal = REWARDS.reduce((s, r) => s + r.cost, 0);
  const completedAcademic = ACADEMIC_TASKS.filter((t) => week.academic[t.id]).map((t) => t.label);
  const nearRewards = REWARDS.filter((r) => !unlockedRewards.includes(r.id) && r.cost - rewardFund <= 200).map((r) => r.label);

  // ── Init
  useEffect(() => { setTimeout(() => setAnimIn(true), 80); }, []);

  // ── SCOUT init greeting
  useEffect(() => {
    const greeting = {
      role: "assistant",
      content: `🛸 Hey Explorer! I'm **SCOUT** — your learning companion inside K-9. I help you *figure things out*, not just get answers.\n\nYou've earned **${fmt(rewardFund)}** toward your rewards so far this week. Let's make sure your brain is leveling up too! 🧠\n\nWhat subject do you want to tackle today?`,
      ts: Date.now(),
    };
    setScoutMessages([greeting]);
    conversationRef.current = [];
  }, []);

  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [scoutMessages]);

  // ── Voice
  const startListening = useCallback(() => {
    if (!("webkitSpeechRecognition" in window) && !("SpeechRecognition" in window)) {
      alert("Voice input works in Chrome or Edge!"); return;
    }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    recognitionRef.current = new SR();
    recognitionRef.current.continuous = false;
    recognitionRef.current.interimResults = false;
    recognitionRef.current.lang = "en-US";
    recognitionRef.current.onstart  = () => setIsListening(true);
    recognitionRef.current.onresult = (e) => { setScoutInput((p) => p + e.results[0][0].transcript); setIsListening(false); };
    recognitionRef.current.onerror  = () => setIsListening(false);
    recognitionRef.current.onend    = () => setIsListening(false);
    recognitionRef.current.start();
  }, []);

  const stopListening = useCallback(() => { recognitionRef.current?.stop(); setIsListening(false); }, []);

  // ── Image
  const handleImageUpload = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      setUploadedImage(URL.createObjectURL(file));
      setImageBase64(ev.target.result.split(",")[1]);
    };
    reader.readAsDataURL(file);
  };

  // ── Send SCOUT message
  const sendScout = async () => {
    if (!scoutInput.trim() && !imageBase64) return;
    const text = scoutInput.trim();
    setScoutInput("");

    const userMsg = { role: "user", content: text, image: uploadedImage, ts: Date.now() };
    setScoutMessages((p) => [...p, userMsg]);

    const apiContent = [];
    if (imageBase64) apiContent.push({ type: "image", source: { type: "base64", media_type: "image/jpeg", data: imageBase64 } });
    if (text) apiContent.push({ type: "text", text });

    conversationRef.current.push({ role: "user", content: imageBase64 ? apiContent : text });

    setUploadedImage(null); setImageBase64(null);
    setScoutLoading(true);
    setSessionStats((s) => ({ ...s, questions: s.questions + 1 }));

    try {
      const ctx = { rewardFund, gross, net, completedAcademic, nearRewards, subject: selectedSubject?.label || "general" };
      const reply = await callScout(conversationRef.current, ctx);
      conversationRef.current.push({ role: "assistant", content: reply });
      const isHint = /hint|clue|think about|what if/i.test(reply);
      if (isHint) setSessionStats((s) => ({ ...s, hints: s.hints + 1 }));
      setScoutMessages((p) => [...p, { role: "assistant", content: reply, ts: Date.now() }]);
    } catch {
      setScoutMessages((p) => [...p, { role: "assistant", content: "Oops! 🛸 Signal lost. Check your connection and try again!", ts: Date.now() }]);
    }
    setScoutLoading(false);
  };

  // ── Jump to SCOUT from a task
  const openScoutForTask = (task) => {
    const subj = SUBJECTS.find((s) => s.id === task.subject) || null;
    setSelectedSubject(subj);
    setActiveTab("scout");
    const prompt = `I just logged "${task.label}" in my K-9 system. Can you help me make sure I actually understand the material?`;
    setScoutInput(prompt);
  };

  // ── Financial actions
  const toggleTask = (cat, id) => setWeek((w) => ({ ...w, [cat]: { ...w[cat], [id]: !w[cat][id] } }));

  const commitWeek = useCallback(() => {
    const entry = { ...week, gross, net, date: new Date().toISOString() };
    setHistory((h) => [entry, ...h]);
    setRewardFund((f) => Math.max(0, f + Math.max(0, net)));
    setWeek({ academic: {}, chores: {}, note: "" });
    setShowPaycheck(true);
  }, [week, gross, net]);

  const getCoachInsight = useCallback(async () => {
    setCoachLoading(true); setCoachMsg("");
    try { setCoachMsg(await callFinanceCoach(week, gross, net, Math.max(0, rewardFund + Math.max(0, net)))); }
    catch { setCoachMsg("Keep building momentum — financial discipline compounds like interest."); }
    setCoachLoading(false);
  }, [week, gross, net, rewardFund]);

  const redeemReward = (r) => {
    if (rewardFund >= r.cost && !unlockedRewards.includes(r.id)) {
      setRewardFund((f) => f - r.cost);
      setUnlockedRewards((u) => [...u, r.id]);
    }
  };

  // ── Video search
  const searchVideos = () => {
    if (!videoQuery.trim()) return;
    const q = videoQuery;
    setVideoResults([
      { id: "aircAruvnKk", title: `${q} — Visual Explainer`, channel: "3Blue1Brown / Khan Style" },
      { id: "WUvTyaaNkzM", title: `${q} for Students — Step by Step`, channel: "TED-Ed" },
      { id: "Ilg3gGewQ5U", title: `Understanding ${q}`, channel: "CrashCourse" },
    ]);
  };

  // ── Export
  const copyToClipboard = (text) => {
    navigator.clipboard.writeText(text);
    setExportNotice("📋 Copied! Paste into Word, Notion, or Google Docs.");
    setTimeout(() => setExportNotice(""), 3500);
  };

  const exportCSV = () => {
    const rows = ["Date,Subject,Type,Gross,Net,Notes"];
    history.forEach((h) => {
      const acad = ACADEMIC_TASKS.filter((t) => h.academic?.[t.id]).map((t) => t.label).join("|");
      rows.push(`"${new Date(h.date).toLocaleDateString()}","${acad || ""}","Weekly",${h.gross},${h.net},""`);
    });
    const blob = new Blob([rows.join("\n")], { type: "text/csv" });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a"); a.href = url; a.download = `K9_Session_${Date.now()}.csv`; a.click();
    setExportNotice("✅ Exported! Open in Excel to see your learning + earning history.");
    setTimeout(() => setExportNotice(""), 4000);
  };

  // ── Formatting
  const fmtMsg = (text) => text
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.*?)\*/g, "<em>$1</em>")
    .replace(/`(.*?)`/g, '<code style="background:#1a1a2e;padding:2px 6px;border-radius:4px;font-family:monospace;font-size:12px">$1</code>')
    .replace(/\n/g, "<br/>");

  // ── Styles
  const C = {
    root:       { minHeight: "100vh", background: "#07071a", fontFamily: "'Segoe UI', system-ui, sans-serif", color: "#e2e8f0", paddingBottom: 40 },
    header:     { background: "linear-gradient(135deg, #0a0a1f 0%, #111130 100%)", borderBottom: "1px solid rgba(245,158,11,0.2)", padding: "16px 20px 0", position: "sticky", top: 0, zIndex: 50 },
    tabBar:     { display: "flex", gap: 2, overflowX: "auto", paddingBottom: 0 },
    tab:        (a) => ({ padding: "9px 16px", fontSize: 10, fontWeight: 700, letterSpacing: 1.5, fontFamily: "monospace", border: "none", borderRadius: "6px 6px 0 0", cursor: "pointer", transition: "all 0.2s", whiteSpace: "nowrap", background: a ? "#f59e0b" : "transparent", color: a ? "#0a0a1f" : "#475569" }),
    body:       { padding: "20px 18px", maxWidth: 820, margin: "0 auto", opacity: animIn ? 1 : 0, transform: animIn ? "translateY(0)" : "translateY(14px)", transition: "all 0.5s cubic-bezier(0.4,0,0.2,1)" },
    card:       { background: "#0d0d24", border: "1px solid rgba(255,255,255,0.06)", borderRadius: 12, padding: 18, marginBottom: 14 },
    label:      { fontSize: 10, letterSpacing: 3, color: "#f59e0b", fontWeight: 700, fontFamily: "monospace", marginBottom: 12, textTransform: "uppercase" },
    primaryBtn: { background: "linear-gradient(135deg, #f59e0b, #d97706)", color: "#0a0a1f", border: "none", borderRadius: 10, padding: "13px 22px", fontWeight: 800, fontSize: 13, letterSpacing: 1.5, cursor: "pointer", width: "100%", transition: "all 0.2s", boxShadow: "0 4px 20px rgba(245,158,11,0.25)" },
    ghostBtn:   { background: "transparent", color: "#22d3ee", border: "1px solid rgba(34,211,238,0.3)", borderRadius: 10, padding: "11px 18px", fontWeight: 700, fontSize: 12, letterSpacing: 1, cursor: "pointer", width: "100%", transition: "all 0.2s" },
    statGrid:   { display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 12, marginBottom: 14 },
    stat:       (accent) => ({ background: `${accent}08`, border: `1px solid ${accent}22`, borderRadius: 10, padding: "14px 16px" }),
    statVal:    (accent) => ({ fontSize: 22, fontWeight: 800, color: accent, fontFamily: "monospace", letterSpacing: -1 }),
    statLbl:    { fontSize: 10, letterSpacing: 2, color: "#475569", marginTop: 2, textTransform: "uppercase" },
  };

  const TABS = [
    { id: "dashboard", label: "⬡ DASHBOARD" },
    { id: "weekly",    label: "📋 WEEKLY SHIFT" },
    { id: "scout",     label: "🛸 SCOUT TUTOR" },
    { id: "videos",    label: "📺 LEARN VIDEOS" },
    { id: "rewards",   label: "🏆 REWARDS" },
    { id: "ledger",    label: "📒 LEDGER" },
  ];

  // ────────────────────────────────────────────────────────── DASHBOARD
  const totalEarned  = history.reduce((s, h) => s + Math.max(0, h.net), 0);
  const weeksWorked  = history.length;
  const annualPace   = weeksWorked > 0 ? (totalEarned / weeksWorked) * 52 : 0;

  const renderDashboard = () => (
    <>
      <div style={C.statGrid}>
        {[
          { val: fmt(rewardFund), lbl: "Reward Fund",   accent: "#f59e0b", bar: { v: rewardFund, m: totalRewardGoal } },
          { val: fmt(gross),      lbl: "Week Gross",     accent: "#22d3ee", bar: { v: gross, m: WEEKLY_TARGET, color: "#22d3ee" } },
          { val: fmt(net >= 0 ? net : net), lbl: "Net Reward Pay", accent: net >= 0 ? "#22c55e" : "#ef4444" },
          { val: fmt(annualPace), lbl: "Annual Pace",    accent: "#a78bfa" },
        ].map((s) => (
          <div key={s.lbl} style={C.stat(s.accent)}>
            <div style={C.statVal(s.accent)}>{s.val}</div>
            <div style={C.statLbl}>{s.lbl}</div>
            {s.bar && (
              <div style={{ marginTop: 10 }}>
                <ProgressBar value={s.bar.v} max={s.bar.m} color={s.bar.color || "#f59e0b"} />
                <div style={{ fontSize: 10, color: "#475569", marginTop: 3, fontFamily: "monospace" }}>
                  {s.bar.v}/{s.bar.m}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* CoL Snapshot */}
      <div style={C.card}>
        <div style={C.label}>Cost of Living Snapshot</div>
        {[{ label: "Rent Share", amount: 125, icon: "🏠" }, { label: "Food Share", amount: 75, icon: "🍔" }, { label: "Utilities", amount: 25, icon: "⚡" }].map((item) => (
          <div key={item.label} style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
            <span style={{ fontSize: 16 }}>{item.icon}</span>
            <div style={{ flex: 1 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <span style={{ fontSize: 13, color: "#94a3b8" }}>{item.label}</span>
                <span style={{ fontFamily: "monospace", fontSize: 12, color: "#ef4444" }}>-{fmt(item.amount)}/wk</span>
              </div>
              <ProgressBar value={item.amount} max={225} color="#ef4444" height={4} />
            </div>
          </div>
        ))}
        <div style={{ marginTop: 10, padding: "9px 14px", background: "#1a1a2e", borderRadius: 8, display: "flex", justifyContent: "space-between", fontFamily: "monospace", fontSize: 13 }}>
          <span style={{ color: "#475569" }}>Weekly CoL Total</span>
          <span style={{ color: "#ef4444", fontWeight: 700 }}>-{fmt(WEEKLY_COL)}</span>
        </div>
      </div>

      {/* SCOUT Quick Card */}
      <div style={{ ...C.card, border: "1px solid rgba(34,211,238,0.2)", background: "linear-gradient(135deg, #0d0d24, #0a1520)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
          <div style={{ width: 36, height: 36, borderRadius: "50%", background: "linear-gradient(135deg, #f59e0b, #22d3ee)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 18 }}>🛸</div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 14, color: "#22d3ee" }}>SCOUT Tutor Status</div>
            <div style={{ fontSize: 11, color: "#475569" }}>{sessionStats.questions} questions • {sessionStats.hints} hints used</div>
          </div>
          <button onClick={() => setActiveTab("scout")} style={{ marginLeft: "auto", background: "rgba(34,211,238,0.1)", border: "1px solid rgba(34,211,238,0.3)", color: "#22d3ee", borderRadius: 8, padding: "6px 14px", fontSize: 12, fontWeight: 700, cursor: "pointer" }}>Open →</button>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {SUBJECTS.map((s) => (
            <button key={s.id} onClick={() => { setSelectedSubject(s); setActiveTab("scout"); }} style={{
              background: `${s.color}15`, border: `1px solid ${s.color}40`, color: s.color,
              borderRadius: 16, padding: "4px 12px", fontSize: 12, fontWeight: 700, cursor: "pointer",
            }}>{s.icon} {s.label}</button>
          ))}
        </div>
      </div>

      {/* AI Coach */}
      <div style={{ ...C.card, border: "1px solid rgba(167,139,250,0.2)" }}>
        <div style={C.label}>⚡ PackAI Finance Coach</div>
        {coachMsg
          ? <p style={{ fontSize: 14, lineHeight: 1.7, color: "#cbd5e1", margin: "0 0 14px" }}>{coachMsg}</p>
          : <p style={{ fontSize: 13, color: "#475569", margin: "0 0 14px" }}>Get personalized coaching based on this week's numbers.</p>
        }
        <button style={C.ghostBtn} onClick={getCoachInsight} disabled={coachLoading}>
          {coachLoading ? "ANALYZING..." : "GET AI COACHING INSIGHT"}
        </button>
      </div>
    </>
  );

  // ────────────────────────────────────────────────────────── WEEKLY SHIFT
  const renderWeekly = () => (
    <>
      <div style={C.card}>
        <div style={C.label}>Category A — Academic Performance</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {ACADEMIC_TASKS.map((t) => (
            <TaskRow key={t.id} task={t} checked={!!week.academic[t.id]}
              onToggle={() => toggleTask("academic", t.id)}
              onStudy={openScoutForTask}
              category="academic" />
          ))}
        </div>
        <div style={{ marginTop: 10, padding: "8px 12px", background: "rgba(34,211,238,0.06)", borderRadius: 8, fontSize: 12, color: "#22d3ee" }}>
          🛸 Tap the SCOUT icon next to any task to get tutoring help on that subject
        </div>
      </div>

      <div style={C.card}>
        <div style={C.label}>Category B — Operations & Logistics</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {CHORE_TASKS.map((t) => (
            <TaskRow key={t.id} task={t} checked={!!week.chores[t.id]}
              onToggle={() => toggleTask("chores", t.id)}
              category="chores" />
          ))}
        </div>
      </div>

      {/* Live Preview */}
      <div style={{ ...C.card, background: net >= 0 ? "rgba(5,46,22,0.06)" : "rgba(69,10,10,0.06)", border: `1px solid ${net >= 0 ? "rgba(34,197,94,0.3)" : "rgba(239,68,68,0.3)"}` }}>
        <div style={C.label}>Live Paycheck Preview</div>
        {[
          { label: "Gross Earnings", val: gross, color: "#22d3ee" },
          { label: "CoL Deduction", val: -WEEKLY_COL, color: "#ef4444" },
        ].map((r) => (
          <div key={r.label} style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
            <span style={{ fontSize: 13, color: "#94a3b8" }}>{r.label}</span>
            <span style={{ fontFamily: "monospace", color: r.color, fontWeight: 700 }}>{r.val >= 0 ? "" : "-"}{fmt(Math.abs(r.val))}</span>
          </div>
        ))}
        <div style={{ display: "flex", justifyContent: "space-between", paddingTop: 10, borderTop: "1px solid rgba(255,255,255,0.07)" }}>
          <span style={{ fontSize: 14, fontWeight: 700 }}>Net Reward Pay</span>
          <span style={{ fontFamily: "monospace", fontWeight: 800, fontSize: 18, color: net >= 0 ? "#22c55e" : "#ef4444" }}>
            {net >= 0 ? "+" : ""}{fmt(net)}
          </span>
        </div>
        {net < 0 && (
          <div style={{ marginTop: 10, fontSize: 12, color: "#fca5a5", background: "rgba(239,68,68,0.07)", padding: "8px 12px", borderRadius: 6 }}>
            ⚠ {fmt(Math.abs(net))} short of CoL this week. No rewards progress. Complete more tasks!
          </div>
        )}
      </div>

      <button style={C.primaryBtn} onClick={commitWeek}>📋 SUBMIT WEEKLY TIMESHEET</button>
    </>
  );

  // ────────────────────────────────────────────────────────── SCOUT TUTOR
  const renderScout = () => (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* Subject + Level selectors */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <span style={{ fontSize: 10, color: "#475569", fontFamily: "monospace", letterSpacing: 2 }}>SUBJECT:</span>
        {SUBJECTS.map((s) => (
          <button key={s.id} onClick={() => setSelectedSubject(selectedSubject?.id === s.id ? null : s)} style={{
            background: selectedSubject?.id === s.id ? `${s.color}25` : "rgba(255,255,255,0.05)",
            border: `1px solid ${selectedSubject?.id === s.id ? s.color + "60" : "rgba(255,255,255,0.1)"}`,
            color: selectedSubject?.id === s.id ? s.color : "#94a3b8",
            borderRadius: 16, padding: "4px 12px", fontSize: 12, fontWeight: 700, cursor: "pointer",
            transition: "all 0.15s",
          }}>{s.icon} {s.label}</button>
        ))}
        <select value={skillLevel} onChange={(e) => setSkillLevel(e.target.value)} style={{
          marginLeft: "auto", background: "rgba(255,255,255,0.07)", border: "1px solid rgba(255,255,255,0.12)",
          color: "#e2e8f0", borderRadius: 16, padding: "4px 12px", fontSize: 12, cursor: "pointer",
        }}>
          <option value="elementary">🌱 Elementary</option>
          <option value="middle">🚀 Middle School</option>
          <option value="high">⚡ High School</option>
        </select>
      </div>

      {/* Stats strip */}
      <div style={{ display: "flex", gap: 10 }}>
        {[{ l: "Questions Asked", v: sessionStats.questions, c: "#22d3ee" }, { l: "Hints Used", v: sessionStats.hints, c: "#f59e0b" }].map((s) => (
          <div key={s.l} style={{ background: `${s.c}10`, border: `1px solid ${s.c}25`, borderRadius: 8, padding: "6px 14px", display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ fontWeight: 800, color: s.c, fontFamily: "monospace" }}>{s.v}</span>
            <span style={{ fontSize: 11, color: "#475569" }}>{s.l}</span>
          </div>
        ))}
      </div>

      {/* Chat */}
      <div style={{ ...C.card, minHeight: 320, maxHeight: 400, overflowY: "auto", display: "flex", flexDirection: "column", gap: 12, padding: 14 }}>
        {scoutMessages.map((msg, i) => (
          <div key={i} style={{ display: "flex", flexDirection: msg.role === "user" ? "row-reverse" : "row", gap: 8, alignItems: "flex-start" }}>
            <div style={{ width: 32, height: 32, borderRadius: "50%", flexShrink: 0, background: msg.role === "user" ? "linear-gradient(135deg, #667eea,#764ba2)" : "linear-gradient(135deg, #f59e0b,#22d3ee)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 15 }}>
              {msg.role === "user" ? "👤" : "🛸"}
            </div>
            <div style={{ maxWidth: "75%", background: msg.role === "user" ? "rgba(102,126,234,0.2)" : "rgba(34,211,238,0.07)", border: `1px solid ${msg.role === "user" ? "rgba(102,126,234,0.3)" : "rgba(34,211,238,0.15)"}`, borderRadius: msg.role === "user" ? "16px 4px 16px 16px" : "4px 16px 16px 16px", padding: "10px 14px" }}>
              {msg.image && <img src={msg.image} alt="" style={{ maxWidth: "100%", borderRadius: 8, marginBottom: 8 }} />}
              <div style={{ fontSize: 13, lineHeight: 1.6 }} dangerouslySetInnerHTML={{ __html: fmtMsg(msg.content || "") }} />
              {msg.role === "assistant" && (
                <button onClick={() => copyToClipboard(msg.content)} style={{ marginTop: 6, background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.1)", color: "#64748b", borderRadius: 6, padding: "2px 10px", fontSize: 10, cursor: "pointer" }}>
                  📋 Copy to Notes
                </button>
              )}
            </div>
          </div>
        ))}
        {scoutLoading && (
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <div style={{ width: 32, height: 32, borderRadius: "50%", background: "linear-gradient(135deg, #f59e0b,#22d3ee)", display: "flex", alignItems: "center", justifyContent: "center" }}>🛸</div>
            <div style={{ background: "rgba(34,211,238,0.07)", border: "1px solid rgba(34,211,238,0.15)", borderRadius: "4px 16px 16px 16px", padding: "12px 16px", display: "flex", gap: 5 }}>
              {[0, 0.2, 0.4].map((d, i) => (
                <div key={i} style={{ width: 7, height: 7, borderRadius: "50%", background: "#22d3ee", animation: `pulse 1s ${d}s infinite` }} />
              ))}
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Image preview */}
      {uploadedImage && (
        <div style={{ display: "flex", alignItems: "center", gap: 10, background: "rgba(255,255,255,0.04)", borderRadius: 8, padding: "8px 14px" }}>
          <img src={uploadedImage} alt="" style={{ width: 44, height: 44, borderRadius: 6, objectFit: "cover" }} />
          <span style={{ fontSize: 12, color: "#64748b" }}>Image ready to send</span>
          <button onClick={() => { setUploadedImage(null); setImageBase64(null); }} style={{ marginLeft: "auto", background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 16 }}>✕</button>
        </div>
      )}

      {/* Input bar */}
      <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
        <button onClick={() => fileInputRef.current.click()} title="Upload homework photo" style={{ width: 40, height: 40, borderRadius: 10, background: "rgba(255,255,255,0.07)", border: "1px solid rgba(255,255,255,0.12)", cursor: "pointer", fontSize: 17, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>📷</button>
        <input ref={fileInputRef} type="file" accept="image/*" onChange={handleImageUpload} style={{ display: "none" }} />
        <textarea
          value={scoutInput}
          onChange={(e) => setScoutInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendScout(); } }}
          placeholder={isListening ? "🎤 Listening..." : "Ask SCOUT a question..."}
          rows={1}
          style={{ flex: 1, background: "rgba(255,255,255,0.07)", border: "1px solid rgba(255,255,255,0.12)", color: "#e2e8f0", borderRadius: 12, padding: "10px 14px", fontSize: 13, resize: "none", fontFamily: "inherit", outline: "none", lineHeight: 1.5, maxHeight: 90, overflowY: "auto" }}
        />
        <button onMouseDown={startListening} onMouseUp={stopListening} onTouchStart={startListening} onTouchEnd={stopListening} style={{ width: 40, height: 40, borderRadius: 10, background: isListening ? "rgba(239,68,68,0.2)" : "rgba(255,255,255,0.07)", border: `1px solid ${isListening ? "rgba(239,68,68,0.5)" : "rgba(255,255,255,0.12)"}`, cursor: "pointer", fontSize: 17, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>🎤</button>
        <button onClick={sendScout} disabled={scoutLoading || (!scoutInput.trim() && !imageBase64)} style={{ width: 40, height: 40, borderRadius: 10, background: scoutInput.trim() || imageBase64 ? "linear-gradient(135deg,#f59e0b,#22d3ee)" : "rgba(255,255,255,0.07)", border: "none", cursor: "pointer", fontSize: 17, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, opacity: scoutLoading ? 0.5 : 1, transition: "all 0.2s" }}>🚀</button>
      </div>

      {exportNotice && (
        <div style={{ padding: "8px 14px", background: "rgba(34,197,94,0.1)", border: "1px solid rgba(34,197,94,0.3)", borderRadius: 8, fontSize: 12, color: "#4ade80" }}>
          {exportNotice}
        </div>
      )}
    </div>
  );

  // ────────────────────────────────────────────────────────── LEARN VIDEOS
  const renderVideos = () => (
    <>
      <div style={C.card}>
        <div style={C.label}>Educational Video Search</div>
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <input value={videoQuery} onChange={(e) => setVideoQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && searchVideos()} placeholder="Search topics (fractions, photosynthesis, budgeting...)" style={{ flex: 1, background: "rgba(255,255,255,0.07)", border: "1px solid rgba(255,255,255,0.12)", color: "#e2e8f0", borderRadius: 10, padding: "9px 14px", fontSize: 13, fontFamily: "inherit", outline: "none" }} />
          <button onClick={searchVideos} style={{ background: "linear-gradient(135deg,#f59e0b,#d97706)", border: "none", color: "#0a0a1f", borderRadius: 10, padding: "9px 18px", fontWeight: 800, fontSize: 13, cursor: "pointer" }}>Search</button>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {VIDEO_TOPICS.map((q) => (
            <button key={q} onClick={() => { setVideoQuery(q); setTimeout(searchVideos, 50); }} style={{ background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.1)", color: "#94a3b8", borderRadius: 16, padding: "3px 12px", fontSize: 12, cursor: "pointer" }}>{q}</button>
          ))}
        </div>
      </div>

      {selectedVideo && (
        <div style={{ ...C.card, padding: 0, overflow: "hidden", border: "1px solid rgba(245,158,11,0.3)" }}>
          <div style={{ position: "relative", paddingBottom: "56.25%", height: 0 }}>
            <iframe src={`https://www.youtube-nocookie.com/embed/${selectedVideo.id}?modestbranding=1&rel=0`}
              style={{ position: "absolute", top: 0, left: 0, width: "100%", height: "100%", border: "none" }}
              allow="accelerometer; autoplay; encrypted-media; picture-in-picture" allowFullScreen title={selectedVideo.title} />
          </div>
          <div style={{ padding: "12px 16px" }}>
            <div style={{ fontWeight: 700, fontSize: 14 }}>{selectedVideo.title}</div>
            <div style={{ fontSize: 12, color: "#475569", marginTop: 2 }}>{selectedVideo.channel}</div>
            <div style={{ fontSize: 12, color: "#22d3ee", marginTop: 6 }}>💡 Take notes as you watch, then ask SCOUT about anything confusing!</div>
          </div>
        </div>
      )}

      {videoResults.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(230px,1fr))", gap: 12 }}>
          {videoResults.map((v) => (
            <div key={v.id} onClick={() => setSelectedVideo(v)} style={{ ...C.card, margin: 0, cursor: "pointer", border: `1px solid ${selectedVideo?.id === v.id ? "rgba(245,158,11,0.5)" : "rgba(255,255,255,0.06)"}`, overflow: "hidden", padding: 0, transition: "transform 0.15s" }}>
              <div style={{ position: "relative", paddingBottom: "56%", background: "#000" }}>
                <img src={`https://img.youtube.com/vi/${v.id}/mqdefault.jpg`} alt={v.title} style={{ position: "absolute", top: 0, left: 0, width: "100%", height: "100%", objectFit: "cover" }} />
              </div>
              <div style={{ padding: "10px 12px" }}>
                <div style={{ fontSize: 12, fontWeight: 700, lineHeight: 1.4 }}>{v.title}</div>
                <div style={{ fontSize: 11, color: "#475569", marginTop: 2 }}>{v.channel}</div>
              </div>
            </div>
          ))}
        </div>
      )}

      {videoResults.length === 0 && (
        <div style={{ textAlign: "center", padding: "50px 20px", color: "#334155" }}>
          <div style={{ fontSize: 44, marginBottom: 12 }}>🔭</div>
          <div style={{ fontSize: 14 }}>Search for a topic above to find educational videos</div>
        </div>
      )}
    </>
  );

  // ────────────────────────────────────────────────────────── REWARDS
  const renderRewards = () => (
    <>
      <div style={{ ...C.card, border: "1px solid rgba(245,158,11,0.3)", textAlign: "center" }}>
        <div style={{ fontSize: 30, fontWeight: 900, color: "#f59e0b", fontFamily: "monospace" }}>{fmt(rewardFund)}</div>
        <div style={{ fontSize: 10, color: "#475569", letterSpacing: 3, marginTop: 4 }}>AVAILABLE REWARD BALANCE</div>
        <div style={{ margin: "14px 0 6px" }}><ProgressBar value={rewardFund} max={totalRewardGoal} height={10} /></div>
        <div style={{ fontSize: 12, color: "#94a3b8", fontFamily: "monospace" }}>{fmt(rewardFund)} / {fmt(totalRewardGoal)} annual goal</div>
      </div>

      {["events", "games", "savings"].map((cat) => {
        const labels = { events: "🏆 Live Events", games: "🎮 Game Drops", savings: "💰 Cash Goals" };
        return (
          <div key={cat} style={{ marginBottom: 14 }}>
            <div style={C.label}>{labels[cat]}</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {REWARDS.filter((r) => r.category === cat).map((r) => {
                const unlocked = unlockedRewards.includes(r.id);
                const canAfford = rewardFund >= r.cost;
                return (
                  <div key={r.id} style={{ ...C.card, margin: 0, display: "flex", alignItems: "center", gap: 14, background: unlocked ? "rgba(5,46,22,0.3)" : "#0d0d24", border: `1px solid ${unlocked ? "rgba(34,197,94,0.4)" : canAfford ? "rgba(245,158,11,0.3)" : "rgba(255,255,255,0.06)"}` }}>
                    <span style={{ fontSize: 22 }}>{r.icon}</span>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 13, fontWeight: 600, color: unlocked ? "#4ade80" : "#e2e8f0" }}>{r.label}</div>
                      <div style={{ marginTop: 6 }}>
                        <ProgressBar value={Math.min(rewardFund, r.cost)} max={r.cost} color={unlocked ? "#22c55e" : "#f59e0b"} height={4} />
                        <div style={{ fontSize: 10, color: "#475569", marginTop: 2, fontFamily: "monospace" }}>{fmt(Math.min(rewardFund, r.cost))} / {fmt(r.cost)}</div>
                      </div>
                    </div>
                    <button onClick={() => redeemReward(r)} disabled={!canAfford || unlocked} style={{ padding: "8px 14px", borderRadius: 8, border: "none", fontWeight: 700, fontSize: 11, letterSpacing: 1, cursor: canAfford && !unlocked ? "pointer" : "not-allowed", background: unlocked ? "#22c55e" : canAfford ? "#f59e0b" : "rgba(255,255,255,0.05)", color: unlocked || canAfford ? "#0a0a1f" : "#334155", transition: "all 0.2s" }}>
                      {unlocked ? "EARNED ✓" : canAfford ? "REDEEM" : "LOCKED"}
                    </button>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </>
  );

  // ────────────────────────────────────────────────────────── LEDGER
  const renderLedger = () => (
    <>
      {/* Productivity Tools */}
      <div style={C.card}>
        <div style={C.label}>🛠️ Productivity Exports</div>
        {exportNotice && (
          <div style={{ padding: "8px 12px", background: "rgba(34,197,94,0.1)", border: "1px solid rgba(34,197,94,0.25)", borderRadius: 8, fontSize: 12, color: "#4ade80", marginBottom: 12 }}>
            {exportNotice}
          </div>
        )}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(200px,1fr))", gap: 10 }}>
          {[
            { icon: "📊", label: "Export to Excel (.csv)", color: "#22c55e", action: exportCSV },
            { icon: "📋", label: "Copy Full Chat", color: "#22d3ee", action: () => copyToClipboard(scoutMessages.map((m) => `${m.role === "user" ? "Me" : "SCOUT"}: ${m.content}`).join("\n\n")) },
            {
              icon: "📝", label: "Copy Study Template", color: "#a78bfa",
              action: () => copyToClipboard("Date\tSubject\tTopic\tTime\tConfidence (1-5)\tQuestions Still Have\n" + new Date().toLocaleDateString() + "\t\t\t\t\t"),
            },
          ].map((item) => (
            <button key={item.label} onClick={item.action} style={{ background: `${item.color}0d`, border: `1px solid ${item.color}30`, color: item.color, borderRadius: 10, padding: "12px 14px", fontSize: 12, fontWeight: 700, cursor: "pointer", display: "flex", alignItems: "center", gap: 8, fontFamily: "inherit" }}>
              <span style={{ fontSize: 18 }}>{item.icon}</span>{item.label}
            </button>
          ))}
        </div>
        <div style={{ marginTop: 12, padding: "10px 14px", background: "rgba(245,158,11,0.06)", borderRadius: 8, fontSize: 12, color: "#94a3b8" }}>
          💡 <strong style={{ color: "#f59e0b" }}>Windows</strong>: Win+Shift+S to snip anything &nbsp;|&nbsp; <strong style={{ color: "#f59e0b" }}>Mac</strong>: Cmd+Shift+4
        </div>
      </div>

      {/* History */}
      <div style={C.label}>Transaction History</div>
      {history.length === 0 ? (
        <div style={{ textAlign: "center", padding: "50px 20px", color: "#334155" }}>
          <div style={{ fontSize: 40, marginBottom: 10 }}>📋</div>
          <div style={{ fontSize: 14 }}>Submit your first weekly timesheet to see history here.</div>
        </div>
      ) : (
        <>
          {history.map((entry, i) => (
            <div key={i} style={{ ...C.card, margin: "0 0 8px", display: "flex", alignItems: "center", gap: 14 }}>
              <div style={{ width: 38, height: 38, borderRadius: 8, background: entry.net >= 0 ? "rgba(5,46,22,0.5)" : "rgba(69,10,10,0.5)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 18, flexShrink: 0 }}>
                {entry.net >= 0 ? "📈" : "📉"}
              </div>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12, color: "#94a3b8", fontFamily: "monospace" }}>
                  {new Date(entry.date).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
                </div>
                <div style={{ fontSize: 11, color: "#334155", marginTop: 1 }}>
                  Gross {fmt(entry.gross)} → CoL -{fmt(WEEKLY_COL)}
                </div>
              </div>
              <div style={{ fontFamily: "monospace", fontWeight: 800, fontSize: 16, color: entry.net >= 0 ? "#22c55e" : "#ef4444" }}>
                {entry.net >= 0 ? "+" : ""}{fmt(entry.net)}
              </div>
            </div>
          ))}
          <div style={{ ...C.card, display: "flex", justifyContent: "space-between", background: "#1a1a2e" }}>
            <span style={{ fontWeight: 700 }}>Total Reward Accumulated</span>
            <span style={{ fontFamily: "monospace", fontWeight: 800, color: "#f59e0b", fontSize: 16 }}>{fmt(rewardFund)}</span>
          </div>
        </>
      )}
    </>
  );

  const TAB_CONTENT = { dashboard: renderDashboard, weekly: renderWeekly, scout: renderScout, videos: renderVideos, rewards: renderRewards, ledger: renderLedger };

  // ────────────────────────────────────────────────────────── RENDER
  return (
    <div style={C.root}>
      <style>{`
        @keyframes popIn { from { opacity:0; transform:scale(0.93) translateY(10px); } to { opacity:1; transform:scale(1) translateY(0); } }
        @keyframes pulse { 0%,100% { opacity:0.3; transform:scale(0.8); } 50% { opacity:1; transform:scale(1.1); } }
        button:hover { filter: brightness(1.1); }
        textarea:focus { border-color: rgba(34,211,238,0.4) !important; }
        ::-webkit-scrollbar { width: 5px; height: 5px; }
        ::-webkit-scrollbar-track { background: rgba(255,255,255,0.03); }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 3px; }
      `}</style>

      {/* ── Header */}
      <div style={C.header}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 14 }}>
          <div style={{ width: 40, height: 40, borderRadius: 10, background: "linear-gradient(135deg,#f59e0b,#22d3ee)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 20, boxShadow: "0 4px 14px rgba(245,158,11,0.3)" }}>⬡</div>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontSize: 18, fontWeight: 900, letterSpacing: -0.5 }}>K-9</span>
              <span style={{ fontSize: 12, color: "#64748b" }}>Financial Literacy OS × SCOUT Tutor</span>
              <span style={{ background: "#f59e0b", color: "#0a0a1f", fontSize: 8, fontWeight: 900, letterSpacing: 2, padding: "2px 7px", borderRadius: 4, fontFamily: "monospace" }}>PACKAI</span>
            </div>
            <div style={{ fontSize: 10, color: "#334155", fontFamily: "monospace", marginTop: 1 }}>Total Compensation Package v2.0 — Unified Module</div>
          </div>

          {/* Quick stats */}
          <div style={{ marginLeft: "auto", display: "flex", gap: 12 }}>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontFamily: "monospace", fontSize: 16, fontWeight: 800, color: "#f59e0b" }}>{fmt(rewardFund)}</div>
              <div style={{ fontSize: 9, color: "#334155", letterSpacing: 2 }}>FUND BALANCE</div>
            </div>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontFamily: "monospace", fontSize: 16, fontWeight: 800, color: net >= 0 ? "#22c55e" : "#ef4444" }}>{net >= 0 ? "+" : ""}{fmt(net)}</div>
              <div style={{ fontSize: 9, color: "#334155", letterSpacing: 2 }}>THIS WEEK NET</div>
            </div>
          </div>
        </div>

        <div style={C.tabBar}>
          {TABS.map((t) => (
            <button key={t.id} style={C.tab(activeTab === t.id)} onClick={() => setActiveTab(t.id)}>{t.label}</button>
          ))}
        </div>
      </div>

      {/* ── Body */}
      <div style={C.body}>{TAB_CONTENT[activeTab]?.()}</div>

      {/* ── Paycheck Modal */}
      {showPaycheck && (
        <PaycheckModal
          gross={history[0]?.gross ?? 0}
          net={history[0]?.net ?? 0}
          onClose={() => setShowPaycheck(false)}
        />
      )}
    </div>
  );
}
