// queue.js — 탭 전환 · 파일 선택/스테이징 · 큐 추가/삭제/취소/재시도 · 큐 탭 렌더

// ─── 서버 로그 조회 ─────────────────────────────────────
async function toggleServerLog() {
  const p = document.getElementById("server-logpanel");
  if (!p) return;
  if (p.style.display === "none") {
    p.style.display = "block";
    try {
      const d = await (await fetch("/api/logs?tail=200")).json();
      p.textContent = (d.lines || []).join("\n") || "(로그 없음)";
      p.scrollTop = p.scrollHeight;
    } catch(e) { p.textContent = "로그 조회 실패: " + e.message; }
  } else {
    p.style.display = "none";
  }
}

// ─── 탭 전환 ────────────────────────────────────────────
function go(name) {
  activePne = name;
  document.querySelectorAll(".tb").forEach((b, i) => {
    const on = ["queue","review","build"][i] === name;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", String(on));
  });
  document.querySelectorAll(".pane").forEach(p => p.classList.remove("on"));
  document.getElementById("pane-" + name).classList.add("on");
}

// ─── 인증 상태 실시간 갱신 ──────────────────────────────
async function refreshAuthIndicators() {
  const ytEl   = document.getElementById("auth-yt-indicator");
  const bandEl = document.getElementById("auth-band-indicator");
  try {
    const r = await fetch("/api/auth/youtube/status");
    const d = await r.json();
    ytAuth = !!d.ok;
    if (ytEl) {
      ytEl.innerHTML = d.ok
        ? `<span style="color:var(--green)">✔ YouTube 인증됨${d.channel ? " (" + d.channel.title + ")" : ""}</span>`
        : `<span style="color:var(--red)">✖ YouTube 미인증</span>&nbsp;<a href="/auth/youtube" style="font-size:12px;color:var(--blue)">인증하기 →</a>`;
    }
  } catch(e) {}
  try {
    const r = await fetch("/api/auth/band/status");
    const d = await r.json();
    bandAuth = !!d.ok;
    if (bandEl) {
      bandEl.innerHTML = d.ok
        ? `<span style="color:var(--green)">✔ BAND 인증됨</span>`
        : d.has_creds
          ? `<span style="color:var(--hint)">○ BAND 미인증</span>&nbsp;<a href="/auth/band" style="font-size:12px;color:var(--blue)">인증하기 →</a>`
          : `<span style="color:var(--hint)">○ BAND (설정 없음)</span>`;
    }
  } catch(e) {}
}

// ─── 파일 선택 ──────────────────────────────────────────
async function doBrowse() {
  try {
    const d = await post("/api/browse", {});
    if (d.paths && d.paths.length > 0) {
      const cur = document.getElementById("paths").value.trim();
      const newPaths = d.paths.join("\n");
      document.getElementById("paths").value = cur ? cur + "\n" + newPaths : newPaths;
      syncStaged();
    }
  } catch(e) { logLine("파일 선택 오류: " + e.message, "fail"); }
}

// ─── 스테이징 (영상별 민감도) ────────────────────────────
function onPathsInput() {
  clearTimeout(pathsTimer);
  pathsTimer = setTimeout(syncStaged, 350);
}

function syncStaged() {
  const raw = document.getElementById("paths").value;
  const newPaths = raw.split("\n").map(l => l.trim()).filter(Boolean);
  // 기존 설정 보존
  const prevMap = Object.fromEntries(staged.map(s => [s.path, s.sensitivity]));
  // 활성 잡 경로 (basename 기준 중복 경고용)
  const activeNames = new Set(
    allJobs.filter(j => !["done","error"].includes(j.status)).map(j => j.video_name)
  );
  staged = newPaths.map(p => ({
    path:        p,
    basename:    p.split(/[/\\]/).pop(),
    sensitivity: prevMap[p] || "normal",
    dup:         activeNames.has(p.split(/[/\\]/).pop()),
  }));
  renderStaged();
}

function renderStaged() {
  const area = document.getElementById("staged-area");
  const list = document.getElementById("staged-list");
  if (!staged.length) { area.style.display = "none"; return; }
  area.style.display = "block";
  document.getElementById("staged-count").textContent = staged.length + "개";

  list.innerHTML = staged.map((s, i) => `
    <div class="staged-row ${s.dup ? "dup" : ""}">
      <span class="staged-name" title="${esc(s.path)}">${esc(s.basename)}</span>
      ${s.dup ? '<span class="staged-dup-badge">⚠ 중복</span>' : ""}
      <select class="sens-sel" onchange="setStagedSens(${i}, this.value)">
        ${Object.entries(SENSITIVITIES).map(([k, v]) =>
          `<option value="${k}" ${s.sensitivity === k ? "selected" : ""}>${v.label}</option>`
        ).join("")}
      </select>
      <button class="ghost sm" onclick="removeStaged(${i})">×</button>
    </div>`).join("");
}

function setStagedSens(i, val) {
  if (staged[i]) staged[i].sensitivity = val;
}

function applyBulkSens(val) {
  if (!val) return;
  staged.forEach(s => s.sensitivity = val);
  renderStaged();
  const lbl = (SENSITIVITIES[val] || {}).label || val;
  logLine(`전체 ${staged.length}개 민감도를 "${lbl}"로 설정`, "done");
}

function removeStaged(i) {
  staged.splice(i, 1);
  // textarea와 동기화
  document.getElementById("paths").value = staged.map(s => s.path).join("\n");
  renderStaged();
}

function clearStaged() {
  staged = [];
  document.getElementById("paths").value = "";
  renderStaged();
}

// ─── 큐 추가 ────────────────────────────────────────────
async function doAdd() {
  if (!staged.length) { syncStaged(); }
  const toAdd = staged.filter(s => !s.dup);
  const dupCount = staged.filter(s => s.dup).length;
  if (!toAdd.length) {
    if (dupCount) logLine(`${dupCount}개 모두 이미 처리 중(중복). 건너뜁니다.`, "fail");
    return;
  }
  const workers  = parseInt(document.getElementById("workers").value) || 6;
  const pre_sec  = parseFloat(document.getElementById("setting-pre")?.value) || 8;
  const post_sec = parseFloat(document.getElementById("setting-post")?.value) || 5;
  const jobs = toAdd.map(s => ({video: s.path, sensitivity: s.sensitivity, workers, pre_sec, post_sec}));
  logLine(`영상 ${jobs.length}개 큐에 추가 중…`, "start");
  const btn = document.getElementById("btn-add");
  btn.disabled = true;
  try {
    const d = await post("/api/jobs/add", {jobs, workers});
    if (d.added && d.added.length > 0) {
      clearStaged();
      logLine(`${d.added.length}개 추가 완료 — 자동 처리 시작`, "done");
    }
    (d.skipped || []).forEach(n => logLine(`중복 건너뜀: ${n}`, "fail"));
    (d.errors  || []).forEach(e => logLine(e, "fail"));
    if (dupCount) logLine(`${dupCount}개 중복 항목 건너뜀`, "fail");
  } catch(e) { logLine("추가 오류: " + e.message, "fail"); }
  finally { btn.disabled = false; }
}

// ─── 큐 탭 렌더 ─────────────────────────────────────────
function renderQueue() {
  const h = JSON.stringify(allJobs.map(j =>
    [j.id, j.status, j.progress, j.n_candidates, j.elapsed_sec,
     j.n_auto, j.n_maybe, j.n_approved]));
  if (h === lastQueueHash) return;
  lastQueueHash = h;

  // 요약 바
  const sumEl = document.getElementById("queue-summary");
  if (allJobs.length > 0) {
    sumEl.style.display = "flex";
    const done    = allJobs.filter(j => ["ready","done"].includes(j.status)).length;
    const proc    = allJobs.filter(j => ["detecting","classifying","building"].includes(j.status)).length;
    const pending = allJobs.filter(j => j.status === "pending").length;
    const elapsed = allJobs.filter(j => j.elapsed_sec > 0).map(j => j.elapsed_sec);
    let timeHtml = "";
    if (elapsed.length > 0) {
      const avg = elapsed.reduce((a, b) => a + b, 0) / elapsed.length;
      // pending(미시작) + proc(처리중) 모두 잔여로 계산
      const remaining = (pending + proc) * avg;
      timeHtml = `<span class="qs-time">· 영상당 평균 ${fmtSec(avg)} · 예상 잔여 ~${fmtSec(remaining)}</span>`;
    } else if (pending + proc > 0) {
      timeHtml = `<span class="qs-time">· 영상당 약 5분 예상</span>`;
    }
    sumEl.innerHTML =
      `<span>전체 ${allJobs.length}개</span>` +
      (done    ? `<span class="qs-done">완료 ${done}개</span>` : "") +
      (proc    ? `<span class="qs-proc">처리 중 ${proc}개</span>` : "") +
      (pending ? `<span class="qs-wait">대기 ${pending}개</span>` : "") +
      timeHtml;
  } else {
    sumEl.style.display = "none";
  }

  const el = document.getElementById("job-list");
  if (!allJobs.length) {
    el.innerHTML = '<div class="hint empty">아직 추가된 영상이 없습니다.</div>';
    return;
  }

  const SLABELS = Object.fromEntries(
    Object.entries(SENSITIVITIES).map(([k, v]) => [k, v.label]));

  let html = "";
  for (const j of allJobs) {
    const prog = j.progress || {};
    let progHtml = "";
    if (j.status === "detecting") {
      progHtml = `<div style="margin-top:4px"><span class="spin"></span><span class="hint">오디오 분석 중…</span></div>`;
    } else if (["classifying","building"].includes(j.status) && prog.total > 0) {
      const pct = Math.round(prog.done / prog.total * 100);
      const lbl = j.status === "classifying"
        ? `판별 ${prog.done}/${prog.total}` : `클립 ${prog.done}/${prog.total}`;
      progHtml = `<div style="margin-top:4px">
        <div class="bar"><i style="width:${pct}%"></i></div>
        <span class="hint">${lbl} (${pct}%)</span></div>`;
    }
    const meta = [];
    if (j.duration)      meta.push(fmtDur(j.duration));
    if (j.sensitivity)   meta.push(`민감도: ${SLABELS[j.sensitivity]||j.sensitivity}`);
    if (j.n_candidates)  meta.push(`후보 ${j.n_candidates}개`);
    if (j.n_auto)        meta.push(`<b style="color:var(--accent)">AI채택 ${j.n_auto}</b>`);
    if (j.n_maybe)       meta.push(`<span style="color:var(--warn)">확인필요 ${j.n_maybe}</span>`);
    if (j.usage)         meta.push(`$${j.usage.cost_usd}`);
    if (j.elapsed_sec)   meta.push(`소요 ${fmtSec(j.elapsed_sec)}`);
    if (j.n_approved !== null) meta.push(`<b style="color:var(--blue)">✓ ${j.n_approved}개 저장</b>`);
    // 하이라이트 없음 경고 (판별 완료했으나 채택/확인필요 모두 0)
    const noHighlight = j.status === "ready" && j.vision_used
      && !j.n_auto && !j.n_maybe;
    if (noHighlight)
      meta.push(`<span style="color:var(--warn)">⚠ 하이라이트 없음 — 임계 낮춰 재검토</span>`);
    // API 키 없이 처리된 잡 (비전 판별 생략 — 모든 후보가 채택 취급됨)
    if (["ready","done"].includes(j.status) && !j.vision_used)
      meta.push(`<span style="color:var(--warn)">⚠ AI 판별 생략됨 (GEMINI_API_KEY 없음)</span>`);
    const err = j.error
      ? `<div class="hint fail" style="margin-top:3px;white-space:pre-wrap;word-break:break-word">오류: ${esc(j.error)}</div>` : "";
    const retryBtn = j.status === "error"
      ? `<button class="ghost sm" onclick="doRetry('${j.id}')">재시도</button>` : "";
    const cancelBtn = ["detecting","classifying","building"].includes(j.status)
      ? `<button class="ghost sm" onclick="doCancel('${j.id}')">취소</button>` : "";
    html += `<div style="display:flex;align-items:flex-start;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)">
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span style="font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:360px">${esc(j.video_name)}</span>
          ${stBadge(j.status)}
        </div>
        <div class="hint" style="margin-top:2px">${meta.join(" &middot; ")}</div>
        ${progHtml}${err}
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0">${retryBtn}${cancelBtn}
        <button class="ghost sm" onclick="doDelete('${j.id}')">삭제</button>
      </div>
    </div>`;
  }
  el.innerHTML = html;
}

// ─── 잡 삭제/취소/재시도 ────────────────────────────────
async function doDelete(jid) {
  const resp = await post(`/api/jobs/${jid}/delete`, {});
  if (resp && resp.deferred) {
    // 처리 중인 작업 — 즉시 사라지지 않고, 취소 완료 후 폴링으로 목록에서 빠짐
    logLine("처리 중인 작업 삭제 요청 — 완료 후 자동으로 정리됩니다.", "start");
    lastQueueHash = "";
    return;
  }
  delete jobCands[jid]; delete localSel[jid];
  delete savedSel[jid]; delete expanded[jid];
  delete confVal[jid];  delete buildData[jid];
  delete prevStatuses[jid];
  autoUploaded.delete(jid);
  persistBuildData();
  lastQueueHash = ""; lastReviewHash = ""; lastBuildHash = "";
}

async function doCancel(jid) {
  const name = allJobs.find(j => j.id === jid)?.video_name || jid;
  try {
    await post(`/api/jobs/${jid}/cancel`, {});
    logLine(`취소 요청: ${name}`, "start");
    lastQueueHash = "";
  } catch(e) { logLine(`취소 실패: ${e.message}`, "fail"); }
}

async function doRetry(jid) {
  const name = allJobs.find(j => j.id === jid)?.video_name || jid;
  try {
    await post(`/api/jobs/${jid}/retry`, {});
    autoUploaded.delete(jid);
    logLine(`재시도: ${name} — 큐에 재투입`, "start");
    lastQueueHash = ""; lastReviewHash = ""; lastBuildHash = "";
  } catch(e) { logLine(`재시도 실패: ${e.message}`, "fail"); }
}

async function clearDone() {
  for (const j of allJobs.filter(x => x.status === "done")) await doDelete(j.id);
}
