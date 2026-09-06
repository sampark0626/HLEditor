// pipeline.js — 원스톱 파이프라인 (YouTube/BAND까지 자동 처리)
//
// 각 단계는 "진입 액션(_enterStep)"과 "완료 판정(advancePipeline)"으로 분리돼 있고,
// 진입 액션은 모두 멱등(idempotent)하다 — 이미 끝난 잡은 서버가 건너뛴다.
// 덕분에 어느 단계에서 실패해도 처음부터가 아니라 그 단계부터 [이어서 진행]할 수 있다.

// 버튼 잠금/해제
function _pipeBtns(disabled) {
  ["btn-add", "run-mode"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = disabled;
  });
}

// 모드별 단계 순서
function _pipeStepList(mode) {
  return mode === "band"
    ? ["processing", "building", "uploading", "posting", "done"]
    : ["processing", "building", "uploading", "band_copy", "done"];
}

const PIPE_STEP_LABELS = {
  processing: "처리 중", building: "영상 생성", uploading: "YouTube 업로드",
  posting: "BAND 게시", band_copy: "BAND 글 준비", done: "완료",
};

// 파이프라인에 속한 잡만 추출
function _pipeJobs(jobs) {
  if (!_pipe) return [];
  return (jobs || allJobs).filter(j => _pipe.jobIds.has(j.id));
}

// 서버에서 최신 잡 상태를 다시 읽어온다 (방금 상태를 바꾼 직후 등, 폴링 캐시가
// 오래된 값을 들고 있으면 안 되는 지점에서 사용)
async function _refreshPipeJobs() {
  try {
    const {jobs} = await (await fetch("/api/jobs")).json();
    allJobs = jobs;
    return _pipeJobs(jobs);
  } catch(e) {
    return _pipeJobs();
  }
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
      alert("YouTube 인증이 필요합니다.\n설정 패널의 \"인증하기 →\" 링크를 클릭하세요.\n\n사유: " + (ytSt.detail || ytSt.reason || ""));
      return;
    }
    if (ytSt.reason === "network") {
      logLine("⚠ YouTube 인증 확인이 네트워크 문제로 실패했습니다 — 저장된 토큰으로 계속 진행합니다.", "fail");
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
    // /api/jobs/add의 added는 잡 id 문자열 배열이다. 예전 코드가 객체로 보고
    // j.id를 꺼내는 바람에 jobIds가 [undefined]가 되어, 파이프라인이 자기 잡을
    // 하나도 못 찾고 20초 뒤 "작업을 찾을 수 없습니다"로 죽었다.
    const addedIds = d.added.map(j => (typeof j === "string" ? j : j?.id)).filter(Boolean);
    if (!addedIds.length) { logLine("추가된 작업 ID를 확인할 수 없습니다.", "fail"); _pipeBtns(false); return; }
    _pipe = {
      mode, jobIds: new Set(addedIds),
      step: "processing", stepAt: Date.now(),
      status: "running", error: "",
      // 파이프라인이 만들 영상의 품질/제목 설정은 시작 시점 값으로 고정해 둔다 —
      // 재개할 때 사용자가 UI를 바꿨더라도 같은 설정으로 이어지도록.
      opts: {
        quality: document.getElementById("hl-quality")?.value || "balanced",
        conf: parseFloat(document.getElementById("global-conf")?.value) || CONF_AUTO,
      },
    };
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

// ─── 단계 전환 ────────────────────────────────────────────
// 단계로 진입하며 해당 단계의 서버 액션을 실행한다.
async function _gotoStep(step) {
  if (!_pipe) return;
  _pipe.step = step;
  _pipe.stepAt = Date.now();
  _pipe.status = "running";
  _pipe.error = "";
  _savePipe();
  renderPipeBar();
  await _enterStep(step);
}

// 실패 처리: 파이프라인을 지우지 않고 "멈춤" 상태로 남겨 재개할 수 있게 한다.
function _pipeFail(msg, opts = {}) {
  if (!_pipe) return;
  _pipe.status = "failed";
  _pipe.error = msg;
  _pipe.canSkip = !!opts.canSkip;
  _savePipe();
  _pipeBtns(false);   // 실패 중엔 다른 작업을 할 수 있게 버튼 해제
  logLine(`[원스톱] ${PIPE_STEP_LABELS[_pipe.step] || _pipe.step} 단계 중단: ${msg}`, "fail");
  logLine(`[원스톱] 원인을 해결한 뒤 진행 바의 [이어서 진행]을 누르면 이 단계부터 재개됩니다.`, "fail");
  sendNotif("원스톱 중단", `${PIPE_STEP_LABELS[_pipe.step] || ""} 단계에서 멈췄습니다. 이어서 진행할 수 있습니다.`);
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

// 멈춘 단계부터 재개
async function resumePipeline() {
  if (!_pipe || _pipe.status !== "failed") return;
  const step = _pipe.step;
  _pipe.status = "running";
  _pipe.error = "";
  _pipe.stepAt = Date.now();
  _pipe.busy = false;
  _savePipe();
  _pipeBtns(true);
  logLine(`[원스톱] ${PIPE_STEP_LABELS[step] || step} 단계부터 이어서 진행합니다.`, "start");
  renderPipeBar();
  await _enterStep(step, {resume: true});
}

// 실패한 단계를 건너뛰고 다음 단계로 (일부만 성공했을 때)
async function skipPipelineStep() {
  if (!_pipe || _pipe.status !== "failed") return;
  const steps = _pipeStepList(_pipe.mode);
  const next  = steps[Math.min(steps.indexOf(_pipe.step) + 1, steps.length - 1)];
  logLine(`[원스톱] 실패 건을 건너뛰고 "${PIPE_STEP_LABELS[next]}" 단계로 진행합니다.`, "start");
  _pipeBtns(true);
  await _gotoStep(next);
}

// ─── 단계별 진입 액션 (모두 멱등) ──────────────────────────
async function _enterStep(step, {resume = false} = {}) {
  if (!_pipe) return;
  const jobIds = [..._pipe.jobIds];
  const pJobs  = _pipeJobs();
  _pipe.busy = true;
  try {
    if (step === "processing") {
      // 재개: 오류로 멈춘 잡을 다시 큐에 넣는다.
      const errored = pJobs.filter(j => j.status === "error");
      for (const j of errored) {
        await post(`/api/jobs/${j.id}/retry`, {}).catch(e =>
          logLine(`재시도 실패 (${j.video_name}): ${e.message}`, "fail"));
      }
      if (errored.length) logLine(`[원스톱] ${errored.length}개 영상 재분석 시작`, "start");

    } else if (step === "building") {
      // 재개: 인코딩에서만 실패한 잡은 분석 결과를 유지한 채 "ready"로 되돌린다
      // (몇 분짜리 재분석 없이 인코딩만 다시 한다).
      const buildErrors = resume ? pJobs.filter(j => j.status === "error") : [];
      for (const j of buildErrors) {
        await post(`/api/jobs/${j.id}/retry`, {stage: "build"}).catch(() =>
          post(`/api/jobs/${j.id}/retry`, {}).catch(() => {}));   // 후보가 없으면 전체 재처리
      }
      if (buildErrors.length) logLine(`[원스톱] ${buildErrors.length}개 영상 생성 재시도`, "start");

      logLine(`[원스톱] AI 판단 자동 승인 & 영상 생성 시작`, "start");
      const conf = _pipe.opts?.conf ?? (parseFloat(document.getElementById("global-conf")?.value) || CONF_AUTO);
      await post("/api/jobs/approve-all", {conf, job_ids: jobIds});

      const ready = (await _refreshPipeJobs()).filter(j => ["ready", "done"].includes(j.status));
      const quality = _pipe.opts?.quality || document.getElementById("hl-quality")?.value || "balanced";
      const titles = {}, yt_titles = {}, outputs = {};
      ready.forEach(j => {
        const base = buildData[j.id]?.title || DEFAULT_TITLE;
        titles[j.id]    = base;                     // 워터마크: 베이스 제목만
        yt_titles[j.id] = ytTitleFor(j.id, base);   // YouTube 제목: 영상명·날짜 포함 전체 형식
        outputs[j.id]   = buildData[j.id]?.output || "";
      });
      const d = await post("/api/jobs/build-all", {
        quality, titles, yt_titles, outputs,
        auto_upload: true, job_ids: jobIds,
        skip_built: true,   // 이미 만들어진 영상은 재인코딩하지 않는다
      });
      if (d.already_built) logLine(`[원스톱] ${d.already_built}개는 이미 생성돼 있어 건너뜁니다.`, "done");

    } else if (step === "uploading") {
      // build-all(auto_upload)이 이미 잡별 업로드를 시작했다.
      // 이 호출은 그때 누락된 잡(인증 실패 등)만 집어내는 안전망이며,
      // 대상이 0건이어도 서버가 200을 반환하므로 파이프라인이 끊기지 않는다.
      const titles = {};
      pJobs.forEach(j => { titles[j.id] = ytTitleFor(j.id, buildData[j.id]?.title || DEFAULT_TITLE); });
      const d = await post("/api/jobs/upload-all-youtube", {titles, job_ids: jobIds});
      if (d.n)  logLine(`[원스톱] YouTube 업로드 시작: ${d.n}개`, "start");
      if (d.already) logLine(`[원스톱] ${d.already}개는 이미 업로드 진행/완료 상태입니다.`, "done");

    } else if (step === "posting") {
      const uploaded = pJobs.filter(j => j.yt_status === "done");
      logLine(`[원스톱] 업로드 완료 (${uploaded.length}개) → BAND 게시 시작`, "start");
      const d = await post("/api/jobs/post-band", {job_ids: jobIds});
      const n = (d.posted || []).reduce((s, p) => s + p.n, 0);
      (d.errors || []).forEach(e2 => logLine(`BAND 오류: ${e2.error}`, "fail"));
      if ((d.errors || []).length && !n) {
        _pipeFail(`BAND 게시 실패: ${d.errors[0].error}`, {canSkip: true});
        return;
      }
      logLine(`[원스톱] BAND 게시 완료: ${n}개 링크`, "done");
      sendNotif("원스톱 완료", "YouTube 업로드 및 BAND 게시가 완료되었습니다.");
      updateBandPreview(_pipe.jobIds);
      const postText = document.getElementById("band-post-preview")?.textContent || "";
      showCompletionModal(postText + "\n\n(위 내용이 BAND에 직접 게시되었습니다.)");
      copyBandText(true).catch(() => {});
      await _gotoStep("done");

    } else if (step === "band_copy") {
      document.getElementById("pipe-detail").textContent = "BAND 게시 텍스트 클립보드 복사 중…";
      updateBandPreview(_pipe.jobIds);
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
      go("build");   // 영상 생성 탭으로 이동해 BAND 게시 카드 표시
      await _gotoStep("done");

    } else if (step === "done") {
      document.getElementById("pipe-detail").textContent = "모든 단계 완료!";
      logLine("[원스톱] 파이프라인 완료", "done");
      _pipeDone();
    }
  } catch(e) {
    _pipeFail(e.message || String(e), {canSkip: step !== "processing"});
  } finally {
    if (_pipe) { _pipe.busy = false; }
  }
}

// 파이프라인 상태 바 렌더링
function renderPipeBar() {
  const bar = document.getElementById("pipe-bar");
  if (!bar || !_pipe) { if (bar) bar.style.display = "none"; return; }
  bar.style.display = "block";

  const failed = _pipe.status === "failed";
  bar.style.background   = failed ? "#2b1218" : "#0d2248";
  bar.style.borderColor  = failed ? "var(--red)" : "var(--blue)";

  const modeLabel = _pipe.mode === "band" ? "BAND까지 원스톱" : "YouTube까지 원스톱";
  const labelEl = document.getElementById("pipe-mode-label");
  labelEl.textContent = (failed ? "⏸ " : "▶ ") + modeLabel + (failed ? " — 중단됨" : "");
  labelEl.style.color = failed ? "var(--red)" : "var(--blue)";

  const STEPS = _pipeStepList(_pipe.mode);
  const stepIdx = STEPS.indexOf(_pipe.step);
  const stepsEl = document.getElementById("pipe-steps");
  stepsEl.innerHTML = STEPS.map((s, i) => {
    const past    = i < stepIdx;
    const current = i === stepIdx;
    let color = "var(--rej)";
    if (past || _pipe.step === "done") color = "var(--accent)";
    else if (current) color = failed ? "var(--red)" : "var(--blue)";
    const weight = current ? "700" : "400";
    let prefix = past ? "✓ " : "";
    if (current && _pipe.step !== "done") prefix = failed ? "⏸ " : `<span class="spin"></span>`;
    const arrow = i < STEPS.length - 1 ? `<span style="color:var(--rej);margin:0 6px;font-size:11px">→</span>` : "";
    return `<span style="color:${color};font-weight:${weight};font-size:12px">${prefix}${PIPE_STEP_LABELS[s]}</span>${arrow}`;
  }).join("");

  // 실패 시 재개 버튼 노출
  const actEl = document.getElementById("pipe-actions");
  if (actEl) {
    if (failed) {
      actEl.style.display = "flex";
      actEl.innerHTML =
        `<button class="accent-btn sm" onclick="resumePipeline()">▶ 이어서 진행</button>` +
        (_pipe.canSkip
          ? `<button class="ghost sm" onclick="skipPipelineStep()">실패 건너뛰고 다음 단계</button>` : "") +
        `<span style="color:var(--red);font-size:11px;align-self:center">${esc(_pipe.error || "")}</span>`;
    } else {
      actEl.style.display = "none";
      actEl.innerHTML = "";
    }
  }
}

// 폴링 사이클마다 파이프라인 상태 점검 및 다음 단계 전환
const PIPE_MAX_MISSING_POLLS = 10;  // 이만큼 연속으로 잡을 못 찾으면(서버 재시작 등) 중단

async function advancePipeline(jobs) {
  if (!_pipe) return;
  renderPipeBar();
  // 멈춤 상태이거나 서버 요청이 진행 중이면 자동 전환하지 않는다.
  if (_pipe.status === "failed" || _pipe.busy) return;

  const { mode, step, stepAt } = _pipe;
  const pJobs = jobs.filter(j => _pipe.jobIds.has(j.id));
  if (!pJobs.length) {
    _pipe._missCount = (_pipe._missCount || 0) + 1;
    if (_pipe._missCount >= PIPE_MAX_MISSING_POLLS) {
      _pipeFail("서버에서 해당 작업을 찾을 수 없습니다 (서버 재시작 등). 큐에 잡이 남아 있으면 [이어서 진행]으로 재개할 수 있습니다.");
    }
    return;
  }
  _pipe._missCount = 0;

  const elapsed = Date.now() - stepAt;
  const detail  = document.getElementById("pipe-detail");

  // ── step: processing ─────────────────────────────────
  if (step === "processing") {
    const active = pJobs.filter(j => ["pending","detecting","classifying"].includes(j.status));
    if (active.length > 0) {
      detail.textContent =
        `처리 중: ${active.length}개 남음 (${pJobs.filter(j => j.status==="ready").length}개 완료)`;
      return;
    }
    if (elapsed < PIPE_STEP_DELAY) return;

    const ready   = pJobs.filter(j => ["ready","done"].includes(j.status));
    const errored = pJobs.filter(j => j.status === "error");
    if (!ready.length) {
      _pipeFail(`모든 영상 처리 실패 (${errored.length}개). ${errored[0]?.error || ""}`);
      return;
    }
    if (errored.length) {
      _pipeFail(`${errored.length}개 영상 처리 실패 — [이어서 진행]으로 재분석하거나 건너뛸 수 있습니다.`,
                {canSkip: true});
      return;
    }
    logLine(`[원스톱] 처리 완료 (${ready.length}개)`, "done");
    await _gotoStep("building");

  // ── step: building ───────────────────────────────────
  } else if (step === "building") {
    const building = pJobs.filter(j => j.status === "building");
    const notBuilt = pJobs.filter(j => ["ready","pending","detecting","classifying"].includes(j.status));
    if (building.length > 0 || (elapsed < PIPE_STEP_DELAY && notBuilt.length > 0)) {
      detail.textContent = `영상 생성 중: ${building.length + notBuilt.length}개 남음`;
      return;
    }
    if (elapsed < PIPE_STEP_DELAY) return;

    const done    = pJobs.filter(j => j.status === "done" && j.output);
    const errored = pJobs.filter(j => j.status === "error");
    if (!done.length) {
      _pipeFail(`영상 생성 실패. ${errored[0]?.error || ""}`);
      return;
    }
    if (errored.length) {
      _pipeFail(`${errored.length}개 영상 생성 실패 — [이어서 진행]으로 재시도하거나 건너뛸 수 있습니다.`,
                {canSkip: true});
      return;
    }
    logLine(`[원스톱] 생성 완료 (${done.length}개)`, "done");
    await _gotoStep("uploading");

  // ── step: uploading ──────────────────────────────────
  } else if (step === "uploading") {
    const outputJobs = pJobs.filter(j => j.status === "done" && j.output);
    const uploading  = outputJobs.filter(j => j.yt_status === "uploading");
    const pending    = outputJobs.filter(j => !j.yt_status);
    if (uploading.length > 0 || (elapsed < PIPE_STEP_DELAY && pending.length > 0)) {
      const yp = uploading[0]?.yt_progress || {};
      const pct = yp.total > 0 ? ` (${Math.round(yp.done / yp.total * 100)}%)` : "";
      detail.textContent = `업로드 중: ${uploading.length + pending.length}개 남음${pct}`;
      return;
    }
    if (elapsed < PIPE_STEP_DELAY) return;

    const uploaded = outputJobs.filter(j => j.yt_status === "done");
    const failed   = outputJobs.filter(j => j.yt_status === "error");
    if (!uploaded.length) {
      _pipeFail(`YouTube 업로드 실패. ${failed[0]?.yt_error || "업로드된 영상이 없습니다."}`);
      return;
    }
    if (failed.length) {
      _pipeFail(`${failed.length}개 업로드 실패 (${failed[0].yt_error || ""}) — [이어서 진행]으로 재업로드할 수 있습니다.`,
                {canSkip: true});
      return;
    }
    logLine(`[원스톱] YouTube 업로드 완료 (${uploaded.length}개)`, "done");
    await _gotoStep(mode === "band" ? "posting" : "band_copy");
  }
  // posting / band_copy / done 은 _enterStep에서 즉시 처리되고 스스로 다음 단계로 넘어간다.
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
