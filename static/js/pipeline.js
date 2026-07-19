// pipeline.js — 원스톱 파이프라인 (YouTube/BAND까지 자동 처리)

// 버튼 잠금/해제
function _pipeBtns(disabled) {
  ["btn-add", "run-mode"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = disabled;
  });
}

// 작업 모드 변경 핸들러
function onRunModeChange(val) {
  const descEl = document.getElementById("run-mode-desc");
  const btnEl = document.getElementById("btn-add");
  if (!descEl || !btnEl) return;
  
  if (val === "manual") {
    btnEl.textContent = "🚀 작업 시작";
    btnEl.className = "";
    descEl.textContent = "수동 리뷰: 분석 완료 후 [리뷰] 탭에서 검토할 수 있습니다.";
  } else if (val === "youtube") {
    btnEl.textContent = "🚀 원스톱 자동화 시작";
    btnEl.className = "accent-btn";
    descEl.textContent = "YouTube 자동화: 분석 후 자동 승인 → 인코딩 → YouTube 업로드 → BAND 코멘트 준비까지 한 번에 처리합니다.";
  } else if (val === "band") {
    btnEl.textContent = "🚀 원스톱 자동화 시작";
    btnEl.className = "accent-btn";
    descEl.textContent = "BAND 자동화: 분석 후 자동 승인 → 인코딩 → YouTube 업로드 → BAND 직접 게시까지 완전히 자동으로 끝냅니다.";
  }
  localStorage.setItem("hl_run_mode", val);
}
window.onRunModeChange = onRunModeChange;

// 작업 시작 진입점
async function doStartPipeline() {
  const mode = document.getElementById("run-mode").value;
  if (mode === "manual") {
    await doAdd();
  } else {
    await doOnestop(mode);
  }
}

// 파이프라인 시작
async function doOnestop(mode) {
  if (!staged.length) syncStaged();
  const toAdd = staged.filter(s => !s.dup);
  if (!toAdd.length) { logLine("추가할 영상이 없습니다.", "fail"); return; }
  // 인증 토큰 실시간 검증 (YouTube는 항상, BAND 모드면 BAND도 추가 확인)
  try {
    const ytSt = await (await fetch("/api/auth/youtube/status")).json();
    if (!ytSt.ok) {
      refreshAuthIndicators();
      alert("YouTube 인증이 만료되었습니다.\n설정 패널의 \"인증하기 →\" 링크를 클릭하세요.");
      return;
    }
    if (mode === "band") {
      const bSt = await (await fetch("/api/auth/band/status")).json();
      if (!bSt.ok) {
        refreshAuthIndicators();
        alert("BAND 인증이 만료되었습니다.\n설정 패널의 \"인증하기 →\" 링크를 클릭하세요.");
        return;
      }
    }
  } catch(e) { alert("인증 상태 확인 실패. 네트워크를 확인하세요."); return; }
  if (_pipe) { if (!confirm("진행 중인 파이프라인을 취소하고 새로 시작할까요?")) return; cancelPipeline(true); }

  const workers  = parseInt(document.getElementById("workers").value) || 6;
  const pre_sec  = parseFloat(document.getElementById("setting-pre")?.value) || 8;
  const post_sec = parseFloat(document.getElementById("setting-post")?.value) || 5;
  const jobs = toAdd.map(s => ({video: s.path, sensitivity: s.sensitivity, workers, pre_sec, post_sec}));
  _pipeBtns(true);
  try {
    const d = await post("/api/jobs/add", {jobs, workers});
    if (!d.added?.length) { logLine("추가 실패", "fail"); _pipeBtns(false); return; }
    _pipe = { mode, jobIds: new Set(d.added.map(j => j.id)), step: "processing", stepAt: Date.now() };
    _savePipe();
    clearStaged();
    const modeLabel = mode === "band" ? "BAND까지 원스톱" : "YouTube까지 원스톱";
    logLine(`[원스톱] ${d.added.length}개 추가 — ${modeLabel} 시작`, "start");
    renderPipeBar();
  } catch(e) { logLine("원스톱 오류: " + e.message, "fail"); _pipeBtns(false); }
}

function cancelPipeline(silent) {
  if (_pipe && _pipe.jobIds) {
    for (const jid of _pipe.jobIds) {
      post(`/api/jobs/${jid}/cancel`, {}).catch(() => {});  // 이미 끝난 잡은 400 무시
    }
  }
  _pipe = null;
  _savePipe();
  _pipeBtns(false);
  document.getElementById("pipe-bar").style.display = "none";
  if (!silent) logLine("[원스톱] 파이프라인 취소됨", "fail");
}

function _pipeSetStep(step) {
  _pipe.step = step;
  _pipe.stepAt = Date.now();
  _savePipe();
  renderPipeBar();
}

// 파이프라인 완료
function _pipeDone() {
  _pipe = null;
  _savePipe();
  _pipeBtns(false);
  setTimeout(() => {
    const el = document.getElementById("pipe-bar");
    if (el) el.style.display = "none";
  }, 8000);  // 8초 후 숨김
}

// 파이프라인 상태 바 렌더링
function renderPipeBar() {
  const bar = document.getElementById("pipe-bar");
  if (!bar || !_pipe) { if (bar) bar.style.display = "none"; return; }
  bar.style.display = "block";

  const modeLabel = _pipe.mode === "band" ? "BAND까지 원스톱" : "YouTube까지 원스톱";
  document.getElementById("pipe-mode-label").textContent = "▶ " + modeLabel;

  const STEPS = _pipe.mode === "band"
    ? ["processing","building","uploading","posting","done"]
    : ["processing","building","uploading","band_copy","done"];
  const LABELS = { processing:"처리 중", building:"영상 생성", uploading:"YouTube 업로드", posting:"BAND 게시", band_copy:"BAND 글 준비", done:"완료" };

  const stepIdx = STEPS.indexOf(_pipe.step);
  const stepsEl = document.getElementById("pipe-steps");
  stepsEl.innerHTML = STEPS.map((s, i) => {
    const past    = i < stepIdx;
    const current = i === stepIdx;
    const color   = _pipe.step === "done" ? "var(--accent)" : (current ? "var(--blue)" : (past ? "var(--accent)" : "var(--rej)"));
    const weight  = current ? "700" : "400";
    const prefix  = past ? "✓ " : (current && _pipe.step !== "done" ? `<span class="spin"></span>` : "");
    const arrow   = i < STEPS.length - 1 ? `<span style="color:var(--rej);margin:0 6px;font-size:11px">→</span>` : "";
    return `<span style="color:${color};font-weight:${weight};font-size:12px">${prefix}${LABELS[s]}</span>${arrow}`;
  }).join("");
}

// 폴링 사이클마다 파이프라인 상태 점검 및 다음 단계 전환
const PIPE_MAX_MISSING_POLLS = 10;  // 이만큼 연속으로 잡을 못 찾으면(서버 재시작 등) 자동 취소

async function advancePipeline(jobs) {
  if (!_pipe) return;
  const { mode, jobIds, step, stepAt } = _pipe;
  const pJobs = jobs.filter(j => jobIds.has(j.id));
  if (!pJobs.length) {
    _pipe._missCount = (_pipe._missCount || 0) + 1;
    if (_pipe._missCount >= PIPE_MAX_MISSING_POLLS) {
      logLine("[원스톱] 서버에서 해당 작업을 찾을 수 없어 파이프라인을 자동 취소합니다 (서버 재시작 등).", "fail");
      cancelPipeline(true);
    }
    return;
  }
  _pipe._missCount = 0;

  renderPipeBar();

  // 단계 전환 직후 안정화 대기
  const elapsed = Date.now() - stepAt;

  // ── step: processing ─────────────────────────────────
  if (step === "processing") {
    const active = pJobs.filter(j => ["pending","detecting","classifying"].includes(j.status));
    if (active.length > 0) {
      document.getElementById("pipe-detail").textContent =
        `처리 중: ${active.length}개 남음 (${pJobs.filter(j => j.status==="ready").length}개 완료)`;
      return;
    }
    if (elapsed < PIPE_STEP_DELAY) return;

    const ready = pJobs.filter(j => ["ready","done"].includes(j.status));
    if (!ready.length) { logLine("[원스톱] 모든 영상 처리 오류 — 파이프라인 중단", "fail"); _pipeDone(); return; }

    _pipeSetStep("building");
    logLine(`[원스톱] 처리 완료 (${ready.length}개) → AI 판단 자동 승인 & 영상 생성`, "start");
    try {
      const conf = parseFloat(document.getElementById("global-conf")?.value) || CONF_AUTO;
      await post("/api/jobs/approve-all", {conf});

      const quality = document.getElementById("hl-quality")?.value || "balanced";
      const titles = {}, yt_titles = {}, outputs = {};
      ready.forEach(j => {
        const base = buildData[j.id]?.title || DEFAULT_TITLE;
        titles[j.id]    = base;                      // 워터마크: 베이스 제목만
        yt_titles[j.id] = ytTitleFor(j.id, base);     // YouTube 제목: 영상명·날짜 포함 전체 형식
        outputs[j.id]   = buildData[j.id]?.output || "";
      });
      await post("/api/jobs/build-all", {quality, titles, yt_titles, outputs, auto_upload: true});
    } catch(e) { logLine("[원스톱] 생성 요청 오류: " + e.message, "fail"); _pipeDone(); }

  // ── step: building ───────────────────────────────────
  } else if (step === "building") {
    const building = pJobs.filter(j => j.status === "building");
    const notBuilt  = pJobs.filter(j => ["ready","pending","detecting","classifying"].includes(j.status));
    if (building.length > 0 || (elapsed < PIPE_STEP_DELAY && notBuilt.length > 0)) {
      document.getElementById("pipe-detail").textContent =
        `영상 생성 중: ${building.length + notBuilt.length}개 남음`;
      return;
    }
    if (elapsed < PIPE_STEP_DELAY) return;

    const done = pJobs.filter(j => j.status === "done" && j.output);
    if (!done.length) { logLine("[원스톱] 영상 생성 실패 — 파이프라인 중단", "fail"); _pipeDone(); return; }

    _pipeSetStep("uploading");
    logLine(`[원스톱] 생성 완료 (${done.length}개) → YouTube 업로드 시작`, "start");
    try {
      const titles = {};
      done.forEach(j => { titles[j.id] = ytTitleFor(j.id, buildData[j.id]?.title || DEFAULT_TITLE); });
      await post("/api/jobs/upload-all-youtube", {titles});
    } catch(e) { logLine("[원스톱] 업로드 요청 오류: " + e.message, "fail"); _pipeDone(); }

  // ── step: uploading ──────────────────────────────────
  } else if (step === "uploading") {
    const outputJobs = pJobs.filter(j => j.status === "done" && j.output);
    const uploading  = outputJobs.filter(j => j.yt_status === "uploading");
    const pending    = outputJobs.filter(j => !j.yt_status);
    if (uploading.length > 0 || (elapsed < PIPE_STEP_DELAY && pending.length > 0)) {
      document.getElementById("pipe-detail").textContent =
        `업로드 중: ${uploading.length + pending.length}개 남음`;
      return;
    }
    if (elapsed < PIPE_STEP_DELAY) return;

    const uploaded = outputJobs.filter(j => j.yt_status === "done");
    if (!uploaded.length) { logLine("[원스톱] YouTube 업로드 실패 — 파이프라인 중단", "fail"); _pipeDone(); return; }

    if (mode === "band") {
      _pipeSetStep("posting");
      logLine(`[원스톱] 업로드 완료 (${uploaded.length}개) → BAND 게시 시작`, "start");
      try {
        const d = await post("/api/jobs/post-band", {});  // band_key 없으면 .env의 BAND_TARGET_KEY 사용
        const n = (d.posted||[]).reduce((s,p) => s+p.n, 0);
        logLine(`[원스톱] BAND 게시 완료: ${n}개 링크`, "done");
        sendNotif("원스톱 완료", "YouTube 업로드 및 BAND 게시가 완료되었습니다.");
        (d.errors||[]).forEach(e2 => logLine(`BAND 오류: ${e2.error}`, "fail"));
        
        // BAND 게시 성공 시에도 작성글 및 상세 팝업 표시
        updateBandPreview();
        const postText = document.getElementById("band-post-preview")?.textContent || "";
        showCompletionModal(postText + "\n\n(위 내용이 BAND에 직접 게시되었습니다.)");
      } catch(e) { logLine("[원스톱] BAND 게시 오류: " + e.message, "fail"); }
      _pipeSetStep("done");
      document.getElementById("pipe-detail").textContent = "모든 단계 완료!";
      _pipeDone();
      // BAND API 게시 후에도 텍스트 클립보드 복사 (백업)
      updateBandPreview();
      copyBandText(true).catch(() => {});
    } else {
      // youtube 모드: 업로드 완료 후 BAND 글 자동 준비 단계로
      _pipeSetStep("band_copy");
      logLine(`[원스톱] YouTube 업로드 완료 (${uploaded.length}개) → BAND 게시 글 준비 중`, "start");
    }

  // ── step: band_copy ─────────────────────────────────
  } else if (step === "band_copy") {
    if (elapsed < PIPE_STEP_DELAY) return;
    document.getElementById("pipe-detail").textContent = "BAND 게시 텍스트 클립보드 복사 중…";
    updateBandPreview();
    const postText = document.getElementById("band-post-preview")?.textContent || "";
    showCompletionModal(postText);
    try {
      await copyBandText(true);
      logLine(`[원스톱] 완료! BAND 게시 텍스트가 클립보드에 복사됐습니다.`, "done");
      sendNotif("원스톱 완료", "YouTube 업로드 완료 · BAND 게시 텍스트가 클립보드에 복사됐습니다.");
    } catch(e) {
      logLine(`[원스톱] 완료! (클립보드 자동 복사 실패 — 팝업창에서 직접 복사해 주세요)`, "done");
      sendNotif("원스톱 완료", "YouTube 업로드 완료.");
    }
    // 영상 생성 탭으로 이동해 BAND 게시 카드 표시
    go("build");
    _pipeSetStep("done");
    document.getElementById("pipe-detail").textContent = "YouTube 업로드 + BAND 글 준비 완료!";
    _pipeDone();
  }
}

// 완료 팝업 모달 제어 함수군
function showCompletionModal(text) {
  const modal = document.getElementById("completion-modal");
  const modalText = document.getElementById("modal-band-text");
  if (modal && modalText) {
    modalText.value = text.trim();
    modal.style.display = "flex";
  }
}

function closeCompletionModal() {
  const modal = document.getElementById("completion-modal");
  if (modal) {
    modal.style.display = "none";
  }
}

async function copyModalBandText() {
  const modalText = document.getElementById("modal-band-text");
  const resultEl = document.getElementById("modal-copy-result");
  if (!modalText || !resultEl) return;
  try {
    await navigator.clipboard.writeText(modalText.value);
    resultEl.innerHTML = '<span style="color:var(--accent);font-weight:600">✓ 복사 성공!</span>';
  } catch(e) {
    modalText.select();
    resultEl.innerHTML = '<span class="hint">복사 실패. 드래그된 텍스트를 Ctrl+C로 복사하세요.</span>';
  }
  setTimeout(() => { if (resultEl) resultEl.innerHTML = ""; }, 4000);
}
