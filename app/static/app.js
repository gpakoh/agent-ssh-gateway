/**
 * agent-ssh-gateway — Frontend Application
 * Vanilla JS, no frameworks
 */

// ============================================
// State
// ============================================

const state = {
    sessionId: null,
    host: '',
    username: '',
    ws: null,
    wsReconnectCount: 0,
    commandHistory: JSON.parse(localStorage.getItem('ssh_history') || '[]'),
    historyIndex: -1,
    sessions: [],
    jobs: [],
    heartbeatInterval: null,
};
window.state = state;

// ============================================
// DOM Elements
// ============================================

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const els = {
    // Connection
    connectForm: $('#connectForm'),
    host: $('#host'),
    port: $('#port'),
    username: $('#username'),
    password: $('#password'),
    privateKey: $('#privateKey'),
    keyPassphrase: $('#keyPassphrase'),
    keyFile: $('#keyFile'),
    connectBtn: $('#connectBtn'),
    disconnectBtn: $('#disconnectBtn'),
    errorMessage: $('#errorMessage'),

    // Terminal
    terminal: $('#terminal'),
    commandInput: $('#commandInput'),
    executeBtn: $('#executeBtn'),
    ctrlCBtn: $('#ctrlCBtn'),
    copyBtn: $('#copyBtn'),
    clearBtn: $('#clearBtn'),
    prompt: $('#prompt'),
    statusIndicator: $('#connectionStatus'),

    // Sessions & History
    sessionsList: $('#sessionsList'),
    historyList: $('#historyList'),

    // Toast
    toastContainer: $('#toastContainer'),

    // Auth tabs
    authTabs: $$('.auth-tab'),
    authContents: $$('.auth-content'),

    // Jobs
    jobsList: $('#jobsList'),
    fileEditBtn: $('#fileEditBtn'),

    // Servers
    serversList: $('#serversList'),
    serversBadge: $('#serversBadge'),
    addServerBtn: $('#addServerBtn'),
    serverForm: $('#serverForm'),
    serverId: $('#serverId'),
    serverName: $('#serverName'),
    serverHost: $('#serverHost'),
    serverPort: $('#serverPort'),
    serverUsername: $('#serverUsername'),
    serverDesc: $('#serverDesc'),
    saveServerBtn: $('#saveServerBtn'),
    cancelServerBtn: $('#cancelServerBtn'),

    // Editor
    editorStatus: $('#editorStatus'),
    editorPathInput: $('#editorPathInput'),
    openFileBtn: $('#openFileBtn'),
    editorSaveStatus: $('#editorSaveStatus'),
};

// ============================================
// ANSI Parser
// ============================================

const ANSI_COLORS = {
    '30': 'ansi-black', '31': 'ansi-red', '32': 'ansi-green',
    '33': 'ansi-yellow', '34': 'ansi-blue', '35': 'ansi-magenta',
    '36': 'ansi-cyan', '37': 'ansi-white',
    '90': 'ansi-black', '91': 'ansi-red', '92': 'ansi-green',
    '93': 'ansi-yellow', '94': 'ansi-blue', '95': 'ansi-magenta',
    '96': 'ansi-cyan', '97': 'ansi-white',
};

function parseAnsi(text) {
    const re = /\x1b\[([0-9;]*)m/g;
    let result = '';
    let lastIndex = 0;
    let currentClasses = [];
    let match;

    while ((match = re.exec(text)) !== null) {
        result += escapeHtml(text.slice(lastIndex, match.index));
        const codes = match[1].split(';').filter(Boolean);

        if (match[1] === '' || codes.includes('0')) {
            if (currentClasses.length > 0) {
                result += `</span>`.repeat(currentClasses.length);
                currentClasses = [];
            }
        }

        const classes = [];
        for (const code of codes) {
            if (ANSI_COLORS[code]) classes.push(ANSI_COLORS[code]);
            if (code === '1') classes.push('ansi-bold');
            if (code === '3') classes.push('ansi-italic');
        }

        if (classes.length > 0) {
            const cls = classes.join(' ');
            currentClasses.push(cls);
            result += `<span class="${cls}">`;
        }
        lastIndex = re.lastIndex;
    }

    result += escapeHtml(text.slice(lastIndex));
    if (currentClasses.length > 0) {
        result += '</span>'.repeat(currentClasses.length);
    }

    return result;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function escapeAttr(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ============================================
// Terminal
// ============================================

function appendLine(content, type = 'stdout') {
    const line = document.createElement('div');
    line.className = `terminal-line ${type}`;
    line.innerHTML = type === 'system' ? content : parseAnsi(content);
    els.terminal.appendChild(line);
    els.terminal.scrollTop = els.terminal.scrollHeight;
}

function setTerminalHtml(html) {
    els.terminal.innerHTML = html;
    els.terminal.scrollTop = els.terminal.scrollHeight;
}

function clearTerminal() {
    els.terminal.innerHTML = '';
}

function updatePrompt() {
    if (state.sessionId && state.username && state.host) {
        els.prompt.textContent = `[${state.username}@${state.host}]$`;
    } else {
        els.prompt.textContent = '$';
    }
}

// ============================================
// Toast Notifications
// ============================================

function showToast(message, type = 'info', duration = 4000) {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    els.toastContainer.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'fadeOut 0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// ============================================
// Auth Tabs
// ============================================

els.authTabs.forEach(tab => {
    tab.addEventListener('click', () => {
        els.authTabs.forEach(t => t.classList.remove('active'));
        els.authContents.forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        $(`#${tab.dataset.tab}Tab`).classList.add('active');
    });
});

// ============================================
// Key File Upload
// ============================================

els.keyFile.addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
        els.privateKey.value = ev.target.result;
        showToast('Private key loaded', 'success');
    };
    reader.readAsText(file);
});

// ============================================
// API Calls
// ============================================

async function apiConnect(body) {
    const res = await fetch('/api/ssh/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Connection failed');
    return data;
}

async function apiExecute(sessionId, command, timeout = 30) {
    const res = await fetch('/api/ssh/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ session_id: sessionId, command, timeout }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Execution failed');
    return data;
}

async function apiDisconnect(sessionId) {
    const res = await fetch('/api/ssh/disconnect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ session_id: sessionId }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Disconnect failed');
    return data;
}

async function apiSessions() {
    const res = await fetch('/api/ssh/sessions', { credentials: 'include' });
    return res.json();
}

async function apiHeartbeat(sessionId) {
    const res = await fetch('/api/ssh/heartbeat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ session_id: sessionId }),
    });
    return res.json();
}

// Jobs API
async function apiJobRun(sessionId, command) {
    const res = await fetch('/api/jobs/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ session_id: sessionId, command }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed to start job');
    return data;
}

async function apiJobsList(sessionId) {
    const url = sessionId ? `/api/jobs?session_id=${sessionId}` : '/api/jobs';
    const res = await fetch(url, { credentials: 'include' });
    return res.json();
}

async function apiJobStatus(jobId) {
    const res = await fetch(`/api/jobs/${jobId}/status`, { credentials: 'include' });
    return res.json();
}

async function apiJobResult(jobId) {
    const res = await fetch(`/api/jobs/${jobId}/result`, { credentials: 'include' });
    return res.json();
}

async function apiJobCancel(jobId) {
    const res = await fetch(`/api/jobs/${jobId}/cancel`, { method: 'POST', credentials: 'include' });
    return res.json();
}

// File Edit API
async function apiFileRead(sessionId, path) {
    const res = await fetch('/api/file/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ session_id: sessionId, path }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed to read file');
    return data;
}

async function apiFileEdit(sessionId, path, operations) {
    const res = await fetch('/api/file/edit', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ session_id: sessionId, path, operations }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed to edit file');
    return data;
}

// ============================================
// WebSocket
// ============================================

function wsConnect(sessionId, command) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/api/ssh/execute/stream`;

    const ws = new WebSocket(wsUrl);
    state.ws = ws;

    ws.onopen = () => {
        ws.send(JSON.stringify({ session_id: sessionId, command }));
        state.wsReconnectCount = 0;
    };

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        switch (msg.type) {
            case 'stdout':
                appendLine(msg.data, 'stdout');
                break;
            case 'stderr':
                appendLine(msg.data, 'stderr');
                break;
            case 'exit':
                appendLine(`Exit code: ${msg.data}`, 'system');
                wsClose();
                setControlsEnabled(true);
                break;
            case 'error':
                appendLine(`Error: ${msg.data}`, 'stderr');
                wsClose();
                setControlsEnabled(true);
                break;
        }
    };

    ws.onerror = () => {
        appendLine('WebSocket error', 'stderr');
        wsClose();
        setControlsEnabled(true);
    };

    ws.onclose = () => {
        state.ws = null;
    };

    return ws;
}

function wsClose() {
    if (state.ws) {
        state.ws.close();
        state.ws = null;
    }
}

// ============================================
// Connection / Disconnection
// ============================================

els.connectForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    hideError();

    const body = {
        host: els.host.value.trim(),
        port: parseInt(els.port.value) || 22,
        username: els.username.value.trim(),
    };

    const useKey = els.authContents[1].classList.contains('active');
    if (useKey) {
        body.private_key = els.privateKey.value.trim();
        if (els.keyPassphrase.value) body.key_passphrase = els.keyPassphrase.value;
    } else {
        body.password = els.password.value;
    }

    try {
        setControlsEnabled(false);
        els.connectBtn.textContent = 'Connecting...';

        const result = await apiConnect(body);
        state.sessionId = result.session_id;
        state.host = body.host;
        state.username = body.username;

        setConnected(true);
        clearTerminal();
        updatePrompt();
        appendLine(`Connected to ${state.username}@${state.host}`, 'system');
        showToast('Connected successfully', 'success');
        refreshSessions();

    } catch (err) {
        showError(err.message);
        showToast(err.message, 'error');
    } finally {
        els.connectBtn.textContent = 'Connect';
        setControlsEnabled(true);
    }
});

els.disconnectBtn.addEventListener('click', async () => {
    if (!state.sessionId) return;

    try {
        await apiDisconnect(state.sessionId);
        wsClose();
        appendLine(`Disconnected from ${state.host}`, 'system');
        showToast('Disconnected', 'info');
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        state.sessionId = null;
        state.host = '';
        state.username = '';
        setConnected(false);
        updatePrompt();
        refreshSessions();
    }
});

function setConnected(connected) {
    if (connected) {
        els.connectBtn.disabled = true;
        els.disconnectBtn.disabled = false;
        els.statusIndicator.textContent = `Connected: ${state.username}@${state.host}`;
        els.statusIndicator.classList.add('connected');
        els.commandInput.disabled = false;
        els.executeBtn.disabled = false;
        els.ctrlCBtn.disabled = false;
        startHeartbeat();
    } else {
        els.connectBtn.disabled = false;
        els.disconnectBtn.disabled = true;
        els.statusIndicator.textContent = 'Disconnected';
        els.statusIndicator.classList.remove('connected');
        els.commandInput.disabled = true;
        els.executeBtn.disabled = true;
        els.ctrlCBtn.disabled = true;
        stopHeartbeat();
    }
}

function setControlsEnabled(enabled) {
    els.connectBtn.disabled = !enabled || !!state.sessionId;
    els.disconnectBtn.disabled = !enabled || !state.sessionId;
    els.commandInput.disabled = !enabled || !state.sessionId;
    els.executeBtn.disabled = !enabled || !state.sessionId;
    els.ctrlCBtn.disabled = !enabled || !state.sessionId;
}

// ============================================
// Command Execution
// ============================================

async function runCommand() {
    const command = els.commandInput.value.trim();
    if (!command || !state.sessionId) return;

    addToHistory(command);
    appendLine(`${els.prompt.textContent} ${command}`, 'prompt-line');
    els.commandInput.value = '';
    els.commandInput.focus();

    setControlsEnabled(false);

    try {
        // Check if it's a background job (prefix with 'bg:')
        if (command.startsWith('bg:')) {
            const realCommand = command.slice(3).trim();
            const result = await apiJobRun(state.sessionId, realCommand);
            appendLine(`[Job ${result.job_id}] Started: ${realCommand}`, 'system');
            showToast(`Job ${result.job_id} started`, 'success');
            refreshJobs();
            setControlsEnabled(true);
        } else {
            // Use WebSocket for streaming output
            wsConnect(state.sessionId, command);
        }
    } catch (err) {
        appendLine(`Error: ${err.message}`, 'stderr');
        setControlsEnabled(true);
    }
}

els.executeBtn.addEventListener('click', runCommand);

els.commandInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        e.preventDefault();
        runCommand();
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        navigateHistory(-1);
    } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        navigateHistory(1);
    } else if (e.key === 'c' && e.ctrlKey) {
        e.preventDefault();
        sendCtrlC();
    }
});

els.ctrlCBtn.addEventListener('click', sendCtrlC);

function sendCtrlC() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        // Send a special interrupt signal via API
        fetch('/api/ssh/execute', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: state.sessionId, command: '\\x03', timeout: 5 }),
        }).catch(() => {});
        wsClose();
        appendLine('^C', 'system');
        setControlsEnabled(true);
    }
}

// ============================================
// Command History
// ============================================

function addToHistory(command) {
    state.commandHistory.unshift(command);
    if (state.commandHistory.length > 200) {
        state.commandHistory.pop();
    }
    state.historyIndex = -1;
    localStorage.setItem('ssh_history', JSON.stringify(state.commandHistory));
    renderHistory();
}

function navigateHistory(direction) {
    if (state.commandHistory.length === 0) return;

    state.historyIndex += direction;
    if (state.historyIndex < 0) state.historyIndex = 0;
    if (state.historyIndex >= state.commandHistory.length) {
        state.historyIndex = -1;
        els.commandInput.value = '';
        return;
    }

    els.commandInput.value = state.commandHistory[state.historyIndex];
}

function renderHistory() {
    els.historyList.innerHTML = '';
    const recent = state.commandHistory.slice(0, 30);
    for (const cmd of recent) {
        const item = document.createElement('div');
        item.className = 'history-item';
        item.textContent = cmd;
        item.title = cmd;
        item.addEventListener('click', () => {
            els.commandInput.value = cmd;
            els.commandInput.focus();
        });
        els.historyList.appendChild(item);
    }
}

renderHistory();

// ============================================
// Heartbeat
// ============================================

function startHeartbeat() {
    if (state.heartbeatInterval) clearInterval(state.heartbeatInterval);
    state.heartbeatInterval = setInterval(async () => {
        if (state.sessionId) {
            try {
                await apiHeartbeat(state.sessionId);
            } catch (err) {
                console.warn('Heartbeat failed:', err);
            }
        }
    }, 30000); // Every 30 seconds
}

function stopHeartbeat() {
    if (state.heartbeatInterval) {
        clearInterval(state.heartbeatInterval);
        state.heartbeatInterval = null;
    }
}

// ============================================
// Jobs Panel — Live monitoring + SSE streaming
// ============================================

const _jobTimers = {};   // job_id → setInterval for elapsed display
const _jobPreviews = {}; // job_id → last stdout/stderr chunk
const _jobStreams = {};  // job_id → AbortController
let _jobsPollInterval = null;

function startJobsPolling() {
    if (_jobsPollInterval) return;
    refreshJobs();
    _jobsPollInterval = setInterval(refreshJobs, 2000);
}

function stopJobsPolling() {
    if (_jobsPollInterval) {
        clearInterval(_jobsPollInterval);
        _jobsPollInterval = null;
    }
}

function hasActiveJobs(jobs) {
    return jobs.some(j => j.status === 'pending' || j.status === 'running');
}

function formatElapsed(seconds) {
    if (!seconds || seconds < 0) return '—';
    if (seconds < 60) return seconds.toFixed(0) + 's';
    if (seconds < 3600) return (seconds / 60).toFixed(1) + 'm';
    return (seconds / 3600).toFixed(1) + 'h';
}

function previewText(text, maxLen) {
    if (!text) return '';
    const lines = text.split('\n').filter(Boolean);
    const last = lines[lines.length - 1] || '';
    return last.length > (maxLen || 50) ? last.slice(0, (maxLen || 50)) + '...' : last;
}

async function refreshJobs() {
    if (!state.sessionId) {
        els.jobsList.innerHTML = '<p class="empty-text">No jobs</p>';
        return;
    }
    try {
        const data = await apiJobsList(state.sessionId);
        state.jobs = data.jobs || [];

        if (state.jobs.length === 0) {
            els.jobsList.innerHTML = '<p class="empty-text">No jobs</p>';
            return;
        }

        // Start/stop polling based on active jobs
        if (hasActiveJobs(state.jobs)) {
            startJobsPolling();
        }

        els.jobsList.innerHTML = '';
        for (const job of state.jobs.slice(0, 20)) {
            const card = document.createElement('div');
            card.className = 'job-card';
            card.id = 'job-card-' + job.job_id;
            const isActive = job.status === 'pending' || job.status === 'running';
            const statusColor = job.status === 'completed' ? 'ansi-green' :
                               job.status === 'failed' ? 'ansi-red' :
                               job.status === 'running' ? 'ansi-yellow' : 'ansi-blue';

            // Elapsed time
            let elapsed;
            if (job.status === 'running') {
                const start = job.started_at || job.created_at;
                elapsed = Date.now() / 1000 - start;
                elapsed = formatElapsed(elapsed);
            } else {
                elapsed = job.duration ? formatElapsed(job.duration) : '—';
            }

            // Last event preview
            const preview = _jobPreviews[job.job_id] || '';

            card.innerHTML = `
                <div class="job-top">
                    <div class="job-id">${escapeHtml(job.job_id.slice(0, 8))}</div>
                    <div class="job-status ${statusColor}">${job.status}</div>
                </div>
                <div class="job-cmd" title="${escapeHtml(job.command)}">${escapeHtml(job.command.slice(0, 50))}${job.command.length > 50 ? '...' : ''}</div>
                <div class="job-meta">
                    <span class="job-elapsed" id="jelapsed-${job.job_id}">${elapsed}</span>
                    ${isActive ? '<span class="job-preview">' + escapeHtml(previewText(preview, 40)) + '</span>' : ''}
                </div>
                <div class="job-actions">
                    ${isActive ? `<button class="btn btn-danger btn-sm" data-jid="${job.job_id}" data-action="cancel">Cancel</button>` : ''}
                    ${isActive ? `<button class="btn btn-outline btn-sm" data-jid="${job.job_id}" data-action="stream">Stream</button>` : ''}
                    <button class="btn btn-icon btn-sm" data-jid="${job.job_id}" data-action="view">View</button>
                </div>
            `;

            card.querySelectorAll('button').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    const jid = e.target.dataset.jid;
                    const action = e.target.dataset.action;
                    if (action === 'view') {
                        await viewJobResult(jid);
                    } else if (action === 'stream') {
                        await streamJob(jid);
                    } else if (action === 'cancel') {
                        try {
                            await apiJobCancel(jid);
                            showToast('Job cancelled', 'info');
                        } catch (err) {
                            showToast(err.message, 'error');
                        }
                    }
                });
            });
            els.jobsList.appendChild(card);

            // Start per-second elapsed timer for running jobs
            updateJobTimer(job);
        }
    } catch (err) {
        console.error('Failed to refresh jobs:', err);
    }
}

function updateJobTimer(job) {
    const jid = job.job_id;
    if (_jobTimers[jid]) {
        clearInterval(_jobTimers[jid]);
        delete _jobTimers[jid];
    }
    if (job.status !== 'running') return;
    const start = job.started_at || job.created_at;
    _jobTimers[jid] = setInterval(() => {
        const el = document.getElementById('jelapsed-' + jid);
        if (el) {
            const secs = Date.now() / 1000 - start;
            el.textContent = formatElapsed(secs);
        }
    }, 1000);
}

// SSE stream reader — connect to /api/jobs/{id}/stream and print to terminal
async function streamJob(jobId) {
    if (_jobStreams[jobId]) {
        showToast('Already streaming job ' + jobId, 'info');
        return;
    }

    clearTerminal();
    appendLine(`[Stream] Connecting to job ${jobId}...`, 'system');

    const controller = new AbortController();
    _jobStreams[jobId] = controller;

    try {
        const res = await fetch(`/api/jobs/${jobId}/stream`, {
            credentials: 'include',
            signal: controller.signal,
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            appendLine(`[Stream] Failed: ${err.detail || 'HTTP ' + res.status}`, 'stderr');
            delete _jobStreams[jobId];
            // Fallback to status polling
            pollJobStatus(jobId);
            return;
        }

        appendLine(`[Stream] Connected — watching ${jobId}`, 'system');

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed || trimmed.startsWith(':')) continue; // skip keepalive
                if (!trimmed.startsWith('data: ')) continue;

                try {
                    const event = JSON.parse(trimmed.slice(6));
                    handleSSEEvent(jobId, event);
                } catch {
                    // malformed JSON line — skip
                }
            }
        }

        appendLine(`[Stream] Disconnected`, 'system');
    } catch (err) {
        if (err.name === 'AbortError') {
            appendLine(`[Stream] Stopped by user`, 'system');
        } else {
            appendLine(`[Stream] Error: ${err.message}`, 'stderr');
            // Fallback to polling
            pollJobStatus(jobId);
        }
    } finally {
        delete _jobStreams[jobId];
    }
}

function handleSSEEvent(jobId, event) {
    const type = event.type;

    if (type === 'status') {
        const s = event.status;
        const label = s === 'pending' ? 'Pending' : s === 'running' ? 'Running' : s === 'completed' ? '✓ Completed' : s === 'failed' ? '✗ Failed' : s === 'cancelled' ? '⊙ Cancelled' : s;
        const color = s === 'completed' ? 'var(--accent-success)' : s === 'failed' ? 'var(--accent-danger)' : s === 'running' ? 'var(--accent-warning)' : 'var(--accent-info)';
        appendLine(`[${label}]`, s === 'completed' || s === 'failed' || s === 'cancelled' ? 'stdout' : 'system');

        if (s === 'completed' && event.exit_code !== undefined) {
            appendLine(`  Exit code: ${event.exit_code}`, 'stdout');
        }
        if (s === 'failed' || s === 'cancelled') {
            stopJobStream(jobId);
        }
    } else if (type === 'stdout') {
        appendLine(event.data || '', 'stdout');
        _jobPreviews[jobId] = event.data || '';
    } else if (type === 'stderr') {
        appendLine(event.data || '', 'stderr');
        _jobPreviews[jobId] = event.data || '';
    } else if (type === 'exit') {
        appendLine(`  Exit code: ${event.exit_code}`, 'stdout');
    } else if (type === 'error') {
        appendLine(`[Error] ${event.error || event.message || 'Unknown error'}`, 'stderr');
        stopJobStream(jobId);
    }

    // Update card preview
    const previewEl = document.querySelector(`#job-card-${jobId} .job-preview`);
    if (previewEl) {
        previewEl.textContent = previewText(_jobPreviews[jobId] || '', 40);
    }
}

function stopJobStream(jobId) {
    if (_jobStreams[jobId]) {
        _jobStreams[jobId].abort();
        delete _jobStreams[jobId];
    }
}

// Polling fallback for jobs where SSE is unavailable
async function pollJobStatus(jobId) {
    appendLine(`[Poll] Checking status every 2s...`, 'system');
    const pollId = setInterval(async () => {
        try {
            const status = await apiJobStatus(jobId);
            const s = status.status;
            appendLine(`[Poll] ${s} (${formatElapsed(status.duration || 0)})`, 'system');

            if (s === 'completed' || s === 'failed' || s === 'cancelled') {
                clearInterval(pollId);
                const result = await apiJobResult(jobId);
                appendLine(`--- stdout ---`, 'system');
                if (result.stdout) appendLine(result.stdout, 'stdout');
                if (result.stderr) appendLine('--- stderr ---' + '\n' + result.stderr, 'stderr');
                if (result.error_message) appendLine(`Error: ${result.error_message}`, 'stderr');
                appendLine(`[Poll] Done (exit: ${result.exit_code})`, 'system');
            }
        } catch {
            clearInterval(pollId);
        }
    }, 2000);
}

async function viewJobResult(jobId) {
    // If job is still active, offer to stream instead
    const job = state.jobs.find(j => j.job_id === jobId);
    if (job && (job.status === 'running' || job.status === 'pending')) {
        await streamJob(jobId);
        return;
    }

    try {
        const result = await apiJobResult(jobId);
        clearTerminal();
        appendLine(`=== Job ${jobId} ===`, 'system');
        appendLine(`Command: ${result.command}`, 'system');
        appendLine(`Status: ${result.status}`, 'system');
        if (result.exit_code !== null && result.exit_code !== undefined) appendLine(`Exit code: ${result.exit_code}`, 'system');
        appendLine(`Duration: ${result.duration || 0}s`, 'system');
        appendLine('--- stdout ---', 'system');
        if (result.stdout) appendLine(result.stdout, 'stdout');
        appendLine('--- stderr ---', 'system');
        if (result.stderr) appendLine(result.stderr, 'stderr');
        if (result.error_message) {
            appendLine(`Error: ${result.error_message}`, 'stderr');
        }
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// Adaptive polling: fast when active, slow when idle
setInterval(() => {
    if (state.jobs && hasActiveJobs(state.jobs)) {
        // already handled by startJobsPolling
    } else {
        refreshJobs();
    }
}, 15000);

// ============================================
// Sessions Panel

// ============================================
// Sessions Panel
// ============================================

async function refreshSessions() {
    try {
        const data = await apiSessions();
        state.sessions = data.sessions;

        if (state.sessions.length === 0) {
            els.sessionsList.innerHTML = '<div class="empty-state"><i data-lucide="users" class="icon-24"></i><p>No active sessions</p><p class="empty-sub">Fill in the form above and click <strong>Connect</strong> to start a session</p></div>';
            if (typeof lucide !== 'undefined') lucide.createIcons();
            return;
        }

        els.sessionsList.innerHTML = '';
        for (const s of state.sessions) {
            const card = document.createElement('div');
            card.className = 'session-card';
            card.innerHTML = `
                <div class="session-host">${escapeHtml(s.host)}:${s.port}</div>
                <div class="session-user">${escapeHtml(s.username)}</div>
                <div class="session-time">${formatTime(s.connected_at)}</div>
                <button class="btn btn-danger" data-sid="${s.session_id}">Disconnect</button>
            `;
            card.querySelector('button').addEventListener('click', async (e) => {
                const sid = e.target.dataset.sid;
                try {
                    await apiDisconnect(sid);
                    showToast('Session disconnected', 'info');
                    if (sid === state.sessionId) {
                        state.sessionId = null;
                        setConnected(false);
                        updatePrompt();
                    }
                    refreshSessions();
                } catch (err) {
                    showToast(err.message, 'error');
                }
            });
            els.sessionsList.appendChild(card);
        }
    } catch (err) {
        console.error('Failed to refresh sessions:', err);
    }
}

function formatTime(iso) {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// Refresh sessions periodically
setInterval(refreshSessions, 10000);

// ============================================
// Server Management
// ============================================

async function apiListServers() {
    const resp = await fetch('/api/servers', { headers: { 'X-API-Key': '' } });
    if (!resp.ok) return { servers: [], count: 0 };
    return resp.json();
}

async function apiCreateServer(data) {
    const resp = await fetch('/api/servers', {
        method: 'POST',
        headers: { 'X-API-Key': '', 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    });
    if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail?.message || err.detail || 'Failed to create server');
    }
    return resp.json();
}

async function apiDeleteServer(serverId) {
    const resp = await fetch(`/api/servers/${encodeURIComponent(serverId)}`, {
        method: 'DELETE',
        headers: { 'X-API-Key': '' },
    });
    if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail?.message || err.detail || 'Failed to delete server');
    }
    return resp.json();
}

async function refreshServers() {
    try {
        const data = await apiListServers();
        if (data.count === 0) {
            els.serversList.innerHTML = '<div class="empty-state"><i data-lucide="hard-drive" class="icon-24"></i><p>No saved servers</p><p class="empty-sub">Click <strong>+</strong> to save a server for quick reconnect</p></div>';
            els.serversBadge.textContent = '0';
            if (typeof lucide !== 'undefined') lucide.createIcons();
            return;
        }
        els.serversBadge.textContent = data.count;
        els.serversList.innerHTML = '';
        for (const s of data.servers) {
            const card = document.createElement('div');
            card.className = 'server-card';
            const statusClass = s.status === 'online' ? 'status-online' : 'status-offline';
            card.innerHTML = `
                <div class="server-info">
                    <span class="server-name">${escapeHtml(s.name || s.id)}</span>
                    <span class="server-host">${escapeHtml(s.host)}:${s.port} (${escapeHtml(s.username)})</span>
                </div>
                <div class="server-actions">
                    <span class="server-status ${statusClass}">${escapeHtml(s.status || 'unknown')}</span>
                    <button class="btn btn-primary btn-compact" data-sid="${s.id}">Connect</button>
                    <button class="btn-icon btn-icon-sm" data-sid="${s.id}" data-action="delete" title="Delete server">
                        <i data-lucide="trash-2" class="icon-14"></i>
                    </button>
                </div>
            `;
            card.querySelector('[data-action="delete"]').addEventListener('click', async (e) => {
                const id = e.target.closest('button').dataset.sid;
                if (confirm(`Delete server "${id}"?`)) {
                    try {
                        await apiDeleteServer(id);
                        showToast('Server deleted', 'info');
                        refreshServers();
                    } catch (err) {
                        showToast(err.message, 'error');
                    }
                }
            });
            const connectBtn = card.querySelector('[data-sid]');
            if (connectBtn) {
                connectBtn.addEventListener('click', () => {
                    els.serverHost.value = s.host;
                    els.serverPort.value = s.port || 22;
                    els.serverUsername.value = s.username || '';
                    els.host.value = s.host;
                    els.port.value = s.port || 22;
                    els.username.value = s.username || '';
                    showToast(`Loaded server "${s.name || s.id}" into connection form`, 'success');
                });
            }
            els.serversList.appendChild(card);
        }
        if (typeof lucide !== 'undefined') lucide.createIcons();
    } catch (err) {
        console.error('Failed to refresh servers:', err);
    }
}

// Server form toggle
els.addServerBtn.addEventListener('click', () => {
    const visible = els.serverForm.style.display !== 'none';
    els.serverForm.style.display = visible ? 'none' : 'block';
    if (!visible) {
        els.serverId.value = '';
        els.serverName.value = '';
        els.serverHost.value = '';
        els.serverPort.value = 22;
        els.serverUsername.value = '';
        els.serverDesc.value = '';
    }
});

els.cancelServerBtn.addEventListener('click', () => {
    els.serverForm.style.display = 'none';
});

els.saveServerBtn.addEventListener('click', async () => {
    const id = els.serverId.value.trim();
    const name = els.serverName.value.trim();
    const host = els.serverHost.value.trim();
    const port = parseInt(els.serverPort.value) || 22;
    const username = els.serverUsername.value.trim();
    const description = els.serverDesc.value.trim();

    if (!id || !host || !username) {
        showToast('ID, Host, and Username are required', 'error');
        return;
    }

    try {
        await apiCreateServer({ id, name: name || id, host, port, username, description: description || undefined });
        showToast(`Server "${id}" created`, 'success');
        els.serverForm.style.display = 'none';
        refreshServers();
    } catch (err) {
        showToast(err.message, 'error');
    }
});

// ============================================
// Terminal Toolbar
// ============================================

els.copyBtn.addEventListener('click', () => {
    const text = Array.from(els.terminal.querySelectorAll('.terminal-line'))
        .map(l => l.textContent).join('\n');
    navigator.clipboard.writeText(text).then(() => {
        showToast('Terminal output copied', 'success');
    }).catch(() => {
        showToast('Failed to copy', 'error');
    });
});

els.clearBtn.addEventListener('click', clearTerminal);

// ============================================
// Error Display
// ============================================

function showError(msg) {
    els.errorMessage.textContent = msg;
    els.errorMessage.classList.add('visible');
}

function hideError() {
    els.errorMessage.classList.remove('visible');
    els.errorMessage.textContent = '';
}

// ============================================
// Utility: escape HTML for session panel
// ============================================

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============================================
// Init
// ============================================

updatePrompt();
console.log('%cagent-ssh-gateway%c v1.0', 'color:#00ff88;font-weight:bold;', 'color:#888;');

// ============================================
// Monaco Editor Integration
// ============================================

let monacoEditor = null;
let currentFilePath = null;
let editorModified = false;

function setEditorStatus(text, type) {
    if (els.editorStatus) {
        els.editorStatus.textContent = text;
        els.editorStatus.className = 'editor-status ' + (type || '');
    }
}

function setEditorSaveStatus(text, type) {
    if (els.editorSaveStatus) {
        els.editorSaveStatus.textContent = text;
        els.editorSaveStatus.className = 'editor-save-status ' + (type || '');
    }
}

// Initialize Monaco Editor (lazy: called on first editor panel open)
function initMonacoEditor() {
    if (window.__monacoInitialized) return;
    window.__monacoInitialized = true;
    
    require(['vs/editor/editor.main'], function() {
        monacoEditor = monaco.editor.create(document.getElementById('editorContainer'), {
            value: '',
            language: 'python',
            theme: 'vs-dark',
            automaticLayout: true,
            minimap: { enabled: false },
            fontSize: 14,
            lineNumbers: 'on',
            roundedSelection: false,
            scrollBeyondLastLine: false,
            readOnly: false,
            wordWrap: 'on',
        });
        
        monacoEditor.onDidChangeModelContent(() => {
            if (!editorModified && currentFilePath) {
                editorModified = true;
                setEditorSaveStatus('● Modified', 'modified');
            }
            const saveBtn = document.getElementById('saveFileBtn');
            if (saveBtn && currentFilePath) saveBtn.disabled = false;
        });
    });
}

// Open file by path from the editor path input
async function openFileByPath() {
    const path = els.editorPathInput.value.trim();
    if (!path) {
        showToast('Enter a file path', 'error');
        return;
    }
    if (!state.sessionId) {
        showToast('No active SSH session. Connect first.', 'error');
        return;
    }
    
    setEditorStatus('Reading...', 'loading');
    try {
        const res = await fetch('/api/file/read', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: state.sessionId,
                path: path,
            }),
        });
        
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail?.message || err.detail || 'Failed to read file');
        }
        
        const data = await res.json();
        loadFileIntoEditor(path, data.content);
        setEditorStatus(`Loaded (${formatBytes(data.size || data.content.length)})`, 'loaded');
    } catch (err) {
        showToast(err.message, 'error');
        setEditorStatus('Error', 'error');
    }
}

function formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}

// Load file into editor
async function loadFileIntoEditor(path, content) {
    if (!monacoEditor) {
        showToast('Editor not initialized', 'error');
        return;
    }
    
    currentFilePath = path;
    editorModified = false;
    
    // Detect language from extension
    const ext = path.split('.').pop();
    const langMap = {
        'py': 'python',
        'js': 'javascript',
        'ts': 'typescript',
        'html': 'html',
        'css': 'css',
        'json': 'json',
        'md': 'markdown',
        'yml': 'yaml',
        'yaml': 'yaml',
        'sh': 'shell',
        'dockerfile': 'dockerfile',
    };
    
    const language = langMap[ext] || 'plaintext';
    
    monacoEditor.setValue(content);
    monaco.editor.setModelLanguage(monacoEditor.getModel(), language);
    
    els.editorFilename.textContent = path;
    els.editorPathInput.value = path;
    document.getElementById('editorPanel').style.display = 'block';
    document.getElementById('saveFileBtn').disabled = true;
    setEditorSaveStatus('✓ Saved', 'saved');
    setEditorStatus(`Loaded (${formatBytes(content.length)})`, 'loaded');
}

// Save file from editor
async function saveFileFromEditor() {
    if (!monacoEditor || !currentFilePath || !state.sessionId) {
        showToast('No file to save', 'error');
        return;
    }
    
    const content = monacoEditor.getValue();
    setEditorStatus('Saving...', 'loading');
    setEditorSaveStatus('Saving...', 'loading');
    
    try {
        const res = await fetch('/api/file/edit', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: state.sessionId,
                path: currentFilePath,
                operations: [{
                    type: 'replace',
                    old: await getOriginalContent(currentFilePath),
                    new: content,
                }],
            }),
        });
        
        if (res.ok) {
            const result = await res.json();
            showToast(`Saved: ${currentFilePath}`, 'success');
            document.getElementById('saveFileBtn').disabled = true;
            editorModified = false;
            setEditorSaveStatus('✓ Saved', 'saved');
            setEditorStatus(`Saved (${result.operations_applied || 0} op(s))`, 'saved');
        } else {
            const err = await res.json();
            const msg = err.detail?.message || err.detail || 'Unknown error';
            showToast(`Save failed: ${msg}`, 'error');
            setEditorSaveStatus('✗ Save failed', 'error');
            setEditorStatus('Error', 'error');
        }
    } catch (e) {
        showToast('Save failed: connection error', 'error');
        setEditorSaveStatus('✗ Save failed', 'error');
        setEditorStatus('Error', 'error');
    }
}

// Get original file content
async function getOriginalContent(path) {
    try {
        const res = await fetch('/api/file/read', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: state.sessionId,
                path: path,
            }),
        });
        
        if (res.ok) {
            const data = await res.json();
            return data.content;
        }
    } catch (e) {
        console.error('Failed to get original content:', e);
    }
    return '';
}

// Editor event listeners
document.addEventListener('DOMContentLoaded', () => {
    initMonacoEditor();
    
    const saveBtn = document.getElementById('saveFileBtn');
    const closeBtn = document.getElementById('closeEditorBtn');
    
    if (saveBtn) saveBtn.addEventListener('click', saveFileFromEditor);
    if (closeBtn) closeBtn.addEventListener('click', () => {
        document.getElementById('editorPanel').style.display = 'none';
        currentFilePath = null;
        editorModified = false;
        setEditorSaveStatus('', '');
        setEditorStatus('', '');
    });

    // File edit button (sidebar) — opens editor panel and focuses path input
    els.fileEditBtn.addEventListener('click', () => {
        document.getElementById('editorPanel').style.display = 'block';
        els.editorPathInput.focus();
    });

    // Open file from path input
    els.openFileBtn.addEventListener('click', openFileByPath);
    els.editorPathInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') openFileByPath();
    });

    // Load servers on startup
    setTimeout(refreshServers, 500);
});

// Expose editor functions globally
window.loadFileIntoEditor = loadFileIntoEditor;
window.openFileByPath = openFileByPath;

// ============================================
// Bulk Operations Modal
// ============================================

const BULK_TEMPLATES = {
    bulk_read: {
        title: 'Bulk Read',
        body: { session_id: '<SESSION>', paths: ['/etc/hostname', '/etc/os-release'] },
        endpoint: '/api/bulk/read',
        method: 'POST',
    },
    bulk_edit: {
        title: 'Bulk Edit',
        body: {
            session_id: '<SESSION>',
            files: [
                { path: '/var/www/app/config.py', operations: [{ type: 'replace', old: 'DEBUG = False', new: 'DEBUG = True' }, { type: 'insert_after', after: 'PORT = 8080', text: "HOST = '0.0.0.0'\n" }] },
                { path: '/var/www/app/settings.py', operations: [{ type: 'replace', old: 'TIMEOUT = 30', new: 'TIMEOUT = 60' }] },
            ],
        },
        endpoint: '/api/bulk/edit',
        method: 'POST',
    },
    bulk_execute: {
        title: 'Run Batch',
        body: { session_id: '<SESSION>', commands: ['uptime', 'df -h /', 'free -m'] },
        endpoint: '/api/bulk/execute',
        method: 'POST',
    },
    // Git safe flow modes
    git_status: {
        title: 'Git Status',
        body: { session_id: '<SESSION>', path: '.' },
        endpoint: '/api/git/simple-status',
        method: 'GET',
        getParams: true,
        formatResponse: function(data) {
            if (data.clean) return `Branch: ${data.branch} — clean (no changes)`;
            let s = `Branch: ${data.branch}`;
            if (data.modified && data.modified.length) s += `\n  Modified: ${data.modified.length} file(s)`;
            if (data.staged && data.staged.length) s += `\n  Staged: ${data.staged.length} file(s)`;
            if (data.untracked && data.untracked.length) s += `\n  Untracked: ${data.untracked.length} file(s)`;
            if (data.ahead) s += `\n  Ahead: ${data.ahead}`;
            if (data.behind) s += `\n  Behind: ${data.behind}`;
            return s;
        },
    },
    git_diff: {
        title: 'Git Diff',
        body: { session_id: '<SESSION>', path: '.', cached: false },
        endpoint: '/api/git/diff',
        method: 'POST',
        formatResponse: function(data) {
            if (!data.diff) return 'No changes to show';
            const lines = data.diff.split('\n').length;
            const files = data.files_changed || 0;
            return `${files} file(s) changed, ${lines} line(s) in diff`;
        },
    },
    git_backup: {
        title: 'Git Backup',
        body: { context_id: '<CONTEXT>', backup_name: 'auto_backup' },
        endpoint: '/api/git/backup',
        method: 'POST',
        queryParams: true,
    },
    git_commit: {
        title: 'Git Commit',
        body: { context_id: '<CONTEXT>', message: 'fix: ...', files: null },
        endpoint: '/api/git/commit',
        method: 'POST',
        confirmMessage: '⚠️  This will create a git commit.\n• Review the diff first (use Diff button)\n• Create a backup first (use Backup button)\n• Commits are local — no push',
    },
    git_restore: {
        title: 'Git Restore',
        body: { context_id: '<CONTEXT>' },
        endpoint: '/api/git/restore',
        method: 'POST',
        queryParams: true,
        confirmMessage: '⚠️  This will RESTORE from backup.\n• Current working tree changes will be OVERWRITTEN\n• Ensure a backup exists (list with /api/recovery/backups)\n• This cannot be undone',
    },
    // Project Navigation modes
    nav_tree: {
        title: 'Project Tree',
        body: { session_id: '<SESSION>', path: '.', max_depth: 3 },
        endpoint: '/api/project/tree',
        method: 'GET',
        getParams: true,
        formatResponse: function(data) {
            if (!data.items || !data.items.length) return 'Empty project (0 items)';
            const files = data.items.filter(function(i) { return i.type === 'file'; }).length;
            const dirs = data.items.filter(function(i) { return i.type === 'directory'; }).length;
            var s = data.count + ' item(s) — ' + dirs + ' dir(s), ' + files + ' file(s)';
            var maxShow = 20;
            for (var i = 0; i < Math.min(data.items.length, maxShow); i++) {
                var item = data.items[i];
                var sym = item.type === 'directory' ? '[D]' : '[F]';
                var sizeStr = item.size != null ? ' (' + item.size + ' B)' : '';
                s += '\n  ' + sym + ' ' + item.path + sizeStr;
            }
            if (data.items.length > maxShow) s += '\n  ... and ' + (data.items.length - maxShow) + ' more';
            return s;
        },
    },
    nav_search: {
        title: 'Search',
        body: { session_id: '<SESSION>', path: '.', query: 'TODO', file_pattern: '*.py', context_lines: 2 },
        endpoint: '/api/search/global',
        method: 'POST',
        formatResponse: function(data) {
            if (!data.total_count) return 'No matches found';
            var s = data.total_count + ' match(es) in ' + data.files_affected.length + ' file(s)';
            var matches = (data.matches || []).slice(0, 10);
            for (var i = 0; i < matches.length; i++) {
                var m = matches[i];
                s += '\n  ' + m.path + ':' + m.line + ' — ' + m.content.trim().substring(0, 80);
            }
            if (data.total_count > 10) s += '\n  ... and ' + (data.total_count - 10) + ' more match(es)';
            return s;
        },
    },
    nav_context: {
        title: 'New Context',
        body: { session_id: '<SESSION>', path: '.', name: 'my-task', auto_commit: false },
        endpoint: '/api/context/create',
        method: 'POST',
        formatResponse: function(data) {
            return 'Context: ' + data.context_id + ' — ' + data.path + ' (' + (data.branch || 'no branch') + ')';
        },
    },
    nav_bookmark: {
        title: 'Bookmark',
        body: { context_id: '<CONTEXT>', path: 'config.py', line: 5, note: 'important location' },
        endpoint: '/api/context/bookmark',
        method: 'POST',
        formatResponse: function(data) {
            if (data.status === 'added' && data.bookmark) {
                return 'Bookmarked: ' + data.bookmark.path + ':' + data.bookmark.line + ' — ' + (data.bookmark.note || 'no note');
            }
            return 'Bookmark: ' + (data.status || 'ok');
        },
    },
    // Templates & Code Generation modes
    tpl_list: {
        title: 'List Templates',
        body: {},
        endpoint: '/api/templates',
        method: 'GET',
        getParams: true,
        formatResponse: function(data) {
            if (!data.templates || !data.templates.length) return 'No templates available';
            var s = data.count + ' template(s):';
            for (var i = 0; i < data.templates.length; i++) {
                var t = data.templates[i];
                s += '\n  ' + t.id + ' — ' + t.name + ' (' + t.language + ')';
            }
            return s;
        },
    },
    tpl_render: {
        title: 'Render Template',
        body: { context_id: '<CONTEXT>', template_id: 'fastapi_endpoint', params: { method: 'get', path: '/items', handler_name: 'list_items', params: '', description: 'List items', response: '{}' }, target_path: '/var/www/app/routes/items.py', auto_commit: false },
        endpoint: '/api/templates/render',
        method: 'POST',
        formatResponse: function(data) {
            if (!data.success) return 'Render failed';
            var s = 'Rendered: ' + data.target_path;
            s += '\n  Template: ' + data.template_id;
            s += '\n  Lines: ' + (data.code ? data.code.split('\n').length : 0);
            if (data.git_commit) s += '\n  Commit: ' + data.git_commit;
            s += '\n\nRendered code:\n' + (data.code || '');
            return s;
        },
    },
    tpl_run: {
        title: 'Run Command Template',
        body: { session_id: '<SESSION>', template: 'healthcheck', params: { service: 'nginx' } },
        endpoint: '/api/templates/run',
        method: 'POST',
        formatResponse: function(data) {
            var s = 'Exit code: ' + data.exit_code;
            s += '\nDuration: ' + data.duration + 's';
            if (data.stdout) s += '\n\n' + data.stdout.trim().substring(0, 500);
            if (data.stderr) s += '\n\nstderr:\n' + data.stderr.trim().substring(0, 500);
            return s;
        },
    },
    tpl_scaffold: {
        title: 'Scaffold Python Class',
        body: { session_id: '<SESSION>', module_path: 'app/services', class_name: 'UserService', methods: ['get_user', 'create_user', 'delete_user'], include_test: true },
        endpoint: '/api/scaffold/python-class',
        method: 'POST',
        formatResponse: function(data) {
            if (!data.files_created || !data.files_created.length) return 'No files created';
            var s = data.message;
            s += '\n  Files:';
            for (var i = 0; i < data.files_created.length; i++) {
                s += '\n    ' + (i + 1) + '. ' + data.files_created[i];
            }
            return s;
        },
    },
    tpl_generate: {
        title: 'Generate Code',
        body: { instruction: 'FastAPI route that returns a list of users', language: 'python' },
        endpoint: '/api/code/generate',
        method: 'POST',
        formatResponse: function(data) {
            if (!data.code) return 'No code generated';
            var lines = data.code.split('\n').length;
            var s = data.language + ' — ' + lines + ' line(s)';
            s += '\n' + data.explanation;
            s += '\n\n' + data.code;
            return s;
        },
    },
    tpl_complete: {
        title: 'Complete Code',
        body: { session_id: '<SESSION>', path: 'app/main.py', partial_code: 'async def get_user', language: 'python' },
        endpoint: '/api/code/complete',
        method: 'POST',
        formatResponse: function(data) {
            if (!data.completion) return 'No completion generated';
            return 'Completion: ' + data.completion.substring(0, 200);
        },
    },
    // Observability modes
    obs_analytics: {
        title: 'Project Analytics',
        body: { session_id: '<SESSION>', path: '.' },
        endpoint: '/api/analytics',
        method: 'POST',
        formatResponse: function(data) {
            if (!data.files) return 'No analytics data';
            var s = 'Analytics for ' + data.project_path;
            // Files
            var fe = data.files;
            s += '\n  Files: ' + fe.total_files + ' file(s), ' + fe.total_directories + ' dir(s)';
            if (fe.extensions) {
                var extKeys = Object.keys(fe.extensions);
                var extStrs = [];
                for (var i = 0; i < extKeys.length; i++) {
                    extStrs.push(extKeys[i] + '(' + fe.extensions[extKeys[i]] + ')');
                }
                if (extStrs.length) s += ', ext: ' + extStrs.join(', ');
            }
            // Code
            var cd = data.code;
            if (cd) s += '\n  Code: ' + cd.python_lines_of_code + ' LOC, ' + cd.classes + ' class(es), ' + cd.functions + ' function(s)';
            // Git
            var gt = data.git;
            if (gt) {
                if (gt.is_git_repo) {
                    s += '\n  Git: ' + gt.total_commits + ' commit(s), ' + gt.branches + ' branch(es), ' + gt.contributors + ' contributor(s)';
                    s += '  Last commit: ' + gt.last_commit;
                } else {
                    s += '\n  Git: not a git repo';
                }
            }
            // Tests
            var ts = data.tests;
            if (ts) s += '\n  Tests: ' + ts.test_files + ' file(s), ' + ts.total_tests + ' test(s) ' + (ts.has_tests ? '✓' : '✗');
            // Deps
            var dp = data.dependencies;
            if (dp) s += '\n  Deps: ' + dp.requirements_count + ' requirement(s)' + (dp.has_pyproject ? ', pyproject ✓' : '') + (dp.outdated_packages ? ', ' + dp.outdated_packages + ' outdated' : '');
            return s;
        },
    },
    obs_metrics: {
        title: 'Metrics',
        body: {},
        endpoint: '/metrics',
        method: 'GET',
        getParams: true,
        rawText: true,
        formatResponse: function(data) {
            var text = data._raw || '';
            if (!text) return 'No metrics data';
            var lines = text.split('\n').filter(function(l) { return l && !l.startsWith('#'); });
            var s = lines.length + ' metric line(s)';
            // Parse some key metrics
            var parseMetric = function(prefix) {
                for (var i = 0; i < lines.length; i++) {
                    if (lines[i].startsWith(prefix)) {
                        var parts = lines[i].split(' ');
                        return parts[parts.length - 1];
                    }
                }
                return null;
            };
            var active = parseMetric('ssh_gateway_ssh_connections_active');
            if (active != null) s += '\n  Active connections: ' + active;
            var queue = parseMetric('ssh_gateway_queue_depth{queue="pending"}');
            if (queue != null) s += '\n  Queue depth (pending): ' + queue;
            var reqTotal = parseMetric('ssh_gateway_requests_total');
            if (reqTotal != null) {
                s += '\n  Total requests: ' + reqTotal;
            }
            s += '\n\nFirst 30 lines:\n' + text.split('\n').slice(0, 30).join('\n');
            return s;
        },
    },
    obs_journal: {
        title: 'Journal Logs',
        body: { session_id: '<SESSION>', lines: 20, priority: 'err' },
        endpoint: '/api/logs/journal',
        method: 'GET',
        getParams: true,
        formatResponse: function(data) {
            var text = data.stdout || '';
            if (data.exit_code != null) {
                var s = 'Exit: ' + data.exit_code + ', duration: ' + (data.duration || 0) + 's';
                s += '\n  Lines: ' + (text ? text.split('\n').length : 0);
                if (data.stderr) s += ', stderr: ' + data.stderr.length + ' char(s)';
                if (text) s += '\n\n' + text.substring(0, 2000);
                return s;
            }
            return text ? text.substring(0, 2000) : 'No output';
        },
    },
    obs_docker: {
        title: 'Docker Logs',
        body: { session_id: '<SESSION>', container: 'web-app', lines: 30, timestamps: false },
        endpoint: '/api/logs/docker',
        method: 'GET',
        getParams: true,
        formatResponse: function(data) {
            var text = data.stdout || '';
            if (data.exit_code != null) {
                var s = 'Exit: ' + data.exit_code + ', duration: ' + (data.duration || 0) + 's';
                s += '\n  Lines: ' + (text ? text.split('\n').length : 0);
                if (data.stderr) s += ', stderr: ' + data.stderr.length + ' char(s)';
                if (text) s += '\n\n' + text.substring(0, 2000);
                return s;
            }
            return text ? text.substring(0, 2000) : 'No output';
        },
    },
    obs_webhooks: {
        title: 'Webhooks',
        body: {},
        endpoint: '/api/webhooks',
        method: 'GET',
        getParams: true,
        formatResponse: function(data) {
            if (!data.webhooks || !data.webhooks.length) return 'No webhooks configured';
            var s = data.count + ' webhook(s):';
            for (var i = 0; i < data.webhooks.length; i++) {
                var wh = data.webhooks[i];
                s += '\n  ' + wh.id + ' — ' + wh.name + ' (' + wh.webhook_type + ')';
                s += '\n    Target: ' + wh.target_path;
                s += '\n    Cmd: ' + wh.deploy_command.substring(0, 60);
                s += '\n    Enabled: ' + wh.enabled;
            }
            return s;
        },
    },
    obs_hooks: {
        title: 'Event Hooks',
        body: {},
        endpoint: '/api/event-hooks',
        method: 'GET',
        getParams: true,
        formatResponse: function(data) {
            if (!data.hooks || !data.hooks.length) return 'No event hooks registered';
            var s = data.count + ' event hook(s):';
            for (var i = 0; i < data.hooks.length; i++) {
                var hk = data.hooks[i];
                s += '\n  ' + hk.id + ' → ' + hk.url;
                s += '\n    Events: ' + (hk.events || []).join(', ');
                s += '\n    Active: ' + hk.is_active;
                if (hk.session_id) s += ', session: ' + hk.session_id;
            }
            return s;
        },
    },
    // Recovery modes
    rec_backup: {
        title: 'Create Backup',
        body: { context_id: '<CONTEXT>', name: 'before_edit' },
        endpoint: '/api/recovery/backup',
        method: 'POST',
        formatResponse: function(data) {
            if (data.success) {
                return 'Backup: ' + (data.backup_id || data.message || 'ok');
            }
            return 'Backup failed: ' + (data.message || 'unknown error');
        },
    },
    rec_backups: {
        title: 'List Backups',
        body: { context_id: '<CONTEXT>' },
        endpoint: '/api/recovery/backups',
        method: 'GET',
        getParams: true,
        formatResponse: function(data) {
            if (!data.backups || !data.backups.length) {
                return 'No backups created yet.\nTip: Use Backup button above to create one before risky operations.';
            }
            var s = data.count + ' backup(s):';
            for (var i = 0; i < data.backups.length; i++) {
                var b = data.backups[i];
                s += '\n  [' + b.id + '] ' + b.name + ' — ' + new Date(b.created_at * 1000).toLocaleString();
            }
            return s;
        },
    },
    rec_snapshots: {
        title: 'List Snapshots',
        body: { context_id: '<CONTEXT>' },
        endpoint: '/api/snapshots',
        method: 'GET',
        getParams: true,
        formatResponse: function(data) {
            if (!data.snapshots || !data.snapshots.length) {
                return 'No snapshots created yet.\nTip: Use Snapshots POST /api/snapshots with a context to create one.';
            }
            var s = data.count + ' snapshot(s):';
            for (var i = 0; i < data.snapshots.length; i++) {
                var sn = data.snapshots[i];
                s += '\n  [' + sn.id + '] ' + sn.name + ' — ' + new Date(sn.created_at * 1000).toLocaleString();
                if (sn.description) s += '\n    ' + sn.description;
                if (sn.files && sn.files.length) s += '\n    Files: ' + sn.files.join(', ');
                if (sn.git_commit_before) s += '\n    Git: ' + sn.git_commit_before;
                if (sn.size_bytes) s += '\n    Size: ' + sn.size_bytes + ' B';
            }
            return s;
        },
    },
    rec_restore: {
        title: 'Restore Snapshot',
        body: { context_id: '<CONTEXT>', snapshot_id: 'snap_abc123' },
        endpoint: '/api/snapshots/restore',
        method: 'POST',
        confirmMessage: '⚠️  RESTORE FROM SNAPSHOT\n• This will OVERWRITE current files with snapshot contents\n• Files not in the snapshot are NOT affected\n• Create a backup first: use Backup button above\n• This cannot be reverted\n\nType CONFIRM and click Execute to proceed.',
        formatResponse: function(data) {
            if (data.success) {
                return 'Restored: ' + data.message;
            }
            return 'Restore failed: ' + (data.message || 'unknown error');
        },
    },
    rec_hosts: {
        title: 'Known Hosts',
        body: {},
        endpoint: '/api/known-hosts',
        method: 'GET',
        getParams: true,
        formatResponse: function(data) {
            if (!data.hosts || !data.hosts.length) {
                return 'No known hosts. Hosts are added automatically on SSH connection.';
            }
            var s = data.hosts.length + ' known host(s):';
            for (var i = 0; i < data.hosts.length; i++) {
                var h = data.hosts[i];
                s += '\n  ' + h.host + ':' + (h.port || 22) + ' — ' + h.key_type;
                if (h.fingerprint) s += '\n    FP: ' + h.fingerprint;
            }
            return s;
        },
    },
};

let currentBulkMode = 'bulk_read';

function getBulkEls() {
    return {
        overlay: document.getElementById('bulkOverlay'),
        title: document.getElementById('bulkModalTitle'),
        mode: document.getElementById('bulkModeSelect'),
        textarea: document.getElementById('bulkRequestBody'),
        validation: document.getElementById('bulkValidationResult'),
        sessionHint: document.getElementById('bulkSessionHint'),
        loadExample: document.getElementById('bulkLoadExample'),
        validateBtn: document.getElementById('bulkValidate'),
        executeBtn: document.getElementById('bulkExecute'),
        close: document.getElementById('bulkModalClose'),
    };
}

function fillSessionId(body) {
    const s = JSON.stringify(body);
    return state.sessionId ? s.replace(/<SESSION>/g, state.sessionId) : s;
}

function openBulkModal(mode) {
    currentBulkMode = mode;
    const el = getBulkEls();
    const tpl = BULK_TEMPLATES[mode];
    if (!el.overlay) return;
    el.title.textContent = tpl.title;
    el.mode.value = mode;
    el.textarea.value = fillSessionId(tpl.body);
    if (state.sessionId) {
        el.sessionHint.textContent = 'Active session: ' + state.sessionId;
        el.sessionHint.className = 'bulk-hint active';
    } else {
        el.sessionHint.textContent = 'No active session — connect first';
        el.sessionHint.className = 'bulk-hint warning';
    }
    el.validation.textContent = '';
    el.validation.className = 'bulk-validation';
    el.overlay.style.display = 'flex';
    setTimeout(() => el.textarea.focus(), 100);
}

function closeBulkModal() {
    const el = getBulkEls();
    if (el.overlay) el.overlay.style.display = 'none';
}

function validateBulkJson() {
    const el = getBulkEls();
    try {
        const parsed = JSON.parse(el.textarea.value);
        const tpl = BULK_TEMPLATES[currentBulkMode];

        // Check for confirmation message (commit/restore)
        if (tpl.confirmMessage) {
            el.validation.textContent = tpl.confirmMessage + '\n\nType CONFIRM and click Execute to proceed.';
            el.validation.className = 'bulk-validation warning';
            if (el.textarea.value.includes('CONFIRM')) {
                // User added CONFIRM — check required fields too
            } else {
                return null; // require CONFIRM string in body
            }
        }

        const required = {
            bulk_read: ['session_id', 'paths'],
            bulk_edit: ['session_id', 'files'],
            bulk_execute: ['session_id', 'commands'],
            git_status: ['session_id', 'path'],
            git_diff: ['session_id', 'path'],
            git_backup: ['context_id'],
            git_commit: ['context_id', 'message'],
            git_restore: ['context_id'],
            nav_tree: ['session_id', 'path'],
            nav_search: ['session_id', 'path', 'query'],
            nav_context: ['session_id', 'path'],
            nav_bookmark: ['context_id', 'path', 'line'],
            tpl_list: [],
            tpl_render: ['context_id', 'template_id', 'target_path'],
            tpl_run: ['session_id', 'template'],
            tpl_scaffold: ['session_id', 'module_path', 'class_name'],
            tpl_generate: ['instruction'],
            tpl_complete: ['session_id', 'path', 'partial_code'],
            obs_analytics: ['session_id', 'path'],
            obs_metrics: [],
            obs_journal: ['session_id'],
            obs_docker: ['session_id', 'container'],
            obs_webhooks: [],
            obs_hooks: [],
            rec_backup: ['context_id'],
            rec_backups: ['context_id'],
            rec_snapshots: ['context_id'],
            rec_restore: ['context_id', 'snapshot_id'],
            rec_hosts: [],
        };
        const fields = required[currentBulkMode] || [];
        const missing = fields.filter(f => !(f in parsed));
        if (missing.length) {
            el.validation.textContent = 'Missing required fields: ' + missing.join(', ');
            el.validation.className = 'bulk-validation error';
            return null;
        }
        if (currentBulkMode === 'bulk_read' && (!Array.isArray(parsed.paths) || !parsed.paths.length)) {
            el.validation.textContent = 'paths must be a non-empty array';
            el.validation.className = 'bulk-validation error';
            return null;
        }
        if (currentBulkMode === 'bulk_edit' && (!Array.isArray(parsed.files) || !parsed.files.length)) {
            el.validation.textContent = 'files must be a non-empty array';
            el.validation.className = 'bulk-validation error';
            return null;
        }
        if (currentBulkMode === 'bulk_execute' && (!Array.isArray(parsed.commands) || !parsed.commands.length)) {
            el.validation.textContent = 'commands must be a non-empty array';
            el.validation.className = 'bulk-validation error';
            return null;
        }
        el.validation.textContent = 'JSON valid — ready to execute';
        el.validation.className = 'bulk-validation success';
        return parsed;
    } catch (e) {
        el.validation.textContent = 'Invalid JSON: ' + e.message;
        el.validation.className = 'bulk-validation error';
        return null;
    }
}

async function executeBulkRequest(parsed) {
    const el = getBulkEls();
    const tpl = BULK_TEMPLATES[currentBulkMode];
    el.executeBtn.disabled = true;
    el.executeBtn.innerHTML = '<span>Executing...</span>';

    try {
        let url = tpl.endpoint;
        let fetchOpts = {
            method: tpl.method,
            headers: {},
            credentials: 'include',
        };

        if (tpl.method === 'GET') {
            // GET with query params
            const params = new URLSearchParams();
            for (const [k, v] of Object.entries(parsed)) {
                params.set(k, String(v));
            }
            url += '?' + params.toString();
        } else if (tpl.queryParams) {
            // POST with query params (git/backup, git/restore)
            const params = new URLSearchParams();
            for (const [k, v] of Object.entries(parsed)) {
                params.set(k, String(v));
            }
            url += '?' + params.toString();
            fetchOpts.headers = {};
        } else {
            // Standard POST/PATCH with JSON body
            fetchOpts.headers = { 'Content-Type': 'application/json' };
            fetchOpts.body = JSON.stringify(parsed);
        }

        const res = await fetch(url, fetchOpts);

        let data;
        const contentType = res.headers.get('content-type') || '';
        if (contentType.includes('application/json')) {
            data = await res.json();
        } else {
            data = { _raw: await res.text() };
        }

        if (!res.ok) {
            const msg = typeof data.detail === 'object' ? JSON.stringify(data.detail) : (data.detail || data.error || res.statusText);
            el.validation.textContent = 'HTTP ' + res.status + ': ' + msg;
            el.validation.className = 'bulk-validation error';
        } else {
            el.validation.textContent = 'OK (' + res.status + ')';
            el.validation.className = 'bulk-validation success';
        }

        printBulkResult(currentBulkMode, data, res.ok);
    } catch (e) {
        el.validation.textContent = 'Request failed: ' + e.message;
        el.validation.className = 'bulk-validation error';
    } finally {
        el.executeBtn.disabled = false;
        el.executeBtn.innerHTML = '<i data-lucide="play" class="icon-14"></i><span>Execute</span>';
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }
}

function printBulkResult(mode, response, success) {
    const modeLabels = {
        bulk_read: 'Bulk Read', bulk_edit: 'Bulk Edit', bulk_execute: 'Run Batch',
        git_status: 'Git Status', git_diff: 'Git Diff',
        git_backup: 'Git Backup', git_commit: 'Git Commit', git_restore: 'Git Restore',
        nav_tree: 'Project Tree', nav_search: 'Search',
        nav_context: 'New Context', nav_bookmark: 'Bookmark',
        tpl_list: 'List Templates', tpl_render: 'Render',
        tpl_run: 'Run Template', tpl_scaffold: 'Scaffold',
        tpl_generate: 'Generate', tpl_complete: 'Complete',
        obs_analytics: 'Analytics', obs_metrics: 'Metrics',
        obs_journal: 'Journal', obs_docker: 'Docker Logs',
        obs_webhooks: 'Webhooks', obs_hooks: 'Event Hooks',
        rec_backup: 'Backup', rec_backups: 'Backups', rec_snapshots: 'Snapshots',
        rec_restore: 'Restore Snapshot', rec_hosts: 'Known Hosts',
    };
    const label = modeLabels[mode] || mode;
    const tpl = BULK_TEMPLATES[mode];
    const ep = tpl.endpoint;
    const summary = tpl.formatResponse ? tpl.formatResponse(response) : summaryLine(mode, response);
    const time = new Date().toLocaleTimeString();

    let html = '<div class="terminal-line" style="border-top:1px solid var(--border-subtle);padding-top:6px;margin-top:6px">';
    html += '<span style="color:var(--accent-info)">[' + time + ']</span> ';
    html += '<strong>' + label + '</strong> ';
    html += '<span style="color:var(--text-tertiary)">' + ep + '</span></div>';
    html += '<div class="terminal-line" style="color:var(--accent-success)">' + escapeHtml(summary) + '</div>';

        // For git endpoints with formatted response, skip raw JSON unless there's an error
        if (tpl.formatResponse && success) {
            // For diff, also show the actual diff content (may be verbose)
            if (mode === 'git_diff' && response.diff && response.diff.length < 2000) {
                html += '<div class="terminal-line"><pre style="margin:0;white-space:pre-wrap;font:var(--font-mono);font-size:11px;color:var(--text-secondary);line-height:1.4">' + escapeHtml(response.diff) + '</pre></div>';
            }
            // For nav_search and nav_tree, also show raw JSON (detailed data)
            if (mode === 'nav_search' || mode === 'nav_tree') {
                html += '<div class="terminal-line"><pre style="margin:0;white-space:pre-wrap;font:var(--font-mono);font-size:11px;color:var(--text-tertiary);line-height:1.4">' + escapeHtml(JSON.stringify(response, null, 2).substring(0, 3000)) + '</pre></div>';
            }
        } else {
        html += '<div class="terminal-line"><pre style="margin:0;white-space:pre-wrap;font:var(--font-mono);font-size:11px;color:var(--text-secondary);line-height:1.4">' + escapeHtml(JSON.stringify(response, null, 2)) + '</pre></div>';
    }

    els.terminal.insertAdjacentHTML('beforeend', html);
    els.terminal.scrollTop = els.terminal.scrollHeight;
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function summaryLine(mode, data) {
    if (mode === 'bulk_read') {
        const paths = data.files ? Object.keys(data.files) : [];
        const errors = data.errors ? Object.keys(data.errors) : [];
        let s = paths.length + ' file(s) read';
        if (errors.length) s += ', ' + errors.length + ' error(s)';
        return s;
    }
    if (mode === 'bulk_edit') {
        if (data.results) {
            const ok = data.results.filter(r => r.success).length;
            const fail = data.results.filter(r => !r.success).length;
            return data.total_operations + ' op(s) across ' + data.total_files + ' file(s) (' + ok + ' ok' + (fail ? ', ' + fail + ' failed' : '') + ')';
        }
        return 'Edit completed';
    }
    if (mode === 'bulk_execute') {
        return data.total_commands + ' command(s) in ' + (data.total_duration || 0).toFixed(2) + 's (' + (data.successful || 0) + ' ok, ' + (data.failed || 0) + ' failed)';
    }
    if (mode === 'git_backup') return data.success ? 'Backup: ' + (data.message || 'ok') : 'Backup failed';
    if (mode === 'git_commit') return data.success ? 'Commit: ' + (data.hash || data.message || 'ok') : 'Commit failed';
    if (mode === 'git_restore') return data.success ? 'Restored: ' + (data.message || 'ok') : 'Restore failed';
    if (mode === 'nav_tree') return (data.count || 0) + ' item(s)';
    if (mode === 'nav_search') return (data.total_count || 0) + ' match(es) in ' + (data.files_affected ? data.files_affected.length : 0) + ' file(s)';
    if (mode === 'nav_context') return data.context_id ? 'Context: ' + data.context_id : 'Created';
    if (mode === 'nav_bookmark') return data.status === 'added' ? 'Bookmarked' : 'Done';
    if (mode === 'tpl_list') return (data.count || 0) + ' template(s)';
    if (mode === 'tpl_render') return data.success ? 'Rendered: ' + data.target_path : 'Render failed';
    if (mode === 'tpl_run') return 'Exit: ' + (data.exit_code != null ? data.exit_code : '?') + ' in ' + (data.duration || 0) + 's';
    if (mode === 'tpl_scaffold') return (data.files_created ? data.files_created.length : 0) + ' file(s) created';
    if (mode === 'tpl_generate') return data.code ? (data.code.split('\n').length + ' line(s) generated') : 'No output';
    if (mode === 'tpl_complete') return data.completion ? 'Completion ready' : 'No suggestion';
    if (mode === 'obs_analytics') return data.files ? 'Analytics: ' + data.files.total_files + ' files, ' + (data.code ? data.code.python_lines_of_code + ' LOC' : '') : 'No data';
    if (mode === 'obs_metrics') return data._raw ? (data._raw.split('\n').filter(function(l) { return l && !l.startsWith('#'); }).length + ' metric(s)') : 'No data';
    if (mode === 'obs_journal') return (data.stdout ? data.stdout.split('\n').length : 0) + ' line(s), exit ' + (data.exit_code != null ? data.exit_code : '?');
    if (mode === 'obs_docker') return (data.stdout ? data.stdout.split('\n').length : 0) + ' line(s), exit ' + (data.exit_code != null ? data.exit_code : '?');
    if (mode === 'obs_webhooks') return (data.count || 0) + ' webhook(s)';
    if (mode === 'obs_hooks') return (data.count || 0) + ' hook(s)';
    if (mode === 'obs_hooks') return (data.count || 0) + ' hook(s)';
    if (mode === 'rec_backups') return (data.count || 0) + ' backup(s)';
    if (mode === 'rec_snapshots') return (data.count || 0) + ' snapshot(s)';
    if (mode === 'rec_hosts') return (data.hosts ? data.hosts.length : 0) + ' host(s)';
    if (mode === 'rec_backup') return data.success ? 'Backup: ' + (data.backup_id || data.message || 'ok') : 'Backup failed';
    if (mode === 'rec_restore') return data.success ? 'Restored: ' + data.message : 'Restore failed';
    return 'Completed';
}

// Bind bulk modal events (also handles git buttons)
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.bulk-btn, .git-btn, .nav-btn, .tpl-btn, .obs-btn, .rec-btn').forEach(btn => {
        btn.addEventListener('click', () => openBulkModal(btn.dataset.mode));
    });

    const el = getBulkEls();
    if (el.close) el.close.addEventListener('click', closeBulkModal);
    if (el.overlay) el.overlay.addEventListener('click', (e) => { if (e.target === el.overlay) closeBulkModal(); });
    if (el.validateBtn) el.validateBtn.addEventListener('click', validateBulkJson);
    if (el.executeBtn) el.executeBtn.addEventListener('click', () => {
        const parsed = validateBulkJson();
        if (parsed) executeBulkRequest(parsed);
    });
    if (el.loadExample) el.loadExample.addEventListener('click', () => {
        const tpl = BULK_TEMPLATES[currentBulkMode];
        const el2 = getBulkEls();
        el2.textarea.value = fillSessionId(tpl.body);
        el2.validation.textContent = '';
        el2.validation.className = 'bulk-validation';
    });
    // Keyboard shortcut: Enter in textarea when mod is held
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && document.getElementById('bulkOverlay').style.display === 'flex') {
            e.preventDefault();
            const parsed = validateBulkJson();
            if (parsed) executeBulkRequest(parsed);
        }
    });

    // === SSH Trust Flow ===
    const trustIndicator = document.getElementById('trustIndicator');
    const trustText = document.getElementById('trustText');
    const checkTrustBtn = document.getElementById('checkTrustBtn');
    const trustChangedBanner = document.getElementById('trustChangedBanner');
    const hostInput = document.getElementById('host');
    const portInput = document.getElementById('port');
    const connectBtn = document.getElementById('connectBtn');

    let trustState = 'unknown';
    let trustDebounceTimer = null;

    function setTrustState(state, detail) {
        trustState = state;
        trustIndicator.className = 'trust-indicator';
        if (state === 'known') {
            trustIndicator.classList.add('trust-known');
            var fp = detail ? ' — ' + detail.substring(0, 20) + String.fromCharCode(8230) : '';
            trustText.textContent = 'Trusted' + fp;
            connectBtn.disabled = false;
            trustChangedBanner.classList.remove('visible');
        } else if (state === 'unknown') {
            trustIndicator.classList.add('trust-unknown');
            trustText.textContent = 'Host not in known-hosts yet';
            connectBtn.disabled = false;
            trustChangedBanner.classList.remove('visible');
        } else if (state === 'changed') {
            trustIndicator.classList.add('trust-changed');
            trustText.textContent = 'Host key CHANGED — Connect blocked';
            connectBtn.disabled = true;
            trustChangedBanner.classList.add('visible');
        }
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }

    function doTrustCheck() {
        var host = hostInput ? hostInput.value.trim() : '';
        var port = portInput ? (portInput.value.trim() || '22') : '22';
        if (!host) {
            setTrustState('unknown');
            return;
        }
        fetch('/api/known-hosts/check?host=' + encodeURIComponent(host) + '&port=' + encodeURIComponent(port), {
            credentials: 'include',
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            setTrustState(data.status === 'known' ? 'known' : 'unknown');
        })
        .catch(function() {
            // silently fail — trust state stays as-is
        });
    }

    function scheduleTrustCheck() {
        if (trustDebounceTimer) clearTimeout(trustDebounceTimer);
        trustDebounceTimer = setTimeout(doTrustCheck, 800);
    }

    if (hostInput) hostInput.addEventListener('blur', scheduleTrustCheck);
    if (portInput) portInput.addEventListener('blur', scheduleTrustCheck);

    if (checkTrustBtn) {
        checkTrustBtn.addEventListener('click', function(e) {
            e.preventDefault();
            doTrustCheck();
        });
    }

    // Connect error handling — detect changed key via mutation observer on errorMessage
    (function() {
        var errorMsg = document.getElementById('errorMessage');
        var errorText = errorMsg ? errorMsg.querySelector('.error-text') : null;
        if (!errorText) return;
        var observer = new MutationObserver(function() {
            var msg = (errorText.textContent || '').toLowerCase();
            if (msg.indexOf('changed') !== -1 || msg.indexOf('mitm') !== -1 || msg.indexOf('host key mismatch') !== -1) {
                setTrustState('changed');
                observer.disconnect();
            } else if (msg.indexOf('unknown host') !== -1 || msg.indexOf('not found in known_hosts') !== -1) {
                setTrustState('unknown');
                observer.disconnect();
            }
        });
        observer.observe(errorText, { childList: true, characterData: true, subtree: true });
    })();

    // === Known Hosts inline sub-block ===
    var khList = document.getElementById('khList');
    var khCount = document.getElementById('khCount');
    var khRefreshBtn = document.getElementById('khRefreshBtn');
    var khClearAllBtn = document.getElementById('khClearAllBtn');

    function appendTerminal(mode, text, label) {
        var els2 = getBulkEls();
        var time = new Date().toLocaleTimeString();
        var html = '<div class="terminal-line" style="border-top:1px solid var(--border-subtle);padding-top:6px;margin-top:6px">';
        html += '<span style="color:var(--accent-info)">[' + time + ']</span> ';
        html += '<strong>' + escapeHtml(label || mode) + '</strong></div>';
        html += '<div class="terminal-line"><pre style="margin:0;white-space:pre-wrap;font:var(--font-mono);font-size:11px;color:var(--text-secondary);line-height:1.4">' + escapeHtml(text) + '</pre></div>';
        if (els2.terminal) {
            els2.terminal.insertAdjacentHTML('beforeend', html);
            els2.terminal.scrollTop = els2.terminal.scrollHeight;
        }
    }

    function renderKnownHosts() {
        fetch('/api/known-hosts', { credentials: 'include' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var hosts = data.hosts || [];
            if (khCount) khCount.textContent = '(' + hosts.length + ')';
            if (!khList) return;
            if (!hosts.length) {
                khList.innerHTML = '<div class="kh-empty">No known hosts yet.</div>';
                return;
            }
            var html = '';
            for (var i = 0; i < hosts.length; i++) {
                var h = hosts[i];
                var hostPort = h.host + ':' + (h.port || 22);
                var label = h.key_type || 'ssh-key';
                html += '<div class="kh-item" data-host="' + escapeAttr(h.host) + '" data-port="' + (h.port || 22) + '">';
                html += '  <span class="kh-item-host" title="' + escapeAttr(hostPort + ' (' + (h.fingerprint || '') + ')') + '">' + escapeHtml(hostPort) + ' <span style="color:var(--text-tertiary)">(' + escapeHtml(label) + ')</span></span>';
                html += '  <div class="kh-item-actions">';
                html += '    <button type="button" class="btn btn-outline btn-compact kh-view-btn" title="View fingerprint">View</button>';
                html += '    <button type="button" class="btn btn-outline btn-compact kh-del-btn" title="Delete entry">Del</button>';
                html += '  </div>';
                html += '</div>';
            }
            khList.innerHTML = html;

            khList.querySelectorAll('.kh-view-btn').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    var item = this.closest('.kh-item');
                    var host = item.dataset.host;
                    var port = item.dataset.port;
                    fetch('/api/known-hosts/' + encodeURIComponent(host) + '?port=' + encodeURIComponent(port), {
                        credentials: 'include',
                    })
                    .then(function(r) { return r.json(); })
                    .then(function(entry) {
                        var out = 'Known Host: ' + entry.host + ':' + entry.port;
                        out += '\n  Key Type: ' + (entry.key_type || '?');
                        out += '\n  Fingerprint: ' + (entry.fingerprint || '?');
                        appendTerminal('known-hosts', out, 'View: ' + host + ':' + port);
                    })
                    .catch(function() { appendTerminal('known-hosts', 'Failed to load entry', 'Error'); });
                });
            });

            khList.querySelectorAll('.kh-del-btn').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    var item = this.closest('.kh-item');
                    var host = item.dataset.host;
                    var port = item.dataset.port;
                    if (!confirm('Delete ' + host + ':' + port + ' from known hosts?')) return;
                    fetch('/api/known-hosts/' + encodeURIComponent(host) + '?port=' + encodeURIComponent(port), {
                        method: 'DELETE',
                        credentials: 'include',
                    })
                    .then(function(r) {
                        if (!r.ok) throw new Error('Delete failed');
                        return r.json();
                    })
                    .then(function() {
                        appendTerminal('known-hosts', 'Deleted ' + host + ':' + port, 'Delete');
                        renderKnownHosts();
                    })
                    .catch(function() { appendTerminal('known-hosts', 'Failed to delete ' + host + ':' + port, 'Error'); });
                });
            });
        })
        .catch(function() {
            if (khList) khList.innerHTML = '<div class="kh-empty" style="color:var(--accent-danger)">Failed to load known hosts.</div>';
        });
    }

    if (khClearAllBtn) {
        khClearAllBtn.addEventListener('click', function() {
            if (!confirm('Clear ALL known hosts? This cannot be undone. All hosts will become unknown on next connect.')) return;
            fetch('/api/known-hosts', {
                method: 'DELETE',
                credentials: 'include',
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                appendTerminal('known-hosts', 'Cleared ' + data.deleted + ' known host(s)', 'Clear All');
                renderKnownHosts();
                setTrustState('unknown');
            })
            .catch(function() { appendTerminal('known-hosts', 'Failed to clear known hosts', 'Error'); });
        });
    }

    if (khRefreshBtn) {
        khRefreshBtn.addEventListener('click', function() {
            renderKnownHosts();
        });
    }

    renderKnownHosts();
});
