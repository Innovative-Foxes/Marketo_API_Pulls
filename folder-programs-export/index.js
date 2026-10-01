const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '.env') });
const axios = require('axios');
const fs = require('fs');
const ExcelJS = require('exceljs');

// REST endpoint host only, e.g. https://123-ABC-456.mktorest.com (a trailing /rest is tolerated).
const BASE_URL = (process.env.MARKETO_REST_BASE || '').trim().replace(/\/+$/, '').replace(/\/rest$/, '');
const CLIENT_ID = (process.env.MARKETO_CLIENT_ID || '').trim();
const CLIENT_SECRET = (process.env.MARKETO_CLIENT_SECRET || '').trim();

// The folder to export: --root "Folder Name" on the command line, else ROOT_FOLDER_NAME in .env.
function argValue(flag) {
  const i = process.argv.indexOf(flag);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : null;
}
const ROOT_FOLDER_NAME = (argValue('--root') || process.env.ROOT_FOLDER_NAME || '').trim();
const OUTPUT_DIR = path.join(__dirname, 'output');
const REQUEST_TIMEOUT_MS = 30_000;

let accessToken = null;
let tokenExpiresAt = 0;
let apiCallCount = 0;
const startedAt = Date.now();

// Marketo limits the asset API to 100 requests per 20 seconds. Cap ourselves below that.
const RATE_LIMIT_MIN_INTERVAL_MS = 220; // ≈ 4.5 req/sec
let lastRequestAt = 0;
async function rateLimitDelay() {
  const since = Date.now() - lastRequestAt;
  if (since < RATE_LIMIT_MIN_INTERVAL_MS) {
    await new Promise(r => setTimeout(r, RATE_LIMIT_MIN_INTERVAL_MS - since));
  }
  lastRequestAt = Date.now();
}

function elapsed() {
  return `${((Date.now() - startedAt) / 1000).toFixed(1)}s`;
}

function log(msg) {
  console.log(`[${elapsed()}] ${msg}`);
}

async function getAccessToken() {
  if (accessToken && Date.now() < tokenExpiresAt - 60_000) return accessToken;

  log('Requesting OAuth token...');
  const t0 = Date.now();
  const res = await axios.get(`${BASE_URL}/identity/oauth/token`, {
    params: {
      grant_type: 'client_credentials',
      client_id: CLIENT_ID,
      client_secret: CLIENT_SECRET,
    },
    timeout: REQUEST_TIMEOUT_MS,
  });

  accessToken = res.data.access_token;
  tokenExpiresAt = Date.now() + res.data.expires_in * 1000;
  log(`OAuth token acquired in ${Date.now() - t0}ms (expires in ${res.data.expires_in}s)`);
  return accessToken;
}

async function mktoGet(endpoint, params = {}) {
  const token = await getAccessToken();
  await rateLimitDelay();
  apiCallCount += 1;
  const callId = apiCallCount;
  const paramSummary = Object.entries(params)
    .filter(([k]) => k !== 'access_token')
    .map(([k, v]) => `${k}=${typeof v === 'string' && v.length > 40 ? v.slice(0, 40) + '…' : v}`)
    .join(' ');
  log(`  → #${callId} GET ${endpoint}  ${paramSummary}`);
  const t0 = Date.now();
  let res;
  for (let attempt = 1; attempt <= 4; attempt += 1) {
    res = await axios.get(`${BASE_URL}${endpoint}`, {
      params: { ...params, access_token: token },
      timeout: REQUEST_TIMEOUT_MS,
    });
    if (res.data.success === false) {
      const err = (res.data.errors && res.data.errors[0]) || {};
      // Rate-limit codes: 606 (rate exceeded), 615 (concurrent limit)
      if ((err.code === '606' || err.code === '615') && attempt < 4) {
        const backoff = 1000 * Math.pow(2, attempt); // 2s, 4s, 8s
        log(`  ⚠ rate-limited (${err.code}); backing off ${backoff}ms (attempt ${attempt}/4)`);
        await new Promise(r => setTimeout(r, backoff));
        continue;
      }
      throw new Error(`Marketo API error ${err.code}: ${err.message}`);
    }
    break;
  }
  const result = res.data.result || [];
  log(`  ← #${callId} ${Date.now() - t0}ms  (${result.length} item${result.length === 1 ? '' : 's'})`);
  return result;
}

async function findFolderByName(name) {
  const result = await mktoGet('/rest/asset/v1/folder/byName.json', { name });
  if (!result.length) throw new Error(`Folder "${name}" not found`);
  // Multiple folders may share the name across workspaces. Programs live under Marketing Activities.
  return result.find(f => (f.path || '').startsWith('/Marketing Activities')) || result[0];
}

function folderRef(folder) {
  const id = folder.folderId?.id ?? folder.id;
  const type = folder.folderId?.type || 'Folder';
  return { id, type };
}

async function buildFolderTree(rootFolder) {
  // folders.json with root=<id>&maxDepth=10 returns the whole descendant tree (paginated).
  const rootId = folderRef(rootFolder).id;
  const all = [];
  const pageSize = 200;
  let offset = 0;
  while (true) {
    const batch = await mktoGet('/rest/asset/v1/folders.json', {
      root: JSON.stringify(folderRef(rootFolder)),
      maxDepth: 10,
      maxReturn: pageSize,
      offset,
      workSpace: rootFolder.workspace || 'Default',
    });
    if (!batch.length) break;
    all.push(...batch);
    if (batch.length < pageSize) break;
    offset += pageSize;
  }

  // Defensive filter:
  //   1. Path must lie under the root path.
  //   2. Skip program internals — Marketo exposes the contents of a Program (Assets,
  //      Smart Campaigns, Operations, Reports) via folders.json with parent.type === "Program".
  //      Real marketing folders have parent.type === "Folder".
  const rootPath = rootFolder.path;
  const descendants = all.filter(f => {
    const underRoot = f.path === rootPath || (f.path && f.path.startsWith(rootPath + '/'));
    const isRealFolder = (f.folderId?.type || 'Folder') === 'Folder';
    const parentIsFolder = !f.parent || f.parent.type === 'Folder';
    return underRoot && isRealFolder && parentIsFolder;
  });

  // Build a map of id -> node, then link children to parents.
  const nodeById = new Map();
  const rootNode = {
    id: rootId,
    name: rootFolder.name,
    path: rootFolder.path,
    folderType: rootFolder.folderType,
    parentId: rootFolder.parent?.id ?? null,
    children: [],
  };
  nodeById.set(rootId, rootNode);

  for (const f of descendants) {
    const id = folderRef(f).id;
    if (id === rootId) continue;
    nodeById.set(id, {
      id,
      name: f.name,
      path: f.path,
      folderType: f.folderType,
      parentId: f.parent?.id ?? null,
      children: [],
    });
  }

  // Link parent → children. If a folder's parent isn't in our map, attach to root.
  for (const node of nodeById.values()) {
    if (node.id === rootId) continue;
    const parent = nodeById.get(node.parentId) || rootNode;
    parent.children.push(node);
  }

  // Sort children alphabetically at every level for stable output.
  function sortNode(n) {
    n.children.sort((a, b) => a.name.localeCompare(b.name));
    n.children.forEach(sortNode);
  }
  sortNode(rootNode);

  return rootNode;
}

async function fetchAllProgramsUnder(rootFolder) {
  const folderParam = JSON.stringify(folderRef(rootFolder));
  const all = [];
  const pageSize = 200;
  let offset = 0;
  while (true) {
    const batch = await mktoGet('/rest/asset/v1/programs.json', {
      folder: folderParam,
      maxReturn: pageSize,
      offset,
    });
    if (!batch.length) break;
    all.push(...batch);
    if (batch.length < pageSize) break;
    offset += pageSize;
  }
  return all;
}

function flattenTree(node, ancestry, out) {
  const pathStr = [...ancestry, node.name].join(' / ');
  out.push({ ...node, folderPath: pathStr });
  for (const c of node.children) flattenTree(c, [...ancestry, node.name], out);
  return out;
}

// All folder IDs in the subtree rooted at `node` (inclusive).
function subtreeFolderIds(node) {
  const ids = new Set();
  const stack = [node];
  while (stack.length) {
    const n = stack.pop();
    ids.add(n.id);
    for (const c of n.children) stack.push(c);
  }
  return ids;
}

// Strip the tab's root ancestry prefix from a path so the tab shows a clean location.
function trimPathPrefix(fullPath, tabRootPath) {
  if (!fullPath.startsWith(tabRootPath)) return fullPath;
  const rest = fullPath.slice(tabRootPath.length).replace(/^ \/ /, '');
  return rest || tabRootPath; // empty means the program lives directly in the tab root
}

// Excel sheet names: max 31 characters, none of  : \\ / ? * [ ]  and unique (case-insensitive).
function safeSheetName(name, used) {
  const base = (String(name).replace(/[:\\/?*[\]]/g, '-').trim() || 'Sheet').slice(0, 31);
  let candidate = base;
  let n = 2;
  while (used.has(candidate.toLowerCase())) {
    const suffix = ` (${n})`;
    candidate = base.slice(0, 31 - suffix.length) + suffix;
    n += 1;
  }
  used.add(candidate.toLowerCase());
  return candidate;
}

async function writeExcel(tree, programsByFolder, flat) {
  const folderById = new Map(flat.map(f => [f.id, f]));

  // One tab per direct child folder of the root (covering that child's whole subtree),
  // plus a first tab for everything else directly under the root.
  const childSubtrees = tree.children.map(child => ({ child, ids: subtreeFolderIds(child) }));
  const inAnyChild = new Set(childSubtrees.flatMap(c => [...c.ids]));

  const tabs = [
    {
      name: tree.name,
      pathPrefix: tree.name,
      include: (fid) => folderById.has(fid) && !inAnyChild.has(fid),
    },
    ...childSubtrees.map(({ child, ids }) => ({
      name: child.name,
      pathPrefix: `${tree.name} / ${child.name}`,
      include: (fid) => ids.has(fid),
    })),
  ];
  const usedSheetNames = new Set();

  const workbook = new ExcelJS.Workbook();
  workbook.creator = 'folder-programs-export';
  workbook.created = new Date();

  for (const tab of tabs) {
    const sheet = workbook.addWorksheet(safeSheetName(tab.name, usedSheetNames), { views: [{ state: 'frozen', ySplit: 3 }] });

    // Collect rows: every program whose folder.id is in this tab's subtree.
    const rows = [];
    for (const [fid, programs] of programsByFolder) {
      if (!tab.include(fid)) continue;
      const folder = folderById.get(fid);
      const location = folder ? trimPathPrefix(folder.folderPath, tab.pathPrefix) : '(unknown)';
      for (const p of programs) {
        rows.push({
          name: p.name,
          location,
          channel: p.channel || '',
          status: p.status || '',
          url: p.url || '',
        });
      }
    }
    rows.sort((a, b) =>
      a.location.localeCompare(b.location) || a.name.localeCompare(b.name)
    );

    // Row 1: Total programs
    sheet.mergeCells('A1:E1');
    const totalCell = sheet.getCell('A1');
    totalCell.value = `Total Programs: ${rows.length}`;
    totalCell.font = { bold: true, size: 14 };
    totalCell.alignment = { vertical: 'middle', horizontal: 'left' };
    sheet.getRow(1).height = 22;

    // Row 2: blank (used for spacing)

    // Row 3: column headers
    const headerRow = sheet.getRow(3);
    headerRow.values = ['Program Name', 'Folder Location', 'Channel', 'Status', 'Marketo URL'];
    headerRow.font = { bold: true };
    headerRow.alignment = { vertical: 'middle' };
    headerRow.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFE8E8E8' } };
    headerRow.border = { bottom: { style: 'thin', color: { argb: 'FF888888' } } };

    // Data rows starting at row 4
    rows.forEach((r, i) => {
      const row = sheet.getRow(4 + i);
      row.values = [r.name, r.location, r.channel, r.status, r.url];
      if (r.url) {
        row.getCell(5).value = { text: r.url, hyperlink: r.url };
        row.getCell(5).font = { color: { argb: 'FF1A6FB4' }, underline: true };
      }
    });

    // Column widths
    sheet.columns = [
      { key: 'name', width: 50 },
      { key: 'location', width: 60 },
      { key: 'channel', width: 18 },
      { key: 'status', width: 14 },
      { key: 'url', width: 50 },
    ];

    // Auto filter on the header row
    sheet.autoFilter = { from: { row: 3, column: 1 }, to: { row: 3, column: 5 } };
  }

  const xlsxPath = path.join(OUTPUT_DIR, 'programs.xlsx');
  await workbook.xlsx.writeFile(xlsxPath);
  return xlsxPath;
}

function writeOutputs(byFolder) {
  if (!fs.existsSync(OUTPUT_DIR)) fs.mkdirSync(OUTPUT_DIR, { recursive: true });

  fs.writeFileSync(path.join(OUTPUT_DIR, 'programs.json'), JSON.stringify(byFolder, null, 2));

  const csvRows = [
    ['Folder Path', 'Folder ID', 'Program ID', 'Program Name', 'Program Type', 'Channel', 'Status', 'Created At', 'Updated At', 'URL'],
  ];
  for (const f of byFolder) {
    if (!f.programs.length) {
      csvRows.push([f.folderPath, f.folderId, '', '(no programs)', '', '', '', '', '', '']);
      continue;
    }
    for (const p of f.programs) {
      csvRows.push([
        f.folderPath, f.folderId, p.id, p.name, p.type || '', p.channel || '',
        p.status || '', p.createdAt || '', p.updatedAt || '', p.url || '',
      ]);
    }
  }
  const csv = csvRows.map(r => r.map(cell => {
    const s = String(cell ?? '');
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }).join(',')).join('\n');
  fs.writeFileSync(path.join(OUTPUT_DIR, 'programs.csv'), csv);

  const txtLines = [];
  for (const f of byFolder) {
    const header = `${f.folderPath}  (${f.programs.length} program${f.programs.length === 1 ? '' : 's'})`;
    txtLines.push(`\n${header}`);
    txtLines.push('-'.repeat(header.length));
    if (!f.programs.length) txtLines.push('  (none)');
    for (const p of f.programs) txtLines.push(`  - ${p.name}${p.channel ? ` [${p.channel}]` : ''}`);
  }
  fs.writeFileSync(path.join(OUTPUT_DIR, 'programs.txt'), txtLines.join('\n'));
}

(async () => {
  if (!BASE_URL || !CLIENT_ID || !CLIENT_SECRET) {
    console.error('Missing MARKETO_REST_BASE / MARKETO_CLIENT_ID / MARKETO_CLIENT_SECRET.');
    console.error('Copy .env.example to .env in this folder and fill in the values (see README).');
    process.exit(1);
  }
  if (!ROOT_FOLDER_NAME) {
    console.error('No root folder given. Run:  node index.js --root "Your Folder Name"');
    console.error('or set ROOT_FOLDER_NAME in .env.');
    process.exit(1);
  }

  if (!fs.existsSync(OUTPUT_DIR)) fs.mkdirSync(OUTPUT_DIR, { recursive: true });

  try {
    log(`Looking up root folder: "${ROOT_FOLDER_NAME}"`);
    const root = await findFolderByName(ROOT_FOLDER_NAME);
    log(`Root folder resolved: id=${folderRef(root).id}  path=${root.path}  workspace=${root.workspace || 'Default'}`);

    log(`Building folder tree under "${ROOT_FOLDER_NAME}"...`);
    const tree = await buildFolderTree(root);
    const flat = flattenTree(tree, [], []);
    log(`Folder tree built: ${flat.length} folders`);

    log(`Fetching all programs under "${ROOT_FOLDER_NAME}" (one paginated query)...`);
    const allPrograms = await fetchAllProgramsUnder(root);
    log(`Fetched ${allPrograms.length} programs total`);

    // Group programs by their parent folder id. Program objects use {type, value, folderName}
    // for the folder ref (not {id, type}).
    const programsByFolder = new Map();
    let orphanCount = 0;
    for (const p of allPrograms) {
      const fid = p.folder?.value ?? p.folder?.id;
      if (fid == null) { orphanCount += 1; continue; }
      if (!programsByFolder.has(fid)) programsByFolder.set(fid, []);
      programsByFolder.get(fid).push({
        id: p.id,
        name: p.name,
        type: p.type,
        channel: p.channel,
        status: p.status,
        url: p.url,
        createdAt: p.createdAt,
        updatedAt: p.updatedAt,
        folderName: p.folder?.folderName,
      });
    }
    if (orphanCount) log(`Note: ${orphanCount} program(s) had no folder reference and were skipped`);

    // Build per-folder output rows in tree order
    const byFolder = flat.map(f => ({
      folderId: f.id,
      folderName: f.name,
      folderPath: f.folderPath,
      folderType: f.folderType,
      programs: programsByFolder.get(f.id) || [],
    }));

    // Marketo's programs.json folder filter can return programs outside the requested folder
    // (sometimes the whole workspace). We keep only programs whose folder.value is in our tree.
    const treeIds = new Set(flat.map(f => f.id));
    const unmatchedFolders = [...programsByFolder.keys()].filter(id => !treeIds.has(id));
    const unmatchedPrograms = unmatchedFolders.reduce((n, fid) => n + (programsByFolder.get(fid)?.length || 0), 0);
    log(`Filtering: ${unmatchedPrograms} program(s) in ${unmatchedFolders.length} folder(s) lie outside "${ROOT_FOLDER_NAME}" and are excluded.`);

    log('Writing output files...');
    writeOutputs(byFolder);
    const xlsxPath = await writeExcel(tree, programsByFolder, flat);
    log(`Wrote Excel workbook with ${tree.children.length + 1} tab(s) → ${path.relative(process.cwd(), xlsxPath)}`);

    const totalPrograms = byFolder.reduce((n, f) => n + f.programs.length, 0);

    console.log('\n=== Summary ===');
    console.log(`Folders scanned:  ${byFolder.length}`);
    console.log(`Programs matched: ${totalPrograms}`);
    console.log(`API calls made:   ${apiCallCount}`);
    console.log(`Total time:       ${elapsed()}`);
    console.log(`Output: ${path.relative(process.cwd(), OUTPUT_DIR)}/{programs.json,programs.csv,programs.txt,programs.xlsx}`);
  } catch (err) {
    console.error(`[${elapsed()}] Failed:`, err.response?.data || err.message);
    if (err.code) console.error(`  (axios code: ${err.code})`);
    process.exit(1);
  }
})();
