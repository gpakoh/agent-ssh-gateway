/**
 * Web SSH Gateway — Frontend Application
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
// Jobs Panel
// ============================================

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

        els.jobsList.innerHTML = '';
        for (const job of state.jobs.slice(0, 20)) {
            const card = document.createElement('div');
            card.className = 'job-card';
            const statusColor = job.status === 'completed' ? 'ansi-green' :
                               job.status === 'failed' ? 'ansi-red' :
                               job.status === 'running' ? 'ansi-yellow' : 'ansi-blue';
            card.innerHTML = `
                <div class="job-id">${escapeHtml(job.job_id.slice(0, 8))}</div>
                <div class="job-cmd" title="${escapeHtml(job.command)}">${escapeHtml(job.command.slice(0, 40))}${job.command.length > 40 ? '...' : ''}</div>
                <div class="job-status ${statusColor}">${job.status}</div>
                <div class="job-actions">
                    ${job.status === 'running' ? `<button class="btn btn-danger btn-sm" data-jid="${job.job_id}">Cancel</button>` : ''}
                    <button class="btn btn-icon btn-sm" data-jid="${job.job_id}" data-action="view">View</button>
                </div>
            `;

            card.querySelectorAll('button').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    const jid = e.target.dataset.jid;
                    const action = e.target.dataset.action;
                    if (action === 'view') {
                        await viewJobResult(jid);
                    } else {
                        try {
                            await apiJobCancel(jid);
                            showToast('Job cancelled', 'info');
                            refreshJobs();
                        } catch (err) {
                            showToast(err.message, 'error');
                        }
                    }
                });
            });
            els.jobsList.appendChild(card);
        }
    } catch (err) {
        console.error('Failed to refresh jobs:', err);
    }
}

async function viewJobResult(jobId) {
    try {
        const result = await apiJobResult(jobId);
        clearTerminal();
        appendLine(`=== Job ${jobId} ===`, 'system');
        appendLine(`Command: ${result.command}`, 'system');
        appendLine(`Status: ${result.status}`, 'system');
        appendLine(`Exit code: ${result.exit_code}`, 'system');
        appendLine(`Duration: ${result.duration}s`, 'system');
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

// Refresh jobs periodically
setInterval(refreshJobs, 15000);

// ============================================
// Sessions Panel
// ============================================

async function refreshSessions() {
    try {
        const data = await apiSessions();
        state.sessions = data.sessions;

        if (state.sessions.length === 0) {
            els.sessionsList.innerHTML = '<p class="empty-text">No active sessions</p>';
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
console.log('%cWeb SSH Gateway%c v1.0', 'color:#00ff88;font-weight:bold;', 'color:#888;');

// ============================================
// Monaco Editor Integration
// ============================================

let monacoEditor = null;
let currentFilePath = null;

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
        
        // Enable save button when editor has content
        monacoEditor.onDidChangeModelContent(() => {
            const saveBtn = document.getElementById('saveFileBtn');
            if (saveBtn) saveBtn.disabled = false;
        });
    });
}

// Load file into editor
async function loadFileIntoEditor(path, content) {
    if (!monacoEditor) {
        showToast('Editor not initialized', 'error');
        return;
    }
    
    currentFilePath = path;
    
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
    
    document.getElementById('editorFilename').textContent = path;
    document.getElementById('editorPanel').style.display = 'block';
    document.getElementById('saveFileBtn').disabled = true;
}

// Save file from editor
async function saveFileFromEditor() {
    if (!monacoEditor || !currentFilePath || !state.sessionId) {
        showToast('No file to save', 'error');
        return;
    }
    
    const content = monacoEditor.getValue();
    
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
            showToast(`File saved: ${currentFilePath}`, 'success');
            document.getElementById('saveFileBtn').disabled = true;
        } else {
            const err = await res.json();
            showToast(`Save failed: ${err.detail}`, 'error');
        }
    } catch (e) {
        showToast('Save failed', 'error');
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
    });
});

// Expose editor functions globally
window.loadFileIntoEditor = loadFileIntoEditor;
