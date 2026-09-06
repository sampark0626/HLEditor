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
    const on = ["queue","review","build","dotplay"][i] === name;
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
      if (d.ok && d.reason === "network") {
        // 네트워크 문제로 확인만 실패한 상태 — 토큰은 살아 있으므로 미인증이 아니다.
        ytEl.innerHTML = `<span style="color:var(--hint)">◐ YouTube 인증 확인 실패 (네트워크) — 저장된 인증으로 진행</span>`;
      } else if (d.ok) {
        ytEl.innerHTML = `<span style="color:var(--green)">✔ YouTube 인증됨${d.channel ? " (" + d.channel.title + ")" : ""}</span>`;
      } else {
        const why = d.reason === "no_channel" ? " (채널 없음)" : "";
        ytEl.innerHTML = `<span style="color:var(--red)">✖ YouTube 미인증${why}</span>&nbsp;<a href="/auth/youtube" style="font-size:12px;color:var(--blue)">인증하기 →</a>`;
      }
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
  // 합치기 그룹 정리: 목록에서 사라진 파트는 제거, 2개 미만 남은 그룹은 해제
  const present = new Set(newPaths);
  mergeGroups = mergeGroups
    .map(g => g.filter(p => present.has(p)))
    .filter(g => g.length >= 2);
  for (const p of [...stagedChecked]) if (!present.has(p)) stagedChecked.delete(p);
  renderStaged();
}

// path → 그 파트가 속한 그룹(배열) 또는 null
function _groupOf(path) {
  return mergeGroups.find(g => g.includes(path)) || null;
}

function toggleStagedCheck(path, on) {
  if (on) stagedChecked.add(path); else stagedChecked.delete(path);
  // 합치기 버튼 활성/비활성만 갱신 (전체 재렌더 불필요)
  const btn = document.getElementById("btn-merge-sel");
  if (btn) btn.disabled = stagedChecked.size < 2;
}

function mergeSelected() {
  const picked = staged.map(s => s.path).filter(p => stagedChecked.has(p) && !_groupOf(p));
  if (picked.length < 2) return;
  mergeGroups.push(picked);
  stagedChecked.clear();
  renderStaged();
  logLine(`${picked.length}개 파일을 한 경기로 묶었습니다 — 하나의 하이라이트로 생성됩니다`, "done");
}

function ungroupMerge(firstPath) {
  const g = _groupOf(firstPath);
  if (!g) return;
  mergeGroups = mergeGroups.filter(x => x !== g);
  renderStaged();
}

function _sensSelectHtml(sel, onchange) {
  return `<select class="sens-sel" onchange="${onchange}">
    ${Object.entries(SENSITIVITIES).map(([k, v]) =>
      `<option value="${k}" ${sel === k ? "selected" : ""}>${v.label}</option>`
    ).join("")}</select>`;
}

function renderStaged() {
  const area = document.getElementById("staged-area");
  const list = document.getElementById("staged-list");
  if (!staged.length) { area.style.display = "none"; return; }
  area.style.display = "block";

  const nGroups = mergeGroups.length;
  const nInGroups = mergeGroups.reduce((a, g) => a + g.length, 0);
  const nUnits = staged.length - nInGroups + nGroups;
  document.getElementById("staged-count").textContent =
    nGroups ? `${staged.length}개 파일 · ${nUnits}개 작업` : `${staged.length}개`;

  const seenGroups = new Set();
  let rows = "";
  staged.forEach((s, i) => {
    const g = _groupOf(s.path);
    if (g) {
      if (seenGroups.has(g)) return;        // 그룹의 첫 파트에서만 블록을 그린다
      seenGroups.add(g);
      const members = staged.filter(x => g.includes(x.path));
      const gi = staged.findIndex(x => x.path === g[0]);
      const dupAny = members.some(m => m.dup);
      rows += `
        <div class="staged-row group ${dupAny ? "dup" : ""}" style="flex-wrap:wrap">
          <span class="staged-name" style="font-weight:600">
            🎬 한 경기 (${members.length}개 파트 → 하이라이트 1개)
          </span>
          ${dupAny ? '<span class="staged-dup-badge">⚠ 중복</span>' : ""}
          ${_sensSelectHtml(members[0].sensitivity, `setStagedSens(${gi}, this.value, true)`)}
          <button class="ghost sm" onclick="ungroupMerge('${esc(g[0])}')">묶음 해제</button>
          <div style="flex-basis:100%;padding:4px 0 0 22px">
            ${members.map(m => `<div class="hint" style="font-size:12px">• ${esc(m.basename)}</div>`).join("")}
          </div>
        </div>`;
    } else {
      rows += `
        <div class="staged-row ${s.dup ? "dup" : ""}">
          <input type="checkbox" title="합치기 대상 선택"
                 ${stagedChecked.has(s.path) ? "checked" : ""}
                 onchange="toggleStagedCheck('${esc(s.path)}', this.checked)">
          <span class="staged-name" title="${esc(s.path)}">${esc(s.basename)}</span>
          ${s.dup ? '<span class="staged-dup-badge">⚠ 중복</span>' : ""}
          ${_sensSelectHtml(s.sensitivity, `setStagedSens(${i}, this.value)`)}
          <button class="ghost sm" onclick="removeStaged(${i})">×</button>
        </div>`;
    }
  });

  list.innerHTML = `
    <div style="margin-bottom:6px">
      <button class="ghost sm" id="btn-merge-sel" onclick="mergeSelected()"
              ${stagedChecked.size < 2 ? "disabled" : ""}>
        선택한 파일 한 경기로 합치기
      </button>
      <span class="hint" style="font-size:11px">
        XbotGo가 30분에서 잘라 저장한 파트들을 체크해 합치면 하이라이트가 1개로 만들어집니다
      </span>
    </div>` + rows;
}

// isGroup=true 면 그룹의 모든 파트 민감도를 함께 바꾼다
function setStagedSens(i, val, isGroup) {
  if (!staged[i]) return;
  if (isGroup) {
    const g = _groupOf(staged[i].path);
    if (g) { staged.forEach(s => { if (g.includes(s.path)) s.sensitivity = val; }); return; }
  }
  staged[i].sensitivity = val;
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
  // textarea와 동기화 (syncStaged가 그룹 정리까지 처리)
  document.getElementById("paths").value = staged.map(s => s.path).join("\n");
  syncStaged();
}

function clearStaged() {
  staged = [];
  mergeGroups = [];
  stagedChecked.clear();
  document.getElementById("paths").value = "";
  renderStaged();
}

// ─── staged + mergeGroups → /api/jobs/add 의 jobs[] 항목 ────
// 합치기 그룹은 {videos:[...]}, 단일 파일은 {video:"..."}. 중복 파트가 든 항목은 제외.
function stagedToJobItems(opts) {
  const items = [];
  const seen = new Set();
  for (const s of staged) {
    if (seen.has(s.path)) continue;
    const g = _groupOf(s.path);
    if (g) {
      const members = staged.filter(x => g.includes(x.path));
      members.forEach(m => seen.add(m.path));
      if (members.some(m => m.dup)) continue;
      if (members.length >= 2) {
        items.push({videos: members.map(m => m.path),
                    sensitivity: members[0].sensitivity, ...opts});
      } else if (members.length === 1) {
        // 그룹이 파트 1개로 쪼그라든 경우 — 단일 파일 잡으로 처리
        items.push({video: members[0].path, sensitivity: members[0].sensitivity, ...opts});
      }
    } else {
      seen.add(s.path);
      if (s.dup) continue;
      items.push({video: s.path, sensitivity: s.sensitivity, ...opts});
    }
  }
  return items;
}

// ─── 큐 추가 ────────────────────────────────────────────
async function doAdd() {
  if (!staged.length) { syncStaged(); }
  const dupCount = staged.filter(s => s.dup).length;
  const workers  = parseInt(document.getElementById("workers").value) || 6;
  const pre_sec  = parseFloat(document.getElementById("setting-pre")?.value) || 8;
  const post_sec = parseFloat(document.getElementById("setting-post")?.value) || 5;
  const jobs = stagedToJobItems({workers, pre_sec, post_sec});
  if (!jobs.length) {
    const msg = dupCount
      ? `추가할 작업이 없습니다 — ${dupCount}개 항목이 모두 이미 큐에 있습니다(중복).`
      : "추가할 영상이 없습니다. 파일을 선택하거나 경로를 입력하세요.";
    logLine(msg, "fail");
    alert(msg);
    return;
  }
  logLine(`작업 ${jobs.length}개 큐에 추가 중…`, "start");
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
    // 하나도 못 넣었으면 사용자에게 눈에 보이게 알린다 (로그 패널은 화면 밖일 수 있음)
    if (!d.added || !d.added.length) {
      const reasons = [...(d.skipped || []).map(n => `중복: ${n}`),
                       ...(d.errors || [])];
      alert("큐에 추가된 작업이 없습니다.\n" +
            (reasons.length ? reasons.join("\n") : "이미 처리 중이거나 파일을 찾을 수 없습니다."));
    }
  } catch(e) {
    logLine("추가 오류: " + e.message, "fail");
    alert("추가 오류: " + e.message);
  }
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
    const proc    = allJobs.filter(j => ["merging","detecting","classifying","building"].includes(j.status)).length;
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
    if (j.status === "merging") {
      progHtml = `<div style="margin-top:4px"><span class="spin"></span><span class="hint">${j.n_parts || ""}개 파트를 한 경기로 병합 중…</span></div>`;
    } else if (j.status === "detecting") {
      // 검출 단계는 오디오 분석 → 팬 궤적 분석 두 구간으로 나뉜다.
      // 팬 구간(30분 영상 기준 1~2분)에도 라벨을 바꿔줘야 멈춘 것처럼 보이지 않는다.
      const lbl = prog.stage === "pan" ? "카메라 팬 궤적 분석 중…" : "오디오 분석 중…";
      progHtml = `<div style="margin-top:4px"><span class="spin"></span><span class="hint">${lbl}</span></div>`;
    } else if (["classifying","building"].includes(j.status) && prog.total > 0) {
      const pct = Math.round(prog.done / prog.total * 100);
      const lbl = j.status === "classifying"
        ? `판별 ${prog.done}/${prog.total}` : `클립 ${prog.done}/${prog.total}`;
      progHtml = `<div style="margin-top:4px">
        <div class="bar"><i style="width:${pct}%"></i></div>
        <span class="hint">${lbl} (${pct}%)</span></div>`;
    }
    const meta = [];
    if (j.n_parts > 1)  meta.push(`<span style="color:var(--blue)">🎬 ${j.n_parts}개 파트 합본</span>`);
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
    const cancelBtn = ["merging","detecting","classifying","building"].includes(j.status)
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
