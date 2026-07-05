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
      <td>${visionUsed ? c.confidence : "-"}</td>
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
    c.flag = c.confidence >= conf ? "auto"
           : c.confidence >= CONF_MAYBE ? "maybe" : "reject";
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
