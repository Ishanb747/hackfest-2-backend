/**
 * api.js — Centralised API service for RuleForge Frontend
 * All calls go to the Flask backend at http://localhost:5000
 */

const BASE_URL = process.env.REACT_APP_API_URL || 'https://hackfest-2-backend.onrender.com';

async function apiFetch(path, options = {}) {
  const url = `${BASE_URL}${path}`;
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

// ── Stats / Overview ─────────────────────────────────────────────────────────

export const fetchStats = () => apiFetch('/api/stats');

// ── Rules ────────────────────────────────────────────────────────────────────

export const fetchRules = (params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return apiFetch(`/api/rules${qs ? `?${qs}` : ''}`);
};

// ── Violations ───────────────────────────────────────────────────────────────

export const fetchViolations = (params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return apiFetch(`/api/violations${qs ? `?${qs}` : ''}`);
};

// ── Explanations ─────────────────────────────────────────────────────────────

export const fetchExplanations = () => apiFetch('/api/explanations');

// ── Audit Log ────────────────────────────────────────────────────────────────

export const fetchAuditLog = (limit = 200) =>
  apiFetch(`/api/audit-log?limit=${limit}`);

// ── Versions ─────────────────────────────────────────────────────────────────

export const fetchVersions = () => apiFetch('/api/versions');

// ── Live Status ──────────────────────────────────────────────────────────────

export const fetchLiveStatus = () => apiFetch('/api/live-status');

// ── HITL Decision ────────────────────────────────────────────────────────────

export const submitHITLDecision = (ruleId, action, analyst = 'analyst', notes = '') =>
  apiFetch('/api/hitl-decision', {
    method: 'POST',
    body: JSON.stringify({ rule_id: ruleId, action, analyst, notes }),
  });

// ── Upload PDF ───────────────────────────────────────────────────────────────

export const uploadPDF = async (file) => {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`${BASE_URL}/api/upload`, {
    method: 'POST',
    body: formData,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
};

// ── Upload CSV ───────────────────────────────────────────────────────────────

export const uploadCSV = async (file) => {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`${BASE_URL}/api/upload-csv`, {
    method: 'POST',
    body: formData,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
};

// ── Run Pipeline ─────────────────────────────────────────────────────────────

export const runPipeline = (phase, pdfName = null) =>
  apiFetch('/api/run', {
    method: 'POST',
    body: JSON.stringify({ phase, pdf: pdfName }),
  });

// ── Poll Pipeline Status ─────────────────────────────────────────────────────

export const fetchPipelineStatus = (offset = 0) =>
  apiFetch(`/api/pipeline-status?offset=${offset}`);

// ── Exports ──────────────────────────────────────────────────────────────────

export const getViolationsCSVUrl = () => `${BASE_URL}/api/export/violations`;
export const getComplianceReportUrl = () => `${BASE_URL}/api/export/report`;
