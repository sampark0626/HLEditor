// main.js — 폴링 루프와 페이지 초기화. 모든 다른 static/js 파일이 로드된 뒤 실행되어야
// 하므로 <script> 태그 목록의 맨 마지막에 위치해야 한다.

function updateBadges() {
  const proc  = allJobs.filter(j => ["pending","detecting","classifying"].includes(j.status)).length;
  const ready = allJobs.filter(j => ["ready","done"].includes(j.status)).length;
  const apprd = allJobs.filter(j => j.n_approved !== null && j.n_approved > 0).length;
  document.getElementById("cnt-q").textContent = proc || allJobs.length;
  document.getElementById("cnt-r").textContent = ready;
  document.getElementById("cnt-b").textContent = apprd;
}

// ─── 폴링 ───────────────────────────────────────────────
async function poll() {
  try {
    const {jobs} = await (await fetch("/api/jobs")).json();
    checkNotifications(jobs);
    // 서버에서 사라진 잡(지연 삭제 완료 등)의 로컬 캐시 정리
    const prevIds = new Set(allJobs.map(j => j.id));
    const curIds  = new Set(jobs.map(j => j.id));
    for (const jid of prevIds) {
      if (curIds.has(jid)) continue;
      delete jobCands[jid]; delete localSel[jid]; delete savedSel[jid];
      delete expanded[jid]; delete confVal[jid];  delete buildData[jid];
      delete prevStatuses[jid]; autoUploaded.delete(jid);
    }
    allJobs = jobs;
    updateBadges();
    checkAutoUpload();
    await advancePipeline(jobs);  // 원스톱 파이프라인 상태 점검
    if (activePne === "queue")  renderQueue();
    if (activePne === "review") renderReview();
    if (activePne === "build")  renderBuild();
    // 스테이징 중복 표시 갱신
    if (staged.length) {
      const activeNames = new Set(
        allJobs.filter(j => !["done","error"].includes(j.status)).map(j => j.video_name));
      staged.forEach(s => s.dup = activeNames.has(s.basename));
      renderStaged();
    }
  } catch(e) {}
}

// ─── 초기화 (원본 실행 순서 유지) ────────────────────────
restoreState();
applyRestoredUI();
refreshAuthIndicators();   // 큐 탭 진입 시 인증 상태 확인
initNotif();
setInterval(poll, 2000);
// 페이지 로드 시 저장된 파이프라인 상태 복원
// (멈춤 상태로 복원됐다면 버튼을 잠그지 않는다 — 다른 작업을 하거나 재개할 수 있어야 한다)
if (_pipe) { _pipeBtns(_pipe.status !== "failed"); renderPipeBar(); }
poll();
