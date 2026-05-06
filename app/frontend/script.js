const API = '';  // same origin
let currentJobId = null;

// ── Navigation ────────────────────────────────────────────────────────────────
document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', e => {
    e.preventDefault();
    document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
    item.classList.add('active');
    const panel = item.dataset.panel;
    document.querySelectorAll('.panel').forEach(p => p.style.display = 'none');
    document.getElementById(`panel-${panel}`).style.display = '';
    if (panel === 'history') loadHistory();
  });
});

// ── Smart Scrape ──────────────────────────────────────────────────────────────
document.getElementById('btn-scrape').addEventListener('click', async () => {
  const prompt = document.getElementById('prompt').value.trim();
  const url    = document.getElementById('url').value.trim();
  if (!prompt) { showError('error-box', 'Inserisci un prompt di ricerca.'); return; }

  setLoading(true, 'L\'AI sta pianificando la ricerca...');
  hideEl('results'); hideEl('error-box');

  try {
    const body = { prompt };
    if (url) body.url = url;

    setSpinnerMsg('Scraping in corso...');
    const res = await fetch(`${API}/smart-scrape`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Errore server');
    }

    const data = await res.json();
    currentJobId = data.job_id;
    renderResults(data);
  } catch (err) {
    showError('error-box', `Errore: ${err.message}`);
  } finally {
    setLoading(false);
  }
});

// ── PDF Download ──────────────────────────────────────────────────────────────
document.getElementById('btn-pdf').addEventListener('click', async () => {
  if (!currentJobId) return;
  const a = document.createElement('a');
  a.href = `${API}/report/${currentJobId}`;
  a.download = `report_${currentJobId}.pdf`;
  a.click();
});

// ── History ───────────────────────────────────────────────────────────────────
document.getElementById('btn-refresh-history').addEventListener('click', loadHistory);

async function loadHistory() {
  const list = document.getElementById('history-list');
  list.innerHTML = '<p style="color:var(--text3);padding:20px 0">Caricamento...</p>';
  try {
    const res = await fetch(`${API}/jobs`);
    const jobs = await res.json();
    if (!jobs.length) {
      list.innerHTML = '<p style="color:var(--text3);padding:20px 0">Nessuna ricerca ancora.</p>';
      return;
    }
    list.innerHTML = '';
    jobs.slice().reverse().forEach(job => {
      const card = document.createElement('div');
      card.className = 'history-card';
      card.innerHTML = `
        <div class="hc-info">
          <h3>${escHtml(job.prompt || '—')}</h3>
          <p>${job.created_at ? new Date(job.created_at).toLocaleString('it-IT') : ''} · ${(job.siti_visitati || []).length} siti</p>
        </div>
        <a href="${API}/report/${job.job_id}" download="report_${job.job_id}.pdf" class="btn-secondary">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          PDF
        </a>`;
      list.appendChild(card);
    });
  } catch {
    list.innerHTML = '<p style="color:var(--error)">Errore nel caricamento dello storico.</p>';
  }
}

// ── Direct Scrape ─────────────────────────────────────────────────────────────
document.getElementById('btn-add-sel').addEventListener('click', () => {
  const row = document.createElement('div');
  row.className = 'selector-row';
  row.innerHTML = `
    <input type="text" placeholder="Nome campo" class="sel-name" />
    <input type="text" placeholder="Selettore CSS (es: h1, .price)" class="sel-css" />
    <button class="btn-icon btn-remove-sel" title="Rimuovi">✕</button>`;
  row.querySelector('.btn-remove-sel').addEventListener('click', () => row.remove());
  document.getElementById('selectors-list').appendChild(row);
});

// Remove handler for first row
document.querySelectorAll('.btn-remove-sel').forEach(btn => {
  btn.addEventListener('click', () => btn.closest('.selector-row').remove());
});

document.getElementById('btn-direct-scrape').addEventListener('click', async () => {
  const url = document.getElementById('direct-url').value.trim();
  if (!url) { showError('direct-error', 'Inserisci un URL.'); return; }

  const rows = [...document.querySelectorAll('.selector-row')];
  const selectors = rows.map(r => ({
    name: r.querySelector('.sel-name').value.trim() || 'Campo',
    css:  r.querySelector('.sel-css').value.trim(),
  })).filter(s => s.css);

  if (!selectors.length) { showError('direct-error', 'Aggiungi almeno un selettore CSS.'); return; }

  showEl('direct-spinner'); hideEl('direct-results'); hideEl('direct-error');

  try {
    const res = await fetch(`${API}/scrape-direct`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, selectors }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();
    renderDirectResults(data.dati);
  } catch (err) {
    showError('direct-error', `Errore: ${err.message}`);
  } finally {
    hideEl('direct-spinner');
  }
});

// ── Render helpers ────────────────────────────────────────────────────────────
function renderResults(data) {
  document.getElementById('sites-visited').textContent =
    `Siti analizzati: ${(data.siti_visitati || []).join(', ') || '—'}`;

  const summary = data.riassunto || '';
  if (summary) {
    document.getElementById('summary-text').textContent = summary;
    showEl('summary-box');
  } else {
    hideEl('summary-box');
  }

  const tbody = document.getElementById('data-body');
  tbody.innerHTML = '';
  const dati = data.dati || [];
  if (dati.length) {
    dati.forEach(row => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${escHtml(row.campo)}</td><td>${escHtml(row.valore)}</td><td>${escHtml(row.fonte)}</td>`;
      tbody.appendChild(tr);
    });
    hideEl('no-data');
  } else {
    showEl('no-data');
  }

  showEl('results');
}

function renderDirectResults(dati) {
  const tbody = document.getElementById('direct-body');
  tbody.innerHTML = '';
  (dati || []).forEach(row => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${escHtml(row.campo)}</td><td>${escHtml(row.valore)}</td><td>${escHtml(row.fonte)}</td>`;
    tbody.appendChild(tr);
  });
  showEl('direct-results');
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function setLoading(on, msg = '') {
  const btn = document.getElementById('btn-scrape');
  const spinner = document.getElementById('spinner');
  btn.disabled = on;
  spinner.style.display = on ? 'flex' : 'none';
  if (msg) setSpinnerMsg(msg);
}
function setSpinnerMsg(msg) {
  document.getElementById('spinner-msg').textContent = msg;
}
function showEl(id) { document.getElementById(id).style.display = ''; }
function hideEl(id) { document.getElementById(id).style.display = 'none'; }
function showError(id, msg) {
  const el = document.getElementById(id);
  el.textContent = msg;
  el.style.display = '';
}
function escHtml(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
