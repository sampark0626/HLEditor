// review.js — 리뷰 탭: 후보 목록 조회/승인, 신뢰도 임계 조정

// ─── 리뷰 탭 렌더 ───────────────────────────────────────
function renderReview() {
  const reviewable = allJobs.filter(j => ["ready","done"].includes(j.status));
  const h = JSON.stringify(reviewable.map(j =>
    [j.id, j.status, j.n_approved, j.n_auto, j.n_maybe]));
  if (h === lastReviewHash) return;
  lastReviewHash = h;

  const el = document.getElementById("review-list");
  if (!reviewable.length) {
    el.innerHTML = '<div class="hint" style="padding:24px 0;text-align:center">준비된 영상이 없습니다. 큐 탭에서 영상을 추가하고 처리를 기다리세요.</div>';
    return;
  }

  // 열린 아코디언 body 내용 보존
  const openBodies = {};
  reviewable.forEach(j => {
    const el = document.getElementById("jb-" + j.id);
    if (el && expanded[j.id]) openBodies[j.id] = el.innerHTML;
  });

  let html = "";
  for (const j of reviewable) {
    const isOpen  = !!expanded[j.id];
    const nSaved  = j.n_approved;
    const savedMk = nSaved !== null
      ? `<span style="color:var(--accent);font-size:12px;white-space:nowrap">✓ ${nSaved}개 저장</span>` : "";
    const unsavedMk = nSaved === null
      ? `<span style="color:var(--warn);font-size:11px;white-space:nowrap">미저장</span>` : "";
    const noHl = j.vision_used && !j.n_auto && !j.n_maybe;
    const meta = [
      j.duration ? fmtDur(j.duration) : "",
      `후보 ${j.n_candidates}개`,
      j.n_auto  ? `AI채택 ${j.n_auto}` : "",
      j.n_maybe ? `확인필요 ${j.n_maybe}` : "",
      noHl ? "⚠ 하이라이트 없음" : "",
      !j.vision_used ? "⚠ AI 판별 생략됨 (API 키 없음)" : "",
    ].filter(Boolean).join(" / ");

    html += `
    <div class="ji" id="ji-${j.id}">
      <div class="jh" role="button" tabindex="0" aria-expanded="${isOpen}" aria-controls="jb-${j.id}"
        onclick="toggleJob('${j.id}')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleJob('${j.id}');}">
        <span class="chev ${isOpen?"open":""}">▶</span>
        <span class="jn">${esc(j.video_name)}</span>
        ${stBadge(j.status)}
        <span class="hint" style="white-space:nowrap">${meta}</span>
        ${savedMk}${unsavedMk}
      </div>
      <div class="jb" id="jb-${j.id}" style="display:${isOpen?"block":"none"}">
        ${openBodies[j.id] || '<div class="hint">로딩 중…</div>'}
      </div>
    </div>`;
  }
  el.innerHTML = html;

  // 열린 항목 복원
  reviewable.forEach(j => {
    if (expanded[j.id]) {
      if (jobCands[j.id]) renderCands(j.id);
      else loadCands(j.id);
    }
  });
}

async function toggleJob(jid) {
  expanded[jid] = !expanded[jid];
  const body = document.getElementById("jb-" + jid);
  const chev = document.querySelector(`#ji-${jid} .chev`);
  const header = document.querySelector(`#ji-${jid} .jh`);
  if (header) header.setAttribute("aria-expanded", String(expanded[jid]));
  if (!body) return;
  if (expanded[jid]) {
    body.style.display = "block";
    chev && chev.classList.add("open");
    if (!jobCands[jid]) await loadCands(jid);
    else renderCands(jid);
  } else {
    body.style.display = "none";
    chev && chev.classList.remove("open");
  }
}

async function loadCands(jid) {
  try {
    const conf = confVal[jid] || globalConf;
    const d = await (await fetch(`/api/jobs/${jid}/candidates?conf=${conf}`)).json();
    jobCands[jid] = {cands: d.candidates, visionUsed: d.vision_used};
    if (localSel[jid] === undefined) {
      localSel[jid] = d.approved !== null
        ? new Set(d.approved)
        : new Set(d.candidates.filter(c => c.flag === "auto").map(c => c.idx));
      if (d.approved !== null) savedSel[jid] = d.approved;
    }
    if (!confVal[jid]) confVal[jid] = conf;
    renderCands(jid);
  } catch(e) {
    const el = document.getElementById("jb-" + jid);
    if (el) el.innerHTML = `<span class="hint fail">로드 실패: ${e.message}</span>`;
  }
}

function renderCands(jid) {
  const data = jobCands[jid];
  if (!data) return;
  const {cands, visionUsed} = data;
  const sel  = localSel[jid] || new Set();
  const conf = confVal[jid]  || globalConf;

  let rows = "";
  for (const c of cands) {
    const chk = sel.has(c.idx) ? "checked" : "";
    const tag = c.flag === "auto"  ? `<span class="tag auto">채택</span>`
              : c.flag === "maybe" ? `<span class="tag maybe">확인필요</span>`
              :                      `<span class="tag reject">제외</span>`;
    const t   = c.peak;
    const ts  = `${Math.floor(t/60)}:${String(Math.floor(t%60)).padStart(2,"0")}`;
    rows += `<tr class="${c.flag}">
      <td style="text-align:center"><input type="checkbox" ${chk}
        onchange="toggleCand('${jid}',${c.idx},this.checked)"></td>
      <td>${c.idx+1}</td>
      <td>
        <img src="/api/jobs/${jid}/thumb/${c.idx}" loading="lazy" alt=""
             style="width:64px;height:36px;object-fit:cover;border-radius:4px;background:#000;display:block"
             onerror="this.style.display='none'">
      </td>
      <td style="white-space:nowrap">${ts}
        <button class="ghost sm" style="margin-left:4px" onclick="previewCand('${jid}', ${c.peak})">▶ 미리보기</button>
      </td>
      <td>${c.type}</td>
      <td style="white-space:nowrap">${visionUsed ? c.confidence + (c.pan_bonus
        ? ` <span style="color:var(--accent);font-size:11px;cursor:help" title="카메라 팬 신호(${esc(c.pan_label || "")}) 보정: ${c.confidence} → ${c.conf_eff.toFixed(2)}">▲${c.conf_eff.toFixed(2)}</span>`
        : "") : "-"}</td>
      <td>${tag}</td>
      <td style="color:var(--muted);font-size:12px;max-width:300px;overflow:hidden;
                 text-overflow:ellipsis;white-space:nowrap" title="${esc(c.reason)}">${esc(c.reason)}</td>
    </tr>`;
  }

  const n = sel.size;
  // 다음 미저장 영상이 있는지 확인
  const reviewable = allJobs.filter(j => ["ready","done"].includes(j.status));
  const jids = reviewable.map(j => j.id);
  const ci   = jids.indexOf(jid);
  const hasNext = jids.slice(ci + 1).concat(jids.slice(0, ci))
    .some(id => allJobs.find(j => j.id === id)?.n_approved === null);
  const saveBtnLabel = hasNext ? `저장 &amp; 다음 →` : `저장 (${n}개)`;

  document.getElementById("jb-" + jid).innerHTML = `
  <div class="sl-row" style="margin-bottom:10px">
    <span class="hint" style="white-space:nowrap">이 영상 신뢰도 임계</span>
    <input type="range" min="0.3" max="0.95" step="0.05" value="${conf}"
      oninput="onJobConf('${jid}', this.value)">
    <span class="cv" id="cv-${jid}">${conf.toFixed(2)}</span>
    <button class="ghost sm" onclick="aiSelect('${jid}')">AI 기본 선택</button>
    <button class="ghost sm" onclick="selAll('${jid}',true)">전체 선택</button>
    <button class="ghost sm" onclick="selAll('${jid}',false)">전체 해제</button>
  </div>
  <!-- 오디오 타임라인 시계열 차트 컨테이너 -->
  <div class="chart-container" id="audio-chart-${jid}">
    <div class="hint" style="padding:10px 0;text-align:center">오디오 신호 대기 중...</div>
  </div>
  <video id="preview-${jid}" style="width:100%;max-height:280px;background:#000;border-radius:8px;margin-bottom:10px;display:none" controls></video>
  <div style="overflow-x:auto;max-height:420px;overflow-y:auto">
    <table>
      <thead><tr>
        <th style="width:32px">포함</th>
        <th>#</th><th>썸네일</th><th>시각</th><th>유형</th><th>신뢰도</th><th>AI 판정</th><th>AI 설명</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>
  <div class="row" style="margin-top:12px;justify-content:space-between">
    <span class="hint" id="sc-${jid}">${n}개 선택됨</span>
    <button onclick="saveApproved('${jid}')">${saveBtnLabel}</button>
  </div>`;

  // 차트 렌더링/로딩 자동 트리거
  setTimeout(() => {
    if (jobSignals[jid]) renderAudioChart(jid);
    else loadAudioSignal(jid);
  }, 0);
}

// ─── 구간 미리보기 ───────────────────────────────────────
function previewCand(jid, peak) {
  const vid = document.getElementById("preview-" + jid);
  if (!vid) return;
  const job = allJobs.find(j => j.id === jid);
  const pre = job?.pre_sec ?? 8;
  const start = Math.max(0, peak - pre);
  vid.style.display = "block";
  const seekAndPlay = () => { vid.currentTime = start; vid.play().catch(() => {}); };
  if (vid.dataset.srcLoaded === jid) {
    seekAndPlay();
  } else {
    vid.src = `/api/jobs/${jid}/source`;
    vid.dataset.srcLoaded = jid;
    vid.addEventListener("loadedmetadata", seekAndPlay, {once: true});
  }
}

function onJobConf(jid, val) {
  confVal[jid] = parseFloat(val);
  const el = document.getElementById("cv-" + jid);
  if (el) el.textContent = parseFloat(val).toFixed(2);
  const data = jobCands[jid];
  if (!data) return;
  recomputeFlags(data.cands, parseFloat(val));
  renderCands(jid);
}

// ─── 전역 신뢰도 슬라이더 ────────────────────────────────
function onGlobalConf(val) {
  globalConf = parseFloat(val);
  document.getElementById("gcv").textContent = globalConf.toFixed(2);
  LS.set("globalConf", globalConf);
}

function applyGlobalConf() {
  // 모든 로드된 잡에 globalConf 적용
  for (const jid in jobCands) {
    confVal[jid] = globalConf;
    const data = jobCands[jid];
    if (data) recomputeFlags(data.cands, globalConf);
    // 열린 아코디언만 즉시 재렌더
    if (expanded[jid]) renderCands(jid);
  }
  // 아직 로드 안 된 잡들은 loadCands 시 globalConf 사용
  logLine(`신뢰도 임계 ${globalConf.toFixed(2)} 전체 적용`, "done");
}

function recomputeFlags(cands, conf) {
  cands.forEach(c => {
    if (!c.highlight) { c.flag = "reject"; return; }
    // 서버와 동일하게 보정 신뢰도(conf_eff = confidence + 팬 가산치)로 판정
    const eff = c.conf_eff != null ? c.conf_eff : c.confidence;
    c.flag = eff >= conf ? "auto"
           : eff >= CONF_MAYBE ? "maybe" : "reject";
  });
}

// ─── 후보 선택 조작 ─────────────────────────────────────
function aiSelect(jid) {
  const data = jobCands[jid];
  if (!data) return;
  localSel[jid] = new Set(data.cands.filter(c => c.flag === "auto").map(c => c.idx));
  renderCands(jid);
}

function selAll(jid, on) {
  const data = jobCands[jid];
  if (!data) return;
  localSel[jid] = on ? new Set(data.cands.map(c => c.idx)) : new Set();
  renderCands(jid);
}

function toggleCand(jid, idx, checked) {
  if (!localSel[jid]) localSel[jid] = new Set();
  checked ? localSel[jid].add(idx) : localSel[jid].delete(idx);
  const n  = localSel[jid].size;
  const sc = document.getElementById("sc-" + jid);
  if (sc) sc.textContent = n + "개 선택됨";
  // 저장 버튼 텍스트 업데이트
  const jb = document.getElementById("jb-" + jid);
  if (jb) {
    const btn = jb.querySelector("button[onclick*='saveApproved']");
    if (btn && !btn.textContent.includes("→")) btn.textContent = `저장 (${n}개)`;
  }
}

// ─── 저장 & 다음 영상 자동 이동 ──────────────────────────
async function saveApproved(jid) {
  const sel = localSel[jid] ? [...localSel[jid]] : [];
  try {
    await post(`/api/jobs/${jid}/approve`, {approved: sel});
    savedSel[jid] = sel;
    const j = allJobs.find(x => x.id === jid);
    if (j) j.n_approved = sel.length;
    updateBadges();
    logLine(`저장: ${allJobs.find(x=>x.id===jid)?.video_name||jid} — ${sel.length}개`, "done");
    lastReviewHash = "";
    lastBuildHash  = "";

    // 다음 미저장 영상 찾아서 자동 이동
    const reviewable = allJobs.filter(x => ["ready","done"].includes(x.status));
    const jids = reviewable.map(x => x.id);
    const ci   = jids.indexOf(jid);
    let nextJid = null;
    // 현재 뒤부터 탐색
    for (let i = ci + 1; i < jids.length; i++) {
      if (allJobs.find(x => x.id === jids[i])?.n_approved === null) {
        nextJid = jids[i]; break;
      }
    }
    // 없으면 앞쪽 탐색
    if (!nextJid) {
      for (let i = 0; i < ci; i++) {
        if (allJobs.find(x => x.id === jids[i])?.n_approved === null) {
          nextJid = jids[i]; break;
        }
      }
    }

    // 현재 아코디언 닫기
    expanded[jid] = false;

    if (nextJid) {
      // 다음 영상 열기
      expanded[nextJid] = true;
      renderReview();
      if (!jobCands[nextJid]) await loadCands(nextJid);
      else renderCands(nextJid);
      setTimeout(() => {
        document.getElementById("ji-" + nextJid)
          ?.scrollIntoView({behavior:"smooth", block:"center"});
      }, 120);
    } else {
      // 모두 저장 완료
      renderReview();
      logLine("모든 영상 저장 완료 → 영상 생성 탭으로 이동합니다.", "done");
      sendNotif("리뷰 완료", "모든 영상 저장 완료. 영상 생성을 시작하세요.");
      setTimeout(() => go("build"), 600);
    }
  } catch(e) { logLine("저장 실패: " + e.message, "fail"); }
}

// ─── 일괄 저장 & 생성 ────────────────────────────────────
async function bulkSaveAndBuild() {
  const readyCount = allJobs.filter(j => ["ready","done"].includes(j.status)).length;
  if (!readyCount) {
    logLine("준비된 영상이 없습니다.", "fail"); return;
  }
  if (!confirm(`AI 판단(신뢰도 ${globalConf.toFixed(2)} 이상)으로 ${readyCount}개 영상을 일괄 저장 후 영상 생성을 시작합니다.\n계속하시겠습니까?`))
    return;

  try {
    logLine(`전체 AI 기본값 저장 (임계 ${globalConf.toFixed(2)})…`, "start");
    const ad = await post("/api/jobs/approve-all", {conf: globalConf});
    logLine(`전체 저장 완료: ${ad.total}개 영상`, "done");

    let zeroCnt = 0;
    for (const item of (ad.saved || [])) {
      const j = allJobs.find(x => x.id === item.id);
      if (j) j.n_approved = item.n;
      savedSel[item.id] = [];
      if (item.n === 0) zeroCnt++;
    }
    if (zeroCnt)
      logLine(`⚠ ${zeroCnt}개 영상은 채택 구간 0개 — 임계를 낮추거나 리뷰에서 직접 선택하세요`, "fail");
    updateBadges();
    lastReviewHash = "";
    lastBuildHash  = "";

    // 빌드 설정 기본값 준비
    const quality = document.getElementById("quality")?.value || "balanced";
    const titles = {}, outputs = {};
    allJobs.forEach(j => {
      if (!buildData[j.id]) buildData[j.id] = {
        title:  DEFAULT_TITLE,
        output: "highlights_" + j.video_name.replace(/\.[^.]+$/, "") + ".mp4",
      };
      titles[j.id]  = buildData[j.id].title;
      outputs[j.id] = buildData[j.id].output;
    });
    persistBuildData();

    logLine("영상 생성 시작…", "start");
    const bd = await post("/api/jobs/build-all", {quality, titles, outputs});
    logLine(`영상 생성 진행 중: ${bd.n}개`, "done");
    (bd.skipped || []).forEach(n =>
      logLine(`⚠ ${n}: 채택 0개 — 생성 제외`, "fail"));
    go("build");
  } catch(e) { logLine("오류: " + e.message, "fail"); }
}


// ─── 오디오 시그널 타임라인 및 수동 구간 추가 ───────────────────────
async function loadAudioSignal(jid) {
  const container = document.getElementById(`audio-chart-${jid}`);
  if (!container) return;
  container.innerHTML = '<div class="hint" style="padding:15px 0;text-align:center"><span class="spin"></span>오디오 음량 분석 데이터를 가져오는 중...</div>';
  try {
    const res = await fetch(`/api/jobs/${jid}/audio-signal`);
    const data = await res.json();
    if (data.error) {
      container.innerHTML = `<div class="hint fail" style="padding:15px 0;text-align:center">오디오 로드 실패: ${data.error}</div>`;
      return;
    }
    jobSignals[jid] = data;
    renderAudioChart(jid);
  } catch (e) {
    container.innerHTML = `<div class="hint fail" style="padding:15px 0;text-align:center">오디오 로드 실패: ${e.message}</div>`;
  }
}

function renderAudioChart(jid) {
  const container = document.getElementById(`audio-chart-${jid}`);
  const data = jobSignals[jid];
  if (!container || !data) return;

  container.innerHTML = `
    <canvas id="canvas-${jid}" style="width:100%; height:130px; display:block; cursor:pointer; background:rgba(255,255,255,0.015); border-radius:6px; border:1px solid rgba(255,255,255,0.05);"></canvas>
    <div id="chart-tooltip-${jid}" style="position:absolute; display:none; pointer-events:none; background:rgba(15,20,25,0.92); color:#fff; padding:6px 10px; border-radius:4px; border:1px solid rgba(255,255,255,0.15); font-size:11px; z-index:100; box-shadow:0 4px 12px rgba(0,0,0,0.5);"></div>
    <div id="chart-actions-${jid}" style="position:absolute; display:none; background:var(--panel); border:1px solid var(--accent); padding:6px 10px; border-radius:6px; z-index:101; box-shadow:0 8px 24px rgba(0,0,0,0.6); gap:8px; align-items:center; transform:translate(-50%, -100%); margin-top:-10px;">
      <span id="action-time-${jid}" style="font-size:11px; font-weight:bold; color:var(--txt); white-space:nowrap;"></span>
      <button class="ghost sm" onclick="previewAtTime('${jid}')" style="padding:3px 8px; font-size:11px;">▶ 이동</button>
      <button class="sm accent-btn" onclick="addManualCandidate('${jid}')" style="padding:3px 8px; font-size:11px;">+ 하이라이트 추가</button>
      <button class="ghost sm" onclick="closeChartActions('${jid}')" style="padding:1px 5px; font-size:11px; color:var(--muted);">✕</button>
    </div>
  `;

  const canvas = document.getElementById(`canvas-${jid}`);
  const tooltip = document.getElementById(`chart-tooltip-${jid}`);
  const actions = document.getElementById(`chart-actions-${jid}`);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  // Handle high-DPI displays
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);

  const width = rect.width;
  const height = rect.height;
  const padding = { left: 20, right: 20, top: 15, bottom: 15 };
  const graphWidth = width - padding.left - padding.right;
  const graphHeight = height - padding.top - padding.bottom;

  const times = data.times;
  const delta = data.delta;
  const threshold = data.threshold;
  const maxTime = times[times.length - 1] || 1;

  let minDelta = Math.min(...delta);
  let maxDelta = Math.max(...delta);
  maxDelta = Math.max(maxDelta, threshold + 2);
  minDelta = Math.min(minDelta, -2);
  const deltaRange = maxDelta - minDelta || 1;

  function getX(t) {
    return padding.left + (t / maxTime) * graphWidth;
  }
  function getY(d) {
    return padding.top + graphHeight - ((d - minDelta) / deltaRange) * graphHeight;
  }
  function getTimeFromX(x) {
    const relativeX = x - padding.left;
    const pct = relativeX / graphWidth;
    return Math.max(0, Math.min(maxTime, pct * maxTime));
  }

  function drawBaseGraph(hoverX = null) {
    ctx.clearRect(0, 0, width, height);

    // 0dB baseline
    ctx.strokeStyle = "rgba(255, 255, 255, 0.05)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padding.left, getY(0));
    ctx.lineTo(width - padding.right, getY(0));
    ctx.stroke();

    // Threshold line
    ctx.strokeStyle = "rgba(248, 81, 73, 0.45)"; // --red color
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padding.left, getY(threshold));
    ctx.lineTo(width - padding.right, getY(threshold));
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = "rgba(248, 81, 73, 0.7)";
    ctx.font = "10px Segoe UI, sans-serif";
    ctx.fillText(`임계값 (${threshold.toFixed(1)} dB)`, padding.left + 5, getY(threshold) - 4);

    // Gradient Fill
    const grad = ctx.createLinearGradient(0, padding.top, 0, padding.top + graphHeight);
    grad.addColorStop(0, "rgba(47, 129, 247, 0.18)"); // --blue color
    grad.addColorStop(1, "rgba(47, 129, 247, 0.0)");

    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.moveTo(getX(times[0]), getY(0));
    for (let i = 0; i < times.length; i++) {
      ctx.lineTo(getX(times[i]), getY(delta[i]));
    }
    ctx.lineTo(getX(times[times.length - 1]), getY(0));
    ctx.closePath();
    ctx.fill();

    // Wave Line
    ctx.strokeStyle = "rgba(47, 129, 247, 0.85)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(getX(times[0]), getY(delta[0]));
    for (let i = 1; i < times.length; i++) {
      ctx.lineTo(getX(times[i]), getY(delta[i]));
    }
    ctx.stroke();

    // Candidate vertical lines
    const job = allJobs.find(j => j.id === jid);
    const candidates = jobCands[jid]?.cands || [];
    const sel = localSel[jid] || new Set();

    candidates.forEach(c => {
      const cx = getX(c.peak);
      let color = "rgba(110, 118, 129, 0.35)"; // --rej color
      let width = 1;
      if (sel.has(c.idx)) {
        color = "rgba(63, 185, 80, 0.75)"; // --accent color
        width = 2;
      } else if (c.flag === "maybe") {
        color = "rgba(210, 153, 34, 0.6)"; // --warn color
      }

      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.beginPath();
      ctx.moveTo(cx, padding.top);
      ctx.lineTo(cx, padding.top + graphHeight);
      ctx.stroke();

      // dot at peak
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(cx, getY(c.delta_db || 0.0), 3, 0, 2 * Math.PI);
      ctx.fill();
    });

    // Hover line
    if (hoverX !== null) {
      ctx.strokeStyle = "rgba(255, 255, 255, 0.4)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(hoverX, padding.top);
      ctx.lineTo(hoverX, padding.top + graphHeight);
      ctx.stroke();

      // Tooltip point
      const t = getTimeFromX(hoverX);
      let closestIdx = 0;
      let minDiff = Infinity;
      for (let i = 0; i < times.length; i++) {
        const diff = Math.abs(times[i] - t);
        if (diff < minDiff) {
          minDiff = diff;
          closestIdx = i;
        }
      }
      ctx.fillStyle = "#fff";
      ctx.beginPath();
      ctx.arc(hoverX, getY(delta[closestIdx]), 4.5, 0, 2 * Math.PI);
      ctx.fill();
    }
  }

  // Draw initial base graph
  drawBaseGraph();

  canvas.addEventListener("mousemove", (e) => {
    const mouseX = e.offsetX;
    const mouseY = e.offsetY;

    if (mouseX < padding.left || mouseX > width - padding.right) {
      tooltip.style.display = "none";
      drawBaseGraph();
      return;
    }

    const t = getTimeFromX(mouseX);
    let closestIdx = 0;
    let minDiff = Infinity;
    for (let i = 0; i < times.length; i++) {
      const diff = Math.abs(times[i] - t);
      if (diff < minDiff) {
        minDiff = diff;
        closestIdx = i;
      }
    }

    const dVal = delta[closestIdx];
    const tsStr = fmtDur(t);

    drawBaseGraph(mouseX);

    tooltip.style.display = "block";
    tooltip.style.left = `${mouseX + 12}px`;
    tooltip.style.top = `${mouseY - 45}px`;
    tooltip.innerHTML = `시간: <b>${tsStr}</b><br/>음량: <b>+${dVal.toFixed(1)} dB</b>`;
  });

  canvas.addEventListener("mouseleave", () => {
    tooltip.style.display = "none";
    drawBaseGraph();
  });

  canvas.addEventListener("click", (e) => {
    const mouseX = e.offsetX;
    if (mouseX < padding.left || mouseX > width - padding.right) return;

    const t = getTimeFromX(mouseX);
    
    actions.style.display = "flex";
    actions.style.left = `${mouseX}px`;
    actions.style.top = `${padding.top + 5}px`;
    
    const timeStr = fmtDur(t);
    document.getElementById(`action-time-${jid}`).textContent = timeStr;
    actions.dataset.targetTime = t;
  });
}

function previewAtTime(jid) {
  const actions = document.getElementById(`chart-actions-${jid}`);
  if (!actions) return;
  const t = parseFloat(actions.dataset.targetTime);
  const vid = document.getElementById("preview-" + jid);
  if (!vid) return;

  vid.style.display = "block";
  const start = Math.max(0, t - 3); // 3초 전부터 재생
  const seekAndPlay = () => { vid.currentTime = start; vid.play().catch(() => {}); };
  if (vid.dataset.srcLoaded === jid) {
    seekAndPlay();
  } else {
    vid.src = `/api/jobs/${jid}/source`;
    vid.dataset.srcLoaded = jid;
    vid.addEventListener("loadedmetadata", seekAndPlay, {once: true});
  }
}

async function addManualCandidate(jid) {
  const actions = document.getElementById(`chart-actions-${jid}`);
  if (!actions) return;
  const t = parseFloat(actions.dataset.targetTime);
  if (isNaN(t)) return;

  try {
    logLine(`수동 하이라이트 구간 추가 중: ${fmtDur(t)}`, "start");
    const res = await post(`/api/jobs/${jid}/candidates/add-manual`, { peak: t });
    if (res.error) {
      logLine(`구간 추가 실패: ${res.error}`, "fail");
      return;
    }

    logLine(`구간 추가 완료: ${fmtDur(t)}`, "done");

    // 로컬 상태 동기화 및 재랜더링
    const conf = confVal[jid] || globalConf;
    const d = await (await fetch(`/api/jobs/${jid}/candidates?conf=${conf}`)).json();
    jobCands[jid] = {cands: d.candidates, visionUsed: d.vision_used};
    localSel[jid] = d.approved !== null ? new Set(d.approved) : new Set();
    
    renderCands(jid);
    
    // 추가 완료 직후 추가된 피크 지점 자동 미리보기 재생
    previewCand(jid, t);
    closeChartActions(jid);
  } catch (e) {
    logLine(`구간 추가 오류: ${e.message}`, "fail");
  }
}

function closeChartActions(jid) {
  const actions = document.getElementById(`chart-actions-${jid}`);
  if (actions) actions.style.display = "none";
}
