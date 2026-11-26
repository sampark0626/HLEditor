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
const clipsDoc = (ends = [null, null, null], label = '1경기') => ({
  date: D, label, clips: ends.map((end, i) => ({
    idx: i + 1, name: `${D}-${label}_c00${i + 1}.mp4`, file_id: `fid${i + 1}`, src_start: 100 * (i + 1),
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
    state[`clips_${doc.date}_${doc.label}.json`] = JSON.stringify(clipsDoc(ends, doc.label));
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
