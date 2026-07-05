// utils.js — 공용 유틸리티 (fetch 헬퍼, 포맷팅, 로그 패널)

async function post(url, body) {
  const r = await fetch(url, {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "요청 실패");
  return d;
}

function stBadge(s) {
  const L = {pending:"대기중",detecting:"검출중",classifying:"판별중",
             ready:"준비됨",building:"생성중",done:"완료",error:"오류"};
  return `<span class="st st-${s}">${L[s]||s}</span>`;
}

function fmtDur(s) {
  const m = Math.floor(s/60), sec = Math.round(s%60);
  return m > 0 ? `${m}분 ${sec}초` : `${sec}초`;
}

function fmtSec(s) {
  if (!s || s <= 0) return "0초";
  const m = Math.floor(s/60), sec = Math.round(s%60);
  return m > 0 ? `${m}분 ${sec}초` : `${sec}초`;
}

function esc(s) {
  return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")
                      .replace(/>/g,"&gt;").replace(/"/g,"&quot;")
                      .replace(/'/g,"&#39;");
}

// 로컬 시간대 기준 YYYY-MM-DD (toISOString의 UTC 오프바이원 방지)
function localDateStr(d = new Date()) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function logLine(msg, kind="") {
  const p = document.getElementById("logpanel");
  if (!logStarted) { p.innerHTML = ""; logStarted = true; }
  const t = new Date().toLocaleTimeString("ko-KR", {hour12:false});
  const d = document.createElement("div");
  d.className = "ln " + kind;
  d.innerHTML = `<span class="t">[${t}]</span> ${msg}`;
  p.appendChild(d);
  p.scrollTop = p.scrollHeight;
}
