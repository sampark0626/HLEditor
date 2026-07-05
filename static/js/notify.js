// notify.js — 브라우저 데스크톱 알림

function initNotif() {
  if (!("Notification" in window)) return;
  if (Notification.permission === "default") {
    document.getElementById("notif-bar").style.display = "flex";
  }
}

function requestNotif() {
  Notification.requestPermission().then(p => {
    document.getElementById("notif-bar").style.display = "none";
    if (p === "granted") logLine("알림 권한 허용됨", "done");
  });
}

function sendNotif(title, body) {
  if (Notification.permission !== "granted") return;
  try { new Notification(title, {body, icon:""}); } catch(e){}
}

function checkNotifications(jobs) {
  let hasProcessing = false;
  for (const j of jobs) {
    const prev = prevStatuses[j.id];
    if (!prev) { prevStatuses[j.id] = j.status; continue; }
    if (prev !== j.status) {
      if (j.status === "ready") {
        sendNotif("판별 완료", `${j.video_name} — 리뷰 탭에서 확인하세요`);
        logLine(`${j.video_name} 판별 완료`, "done");
      } else if (j.status === "done") {
        sendNotif("영상 생성 완료", j.video_name);
      } else if (j.status === "error") {
        sendNotif("오류 발생", j.video_name);
      }
    }
    if (["pending","detecting","classifying","building"].includes(j.status)) hasProcessing = true;
    prevStatuses[j.id] = j.status;
  }
  // 전체 처리 완료 알림 (마지막 잡이 완료될 때)
  if (wasProcessing && !hasProcessing && jobs.length > 0) {
    const done  = jobs.filter(j => j.status === "ready").length;
    const error = jobs.filter(j => j.status === "error").length;
    sendNotif("전체 처리 완료",
      `${done}개 준비됨${error ? ` / ${error}개 오류` : ""} — 리뷰 탭을 확인하세요`);
  }
  wasProcessing = hasProcessing;
}
