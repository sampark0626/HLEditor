// Code.gs를 가짜 Apps Script 환경에서 시험한다 — 실행: node kaggle_runner/apps_script/code_test.js
// Apps Script 전역(PropertiesService, DriveApp, UrlFetchApp …)을 흉내 내고 시나리오별로 확인한다.
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SOURCE = fs.readFileSync(path.join(__dirname, 'Code.gs'), 'utf8');
const KAGGLE = 'https://api.kaggle.com/v1/kernels.KernelsApiService/';

// 2026-09-27(일) 12:00 KST = 03:00 UTC
const SUNDAY_NOON = Date.UTC(2026, 8, 27, 3, 0, 0);
const WEDNESDAY_NOON = Date.UTC(2026, 8, 30, 3, 0, 0);
const MIN = 60 * 1000;

// state: Drive `_state` 폴더의 파일들 {이름: JSON 문자열}. null이면 폴더가 없다(검토 기능 이전과 같은 환경).
function makeEnv({files = {}, status = 'COMPLETE', saveResponse = null, bootstrapOk = true,
                  props = {}, now = SUNDAY_NOON, state = null,
                  user = 'me@example.com', owner = 'me@example.com'} = {}) {
  const env = {now, calls: [], status, saveResponse, bootstrapOk, triggers: [], state, user, owner,
               html: null, uuid: 0};
  const store = Object.assign({
    KAGGLE_USERNAME: 'me', KAGGLE_API_TOKEN: 'KGAT_x', ROOT_FOLDER_ID: 'root', NTFY_TOPIC: 'hl-topic',
  }, props);

  class FakeDate extends Date {
    constructor(...args) { if (args.length) super(...args); else super(env.now); }
    static now() { return env.now; }
  }

  const iterator = (items) => { let i = 0; return {hasNext: () => i < items.length, next: () => items[i++]}; };
  const folder = (name) => ({
    getFiles: () => iterator((env.files[name] || []).map((f) => ({
      getName: () => f.name, getMimeType: () => f.mime || 'application/octet-stream', isTrashed: () => !!f.trashed,
    }))),
  });
  env.files = files;

  const stateFile = (name) => ({
    getName: () => name, isTrashed: () => false,
    getBlob: () => ({getDataAsString: () => env.state[name]}),
    setContent: (text) => { env.state[name] = text; },
  });
  const stateFolder = () => ({
    getFiles: () => iterator(Object.keys(env.state).map(stateFile)),
    getFilesByName: (name) => iterator(name in env.state ? [stateFile(name)] : []),
    createFile: (name, text) => { env.state[name] = text; return stateFile(name); },
  });
  env.json = (name) => JSON.parse(env.state[name]);

  const response = (code, body) => ({
    getResponseCode: () => code,
    getContentText: () => (typeof body === 'string' ? body : JSON.stringify(body)),
  });

  const context = {
    Date: FakeDate,
    JSON, Number, String, Math, Error, Object, Array, RegExp,
    console: {error: () => {}, warn: () => {}, log: () => {}},
    PropertiesService: {getScriptProperties: () => ({
      getProperty: (k) => (k in store ? store[k] : null),
      setProperty: (k, v) => { store[k] = String(v); },
      deleteProperty: (k) => { delete store[k]; },
      getProperties: () => Object.assign({}, store),
    })},
    LockService: {getScriptLock: () => ({tryLock: () => true, waitLock: () => {}, releaseLock: () => {}})},
    DriveApp: {getFolderById: (id) => {
      assert.strictEqual(id, 'root');
      return {getFoldersByName: (name) => iterator(
        name === '_state' ? (env.state ? [stateFolder()] : [])
          : name in env.files ? [folder(name)] : [])};
    }},
    Session: {
      getActiveUser: () => ({getEmail: () => env.user}),
      getEffectiveUser: () => ({getEmail: () => env.owner}),
    },
    HtmlService: {
      XFrameOptionsMode: {ALLOWALL: 'ALLOWALL'},
      createTemplateFromFile: (file) => {
        const tpl = {};
        tpl.evaluate = () => {
          const out = {file, initial: tpl.initial};
          out.setTitle = (t) => { out.title = t; return out; };
          out.addMetaTag = (k, v) => { out.meta = [k, v]; return out; };
          out.setXFrameOptionsMode = (m) => { out.frame = m; return out; };
          env.html = out;
          return out;
        };
        return tpl;
      },
    },
    Utilities: {base64Encode: (s) => Buffer.from(s, 'utf8').toString('base64'),
      getUuid: () => 'uuid-' + (++env.uuid),
      formatDate: (date, tz, fmt) => {
        assert.strictEqual(tz, 'Asia/Seoul');
        const kst = new Date(date.getTime() + 9 * 3600 * 1000);
        if (fmt === 'yyyy-MM-dd') return kst.toISOString().slice(0, 10);
        assert.strictEqual(fmt, 'u');
        const day = kst.getUTCDay();  // 0=일
        return String(day === 0 ? 7 : day);
      }},
    UrlFetchApp: {fetch: (url, opts = {}) => {
      env.calls.push({url, opts, body: opts.payload ? JSON.parse(opts.payload) : null});
      if (url.startsWith(KAGGLE + 'GetKernelSessionStatus')) {
        if (env.status === 404) return response(404, {error: 'not found'});
        if (env.status === 403) {   // 실제 Kaggle: 없는 커널 조회는 403 kernels.get
          return response(403, {error: {code: 403, message: "Permission 'kernels.get' was denied",
                                        status: 'PERMISSION_DENIED'}});
        }
        if (env.status === 401) return response(401, {error: {code: 401, message: 'Unauthenticated'}});
        return response(200, {status: env.status});
      }
      if (url.startsWith(KAGGLE + 'SaveKernel')) {
        return response(200, env.saveResponse || {ref: '/code/me/hleditor-inbox', versionNumber: 3});
      }
      if (url.includes('raw.githubusercontent.com')) {
        if (!env.bootstrapOk) throw new Error('network down');
        return response(200, 'print("from github")  # inbox_runner');
      }
      if (url === 'https://ntfy.sh/') return response(200, {id: 'n1'});
      throw new Error('unexpected url ' + url);
    }},
    ContentService: {createTextOutput: (text) => ({text})},
    ScriptApp: {
      getProjectTriggers: () => env.triggers.map((t) => ({getHandlerFunction: () => t.fn, __t: t})),
      deleteTrigger: (tr) => { env.triggers.splice(env.triggers.indexOf(tr.__t), 1); },
      newTrigger: (fn) => {
        const cfg = {fn};
        const b = {
          timeBased: () => b,
          everyMinutes: (n) => { cfg.n = n; return b; },
          everyDays: (n) => { cfg.days = n; return b; },
          atHour: (h) => { cfg.hour = h; return b; },
          nearMinute: (m) => { cfg.minute = m; return b; },
          inTimezone: (tz) => { cfg.tz = tz; return b; },
          create: () => { env.triggers.push(cfg); },
        };
        return b;
      },
    },
  };
  vm.createContext(context);
  vm.runInContext(SOURCE, context);
  env.ctx = context;
  env.props = store;
  env.of = (prefix) => env.calls.filter((c) => c.url.startsWith(prefix));
  return env;
}

const video = (name) => ({name, mime: 'video/mp4'});
const tests = [];
const test = (name, fn) => tests.push({name, fn});

test('인박스가 비면 아무 API도 부르지 않는다', () => {
  const env = makeEnv({files: {'01_inbox': [], '01_inbox_2d': []}});
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
  assert.strictEqual(env.calls.length, 0);
});

test('영상이 있고 커널이 쉬고 있으면 CPU로 push한다(시작 알림은 기본으로 보내지 않는다)', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.MP4'), video('b.mov'), {name: 'note.txt'}]}});
  const msg = env.ctx.tick();
  assert.match(msg, /영상 2개 발견/);
  const push = env.of(KAGGLE + 'SaveKernel')[0];
  assert.ok(push, 'SaveKernel 호출');
  assert.strictEqual(push.opts.headers.Authorization, 'Bearer KGAT_x');
  assert.strictEqual(push.body.slug, 'me/hleditor-inbox');
  assert.strictEqual(push.body.newTitle, 'hleditor-inbox');
  assert.strictEqual(push.body.kernelType, 'script');
  assert.strictEqual(push.body.isPrivate, true);
  assert.strictEqual(push.body.enableInternet, true);
  assert.strictEqual(push.body.enableGpu, false);
  assert.strictEqual(push.body.machineShape, undefined);
  assert.deepStrictEqual(push.body.datasetDataSources, ['me/hl-secrets']);
  assert.match(push.body.text, /from github/);
  assert.strictEqual(env.of('https://ntfy.sh/').length, 0);   // 업로드가 진행 중일 수 있어 일찍 알리지 않는다
  assert.strictEqual(env.props.LAST_PUSH_AT, String(SUNDAY_NOON));
});

test('NOTIFY_START=1이면 push할 때 시작 알림도 보낸다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.MP4'), video('b.mov')]}, props: {NOTIFY_START: '1'}});
  env.ctx.tick();
  const note = env.of('https://ntfy.sh/')[0];
  assert.strictEqual(note.body.topic, 'hl-topic');
  assert.match(note.body.title, /영상 2개 확인/);
});

test('NOTIFY_START가 0이거나 비어 있으면 시작 알림이 없다', () => {
  for (const v of ['0', '', 'off']) {
    const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, props: {NOTIFY_START: v}});
    env.ctx.tick();
    assert.strictEqual(env.of('https://ntfy.sh/').length, 0, 'NOTIFY_START=' + JSON.stringify(v));
  }
});

test('01_inbox_2d에 영상이 있으면 GPU로 push한다', () => {
  const env = makeEnv({files: {'01_inbox': [], '01_inbox_2d': [video('c.mp4')]}});
  env.ctx.tick();
  const push = env.of(KAGGLE + 'SaveKernel')[0];
  assert.strictEqual(push.body.machineShape, 'NvidiaTeslaT4');
  assert.strictEqual(push.body.enableGpu, true);
});

test('커널이 실행 중이면 push하지 않는다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, status: 'KERNEL_WORKER_STATUS_RUNNING'});
  assert.match(env.ctx.tick(), /처리 중/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 0);
});

test('커널이 아직 없으면(404) 첫 push로 만든다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, status: 404});
  assert.match(env.ctx.tick(), /실행 요청함/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 1);
});

test('커널이 아직 없을 때 Kaggle이 주는 403(kernels.get)도 "없음"으로 보고 첫 push로 만든다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, status: 403});
  assert.match(env.ctx.tick(), /실행 요청함/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 1);
  assert.match(env.ctx.showStatus(), /IDLE/);
});

test('다른 인증 오류(401)는 push하지 않고 오류로 알린다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, status: 401});
  assert.match(env.ctx.tick(), /^오류: Kaggle 상태 조회 실패 \(HTTP 401\)/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 0);
});

test('push 직후 10분 동안은 다시 push하지 않는다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}});
  env.ctx.tick();
  env.now += 5 * MIN;
  env.status = 'COMPLETE';   // 상태가 아직 안 바뀐 것처럼 보이는 경우
  assert.match(env.ctx.tick(), /방금 실행을 요청/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 1);
  env.now += 6 * MIN;
  assert.match(env.ctx.tick(), /실행 요청함/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 2);
});

test('확인 요일이 아니면 약 1시간에 한 번만 확인한다', () => {
  const env = makeEnv({files: {'01_inbox': []}, now: WEDNESDAY_NOON});
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');           // 첫 확인은 진행
  env.now += 5 * MIN;
  assert.match(env.ctx.tick(), /건너뜀/);
  env.now += 51 * MIN;                                            // 첫 확인 후 56분
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
});

test('FAST_DAYS 기본값은 일·월 — 월요일엔 매번 확인한다', () => {
  const monday = Date.UTC(2026, 8, 28, 3, 0, 0);
  const env = makeEnv({files: {'01_inbox': []}, now: monday});
  env.ctx.tick();
  env.now += 5 * MIN;
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
  assert.strictEqual(env.props.LAST_SLOW_CHECK_AT, undefined);
});

test('FAST_DAYS를 바꾸면 그 요일에 촘촘히 확인한다', () => {
  const env = makeEnv({files: {'01_inbox': []}, now: WEDNESDAY_NOON, props: {FAST_DAYS: '3'}});
  env.ctx.tick();
  env.now += 5 * MIN;
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
});

test('연속 실패 3회면 자동 실행을 멈추고 알림은 한 번만 보낸다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}});
  for (let i = 0; i < 3; i++) {
    env.ctx.tick();                    // push
    env.now += 11 * MIN;
    env.status = 'ERROR';              // 다음 확인에서 이 실행의 실패를 센다
  }
  const pushes = env.of(KAGGLE + 'SaveKernel').length;
  assert.strictEqual(pushes, 3);
  assert.match(env.ctx.tick(), /자동 실행이 멈춰/);   // 세 번째 실패는 다음 확인에서 세고 바로 멈춘다
  assert.strictEqual(env.props.ERROR_STREAK, '3');
  env.now += 11 * MIN;
  env.ctx.tick();
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, pushes);
  const breakerNotes = env.of('https://ntfy.sh/').filter((c) => /자동 실행 중지/.test(c.body.title));
  assert.strictEqual(breakerNotes.length, 1);
  env.ctx.resetBreaker();
  env.status = 'COMPLETE';
  assert.match(env.ctx.tick(), /실행 요청함/);
});

test('성공한 실행을 확인하면 실패 횟수를 0으로 되돌린다(인박스가 비어 있어도)', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, props: {ERROR_STREAK: '2'}});
  env.ctx.tick();                      // push
  env.files['01_inbox'] = [];
  env.now += 30 * MIN;
  env.status = 'COMPLETE';
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
  assert.strictEqual(env.props.ERROR_STREAK, '0');
  const statusCalls = env.of(KAGGLE + 'GetKernelSessionStatus').length;
  env.now += 5 * MIN;
  env.ctx.tick();                      // 결과를 이미 기록했으므로 상태 조회 없음
  assert.strictEqual(env.of(KAGGLE + 'GetKernelSessionStatus').length, statusCalls);
});

test('SaveKernel이 거절하면 오류를 돌려주고 LAST_PUSH_AT을 남기지 않는다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]},
    saveResponse: {error: 'Notebook not found', invalidDatasetSources: ['me/hl-secrets']}});
  const msg = env.ctx.tick();
  assert.match(msg, /^오류: Kaggle 실행 요청 실패/);
  assert.match(msg, /invalidDatasetSources/);
  assert.strictEqual(env.props.LAST_PUSH_AT, undefined);
  assert.strictEqual(env.of('https://ntfy.sh/').length, 0);
});

test('GitHub에서 bootstrap을 못 받으면 내장 사본으로 push한다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, bootstrapOk: false});
  env.ctx.tick();
  const push = env.of(KAGGLE + 'SaveKernel')[0];
  assert.match(push.body.text, /inbox_runner\.main\(\[\]\)/);
});

test('내장 bootstrap 사본은 저장소 kernel_bootstrap.py의 실행 코드와 같다', () => {
  const env = makeEnv();
  const file = fs.readFileSync(path.join(__dirname, '..', 'kernel_bootstrap.py'), 'utf8');
  const code = (text) => text.replace(/"""[\s\S]*?"""/, '').split('\n')
    .map((l) => l.replace(/\s+#.*$/, '').trim()).filter(Boolean).join('\n');
  assert.strictEqual(code(env.ctx.BOOTSTRAP_FALLBACK), code(file));
});

test('실행 링크: key가 틀리면 거부, 맞으면 요일과 상관없이 확인한다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, now: WEDNESDAY_NOON,
    props: {KICK_KEY: 'secret', LAST_SLOW_CHECK_AT: String(WEDNESDAY_NOON)}});
  assert.match(env.ctx.doGet({parameter: {key: 'nope'}}).text, /거부됨/);
  assert.match(env.ctx.doGet({parameter: {}}).text, /거부됨/);
  assert.strictEqual(env.of(KAGGLE).length, 0);
  assert.match(env.ctx.doGet({parameter: {key: 'secret'}}).text, /실행 요청함/);
});

test('실행 링크: KICK_KEY를 설정하지 않았으면 항상 거부한다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}});
  assert.match(env.ctx.doGet({parameter: {key: ''}}).text, /거부됨/);
});

test('예전 방식 API 키(kaggle.json의 key)는 아이디와 함께 Basic 인증으로 보낸다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, props: {KAGGLE_API_TOKEN: 'abc123'}});
  env.ctx.tick();
  const push = env.of(KAGGLE + 'SaveKernel')[0];
  assert.strictEqual(push.opts.headers.Authorization, 'Basic ' + Buffer.from('me:abc123').toString('base64'));
});

test('스크립트 속성이 빠지면 무엇이 빠졌는지 알려준다', () => {
  const env = makeEnv({props: {NTFY_TOPIC: ''}});
  assert.match(env.ctx.tick(), /스크립트 속성 누락: NTFY_TOPIC/);
});

test('installTrigger는 기존 tick 트리거를 지우고 5분 트리거 하나만 건다', () => {
  const env = makeEnv();
  env.ctx.installTrigger();
  env.ctx.installTrigger();
  assert.deepStrictEqual(env.triggers, [{fn: 'tick', n: 5}]);
});

// ═══ 검토 페이지 · 자동 게시 (MONTHLY_PLAN.md Phase 2) ═══════════════════════════
const RULES = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'tests', 'data', 'tag_rules.json'), 'utf8'));
const plain = (x) => JSON.parse(JSON.stringify(x));       // vm 안에서 만든 배열·객체를 이쪽 realm 것으로 바꿔 비교한다
const D = '2026-12-06';
const DEC6_NOON = Date.UTC(2026, 11, 6, 3, 0, 0);           // 2026-12-06(일) 12:00 KST
const DEC7_10AM = Date.UTC(2026, 11, 7, 1, 0, 0);           // 2026-12-07(월) 10:00 KST — 자동 게시 시각(09:00) 뒤
const REVIEW_URL = 'https://script.google.com/macros/s/AKfy/exec';

const gameDoc = (over = {}) => Object.assign({
  date: D, label: '1경기', status: 'pending', auto_at: '2026-12-07T09:00:00+09:00', kind: null, opponent: '',
  attack_end: null, switch_at_idx: null, clip_tags: {}, tagged_at: null, tag_source: null,
  published_clips: [], yt_url: null, yt_title: null, published_at: null, attempts: 0, error: null,
}, over);
const clipsDoc = (ends = [null, null, null], label = '1경기', date = D) => ({
  date, label, clips: ends.map((end, i) => ({
    idx: i + 1, name: `${date}-${label}_c00${i + 1}.mp4`, file_id: `fid${i + 1}`, src_start: 100 * (i + 1),
    src_end: 100 * (i + 1) + 13, dur: 13, end,
    cands: [{peak: 108 + 100 * i, type: 'goal', confidence: 0.9, delta_db: 12, pan: null}]})),
});
/** games·clips JSON 파일들을 _state 폴더 내용으로 */
const stateOf = (...games) => {
  const state = {};
  games.forEach((g) => {
    const doc = Array.isArray(g) ? g[0] : g;
    const ends = Array.isArray(g) ? g[1] : undefined;
    state[`games_${doc.date}_${doc.label}.json`] = JSON.stringify(doc);
    state[`clips_${doc.date}_${doc.label}.json`] = JSON.stringify(clipsDoc(ends, doc.label, doc.date));
  });
  return state;
};
const reviewEnv = (opts = {}) => makeEnv(Object.assign({state: stateOf(gameDoc()), now: DEC6_NOON}, opts));
const kernelPushes = (env) => env.of(KAGGLE + 'SaveKernel').length;

// ─── 규칙 표 — pytest(tests/test_tags.py)와 같은 파일을 읽는다 ───────────────
RULES.sideCases.forEach((c) => {
  test('규칙 표(공유) sideOf_ — ' + c.name, () => {
    const env = makeEnv();
    assert.strictEqual(env.ctx.sideOf_(c.clip, c.game), c.side);
  });
});
RULES.eligibleCases.forEach((c) => {
  test('규칙 표(공유) 월간·주간 대상 — ' + c.name, () => {
    const env = makeEnv();
    const idxs = (list) => plain(list).map((x) => x.idx);
    assert.deepStrictEqual(idxs(env.ctx.monthlyEligible_(c.clips, c.game)), c.eligible);
    assert.deepStrictEqual(idxs(env.ctx.weeklyClips_(c.clips, c.game)), c.weekly);
  });
});
RULES.nameCases.forEach((c) => {
  test('규칙 표(공유) gameName_ — ' + c.name, () => {
    const env = makeEnv();
    assert.strictEqual(env.ctx.gameName_(c.date, c.label, c.kind, c.opponent), c.title);
    assert.strictEqual(env.ctx.gameName_(c.date, c.label, c.kind, c.opponent, true), c.file);
  });
});

test('주간 영상은 상대 득점(수비·실점) 클립을 빼지 않는다 — "잘못 잡힘"만 뺀다', () => {
  const env = makeEnv();
  const game = {kind: 'opponent', attack_end: 'building', clip_tags: {
    2: {conceded: true}, 3: {side_override: 'defense', conceded: true}}};
  const clips = [{idx: 1, end: 'building'}, {idx: 2, end: 'tree'}, {idx: 3, end: null}];
  assert.deepStrictEqual(plain(env.ctx.weeklyClips_(clips, game)).map((c) => c.idx), [1, 2, 3]);
  assert.deepStrictEqual(plain(env.ctx.monthlyEligible_(clips, game)).map((c) => c.idx), [1]);
});

// ─── 소유자 확인 ─────────────────────────────────────────────────────────────
test('검토 페이지: 소유자가 열면 템플릿을 보여 주고, 주소의 날짜는 형식이 맞을 때만 싣는다', () => {
  const env = reviewEnv();
  const out = env.ctx.doGet({parameter: {page: 'review', date: D}});
  assert.strictEqual(out.file, 'Review');
  assert.deepStrictEqual(JSON.parse(out.initial), {date: D, month: ''});
  assert.strictEqual(out.title, 'HLEditor 검토');
  assert.deepStrictEqual(plain(out.meta), ['viewport', 'width=device-width, initial-scale=1']);
  const evil = env.ctx.doGet({parameter: {page: 'review', date: '2026-12-06";alert(1);//', month: '2026-12'}});
  assert.deepStrictEqual(JSON.parse(evil.initial), {date: '', month: '2026-12'});   // 스크립트 삽입 방지
});

test('검토 페이지: 로그인 정보가 없거나(모든 사용자 배포) 소유자가 아니면 거부한다', () => {
  for (const user of ['', 'someone@else.com']) {
    const env = reviewEnv({user});
    const out = env.ctx.doGet({parameter: {page: 'review'}});
    assert.match(out.text, /거부됨: 소유자만/);
    assert.strictEqual(env.html, null, 'user=' + JSON.stringify(user));
  }
});

test('검토 서버 함수는 모두 소유자만 부를 수 있다', () => {
  const env = reviewEnv({user: ''});
  const calls = [
    () => env.ctx.reviewList(), () => env.ctx.reviewGetDay(D),
    () => env.ctx.reviewSetGame(D, '1경기', {kind: 'scrimmage'}),
    () => env.ctx.reviewTagClip(D, '1경기', 1, {removed: true}),
    () => env.ctx.reviewPublish(D, ['1경기']), () => env.ctx.reviewRecentOpponents(),
  ];
  calls.forEach((fn) => assert.throws(fn, /거부됨: 소유자 본인만/));
  assert.strictEqual(env.json(`games_${D}_1경기.json`).status, 'pending');
  assert.strictEqual(kernelPushes(env), 0);
});

test('실행 링크(KICK_KEY)는 검토 기능과 따로 그대로 동작한다', () => {
  const env = reviewEnv({props: {KICK_KEY: 'secret'}, files: {'01_inbox': [video('a.mp4')]}});
  assert.match(env.ctx.doGet({parameter: {key: 'secret'}}).text, /실행 요청함/);
  assert.match(env.ctx.doGet({parameter: {key: 'x'}}).text, /거부됨: key/);
});

// ─── 화면 데이터 ─────────────────────────────────────────────────────────────
test('reviewGetDay: 클립마다 공격/수비·월간 대상을 서버가 계산해 보낸다', () => {
  const env = makeEnv({now: DEC6_NOON, state: stateOf([gameDoc({
    kind: 'opponent', opponent: 'OO FC', attack_end: 'building',
    clip_tags: {2: {removed: true}, 3: {conceded: true}}}), ['building', 'tree', 'tree']])});
  const day = plain(env.ctx.reviewGetDay(D));
  const [g] = day.games;
  assert.strictEqual(g.name, `${D}-1경기-OO FC`);
  assert.deepStrictEqual(g.clips.map((c) => [c.idx, c.side, c.removed, c.conceded, c.monthly]), [
    [1, 'attack', false, false, true],
    [2, 'defense', true, false, false],        // 잘못 잡힘 — 주간·월간 모두에서 빠짐
    [3, 'defense', false, true, false],        // 실점 — 월간에서만 빠짐(주간에는 남는다)
  ]);
  assert.deepStrictEqual([g.hasEnd, g.weeklyCount, g.weeklySec, g.monthlyCount, g.monthlySec], [true, 2, 26, 1, 13]);
  assert.strictEqual(g.clips[0].preview, 'https://drive.google.com/file/d/fid1/preview');
  assert.strictEqual(g.clips[0].shots, 1);
  assert.deepStrictEqual([g.editable, g.publishable], [true, true]);
});

test('reviewGetDay: 골대 쪽 판별(L1)이 없으면 hasEnd가 거짓이라 클립마다 공격/수비를 고르게 된다', () => {
  const env = reviewEnv();
  const g = plain(env.ctx.reviewGetDay(D)).games[0];
  assert.strictEqual(g.hasEnd, false);
  assert.deepStrictEqual(g.clips.map((c) => c.side), [null, null, null]);
});

test('reviewList: 검토할 경기(pending·실패·태깅 안 된 자동 게시)만 attention으로 표시하고 최근 날짜가 위로', () => {
  const state = Object.assign({},
    stateOf(gameDoc({label: '1경기'}), gameDoc({label: '2경기', status: 'published', kind: 'scrimmage', tagged_at: 'x'}),
      gameDoc({label: '3경기', status: 'auto_published', yt_url: 'https://youtu.be/a'})),
    stateOf(gameDoc({date: '2026-11-29', label: '1경기', status: 'auto_published', kind: 'opponent', opponent: 'A'})),
    {'games_2026-01-03_1경기.json': JSON.stringify(gameDoc({date: '2026-01-03'}))});   // 45일보다 오래됨
  const env = makeEnv({state, now: DEC6_NOON});
  const list = plain(env.ctx.reviewList());
  assert.strictEqual(list.today, D);
  assert.deepStrictEqual(list.days.map((d) => d.date), [D, '2026-11-29']);
  assert.deepStrictEqual(list.days[0].games.map((g) => [g.label, g.attention, g.clips]), [
    ['1경기', true, 3], ['2경기', false, null], ['3경기', true, 3]]);
  assert.strictEqual(list.days[1].games[0].attention, false);          // 이미 태깅한 자동 게시
});

// ─── 경기 태그 ───────────────────────────────────────────────────────────────
test('reviewSetGame: 종류·상대·골대를 저장하고 상대 이름은 다듬어 칩으로 기억한다', () => {
  const env = reviewEnv();
  const view = plain(env.ctx.reviewSetGame(D, '1경기', {kind: 'opponent', opponent: '  OO\n<b>FC</b>  ', attack_end: 'tree'}));
  assert.strictEqual(view.opponent, 'OO b FC /b');
  const game = env.json(`games_${D}_1경기.json`);
  assert.deepStrictEqual([game.kind, game.opponent, game.attack_end, game.tag_source], ['opponent', 'OO b FC /b', 'tree', 'manual']);
  assert.match(game.tagged_at, /^2026-12-06T03:00:00\+00:00$/);          // 러너가 읽는 형식(밀리초·Z 없음)
  assert.deepStrictEqual(plain(env.ctx.reviewRecentOpponents()), ['OO b FC /b']);
  env.ctx.reviewSetGame(D, '1경기', {opponent: 'XX FC'});
  env.ctx.reviewSetGame(D, '1경기', {opponent: 'OO b FC /b'});
  assert.deepStrictEqual(plain(env.ctx.reviewRecentOpponents()), ['OO b FC /b', 'XX FC']);
  assert.strictEqual(kernelPushes(env), 0);                              // 게시 전 태깅은 러너를 깨우지 않는다
});

test('reviewSetGame: 모르는 값은 거부하고, 게시 중(publish·running)인 경기는 못 고친다', () => {
  const env = reviewEnv();
  assert.throws(() => env.ctx.reviewSetGame(D, '1경기', {kind: 'friendly'}), /종류가 맞지 않습니다/);
  assert.throws(() => env.ctx.reviewSetGame(D, '1경기', {attack_end: 'sky'}), /골대 값/);
  assert.throws(() => env.ctx.reviewSetGame(D, '1경기', {switch_at_idx: 0}), /클립 번호/);
  assert.throws(() => env.ctx.reviewSetGame(D, '../x', {kind: 'scrimmage'}), /경기 이름 형식/);
  assert.throws(() => env.ctx.reviewSetGame('2026-12-6', '1경기', {kind: 'scrimmage'}), /날짜 형식/);
  assert.throws(() => env.ctx.reviewSetGame(D, '9경기', {kind: 'scrimmage'}), /경기 기록이 없습니다/);
  for (const status of ['publish', 'running']) {
    const e = makeEnv({state: stateOf(gameDoc({status})), now: DEC6_NOON});
    assert.throws(() => e.ctx.reviewSetGame(D, '1경기', {kind: 'scrimmage'}), /지금은 고칠 수 없습니다/);
    assert.throws(() => e.ctx.reviewTagClip(D, '1경기', 1, {removed: true}), /지금은 고칠 수 없습니다/);
  }
  assert.strictEqual(env.json(`games_${D}_1경기.json`).kind, null);
});

test('reviewSetGame: 여기부터 반대(switch_at_idx)를 저장하고 비울 수 있다', () => {
  const env = reviewEnv();
  assert.strictEqual(env.ctx.reviewSetGame(D, '1경기', {switch_at_idx: '2'}).switchAtIdx, 2);
  assert.strictEqual(env.json(`games_${D}_1경기.json`).switch_at_idx, 2);
  assert.strictEqual(env.ctx.reviewSetGame(D, '1경기', {switch_at_idx: null}).switchAtIdx, null);
});

test('늦은 태깅: 자동 게시된 경기의 이름이 바뀌면 제목 변경 요청을 넣고 러너를 깨운다', () => {
  const env = makeEnv({now: DEC6_NOON, state: stateOf(gameDoc({
    status: 'auto_published', tag_source: 'auto', yt_url: 'https://youtu.be/a', yt_title: `${D}-1경기`,
    published_clips: [1, 2, 3]}))});
  env.ctx.reviewSetGame(D, '1경기', {kind: 'opponent', opponent: 'OO FC'});
  const doc = env.json('requests.json');
  assert.deepStrictEqual(doc.items.map((i) => [i.type, i.date, i.label]), [['retitle', D, '1경기']]);
  assert.ok(doc.items[0].id && doc.items[0].at);
  assert.strictEqual(env.json(`games_${D}_1경기.json`).status, 'auto_published');   // 상태는 그대로
  assert.strictEqual(kernelPushes(env), 1);                                          // 인박스가 비어도 요청이 있어 푸시

  env.ctx.reviewSetGame(D, '1경기', {opponent: 'OO FC'});                             // 같은 이름 — 요청 중복 없음
  assert.strictEqual(env.json('requests.json').items.length, 1);
});

test('늦은 태깅: 이름이 그대로면(잘못 잡힘만 표시) 제목 변경 요청이 없다', () => {
  const env = makeEnv({now: DEC6_NOON, state: stateOf(gameDoc({
    status: 'auto_published', tag_source: 'auto', yt_url: 'https://youtu.be/a', yt_title: `${D}-1경기`}))});
  env.ctx.reviewTagClip(D, '1경기', 2, {removed: true});
  assert.strictEqual(env.state['requests.json'], undefined);
  assert.strictEqual(kernelPushes(env), 0);
});

// ─── 클립 태그 ───────────────────────────────────────────────────────────────
test('reviewTagClip: 잘못 잡힘·공격/수비·실점을 저장하고 빈 태그는 지운다', () => {
  const env = reviewEnv();
  let v = plain(env.ctx.reviewTagClip(D, '1경기', 1, {removed: true}));
  assert.strictEqual(v.clips[0].removed, true);
  assert.deepStrictEqual(env.json(`games_${D}_1경기.json`).clip_tags, {1: {removed: true}});
  v = plain(env.ctx.reviewTagClip(D, '1경기', 2, {side_override: 'defense'}));
  assert.strictEqual(v.clips[1].side, 'defense');
  v = plain(env.ctx.reviewTagClip(D, '1경기', 2, {conceded: true}));
  assert.deepStrictEqual([v.clips[1].conceded, v.monthlyCount], [true, 0]);
  assert.deepStrictEqual(env.json(`games_${D}_1경기.json`).clip_tags['2'], {side_override: 'defense', conceded: true});
  env.ctx.reviewTagClip(D, '1경기', 1, {removed: false});
  assert.deepStrictEqual(Object.keys(env.json(`games_${D}_1경기.json`).clip_tags), ['2']);   // 빈 태그는 키째 지운다
  assert.match(env.json(`games_${D}_1경기.json`).tagged_at, /^2026-12-06T03:00:00\+00:00$/);
});

test('reviewTagClip: 실점은 수비 클립에서만 — 공격으로 바꾸면 실점 표시도 사라진다', () => {
  const env = reviewEnv();
  assert.throws(() => env.ctx.reviewTagClip(D, '1경기', 1, {conceded: true}), /수비 클립에서만/);   // 판단 불가
  assert.deepStrictEqual(env.json(`games_${D}_1경기.json`).clip_tags, {});
  env.ctx.reviewTagClip(D, '1경기', 1, {side_override: 'attack'});
  assert.throws(() => env.ctx.reviewTagClip(D, '1경기', 1, {conceded: true}), /수비 클립에서만/);   // 공격
  env.ctx.reviewTagClip(D, '1경기', 1, {side_override: 'defense', conceded: true});
  const v = plain(env.ctx.reviewTagClip(D, '1경기', 1, {side_override: 'attack'}));
  assert.deepStrictEqual(env.json(`games_${D}_1경기.json`).clip_tags['1'], {side_override: 'attack'});
  assert.strictEqual(v.clips[0].conceded, false);
  assert.throws(() => env.ctx.reviewTagClip(D, '1경기', 9, {removed: true}), /클립 9번이 없습니다/);
});

// ─── 게시 ────────────────────────────────────────────────────────────────────
test('reviewPublish: 종류를 안 골랐으면 거부하고, 다른 팀은 상대 이름이 있어야 한다', () => {
  const env = reviewEnv();
  let out = plain(env.ctx.reviewPublish(D, ['1경기']));
  assert.deepStrictEqual([out.started, out.skipped.map((s) => s.reason)], [[], ['종류(청백전 / 다른 팀)를 먼저 고르세요']]);
  env.ctx.reviewSetGame(D, '1경기', {kind: 'opponent'});
  out = plain(env.ctx.reviewPublish(D, ['1경기']));
  assert.deepStrictEqual(out.skipped.map((s) => s.reason), ['상대 팀 이름을 적어 주세요']);
  assert.strictEqual(env.json(`games_${D}_1경기.json`).status, 'pending');
  assert.strictEqual(env.state['requests.json'], undefined);
  assert.strictEqual(kernelPushes(env), 0);
});

test('reviewPublish: publish로 바꾸고 대기열에 넣은 뒤 인박스가 비어 있어도 커널을 띄운다', () => {
  const env = reviewEnv();
  env.ctx.reviewSetGame(D, '1경기', {kind: 'scrimmage'});
  env.ctx.reviewTagClip(D, '1경기', 3, {removed: true});
  const out = plain(env.ctx.reviewPublish(D, ['1경기']));
  assert.deepStrictEqual(out.started, ['1경기']);
  assert.match(out.tick, /게시 요청 1건 발견 — Kaggle 실행 요청함/);
  const game = env.json(`games_${D}_1경기.json`);
  assert.deepStrictEqual([game.status, game.tag_source, game.attempts, game.error], ['publish', 'manual', 0, null]);
  assert.deepStrictEqual(game.clip_tags, {3: {removed: true}});
  assert.deepStrictEqual(env.json('requests.json').items.map((i) => [i.type, i.date]), [['publish', D]]);
  assert.strictEqual(kernelPushes(env), 1);
});

test('reviewPublish: labels가 비면 그날 게시할 수 있는 경기만 모두 게시하고 이미 게시된 경기는 건드리지 않는다', () => {
  const env = makeEnv({now: DEC6_NOON, state: stateOf(
    gameDoc({label: '1경기', kind: 'scrimmage'}),
    gameDoc({label: '2경기', kind: 'opponent', opponent: 'OO FC'}),
    gameDoc({label: '3경기', status: 'published', kind: 'scrimmage', yt_url: 'https://youtu.be/p'}),
    gameDoc({label: '4경기', status: 'failed', kind: 'scrimmage', attempts: 2, error: '실패'}))});
  const out = plain(env.ctx.reviewPublish(D, []));
  assert.deepStrictEqual(out.started, ['1경기', '2경기', '4경기']);
  assert.strictEqual(env.json(`games_${D}_3경기.json`).status, 'published');
  const failed = env.json(`games_${D}_4경기.json`);                   // 실패한 경기는 "다시 게시"
  assert.deepStrictEqual([failed.status, failed.attempts, failed.error], ['publish', 0, null]);
  assert.strictEqual(env.json('requests.json').items.length, 1);      // 같은 날짜는 요청 하나
  const again = plain(env.ctx.reviewPublish(D, ['3경기']));
  assert.match(again.skipped[0].reason, /지금은 게시할 수 없는 상태입니다\(published\)/);
});

// ─── tick: 대기열도 일거리 ───────────────────────────────────────────────────
test('tick: 인박스가 비어도 안 끝난 게시 요청이 있으면 커널을 띄운다(M10)', () => {
  const state = Object.assign(stateOf(gameDoc({status: 'publish', kind: 'scrimmage'})),
    {'requests.json': JSON.stringify({items: [{id: 'r1', type: 'publish', date: D}]})});
  const env = makeEnv({now: DEC6_NOON, state, files: {'01_inbox': [], '01_inbox_2d': []}});
  assert.match(env.ctx.tick(), /^게시 요청 1건 발견 — Kaggle 실행 요청함$/);
  assert.strictEqual(kernelPushes(env), 1);
  assert.strictEqual(env.of('https://ntfy.sh/').length, 0);
});

test('tick: 영상과 요청이 함께 있으면 둘 다 알린다', () => {
  const state = Object.assign(stateOf(gameDoc({status: 'running', kind: 'scrimmage'})),
    {'requests.json': JSON.stringify({items: [{id: 'r1', type: 'publish', date: D}]})});
  const env = makeEnv({now: DEC6_NOON, state, files: {'01_inbox': [video('a.mp4')]}});
  assert.match(env.ctx.tick(), /^영상 1개·게시 요청 1건 발견/);
});

test('tick: 대상 경기가 이미 끝났으면 요청이 남아 있어도 일거리가 아니다', () => {
  const state = Object.assign(stateOf(gameDoc({status: 'published', kind: 'scrimmage'})),
    {'requests.json': JSON.stringify({items: [{id: 'r1', type: 'publish', date: D}, {id: 'r2', type: 'monthly', month: '2026-12'}]})});
  const env = makeEnv({now: DEC6_NOON, state, files: {'01_inbox': []}});
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
  assert.strictEqual(env.calls.length, 0);                              // Kaggle API를 부르지 않는다
});

test('tick: 제목 변경 요청은 태깅이 반영(retitled_at)되기 전까지만 일거리다', () => {
  const tagged = '2026-12-06T05:00:00+00:00';
  const req = {'requests.json': JSON.stringify({items: [{id: 'r1', type: 'retitle', date: D, label: '1경기'}]})};
  const base = {status: 'auto_published', kind: 'opponent', opponent: 'OO FC', tagged_at: tagged};
  const pending = makeEnv({now: DEC6_NOON, state: Object.assign(stateOf(gameDoc(base)), req)});
  assert.match(pending.ctx.tick(), /게시 요청 1건 발견/);
  const done = makeEnv({now: DEC6_NOON, state: Object.assign(stateOf(gameDoc(
    Object.assign({retitled_at: '2026-12-06T06:00:00+00:00'}, base))), req)});
  assert.strictEqual(done.ctx.tick(), '새 영상 없음');
  const gaveUp = makeEnv({now: DEC6_NOON, state: Object.assign(stateOf(gameDoc(
    Object.assign({retitle_attempts: 3}, base))), req)});
  assert.strictEqual(gaveUp.ctx.tick(), '새 영상 없음');                // 3번 실패하면 계속 깨우지 않는다
});

test('tick: _state 폴더가 없으면(검토 기능을 안 쓰는 환경) 예전과 똑같이 동작한다', () => {
  const env = makeEnv({files: {'01_inbox': []}});
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
  assert.strictEqual(env.calls.length, 0);
});

// ─── 매일 09:00 ──────────────────────────────────────────────────────────────
test('dailyJobs: 자동 게시 시각이 지난 pending만 publish로 바꾸고 대기열·커널·알림(클릭=검토 페이지)을 보낸다', () => {
  const env = makeEnv({now: DEC7_10AM, props: {REVIEW_URL}, state: stateOf(
    gameDoc({label: '1경기'}),                                                   // 아무 태그 없음 → auto
    gameDoc({label: '2경기', tagged_at: '2026-12-06T08:00:00+00:00', kind: 'opponent', opponent: 'OO FC',
      clip_tags: {1: {removed: true}}}),                                         // 일부 태깅 → 그 태그 그대로
    gameDoc({label: '3경기', auto_at: '2026-12-08T09:00+09:00'}),                  // 아직 시각이 안 됨
    gameDoc({label: '4경기', status: 'published', kind: 'scrimmage'}))});
  const msg = env.ctx.dailyJobs();
  assert.match(msg, /^자동 게시 2경기 \(2026-12-06-1경기, 2026-12-06-2경기-OO FC\) · 정리한 요청 0건/);
  const g = (label) => env.json(`games_${D}_${label}.json`);
  assert.deepStrictEqual([g('1경기').status, g('1경기').tag_source], ['publish', 'auto']);
  assert.deepStrictEqual([g('2경기').status, g('2경기').tag_source], ['publish', 'manual']);
  assert.deepStrictEqual(g('2경기').clip_tags, {1: {removed: true}});
  assert.strictEqual(g('3경기').status, 'pending');
  assert.strictEqual(g('4경기').status, 'published');
  assert.deepStrictEqual(env.json('requests.json').items.map((i) => [i.type, i.date]), [['publish', D]]);
  assert.strictEqual(kernelPushes(env), 1);
  const [note] = env.of('https://ntfy.sh/');
  assert.strictEqual(note.body.title, '2경기를 검토 없이 자동 게시합니다');
  assert.strictEqual(note.body.click, `${REVIEW_URL}?page=review&date=${D}`);
  assert.match(note.body.message, /2026-12-06-1경기\n2026-12-06-2경기-OO FC/);
});

test('dailyJobs: 자동 게시할 경기가 없으면 아무것도 바꾸지 않고, 끝난 요청만 정리한다', () => {
  const state = Object.assign(stateOf(gameDoc({status: 'published', kind: 'scrimmage'}),
      gameDoc({label: '2경기', status: 'publish', kind: 'scrimmage'})),
    {'requests.json': JSON.stringify({items: [
      {id: 'r1', type: 'publish', date: '2026-11-29'},                             // 그날 경기가 없다 → 끝난 것
      {id: 'r2', type: 'publish', date: D},                                        // 2경기가 아직 publish → 남긴다
      {id: 'r3', type: 'retitle', date: D, label: '1경기'}]})});                   // 태깅이 없다 → 끝난 것
  const env = makeEnv({now: DEC7_10AM, state});
  assert.match(env.ctx.dailyJobs(), /^자동 게시 0경기 · 정리한 요청 2건/);
  assert.deepStrictEqual(env.json('requests.json').items.map((i) => i.id), ['r2']);
  assert.strictEqual(kernelPushes(env), 0);
  assert.strictEqual(env.of('https://ntfy.sh/').length, 0);
});

test('dailyJobs: REVIEW_URL이 없으면 알림에 click이 없다', () => {
  const env = makeEnv({now: DEC7_10AM, state: stateOf(gameDoc())});
  env.ctx.dailyJobs();
  assert.strictEqual(env.of('https://ntfy.sh/')[0].body.click, undefined);
});

test('dailyJobs: _state 폴더가 없거나 오류가 나도 예외 없이 문자열을 돌려준다', () => {
  assert.match(makeEnv({now: DEC7_10AM}).ctx.dailyJobs(), /^자동 게시 0경기/);
  assert.match(makeEnv({now: DEC7_10AM, props: {ROOT_FOLDER_ID: ''}}).ctx.dailyJobs(), /^오류: 스크립트 속성 누락: ROOT_FOLDER_ID/);
});

test('installDailyTrigger: 매일 09:00(Asia/Seoul) 트리거를 걸고 5분 tick 트리거는 건드리지 않는다', () => {
  const env = makeEnv();
  env.ctx.installTrigger();
  env.ctx.installDailyTrigger();
  env.ctx.installDailyTrigger();
  assert.deepStrictEqual(env.triggers, [{fn: 'tick', n: 5},
    {fn: 'dailyJobs', days: 1, hour: 9, minute: 0, tz: 'Asia/Seoul'}]);
  env.ctx.installTrigger();                                                  // tick을 다시 걸어도 daily는 그대로
  assert.strictEqual(env.triggers.filter((t) => t.fn === 'dailyJobs').length, 1);
});

test('notify_: click이 있으면 payload에 싣고 없으면 싣지 않는다(탭하면 복사)', () => {
  const env = makeEnv();
  const cfg = {topic: 'hl-topic'};
  env.ctx.notify_(cfg, 't1', 'm1', ['soccer']);
  env.ctx.notify_(cfg, 't2', 'm2', [], 'https://example.com/x');
  const [a, b] = env.of('https://ntfy.sh/').map((c) => c.body);
  assert.deepStrictEqual(plain(a), {topic: 'hl-topic', title: 't1', message: 'm1', tags: ['soccer']});
  assert.strictEqual(b.click, 'https://example.com/x');
});

// ═══ 월간 영상 · 클립 정리 (MONTHLY_PLAN.md Phase 3) ═══════════════════════════════
const CLIP_RULES = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'tests', 'data', 'clip_rules.json'), 'utf8'));
const JAN1_9AM = Date.UTC(2027, 0, 1, 0, 0, 0);               // 2027-01-01(금) 09:00 KST — 지난달(12월) 월간을 만드는 날
const DAY = 86400000;
const M = '2026-12';

/** 12월 경기들 — kind가 null이면 태깅 안 된 경기 */
const decGames = (...specs) => specs.map(([date, label, kind, over]) => gameDoc(Object.assign({
  date, label, kind, status: 'published', tag_source: kind ? 'manual' : 'auto', auto_at: null,
  opponent: kind === 'opponent' ? 'OO FC' : '', yt_url: 'https://youtu.be/x', yt_title: `${date}-${label}`}, over || {})));
const monthlyDoc = (over = {}) => Object.assign({month: M, status: 'waiting_tags', picked: [], yt_url: null,
  drive_file_id: null, drive_link: null, done_at: null, attempts: 0, error: null,
  created_at: '2027-01-01T00:00:00+00:00', reminded_at: null}, over);
const monthEnv = (games, extra = {}, opts = {}) => {
  const state = stateOf(...games);
  Object.keys(extra).forEach((k) => { state[k] = JSON.stringify(extra[k]); });
  return makeEnv(Object.assign({now: JAN1_9AM, state, props: {REVIEW_URL}}, opts));
};
const requestsOf = (env) => (env.state['requests.json'] ? env.json('requests.json').items.map((i) => i.type + ':' + (i.month || i.date)) : []);

// ─── 클립 만료 규칙 — pytest(tests/test_retention.py)와 같은 파일을 읽는다 ──────────
CLIP_RULES.expiryCases.forEach((c) => {
  test('규칙 표(공유) clipExpiresAt_ — ' + c.name, () => {
    const env = makeEnv();
    const done = c.monthlyDoneAt ? Date.parse(c.monthlyDoneAt) : null;
    assert.strictEqual(env.ctx.clipExpiresAt_(c.gameDate, done), Date.parse(c.expiresAt));
  });
});

test('클립 보관 기간 상수는 러너(retention.py)와 똑같다 — 러너는 비밀값으로 못 바꾸므로 값이 곧 기준이다', () => {
  const env = makeEnv();
  const py = fs.readFileSync(path.join(__dirname, '..', 'retention.py'), 'utf8');
  const pyConst = (name) => Number(new RegExp('^' + name + ' = (\\d+)', 'm').exec(py)[1]);
  assert.strictEqual(env.ctx.CLIP_KEEP_AFTER_DONE_DAYS, pyConst('CLIP_KEEP_AFTER_DONE_DAYS'));
  assert.strictEqual(env.ctx.CLIP_MAX_DAYS, pyConst('CLIP_MAX_DAYS'));
  assert.deepStrictEqual([env.ctx.CLIP_KEEP_AFTER_DONE_DAYS, env.ctx.CLIP_MAX_DAYS], [7, 75]);
});

test('previousMonth_: 1월에는 지난해 12월', () => {
  const env = makeEnv();
  assert.strictEqual(env.ctx.previousMonth_('2027-01-01'), '2026-12');
  assert.strictEqual(env.ctx.previousMonth_('2026-12-01'), '2026-11');
  assert.strictEqual(env.ctx.previousMonth_('2026-03-01'), '2026-02');
});

// ─── 1일: 지난달 월간 만들기 시작 ────────────────────────────────────────────
test('1일 dailyJobs: 모든 경기가 태깅됐으면 월간을 바로 build로 만들고 요청·러너를 보낸다', () => {
  const env = monthEnv(decGames(['2026-12-06', '1경기', 'opponent'], ['2026-12-06', '2경기', 'scrimmage']));
  const msg = env.ctx.dailyJobs();
  assert.match(msg, / · 월간: 2026-12 만들기 시작$/);
  const doc = env.json(`monthly_${M}.json`);
  assert.deepStrictEqual([doc.status, doc.attempts, doc.error, doc.yt_url], ['build', 0, null, null]);
  assert.match(doc.created_at, /^2027-01-01T00:00:00\+00:00$/);
  assert.deepStrictEqual(requestsOf(env), [`monthly:${M}`]);
  assert.strictEqual(kernelPushes(env), 1);
  assert.strictEqual(env.of('https://ntfy.sh/').length, 0);            // 완료 알림은 러너가 보낸다
});

test('1일 dailyJobs: 태깅 안 된 경기가 있으면 waiting_tags로 기다리며 알린다(클릭 = 월간 화면)', () => {
  const env = monthEnv(decGames(['2026-12-06', '1경기', 'opponent'], ['2026-12-13', '1경기', null], ['2026-12-20', '1경기', null]));
  env.ctx.dailyJobs();
  assert.strictEqual(env.json(`monthly_${M}.json`).status, 'waiting_tags');
  assert.deepStrictEqual(requestsOf(env), []);
  assert.strictEqual(kernelPushes(env), 0);
  const [note] = env.of('https://ntfy.sh/');
  assert.strictEqual(note.body.title, '태깅 안 된 경기 2개');
  assert.strictEqual(note.body.click, `${REVIEW_URL}?page=review&month=${M}`);
  assert.match(note.body.message, /2026년 12월 월간 하이라이트를 만들려면/);
  assert.match(note.body.message, /2026-12-13-1경기\n2026-12-20-1경기/);
  assert.doesNotMatch(note.body.message, /2026-12-06/);               // 태깅된 경기는 목록에 없다
});

test('1일이 아니면 월간을 만들지 않는다 / 경기가 없는 달은 만들지 않는다 / 두 번 불러도 한 번만', () => {
  const games = decGames(['2026-12-06', '1경기', 'opponent']);
  const notFirst = monthEnv(games, {}, {now: JAN1_9AM + DAY});          // 1월 2일
  notFirst.ctx.dailyJobs();
  assert.strictEqual(notFirst.state[`monthly_${M}.json`], undefined);
  const empty = makeEnv({now: JAN1_9AM, state: {}});
  empty.ctx.dailyJobs();
  assert.deepStrictEqual(Object.keys(empty.state), []);                 // 12월 경기가 없다
  const env = monthEnv(decGames(['2026-12-06', '1경기', null]));
  env.ctx.dailyJobs();
  env.ctx.dailyJobs();
  assert.strictEqual(env.of('https://ntfy.sh/').length, 1);             // 알림도 한 번
});

test('1일 dailyJobs: 같은 날 자동 게시된 경기(종류 없음)도 태깅 안 된 경기로 센다', () => {
  const pending = gameDoc({date: '2026-12-31', label: '1경기', status: 'pending', auto_at: '2027-01-01T09:00:00+09:00'});
  const env = monthEnv([pending].concat(decGames(['2026-12-06', '1경기', 'scrimmage'])));
  env.ctx.dailyJobs();
  assert.strictEqual(env.json('games_2026-12-31_1경기.json').status, 'publish');   // 먼저 자동 게시
  assert.strictEqual(env.json(`monthly_${M}.json`).status, 'waiting_tags');         // 그 경기는 아직 태깅 전
});

// ─── 태깅을 기다리는 월간 ────────────────────────────────────────────────────
test('waiting_tags: 3일 뒤 한 번 재알림하고, 그 전에는 아무것도 하지 않는다', () => {
  const games = decGames(['2026-12-06', '1경기', 'opponent'], ['2026-12-13', '1경기', null]);
  const early = monthEnv(games, {[`monthly_${M}.json`]: monthlyDoc()}, {now: JAN1_9AM + 2 * DAY});
  early.ctx.dailyJobs();
  assert.strictEqual(early.of('https://ntfy.sh/').length, 0);
  const env = monthEnv(games, {[`monthly_${M}.json`]: monthlyDoc()}, {now: JAN1_9AM + 3 * DAY});
  assert.match(env.ctx.dailyJobs(), /월간: 2026-12 재알림/);
  const [note] = env.of('https://ntfy.sh/');
  assert.match(note.body.title, /^태깅 안 된 경기 1개 — 2026년 12월 월간 대기 중$/);
  assert.match(note.body.message, /^4일 안에 태깅하지 않으면/);
  assert.ok(env.json(`monthly_${M}.json`).reminded_at);
  env.now += DAY;
  env.ctx.dailyJobs();
  assert.strictEqual(env.of('https://ntfy.sh/').length, 1);               // 다시 알리지 않는다
  assert.strictEqual(env.json(`monthly_${M}.json`).status, 'waiting_tags');
});

test('waiting_tags: 7일이 지나면 태깅 안 된 경기를 빼고 만들기 시작한다', () => {
  const env = monthEnv(decGames(['2026-12-06', '1경기', 'opponent'], ['2026-12-13', '1경기', null]),
    {[`monthly_${M}.json`]: monthlyDoc()}, {now: JAN1_9AM + 7 * DAY});
  assert.match(env.ctx.dailyJobs(), /월간: 2026-12 태깅 안 된 1경기를 빼고 만들기 시작/);
  assert.strictEqual(env.json(`monthly_${M}.json`).status, 'build');
  assert.deepStrictEqual(requestsOf(env), [`monthly:${M}`]);
  assert.strictEqual(kernelPushes(env), 1);
});

test('waiting_tags: 그사이 모두 태깅됐으면 다음 dailyJobs에서 만들기 시작한다', () => {
  const env = monthEnv(decGames(['2026-12-06', '1경기', 'opponent'], ['2026-12-13', '1경기', 'scrimmage']),
    {[`monthly_${M}.json`]: monthlyDoc()}, {now: JAN1_9AM + DAY});
  env.ctx.dailyJobs();
  assert.strictEqual(env.json(`monthly_${M}.json`).status, 'build');
  assert.strictEqual(kernelPushes(env), 1);
});

test('마지막 미태깅 경기를 태깅하는 순간 기다리던 월간이 만들기 시작한다', () => {
  const unposted = {status: 'pending', yt_url: null, yt_title: null};      // 게시 전이라 제목 변경 요청이 끼지 않는다
  const env = monthEnv(decGames(['2026-12-06', '1경기', 'opponent'], ['2026-12-13', '1경기', null, unposted],
      ['2026-12-20', '1경기', null, unposted]),
    {[`monthly_${M}.json`]: monthlyDoc()}, {now: JAN1_9AM + DAY});
  env.ctx.reviewSetGame('2026-12-13', '1경기', {kind: 'scrimmage'});
  assert.strictEqual(env.json(`monthly_${M}.json`).status, 'waiting_tags');          // 아직 한 경기가 남았다
  assert.strictEqual(kernelPushes(env), 0);
  env.ctx.reviewSetGame('2026-12-20', '1경기', {kind: 'opponent', opponent: 'XX FC'});
  assert.strictEqual(env.json(`monthly_${M}.json`).status, 'build');
  assert.deepStrictEqual(requestsOf(env), [`monthly:${M}`]);
  assert.strictEqual(kernelPushes(env), 1);
});

test('태깅해도 월간이 없거나 이미 만들어졌으면 건드리지 않는다', () => {
  const none = monthEnv(decGames(['2026-12-06', '1경기', null]));
  none.ctx.reviewSetGame('2026-12-06', '1경기', {kind: 'scrimmage'});
  assert.strictEqual(none.state[`monthly_${M}.json`], undefined);
  const done = monthEnv(decGames(['2026-12-06', '1경기', null]), {[`monthly_${M}.json`]: monthlyDoc({status: 'done'})});
  done.ctx.reviewSetGame('2026-12-06', '1경기', {kind: 'scrimmage'});
  assert.strictEqual(done.json(`monthly_${M}.json`).status, 'done');                 // 다시 만들기는 사람이 누른다
});

// ─── tick: 월간 요청도 일거리 ────────────────────────────────────────────────
test('tick: build·running 월간은 일거리이고 done·failed·waiting_tags는 아니다', () => {
  const req = {'requests.json': {items: [{id: 'r1', type: 'monthly', month: M}]}};
  const games = decGames(['2026-12-06', '1경기', 'opponent']);
  for (const status of ['build', 'running']) {
    const env = monthEnv(games, Object.assign({[`monthly_${M}.json`]: monthlyDoc({status})}, req), {files: {'01_inbox': []}});
    assert.match(env.ctx.tick(), /^게시 요청 1건 발견 — Kaggle 실행 요청함$/, status);
  }
  for (const status of ['done', 'failed', 'waiting_tags']) {
    const env = monthEnv(games, Object.assign({[`monthly_${M}.json`]: monthlyDoc({status})}, req), {files: {'01_inbox': []}});
    assert.strictEqual(env.ctx.tick(), '새 영상 없음', status);
  }
});

test('dailyJobs: 끝난 월간 요청은 대기열에서 지우고, 같은 달 요청은 하나만 둔다', () => {
  const env = monthEnv(decGames(['2026-12-06', '1경기', 'opponent']),
    {[`monthly_${M}.json`]: monthlyDoc({status: 'done', done_at: '2027-01-01T01:00:00+00:00'}),
     'requests.json': {items: [{id: 'r1', type: 'monthly', month: M}]}}, {now: JAN1_9AM + 2 * DAY});
  assert.match(env.ctx.dailyJobs(), /정리한 요청 1건/);
  assert.deepStrictEqual(requestsOf(env), []);
});

// ─── 월간 화면 ───────────────────────────────────────────────────────────────
test('reviewMonth: 그달 경기의 태깅 여부·월간 대상 장면 수·정리된 클립과 월간 상태를 보여 준다', () => {
  const games = decGames(['2026-12-06', '1경기', 'opponent', {attack_end: 'building'}], ['2026-12-13', '1경기', null],
    ['2026-12-20', '1경기', 'scrimmage']);
  const state = stateOf([games[0], ['building', 'tree', 'tree']], games[1], games[2]);
  state['clips_2026-12-20_1경기.json'] = JSON.stringify(Object.assign(clipsDoc([null, null], '1경기', '2026-12-20'), {expired: true}));
  state[`monthly_${M}.json`] = JSON.stringify(monthlyDoc({status: 'done', yt_url: 'https://youtu.be/m', drive_link: 'https://d/1',
    done_at: '2027-01-01T01:00:00+00:00', picked: [{}, {}, {}], duration_sec: 361.5}));
  const env = makeEnv({now: JAN1_9AM, state});
  const v = plain(env.ctx.reviewMonth(M));
  assert.strictEqual(v.untagged, 1);
  assert.deepStrictEqual(v.games.map((g) => [g.label, g.tagged, g.clipCount, g.monthlyCount, g.expired]), [
    ['1경기', true, 3, 3, false], ['1경기', false, 3, 0, false], ['1경기', true, 2, 2, true]]);
  assert.deepStrictEqual([v.monthly.status, v.monthly.ytUrl, v.monthly.driveLink, v.monthly.pickedCount, v.monthly.canBuild],
    ['done', 'https://youtu.be/m', 'https://d/1', 3, true]);
  assert.strictEqual(plain(makeEnv({now: JAN1_9AM, state: stateOf(...games)}).ctx.reviewMonth(M)).monthly.status, 'none');
  assert.throws(() => env.ctx.reviewMonth('2026-12-01'), /달 형식/);
});

test('reviewList: 월마다 상태와 태깅 안 된 경기 수를 보여 준다(최근 달이 위)', () => {
  const games = decGames(['2026-11-29', '1경기', 'opponent'], ['2026-12-06', '1경기', null], ['2026-12-13', '1경기', 'scrimmage']);
  const env = monthEnv(games, {[`monthly_${M}.json`]: monthlyDoc()}, {now: Date.UTC(2026, 11, 20, 3)});
  const months = plain(env.ctx.reviewList()).months;
  assert.deepStrictEqual(months.map((m) => [m.month, m.games, m.untagged, m.status]),
    [['2026-12', 2, 1, 'waiting_tags'], ['2026-11', 1, 0, 'none']]);
});

test('reviewRebuildMonth: 끝난·실패한 월간을 다시 build로 돌리고 러너를 깨운다', () => {
  for (const status of ['done', 'failed']) {
    const env = monthEnv(decGames(['2026-12-06', '1경기', 'opponent']),
      {[`monthly_${M}.json`]: monthlyDoc({status, attempts: 3, error: '지난 오류', yt_url: 'https://youtu.be/old'})});
    const out = plain(env.ctx.reviewRebuildMonth(M));
    assert.strictEqual(out.started, true);
    const doc = env.json(`monthly_${M}.json`);
    assert.deepStrictEqual([doc.status, doc.attempts, doc.error], ['build', 0, null], status);
    assert.strictEqual(doc.yt_url, 'https://youtu.be/old');               // 러너가 새 값으로 덮어쓸 때까지 그대로
    assert.deepStrictEqual(requestsOf(env), [`monthly:${M}`]);
    assert.strictEqual(kernelPushes(env), 1);
  }
});

test('reviewRebuildMonth: 만드는 중이면 거부하고, 경기가 없는 달은 거부하고, 문서가 없는 달은 만들 수 있다', () => {
  for (const status of ['build', 'running']) {
    const env = monthEnv(decGames(['2026-12-06', '1경기', 'opponent']), {[`monthly_${M}.json`]: monthlyDoc({status})});
    assert.throws(() => env.ctx.reviewRebuildMonth(M), /이미 만드는 중입니다/);
    assert.strictEqual(kernelPushes(env), 0);
  }
  assert.throws(() => monthEnv(decGames(['2026-11-29', '1경기', 'opponent'])).ctx.reviewRebuildMonth(M), /경기 기록이 없습니다/);
  const env = monthEnv(decGames(['2026-12-06', '1경기', null]));                // 1일 자동 생성을 놓친 달
  env.ctx.reviewRebuildMonth(M);
  assert.strictEqual(env.json(`monthly_${M}.json`).status, 'build');
  assert.ok(env.json(`monthly_${M}.json`).created_at);
  const waiting = monthEnv(decGames(['2026-12-06', '1경기', 'opponent'], ['2026-12-13', '1경기', null]),
    {[`monthly_${M}.json`]: monthlyDoc()});
  waiting.ctx.reviewRebuildMonth(M);                                           // 태깅 대기 중 "지금 만들기"
  assert.strictEqual(waiting.json(`monthly_${M}.json`).status, 'build');
});

test('월간 서버 함수도 소유자만 부를 수 있다', () => {
  const env = monthEnv(decGames(['2026-12-06', '1경기', 'opponent']), {}, {user: ''});
  assert.throws(() => env.ctx.reviewMonth(M), /거부됨: 소유자 본인만/);
  assert.throws(() => env.ctx.reviewRebuildMonth(M), /거부됨: 소유자 본인만/);
  assert.strictEqual(env.state[`monthly_${M}.json`], undefined);
});

test('요청 대기열: 같은 달 월간 요청은 한 번만, 다른 달은 따로 들어간다', () => {
  const env = monthEnv(decGames(['2026-12-06', '1경기', 'opponent']));
  const folder = env.ctx.requireState_();
  env.ctx.addRequest_(folder, {type: 'monthly', month: '2026-12'});
  env.ctx.addRequest_(folder, {type: 'monthly', month: '2026-12'});
  env.ctx.addRequest_(folder, {type: 'monthly', month: '2027-01'});
  assert.deepStrictEqual(requestsOf(env), ['monthly:2026-12', 'monthly:2027-01']);
});

// ─── 클립 정리 7일 전 예고 ───────────────────────────────────────────────────
test('정리 예고: 월간이 끝난 경기는 정리(done + 7일)가 코앞이어도 예고하지 않는다 — 끝난 다음 날부터 매번 울리지 않게', () => {
  const games = decGames(['2026-12-06', '1경기', 'opponent'], ['2026-12-13', '1경기', 'scrimmage']);
  const extra = {[`monthly_${M}.json`]: monthlyDoc({status: 'done', done_at: '2027-01-01T01:00:00+00:00'})};
  for (const days of [0, 2, 6]) {                                              // 완료 당일, 5일 전, 하루 전(정리는 1/8 01:00)
    const env = monthEnv(games, extra, {now: JAN1_9AM + days * DAY});
    assert.doesNotMatch(env.ctx.dailyJobs(), /정리 예고/, 'day+' + days);
    assert.strictEqual(env.of('https://ntfy.sh/').length, 0, 'day+' + days);
    assert.strictEqual(env.props.CLIP_REMINDED, undefined);
  }
  // 월간이 끝나지 않은 달(waiting_tags·build·running·failed)의 경기는 75일 상한 규칙이라 예고 대상이다
  const failed = monthEnv(decGames(['2026-11-01', '1경기', 'opponent']), {['monthly_2026-11.json']: monthlyDoc({month: '2026-11', status: 'failed'})},
    {now: Date.UTC(2027, 0, 10)});
  assert.match(failed.ctx.dailyJobs(), /월간: 정리 예고 1경기/);
});

test('정리 예고: 월간이 안 끝난 경기는 경기일 + 75일 7일 전에 경기마다 한 번만, 월간 화면 링크와 함께', () => {
  const games = decGames(['2026-11-01', '1경기', 'opponent'], ['2026-11-02', '1경기', 'scrimmage']);   // 상한 1/15·1/16
  const env = monthEnv(games, {}, {now: Date.UTC(2027, 0, 10)});
  assert.match(env.ctx.dailyJobs(), /월간: 정리 예고 2경기/);
  const [note] = env.of('https://ntfy.sh/');
  assert.strictEqual(note.body.title, '경기 클립 2개 묶음이 곧 정리됩니다');
  assert.match(note.body.message, /^01\/15 무렵부터 클립이 휴지통으로/);
  assert.match(note.body.message, /2026-11-01-1경기-OO FC\n2026-11-02-1경기-청백전/);
  assert.strictEqual(note.body.click, `${REVIEW_URL}?page=review&month=2026-11`);
  env.now += DAY;
  env.ctx.dailyJobs();
  assert.strictEqual(env.of('https://ntfy.sh/').length, 1);                    // 이미 알린 경기는 다시 안 알린다
  assert.deepStrictEqual(JSON.parse(env.props.CLIP_REMINDED), ['2026-11-01_1경기', '2026-11-02_1경기']);
});

test('정리 예고: 7일보다 멀면 알리지 않고, 이미 정리된 경기·게시 전 경기는 알리지 않는다', () => {
  const games = decGames(['2026-12-06', '1경기', 'opponent']);
  assert.strictEqual(monthEnv(games, {}, {now: JAN1_9AM + 2 * DAY}).ctx.dailyJobs().indexOf('정리 예고'), -1);   // 상한까지 한참
  const pending = [gameDoc({date: '2026-10-01', label: '1경기', status: 'pending', auto_at: '2999-01-01T00:00:00+09:00'})];
  const env = monthEnv(pending, {}, {now: Date.UTC(2026, 11, 10)});                                          // 75일 상한이 코앞이지만 게시 전
  env.ctx.dailyJobs();
  assert.strictEqual(env.of('https://ntfy.sh/').length, 0);
  const old = monthEnv(decGames(['2026-10-01', '1경기', 'opponent']), {}, {now: Date.UTC(2026, 11, 20)});   // 이미 상한을 넘김 — 러너가 정리
  old.ctx.dailyJobs();
  assert.strictEqual(old.of('https://ntfy.sh/').length, 0);
});

test('reviewList: 월간 카드는 그달 경기 전부로 세고(45일 창이 아니라) 이번 달 + 지난 2달만 보여 준다', () => {
  const games = decGames(['2026-09-28', '1경기', 'opponent'],                       // 4달 전 — 목록에서 빠진다
    ['2026-10-05', '1경기', null],                                                  // 76일 전 — 일별 목록엔 없지만 10월 카드엔 센다
    ['2026-11-01', '1경기', 'opponent'], ['2026-11-29', '1경기', null],              // 11/1은 49일 전 — 11월 카드엔 센다
    ['2026-12-06', '1경기', 'scrimmage']);
  const env = monthEnv(games, {}, {now: Date.UTC(2026, 11, 20, 3)});
  const list = plain(env.ctx.reviewList());
  assert.deepStrictEqual(list.months.map((m) => [m.month, m.games, m.untagged]),
    [['2026-12', 1, 0], ['2026-11', 2, 1], ['2026-10', 1, 1]]);                      // 9월은 없다. 11월 = 2경기(예전 방식이면 1경기)
  assert.deepStrictEqual(list.days.map((d) => d.date), ['2026-12-06', '2026-11-29']);   // 일별 목록은 그대로 45일
  assert.strictEqual(list.days.length, 2);
  // 목록에서 빠진 달도 주소로는 열린다
  assert.strictEqual(plain(env.ctx.reviewMonth('2026-09')).games.length, 1);
});

test('reviewList: 연도가 바뀌어도 이번 달 + 지난 2달을 센다', () => {
  const games = decGames(['2026-11-10', '1경기', 'opponent'], ['2026-12-06', '1경기', null], ['2027-01-09', '1경기', null],
    ['2026-10-31', '1경기', 'opponent']);
  const env = monthEnv(games, {}, {now: Date.UTC(2027, 0, 12, 3)});
  assert.deepStrictEqual(plain(env.ctx.reviewList()).months.map((m) => [m.month, m.games, m.untagged]),
    [['2027-01', 1, 1], ['2026-12', 1, 1], ['2026-11', 1, 0]]);                      // 10월은 3달 밖
});

let failed = 0;
for (const t of tests) {
  try {
    t.fn();
    console.log('ok   - ' + t.name);
  } catch (e) {
    failed++;
    console.log('FAIL - ' + t.name + '\n       ' + (e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n       ') : e));
  }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
