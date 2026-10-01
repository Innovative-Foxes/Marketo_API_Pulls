const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '.env') });
const axios = require('axios');
const fs = require('fs');
const readline = require('readline');

// REST endpoint host only, e.g. https://123-ABC-456.mktorest.com (a trailing /rest is tolerated).
const BASE_URL = (process.env.MARKETO_REST_BASE || '').trim().replace(/\/+$/, '').replace(/\/rest$/, '');
const CLIENT_ID = (process.env.MARKETO_CLIENT_ID || '').trim();
const CLIENT_SECRET = (process.env.MARKETO_CLIENT_SECRET || '').trim();

// Flow step "Attribute Name" values as they appear in Marketo (the field's DISPLAY name).
const DATE_NAME = (process.env.MQL_DATE_DISPLAY_NAME || 'Last MQL Date').trim();
const REASON_NAME = (process.env.MQL_REASON_DISPLAY_NAME || 'Last MQL Reason').trim();
const SESSION_BUDGET_MS = Number(process.env.SESSION_BUDGET_MS || 8 * 60 * 1000);
const INTER_CALL_DELAY_MS = Number(process.env.INTER_CALL_DELAY_MS || 220); // ~4.5/sec, under 100/20s limit

const OUTPUT_DIR = path.join(__dirname, 'output');
const LIST_PATH = path.join(OUTPUT_DIR, 'campaigns-list.json');
const RESULTS_PATH = path.join(OUTPUT_DIR, 'results.jsonl');

let accessToken = null;
let tokenExpiresAt = 0;

async function getAccessToken() {
  if (accessToken && Date.now() < tokenExpiresAt - 60_000) return accessToken;
  const res = await axios.get(`${BASE_URL}/identity/oauth/token`, {
    params: { grant_type: 'client_credentials', client_id: CLIENT_ID, client_secret: CLIENT_SECRET },
  });
  accessToken = res.data.access_token;
  tokenExpiresAt = Date.now() + res.data.expires_in * 1000;
  return accessToken;
}

async function mktoGet(endpoint, params = {}) {
  const token = await getAccessToken();
  const res = await axios.get(`${BASE_URL}${endpoint}`, { params: { ...params, access_token: token } });
  if (res.data && res.data.success === false) {
    const err = (res.data.errors && res.data.errors[0]) || {};
    throw new Error(`Marketo error ${err.code}: ${err.message} (${endpoint})`);
  }
  return res.data;
}

function log(msg) { process.stdout.write(`${new Date().toISOString().slice(11, 19)} ${msg}\n`); }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function listAllSmartCampaigns() {
  if (fs.existsSync(LIST_PATH)) {
    const cached = JSON.parse(fs.readFileSync(LIST_PATH, 'utf8'));
    log(`Using cached campaign list (${cached.length} campaigns). Delete ${path.basename(LIST_PATH)} to refresh.`);
    return cached;
  }
  log(`Listing all smart campaigns...`);
  const all = [];
  const pageSize = 200;
  let offset = 0;
  while (true) {
    const data = await mktoGet('/rest/asset/v1/smartCampaigns.json', { maxReturn: pageSize, offset });
    const batch = data.result || [];
    if (!batch.length) break;
    for (const c of batch) {
      all.push({ id: c.id, name: c.name, status: c.status, type: c.type, isActive: c.isActive, folderId: c.folder && c.folder.id, parentProgramId: c.parentProgramId, workspace: c.workspace, updatedAt: c.updatedAt });
    }
    log(`  offset ${offset}: +${batch.length} (total ${all.length})`);
    if (batch.length < pageSize) break;
    offset += pageSize;
    await sleep(INTER_CALL_DELAY_MS);
  }
  if (!fs.existsSync(OUTPUT_DIR)) fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  fs.writeFileSync(LIST_PATH, JSON.stringify(all, null, 2));
  log(`Saved ${all.length} campaigns to ${path.basename(LIST_PATH)}`);
  return all;
}

function analyzeFlow(campaign) {
  const steps = (campaign.flow && campaign.flow.steps) || [];
  const dateSteps = [];
  const reasonSteps = [];
  const dateNameLc = DATE_NAME.toLowerCase();
  const reasonNameLc = REASON_NAME.toLowerCase();
  for (const s of steps) {
    if (s.activityTypeName !== 'Change Data Value') continue;
    for (const sc of (s.stepChoice || [])) {
      const attrs = sc.attributes || [];
      const attrName = attrs.find(a => a.name === 'Attribute Name');
      const newValue = attrs.find(a => a.name === 'New Value');
      if (!attrName || !attrName.value) continue;
      const fld = String(attrName.value).trim().toLowerCase();
      if (fld === dateNameLc) dateSteps.push({ stepId: s.id, choiceId: sc.id, newValue: newValue ? newValue.value : null });
      else if (fld === reasonNameLc) reasonSteps.push({ stepId: s.id, choiceId: sc.id, newValue: newValue ? newValue.value : null });
    }
  }
  return { dateSteps, reasonSteps };
}

async function loadProcessedSet() {
  if (!fs.existsSync(RESULTS_PATH)) return new Map();
  const map = new Map();
  const rl = readline.createInterface({ input: fs.createReadStream(RESULTS_PATH), crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.trim()) continue;
    try { const r = JSON.parse(line); if (r.id) map.set(r.id, r); } catch (_) {}
  }
  return map;
}

async function fetchAndAnalyzeAll(campaignsList) {
  const processed = await loadProcessedSet();
  const targetTotal = campaignsList.length;
  const skipped = [...processed.values()].filter(r => !campaignsList.some(c => c.id === r.id)).length;
  log(`Already processed: ${processed.size} (in-scope hits: ${processed.size - skipped}/${targetTotal})`);
  const stream = fs.createWriteStream(RESULTS_PATH, { flags: 'a' });
  const deadline = Date.now() + SESSION_BUDGET_MS;
  let done = 0;
  let errors = 0;
  let suspectsSoFar = 0;

  try {
    for (const c of campaignsList) {
      if (processed.has(c.id)) { done++; continue; }
      if (Date.now() > deadline) {
        log(`SESSION_BUDGET_EXHAUSTED. Processed ${done} this session. Re-run to continue.`);
        break;
      }
      try {
        const data = await mktoGet(`/rest/asset/v1/smartCampaign/${c.id}.json`);
        const full = (data.result || [])[0];
        if (!full) { errors++; continue; }
        const { dateSteps, reasonSteps } = analyzeFlow(full);
        const isSuspect = dateSteps.length > 0 && reasonSteps.length === 0;
        if (isSuspect) suspectsSoFar++;
        const result = {
          id: full.id,
          name: full.name,
          status: full.status,
          type: full.type,
          isActive: full.isActive,
          isSystem: full.isSystem,
          workspace: full.workspace,
          parentProgramId: full.parentProgramId,
          folderId: full.folder && full.folder.id,
          computedUrl: full.computedUrl,
          updatedAt: full.updatedAt,
          totalSteps: ((full.flow && full.flow.steps) || []).length,
          dateStepCount: dateSteps.length,
          reasonStepCount: reasonSteps.length,
          dateSteps,
          reasonSteps,
          isSuspect,
        };
        stream.write(JSON.stringify(result) + '\n');
        processed.set(c.id, result);
        done++;
      } catch (e) {
        errors++;
        // Some smart campaigns can't be fetched (deleted, permissions, etc.). Record and move on.
        stream.write(JSON.stringify({ id: c.id, name: c.name, error: e.message }) + '\n');
      }
      if (done % 50 === 0) log(`  processed ${done}/${campaignsList.length} (suspects so far: ${suspectsSoFar}, errors: ${errors})`);
      await sleep(INTER_CALL_DELAY_MS);
    }
  } finally {
    await new Promise(r => stream.end(r));
  }
  return { done, errors };
}

async function resolveProgramNames(programIds) {
  const map = new Map();
  for (const pid of programIds) {
    if (!pid) continue;
    try {
      const data = await mktoGet(`/rest/asset/v1/program/${pid}.json`);
      const p = (data.result || [])[0] || {};
      map.set(pid, { name: p.name, url: p.url, folder: p.folder && (p.folder.folderName || p.folder.value) || '', channel: p.channel, type: p.type });
    } catch (e) {
      map.set(pid, { name: '(lookup failed)', url: '', folder: '', channel: '', type: '', error: e.message });
    }
    await sleep(INTER_CALL_DELAY_MS);
  }
  return map;
}

function csvCell(v) {
  const s = String(v ?? '');
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

async function writeOutputs(results) {
  if (!fs.existsSync(OUTPUT_DIR)) fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  const valid = results.filter(r => !r.error);
  const suspects = valid.filter(r => r.isSuspect).sort((a, b) => (a.name || '').localeCompare(b.name || ''));

  log(`Resolving ${new Set(suspects.map(s => s.parentProgramId)).size} parent programs for suspects...`);
  const programMap = await resolveProgramNames([...new Set(suspects.map(s => s.parentProgramId))]);

  const enrichedSuspects = suspects.map(s => {
    const prog = programMap.get(s.parentProgramId) || {};
    return { ...s, programName: prog.name || '', programUrl: prog.url || '', programFolder: prog.folder || '', programChannel: prog.channel || '' };
  });

  fs.writeFileSync(path.join(OUTPUT_DIR, 'suspect-campaigns.json'), JSON.stringify(enrichedSuspects, null, 2));

  const csvHeaders = ['Campaign ID', 'Campaign Name', 'Campaign UI URL', 'Status', 'Type', 'Is Active', 'Workspace', 'Parent Program ID', 'Parent Program Name', 'Parent Program URL', 'Program Folder', 'Program Channel', 'Date Step Count', 'Reason Step Count', 'Total Flow Steps', 'Updated At'];
  const toRow = r => [r.id, r.name, r.computedUrl || '', r.status || '', r.type || '', r.isActive, r.workspace || '', r.parentProgramId || '', r.programName || '', r.programUrl || '', r.programFolder || '', r.programChannel || '', r.dateStepCount, r.reasonStepCount, r.totalSteps, r.updatedAt || ''].map(csvCell).join(',');
  fs.writeFileSync(path.join(OUTPUT_DIR, 'suspect-campaigns.csv'), [csvHeaders.join(','), ...enrichedSuspects.map(toRow)].join('\n'));

  // Program-level dedup
  const byProgram = new Map();
  for (const s of enrichedSuspects) {
    const pid = s.parentProgramId || 0;
    if (!byProgram.has(pid)) byProgram.set(pid, { programId: pid, programName: s.programName, programUrl: s.programUrl, programFolder: s.programFolder, programChannel: s.programChannel, campaigns: [] });
    byProgram.get(pid).campaigns.push({ id: s.id, name: s.name, computedUrl: s.computedUrl, status: s.status, dateStepCount: s.dateStepCount });
  }
  const programsArr = [...byProgram.values()].sort((a, b) => (a.programName || '').localeCompare(b.programName || ''));
  fs.writeFileSync(path.join(OUTPUT_DIR, 'suspect-programs.json'), JSON.stringify(programsArr, null, 2));
  const progHeaders = ['Program ID', 'Program Name', 'Program URL', 'Program Folder', 'Program Channel', '# Suspect Campaigns', 'Suspect Campaign IDs', 'Suspect Campaign Names'];
  const progRows = programsArr.map(p => [p.programId, p.programName, p.programUrl, p.programFolder, p.programChannel, p.campaigns.length, p.campaigns.map(c => c.id).join(';'), p.campaigns.map(c => c.name).join(' | ')].map(csvCell).join(','));
  fs.writeFileSync(path.join(OUTPUT_DIR, 'suspect-programs.csv'), [progHeaders.join(','), ...progRows].join('\n'));

  // All-campaigns summary for sanity-checking
  const summary = valid.map(r => [r.id, r.name, r.computedUrl || '', r.status || '', r.dateStepCount, r.reasonStepCount, r.isSuspect ? 'SUSPECT' : '', r.totalSteps].map(csvCell).join(','));
  fs.writeFileSync(path.join(OUTPUT_DIR, 'all-campaigns-summary.csv'), ['Campaign ID,Campaign Name,UI URL,Status,Date Steps,Reason Steps,Flag,Total Steps', ...summary].join('\n'));

  return { suspectsCount: enrichedSuspects.length, programsCount: programsArr.length, validCount: valid.length, errorsCount: results.length - valid.length };
}

(async () => {
  if (!BASE_URL || !CLIENT_ID || !CLIENT_SECRET) {
    console.error('Missing MARKETO_REST_BASE / MARKETO_CLIENT_ID / MARKETO_CLIENT_SECRET.');
    console.error('Copy .env.example to .env in this folder and fill in the values (see README).');
    process.exit(1);
  }
  log(`Looking for Change Data Value steps targeting:`);
  log(`  Date:   "${DATE_NAME}"`);
  log(`  Reason: "${REASON_NAME}"`);
  log(`Session budget: ${Math.round(SESSION_BUDGET_MS / 60000)} min  Inter-call delay: ${INTER_CALL_DELAY_MS}ms`);

  try {
    const fullList = await listAllSmartCampaigns();
    const scope = (process.env.SCOPE || 'active').toLowerCase();
    let list;
    if (scope === 'active') {
      list = fullList.filter(c => c.isActive === true);
      log(`Scope: active only — ${list.length} / ${fullList.length} campaigns`);
    } else if (scope === 'active-plus') {
      list = fullList.filter(c => c.isActive === true || c.status === 'Recurring Run' || c.status === 'Single Run');
      log(`Scope: active + Recurring/Single Run — ${list.length} / ${fullList.length} campaigns`);
    } else {
      list = fullList;
      log(`Scope: all — ${list.length} campaigns`);
    }
    const { done, errors } = await fetchAndAnalyzeAll(list);
    log(`Session done. Processed ${done} new (errors: ${errors}).`);

    const processed = await loadProcessedSet();
    const inScopeIds = new Set(list.map(c => c.id));
    const inScopeProcessed = [...processed.values()].filter(r => inScopeIds.has(r.id));
    const remaining = list.length - inScopeProcessed.length;
    if (remaining > 0) {
      log(`Remaining: ${remaining}/${list.length}. Re-run to continue. No final output written yet.`);
      return;
    }

    const results = inScopeProcessed;
    const { suspectsCount, programsCount, validCount, errorsCount } = await writeOutputs(results);
    log('=== DONE ===');
    log(`Total campaigns analyzed: ${validCount}  (errors: ${errorsCount})`);
    log(`Suspect campaigns (have Last MQL Date step, no Last MQL Reason step): ${suspectsCount}`);
    log(`Affected programs: ${programsCount}`);
    log(`Outputs: output/suspect-campaigns.{csv,json}, output/suspect-programs.{csv,json}, output/all-campaigns-summary.csv`);
  } catch (err) {
    console.error('\nFailed:', err.response?.data || err.message);
    process.exit(1);
  }
})();
