// dotplay.js — Dot Play(FM 스타일 2D 버드뷰) 탭. 하이라이트 파이프라인의
// allJobs/poll()과 완전히 분리된 자체 폴링 루프를 쓴다(별도 워커/큐 반영).
//
// 렌더링은 잡별 슬롯(dp-slot-{id}) 단위로 부분 갱신한다 — 실행 중 잡의
// 경과시간이 매 폴링마다 바뀌어도, 완료된 잡의 <video> 재생 상태를
// 건드리지 않기 위함(전체 innerHTML 교체 시 재생이 끊긴다).

let dpJobs = [];
let dpStride = 2;
let dpOrder = null;     // 잡 id 순서 — 바뀌면 슬롯 재구성
let dpCardHash = {};    // id → 직전 렌더 내용 해시

const DP_STATUS_LABEL = {
  pending: "대기", running: "처리 중", done: "완료",
  error: "오류", cancelled: "취소됨",
};
const DP_STATUS_CLASS = {
  pending: "st-pending", running: "st-detecting", done: "st-done",
  error: "st-error", cancelled: "st-pending",
};
// dotplay/pipeline.py의 on_progress stage 이름과 일치해야 한다
const DP_STAGES = ["분석", "팀분류", "스무딩", "렌더링"];
// PiP 모드는 마지막에 ffmpeg 합성 단계가 추가된다 (jobs_dotplay._process)
const DP_STAGES_PIP = ["분석", "팀분류", "스무딩", "렌더링", "합성"];

function dpSetStride(v, btn) {
  dpStride = v;
  document.querySelectorAll("#dp-stride-seg button")
    .forEach(b => b.classList.toggle("on", b === btn));
}

async function dpCheckStatus() {
  try {
    const d = await (await fetch("/api/dotplay/status")).json();
    const banner = document.getElementById("dp-nokey-banner");
    if (banner) banner.style.display = d.has_roboflow_key ? "none" : "block";
  } catch (e) { /* noop */ }
}

async function dpBrowse() {
  try {
    const r = await fetch("/api/browse", { method: "POST" });
    const d = await r.json();
    if (d.paths && d.paths.length) {
      document.getElementById("dp-video-path").value = d.paths[0];
      if (d.paths.length > 1) {
        dpMsg(`${d.paths.length}개 선택됨 — dot-play는 한 번에 한 영상만 변환합니다. 첫 번째 파일을 사용합니다.`, "warn");
      }
    }
    if (d.error) console.warn("dp browse:", d.error);
  } catch (e) { /* noop */ }
}

function dpMsg(text, kind) {
  const el = document.getElementById("dp-add-result");
  if (!el) return;
  el.textContent = text;
  el.style.color = kind === "err" ? "var(--red)"
                 : kind === "warn" ? "var(--warn)" : "var(--accent)";
}

async function dpAddJob() {
  const input = document.getElementById("dp-video-path");
  const btn = document.getElementById("dp-btn-add");
  const video = input.value.trim();
  dpMsg("", "");
  if (!video) { dpMsg("영상 경로를 입력하세요.", "err"); return; }
  btn.disabled = true;
  try {
    const r = await fetch("/api/dotplay/jobs/add", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video, stride: dpStride }),
    });
    const d = await r.json();
    if (!r.ok) { dpMsg(d.error || "추가 실패", "err"); return; }
    dpMsg("작업이 추가되었습니다 — 아래 처리 현황에서 진행률을 확인하세요.", "ok");
    input.value = "";
    dpPoll();
  } catch (e) {
    dpMsg("요청 실패: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

// ── 하이라이트 PiP 합성 ─────────────────────────────
async function dpLoadHlJobs() {
  try {
    const { jobs } = await (await fetch("/api/dotplay/hl_jobs")).json();
    const sel = document.getElementById("dp-hl-select");
    if (!sel) return;
    const prev = sel.value;
    if (!jobs.length) {
      sel.innerHTML = '<option value="">완료된 하이라이트가 없습니다</option>';
      return;
    }
    sel.innerHTML = jobs.map(j =>
      `<option value="${j.id}">${esc(j.output_name)} (${esc(j.video_name)} · ${j.n_approved}구간)</option>`
    ).join("");
    if (prev && jobs.some(j => j.id === prev)) sel.value = prev;
  } catch (e) { /* noop */ }
}

function dpPipMsg(text, kind) {
  const el = document.getElementById("dp-pip-result");
  if (!el) return;
  el.textContent = text;
  el.style.color = kind === "err" ? "var(--red)"
                 : kind === "warn" ? "var(--warn)" : "var(--accent)";
}

async function dpAddPip() {
  const sel = document.getElementById("dp-hl-select");
  const btn = document.getElementById("dp-btn-pip");
  dpPipMsg("", "");
  if (!sel.value) { dpPipMsg("합성할 하이라이트를 선택하세요.", "err"); return; }
  btn.disabled = true;
  try {
    const r = await fetch("/api/dotplay/jobs/add_pip", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hl_jid: sel.value, stride: dpStride }),
    });
    const d = await r.json();
    if (!r.ok) { dpPipMsg(d.error || "추가 실패", "err"); return; }
    if (d.note) {
      dpPipMsg(`작업 추가됨 (${d.n_segments}구간, 총 ${Math.round(d.total_sec)}초) — ⚠ ${d.note}`, "warn");
    } else {
      dpPipMsg(`작업이 추가되었습니다 — ${d.n_segments}구간, 총 ${Math.round(d.total_sec)}초 분석 예정.`, "ok");
    }
    dpPoll();
  } catch (e) {
    dpPipMsg("요청 실패: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

async function dpCancel(jid) {
  if (!confirm("변환을 취소할까요? 지금까지의 진행 내용은 사라집니다.")) return;
  await fetch(`/api/dotplay/jobs/${jid}/cancel`, { method: "POST" });
  dpPoll();
}

async function dpDelete(jid) {
  const j = dpJobs.find(x => x.id === jid);
  const extra = j && j.status === "done" ? "\n결과 영상·좌표 파일도 함께 삭제됩니다." : "";
  if (!confirm("이 작업을 삭제할까요?" + extra)) return;
  await fetch(`/api/dotplay/jobs/${jid}`, { method: "DELETE" });
  dpPoll();
}

function dpFmtSec(s) {
  if (s == null) return "-";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h > 0) return `${h}시간 ${m}분`;
  return m > 0 ? `${m}분 ${sec}초` : `${sec}초`;
}

function dpStepper(stage, stages) {
  const idx = stages.indexOf(stage);
  const parts = stages.map((name, i) => {
    const cls = idx < 0 ? "" : i < idx ? "fin" : i === idx ? "act" : "";
    const mark = idx >= 0 && i < idx ? "✓" : i + 1;
    const line = i < stages.length - 1
      ? `<span class="dp-step-line ${idx >= 0 && i < idx ? "fin" : ""}"></span>` : "";
    return `<span class="dp-step ${cls}"><span class="n">${mark}</span>${name}</span>` + line;
  });
  return `<div class="dp-steps">${parts.join("")}</div>`;
}

function dpProgressHtml(j) {
  const p = j.progress || {};
  const stepper = dpStepper(p.stage, j.mode === "pip" ? DP_STAGES_PIP : DP_STAGES);
  if (p.stage === "분석" && p.total > 1) {
    const pct = Math.min(100, Math.round((p.done / p.total) * 100));
    let eta = "";
    if (j.elapsed_sec > 10 && p.done > 0 && p.done < p.total) {
      const remain = (j.elapsed_sec / p.done) * (p.total - p.done);
      eta = ` · 남은 시간 약 ${dpFmtSec(remain)}`;
    }
    return stepper + `
      <div class="dp-bar"><i style="width:${pct}%"></i></div>
      <div class="dp-prog-info">
        <span>프레임 ${p.done.toLocaleString()} / ${p.total.toLocaleString()}</span>
        <span>${pct}%${eta}</span>
      </div>`;
  }
  // 팀분류·스무딩·렌더링 등 총량을 모르는 단계 — 흐름 애니메이션
  return stepper + `
    <div class="dp-bar ind"><i></i></div>
    <div class="dp-prog-info">
      <span>${esc(p.stage || "준비 중")}...</span>
      <span>경과 ${dpFmtSec(j.elapsed_sec)}</span>
    </div>`;
}

function dpRenderJob(j) {
  const label = DP_STATUS_LABEL[j.status] || j.status;
  const cls = DP_STATUS_CLASS[j.status] || "st-pending";
  const isPip = j.mode === "pip";
  const name = isPip ? esc(j.hl_name || j.video_name) : esc(j.video_name);

  let body = "";
  if (j.status === "running") {
    body = dpProgressHtml(j);
  } else if (j.status === "pending") {
    body = `<div class="hint" style="margin-top:6px;font-size:12px">앞 작업이 끝나면 자동으로 시작됩니다.</div>`;
  } else if (j.status === "error") {
    body = `<div class="dp-error-box">${esc(j.error || "알 수 없는 오류")}</div>`;
  } else if (j.status === "done" && j.output_video) {
    const dlMain = isPip
      ? `<a class="dp-dl" href="/api/dotplay/jobs/${j.id}/video" download="pip_${name}">합성 영상 다운로드</a>`
      : `<a class="dp-dl" href="/api/dotplay/jobs/${j.id}/video" download="dotplay_${name}">영상 다운로드</a>`;
    body = `
      <div class="dp-result">
        <video controls preload="metadata" src="/api/dotplay/jobs/${j.id}/video"></video>
        <div class="dp-chips">
          <span class="dp-chip">프레임 <b>${(j.n_frames ?? 0).toLocaleString()}</b></span>
          <span class="dp-chip">트랙 <b>${j.n_tracks ?? 0}</b></span>
          <span class="dp-chip">소요 <b>${dpFmtSec(j.elapsed_sec)}</b></span>
        </div>
        <div class="row" style="gap:8px">
          ${dlMain}
          ${j.output_radar ? `<a class="dp-dl ghost" href="/api/dotplay/jobs/${j.id}/radar" title="합성 전 2D 버드뷰 단독 영상">레이더 영상</a>` : ""}
          ${j.output_coords ? `<a class="dp-dl ghost" href="/api/dotplay/jobs/${j.id}/coords" title="선수·공 좌표 테이블 (분석용)">좌표 데이터 (parquet)</a>` : ""}
        </div>
      </div>`;
  }

  const actions = j.status === "running"
    ? `<button class="ghost sm" onclick="dpCancel('${j.id}')">취소</button>`
    : `<button class="ghost sm" onclick="dpDelete('${j.id}')">삭제</button>`;

  const meta = [`샘플링 1/${j.stride}`];
  if (isPip) meta.push(`원본: ${esc(j.video_name)}`);
  if (j.status === "running" && j.elapsed_sec != null) meta.push(`경과 ${dpFmtSec(j.elapsed_sec)}`);
  const noteHtml = j.note
    ? `<div class="hint" style="margin-top:5px;font-size:11px;color:var(--warn)">⚠ ${esc(j.note)}</div>` : "";

  return `
    <div class="dp-job ${j.status}">
      <div class="dp-job-head">
        <span class="dp-job-name" title="${name}">${isPip ? '<span class="dp-mode-tag">PiP</span>' : ""}${name}</span>
        <span style="display:flex;align-items:center;gap:8px;flex-shrink:0">
          ${j.status === "running" ? '<span class="spin"></span>' : ""}
          <span class="st ${cls}">${label}</span>
          ${actions}
        </span>
      </div>
      <div class="dp-job-meta">${meta.join(" · ")}</div>
      ${noteHtml}
      ${body}
    </div>`;
}

function dpRenderSummary() {
  const el = document.getElementById("dp-summary");
  if (!el) return;
  if (!dpJobs.length) { el.style.display = "none"; return; }
  const n = s => dpJobs.filter(j => j.status === s).length;
  el.style.display = "flex";
  el.innerHTML = `
    <span class="qs-proc">처리 중 ${n("running")}</span>
    <span class="qs-wait">대기 ${n("pending")}</span>
    <span class="qs-done">완료 ${n("done")}</span>
    ${n("error") ? `<span style="color:var(--red);font-weight:700">오류 ${n("error")}</span>` : ""}`;
}

function dpRender() {
  const el = document.getElementById("dp-job-list");
  if (!el) return;

  dpRenderSummary();
  const cnt = document.getElementById("cnt-dp");
  if (cnt) {
    const active = dpJobs.filter(j => ["pending", "running"].includes(j.status)).length;
    cnt.textContent = active || dpJobs.length;
  }

  if (!dpJobs.length) {
    if (dpOrder !== "") {
      dpOrder = "";
      dpCardHash = {};
      el.innerHTML = `<div class="dp-empty">아직 추가된 dot-play 작업이 없습니다.<br>
        <span class="hint">위에서 영상을 선택하고 변환을 시작하세요.</span></div>`;
    }
    return;
  }

  const list = dpJobs.slice().reverse();  // 최신 잡이 위로
  const order = list.map(j => j.id).join(",");
  if (order !== dpOrder) {
    dpOrder = order;
    dpCardHash = {};
    el.innerHTML = list.map(j => `<div id="dp-slot-${j.id}"></div>`).join("");
  }
  for (const j of list) {
    const h = JSON.stringify(j);
    if (dpCardHash[j.id] === h) continue;
    dpCardHash[j.id] = h;
    const slot = document.getElementById(`dp-slot-${j.id}`);
    if (slot) slot.innerHTML = dpRenderJob(j);
  }
}

let dpPollCount = 0;

async function dpPoll() {
  try {
    const { jobs } = await (await fetch("/api/dotplay/jobs")).json();
    dpJobs = jobs;
    dpRender();
    // 하이라이트 목록은 20초마다 갱신 (빌드 완료가 뒤늦게 반영되도록)
    if (dpPollCount++ % 10 === 0) dpLoadHlJobs();
  } catch (e) { /* noop */ }
}

dpCheckStatus();
setInterval(dpPoll, 2000);
dpPoll();
