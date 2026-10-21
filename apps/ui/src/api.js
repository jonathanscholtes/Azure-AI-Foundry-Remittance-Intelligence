// Same-origin API calls; nginx (container) or Vite dev proxy forwards /api to the backend.
const BASE = "/api";

async function request(path, options) {
  const res = await fetch(BASE + path, options);
  if (!res.ok) {
    // HTTP/2 (Azure Container Apps) carries no status reason-phrase, so res.statusText
    // is often empty. Fall back to a text body, then the numeric status, so the error
    // is never blank.
    let detail = "";
    try {
      const body = await res.text();
      try {
        detail = JSON.parse(body).detail || "";
      } catch {
        detail = body.trim();
      }
    } catch {
      /* body unreadable */
    }
    detail = detail || res.statusText || `Request failed (HTTP ${res.status})`;
    throw new Error(detail);
  }
  return res.json();
}

function query(params = {}) {
  const text = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")
  ).toString();
  return text ? `?${text}` : "";
}

const id = (value) => encodeURIComponent(value);

export const api = {
  upload(file) {
    const form = new FormData();
    form.append("file", file);
    return request("/remittances", { method: "POST", body: form });
  },
  list(params = {}) {
    return request(`/remittances${query(params)}`);
  },
  get(processingId) {
    return request(`/remittances/${id(processingId)}`);
  },
  events(processingId, runId) {
    return request(`/remittances/${id(processingId)}/events${query({ runId })}`);
  },
  audit(processingId) {
    return request(`/remittances/${id(processingId)}/audit`);
  },
  files(processingId) {
    return request(`/remittances/${id(processingId)}/files`);
  },
  fileUrl(processingId, container, path) {
    return `${BASE}/remittances/${id(processingId)}/files/${container}/${path.split("/").map(encodeURIComponent).join("/")}`;
  },
  async fileText(processingId, container, path) {
    // Raw text for the in-app artifact viewer; the backend proxies the blob bytes same-origin.
    const res = await fetch(api.fileUrl(processingId, container, path));
    if (!res.ok) throw new Error(res.statusText || `Request failed (HTTP ${res.status})`);
    return res.text();
  },
  csvUrl(processingId) {
    return `${BASE}/remittances/${id(processingId)}/csv`;
  },
  reprocess(processingId) {
    return request(`/remittances/${id(processingId)}/reprocess`, { method: "POST" });
  },
  remove(processingId) {
    // Permanently deletes the run and all its stored content (blobs + ledger/event rows).
    return request(`/remittances/${id(processingId)}`, { method: "DELETE" });
  },
  pipeline(window) {
    return request(`/pipeline/status${query({ window })}`);
  },
  replayDeadLetters() {
    return request(`/pipeline/dead-letter/replay`, { method: "POST" });
  },
  daily(date) {
    return request(`/summaries/daily${query({ date })}`);
  },
  weekly(week) {
    return request(`/summaries/weekly${query({ week })}`);
  },
};
