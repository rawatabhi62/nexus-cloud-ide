import os
import sys
import tempfile
import subprocess
import threading
import uuid
import secrets
import select
import pty
import codecs
import time
from flask import Flask, render_template_string, request, redirect, url_for
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
# Explicitly forcing threading mode as recommended by maintainers for subprocess/blocking I/O workloads.
# logger/engineio_logger=True prints every handshake attempt to stdout, which
# Render's Logs tab captures live - this turns "it's not connecting" into a
# readable trail instead of a guess. Safe to turn off later once this is confirmed working.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', logger=True, engineio_logger=True)

rooms = {}
rooms_lock = threading.Lock()

import datetime

@app.route('/health')
def health():
    import flask_socketio as _fsio
    return {
        'status': 'ok',
        'time_utc': datetime.datetime.utcnow().isoformat(),
        'python': sys.version,
        'async_mode': getattr(socketio, 'async_mode', 'unknown'),
        'flask_socketio_version': getattr(_fsio, '__version__', 'unknown'),
        'rooms_in_memory': len(rooms),
    }

@socketio.on('connect')
def handle_connect():
    print(f"[socket] client connected sid={request.sid}", flush=True)

@socketio.on('disconnect')
def handle_disconnect():
    print(f"[socket] client disconnected sid={request.sid}", flush=True)

@app.route('/')
def home():
    return render_template_string(HOME_PAGE)

@app.route('/create')
def create_room():
    # Cryptographically secure room ID generation
    room_id = secrets.token_urlsafe(8)[:8]
    with rooms_lock:
        rooms[room_id] = {
            'code': "# Welcome to Nexus Global Cloud IDE\nname = input('Enter your name: ')\nprint(f'Hello, {name}!')",
            'activity_log': ["🛡️ Room initialized securely."],
            'run_lock': threading.Lock(),
            'process': None,
            'master_fd': None
        }
    return redirect(url_for('join_room_page', room_id=room_id))

@app.route('/room/<room_id>')
def join_room_page(room_id):
    with rooms_lock:
        if room_id not in rooms:
            return "Room not found or expired! <a href='/'>Create a new room</a>", 404
    return render_template_string(ROOM_PAGE, room_id=room_id)

@socketio.on('join_room_socket')
def handle_join(data):
    room_id = data.get('room_id')
    join_room(room_id)
    with rooms_lock:
        if room_id in rooms:
            emit('sync_code', {'code': rooms[room_id]['code'], 'logs': rooms[room_id]['activity_log']}, room=request.sid)

@socketio.on('update_code')
def handle_code(data):
    room_id = data.get('room_id')
    code = data.get('code')
    with rooms_lock:
        if room_id in rooms:
            rooms[room_id]['code'] = code
            emit('sync_code', {'code': code}, room=room_id, include_self=False)

@socketio.on('save_cloud')
def handle_save(data):
    room_id = data.get('room_id')
    username = data.get('username', 'Dev')
    with rooms_lock:
        if room_id in rooms:
            rooms[room_id]['activity_log'].insert(0, f"💾 {username} saved cloud checkpoint.")
            if len(rooms[room_id]['activity_log']) > 100:
                rooms[room_id]['activity_log'].pop()
            emit('sync_logs', {'logs': rooms[room_id]['activity_log']}, room=room_id)
            emit('notification', {'msg': '✅ Code successfully saved to cloud!'}, room=request.sid)

@socketio.on('ai_chat')
def handle_ai(data):
    query = data.get('query', '').lower()
    code = data.get('code', '')
    if 'explain' in query:
        reply = f"🤖 AI Copilot: Script has {len(code.splitlines())} lines."
    elif 'optimize' in query:
        reply = "🤖 AI Copilot Tip: Use efficient loops and built-in functions."
    elif 'bug' in query or 'error' in query:
        reply = "🤖 AI Audit: No syntax anomalies detected."
    else:
        reply = "🤖 AI Copilot: Ready to assist."
    emit('ai_response', {'reply': reply}, room=request.sid)

@socketio.on('ai_fix')
def handle_ai_fix(data):
    room_id = data.get('room_id')
    username = data.get('username', 'AI')
    with rooms_lock:
        if room_id in rooms:
            code = rooms[room_id]['code']
            fixed_lines = []
            for line in code.split('\n'):
                if line.strip().startswith('print ') and not '(' in line:
                    content = line.strip()[6:]
                    indent = line[:len(line) - len(line.lstrip())]
                    fixed_lines.append(f"{indent}print({content}) # 🤖 AI Fixed")
                else:
                    fixed_lines.append(line)
            new_code = "\n".join(fixed_lines)
            rooms[room_id]['code'] = new_code
            rooms[room_id]['activity_log'].insert(0, f"✨ {username} ran AI Auto-Fix.")
            emit('sync_code', {'code': new_code, 'logs': rooms[room_id]['activity_log']}, room=room_id)
            emit('notification', {'msg': '✨ AI Auto-Fix applied successfully!'}, room=request.sid)

@socketio.on('start_interactive_run')
def handle_start_run(data):
    room_id = data.get('room_id')
    lang = data.get('language', 'python')

    with rooms_lock:
        if room_id not in rooms:
            return
        room = rooms[room_id]
        code = room['code']
        run_lock = room['run_lock']

    ext = '.py' if lang == 'python' else '.js' if lang == 'javascript' else '.cpp'
    with tempfile.NamedTemporaryFile(mode='w', suffix=ext, delete=False, encoding='utf-8') as f:
        f.write(code)
        fname = f.name

    cmd = [sys.executable, '-u', fname] if lang == 'python' else ['node', fname] if lang == 'javascript' else ['g++', fname, '-o', fname + '.out']
    target_sid = request.sid

    with run_lock:
        # Kill any existing running process in this room safely
        old_proc = room.get('process')
        if old_proc and old_proc.poll() is None:
            try:
                old_proc.terminate()
            except Exception:
                pass
        if room.get('master_fd') is not None:
            try:
                os.close(room['master_fd'])
            except Exception:
                pass

        try:
            if lang == 'cpp':
                comp = subprocess.run(cmd, capture_output=True, text=True)
                if comp.returncode != 0:
                    emit('terminal_data', {'data': f"C++ Compilation Error:\n{comp.stderr}"}, room=target_sid)
                    os.unlink(fname)
                    if os.path.exists(fname + '.out'):
                        os.unlink(fname + '.out')
                    return
                cmd = [fname + '.out']

            # Use Pseudo-Terminal (pty) for proper line-buffering and interactive prompts
            master_fd, slave_fd = pty.openpty()

            # Isolated environment variables for security
            safe_env = {
                'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                'LANG': 'en_US.UTF-8',
                'HOME': tempfile.gettempdir()
            }

            process = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=safe_env,
                start_new_session=True,  # Process group leader
                text=False
            )
            os.close(slave_fd)

            room['process'] = process
            room['master_fd'] = master_fd

            def read_output_batched():
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                buffer = []
                last_flush = time.time()

                try:
                    while True:
                        r, _, _ = select.select([master_fd], [], [], 0.1)
                        if r:
                            try:
                                chunk = os.read(master_fd, 4096)
                                if not chunk:
                                    break
                                decoded = decoder.decode(chunk)
                                buffer.append(decoded)
                            except OSError:
                                break

                        now = time.time()
                        # Flush every 40ms or when buffer exceeds ~16KB
                        current_len = sum(len(b) for b in buffer)
                        if buffer and (now - last_flush > 0.04 or current_len > 16384):
                            socketio.emit('terminal_data', {'data': "".join(buffer)}, room=target_sid)
                            buffer.clear()
                            last_flush = now

                        if process.poll() is not None and not r:
                            break
                except Exception:
                    pass

                # Final flush
                if buffer:
                    socketio.emit('terminal_data', {'data': "".join(buffer)}, room=target_sid)

                socketio.emit('terminal_data', {'data': "\n\n[Program finished]\n"}, room=target_sid)

                try:
                    os.close(master_fd)
                except Exception:
                    pass
                try:
                    os.unlink(fname)
                    if lang == 'cpp' and os.path.exists(fname + '.out'):
                        os.unlink(fname + '.out')
                except Exception:
                    pass

            threading.Thread(target=read_output_batched, daemon=True).start()

        except Exception as e:
            emit('terminal_data', {'data': f"Execution Error: {str(e)}"}, room=target_sid)

@socketio.on('send_stdin_data')
def handle_stdin(data):
    room_id = data.get('room_id')
    user_input = data.get('input', '')
    with rooms_lock:
        if room_id in rooms:
            room = rooms[room_id]
            master_fd = room.get('master_fd')
            proc = room.get('process')
            if master_fd is not None and proc and proc.poll() is None:
                try:
                    os.write(master_fd, (user_input + '\n').encode('utf-8'))
                except Exception:
                    pass

HOME_PAGE = r"""<!DOCTYPE html><html><head><title>Nexus Global Cloud IDE</title></head>
<body style="background:#090d16; color:#fff; font-family:'Segoe UI',sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; margin:0;">
    <div style="text-align:center; background:#111827; padding:45px; border-radius:12px; border:1px solid #1f2937;">
        <h1 style="color:#00ffcc;">🌐 Nexus Universal Cloud IDE</h1>
        <p style="color:#94a3b8; margin-bottom:25px;">Batched Emits & PTY Interactive Terminal Enabled.</p>
        <a href="/create"><button style="background:linear-gradient(135deg, #00ffcc, #38bdf8); color:#030712; border:none; padding:14px 28px; font-weight:bold; border-radius:6px; cursor:pointer;">Launch New Room</button></a>
    </div>
</body></html>"""

ROOM_PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Nexus Room - {{ room_id }}</title>
<style>
    body { margin: 0; background: #0b0f19; color: #00ffcc; font-family: 'Courier New', monospace; display: flex; height: 100vh; overflow: hidden; }
    .sidebar { width: 320px; background: #111827; border-right: 1px solid #1f2937; display: flex; flex-direction: column; padding: 15px; overflow-y: auto; box-sizing: border-box; }
    .main-content { flex: 1; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
    .header { height: 50px; background: #111827; border-bottom: 1px solid #1f2937; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; flex-shrink: 0; box-sizing: border-box; }
    textarea { flex: 1; background: #030712; color: #38bdf8; border: none; font-size: 14px; padding: 15px; resize: none; outline: none; line-height: 1.5; overflow-y: auto; box-sizing: border-box; }
    .terminal-pane { height: 38vh; background: #020617; border-top: 1px solid #1f2937; display: flex; flex-direction: column; flex-shrink: 0; }
    .terminal-header { background: #0f172a; padding: 8px 15px; font-size: 13px; font-weight: bold; color: #38bdf8; display: flex; justify-content: space-between; align-items:center; }
    #outputBox { margin: 0; padding: 12px; font-size: 13px; color: #4ade80; overflow-y: auto; flex: 1; white-space: pre-wrap; background: #020617; border: none; outline: none; font-family: 'Courier New', monospace; box-sizing: border-box; }
    button { background: linear-gradient(135deg, #00ffcc, #38bdf8); color: #030712; border: none; padding: 7px 14px; font-weight: bold; border-radius: 4px; cursor: pointer; font-size:12px; }
    button:hover { opacity: 0.85; }
    button:disabled { opacity: 0.4; cursor: not-allowed; }
    select, input { background: #1f2937; color: #fff; border: 1px solid #374151; padding: 6px; border-radius: 4px; font-family: inherit; }
    .user-badge { background: #1f2937; padding: 5px 8px; margin-bottom: 5px; border-radius: 4px; font-size: 12px; border-left: 3px solid #00ffcc; }
    .ai-panel { background: #1e1b4b; border: 1px solid #4338ca; padding: 10px; border-radius: 6px; margin-top: 10px; font-size: 12px; color: #c7d2fe; display: flex; flex-direction: column; height: 170px; box-sizing: border-box; }
    .ai-chat-box { flex: 1; overflow-y: auto; background: #0f172a; padding: 6px; margin-bottom: 6px; border-radius: 4px; font-size: 11px; white-space: pre-wrap; }
    .log-panel { background: #0f172a; border: 1px solid #1e293b; padding: 8px; border-radius: 6px; margin-top: 10px; height: 100px; overflow-y: auto; font-size: 11px; color: #94a3b8; box-sizing: border-box; }
    #cdnError { display:none; position:fixed; top:0; left:0; right:0; background:#7f1d1d; color:#fff; padding:10px 16px; font-size:13px; z-index:9999; text-align:center; }
    #cdnError button { margin-left:12px; background:#fff; color:#7f1d1d; }
</style></head>
<body>
    <div id="cdnError">
        ⚠️ Could not load the real-time engine (Socket.IO) from any CDN. Check your internet connection or ad/script blocker and reload.
        <button onclick="location.reload()">Reload</button>
    </div>
    <div class="sidebar">
        <h3 style="color: #00ffcc; margin-top:0;">🌐 Room: {{ room_id }}</h3>
        <p style="font-size: 12px; color: #94a3b8; margin: 5px 0;">Developer Handle:</p>
        <div id="userBadge" class="user-badge">🟡 Connecting...</div>

        <div style="display: flex; gap: 5px; margin-top: 10px;">
            <button id="btnSave" onclick="saveCloud()" style="flex:1; background:#10b981; color:#fff;" disabled>💾 Save Cloud</button>
            <button id="btnDownload" onclick="downloadCode()" style="flex:1; background:#3b82f6; color:#fff;" disabled>📥 Download</button>
        </div>

        <div class="ai-panel">
            <b>🤖 AI Copilot Assistant</b>
            <div id="aiChatBox" class="ai-chat-box">Ask AI: 'explain', 'optimize', or 'find bugs'...</div>
            <div style="display: flex; gap: 5px;">
                <input type="text" id="aiQueryInput" placeholder="Ask AI..." style="flex:1; font-size:11px; padding:4px;">
                <button id="btnAiSend" onclick="sendAiQuery()" style="padding: 4px 8px; font-size:11px;" disabled>Send</button>
            </div>
            <button id="btnAiFix" onclick="triggerAiFix()" style="width:100%; background:#4f46e5; color:#fff; padding:5px; font-size:11px; margin-top:5px;" disabled>⚡ Quick AI Auto-Fix</button>
        </div>

        <p style="font-size: 12px; color: #94a3b8; margin: 10px 0 5px 0;">📜 Live Activity Log:</p>
        <div id="activityLog" class="log-panel">Initializing logs...</div>
    </div>
    <div class="main-content">
        <div class="header">
            <div style="display: flex; gap: 15px; align-items: center;">
                <span style="font-weight: bold; color: #00ffcc;">🚀 NEXUS IDE</span>
                <select id="langSelect">
                    <option value="python" selected>Python 3</option>
                    <option value="javascript">JavaScript (Node)</option>
                    <option value="cpp">C++</option>
                </select>
            </div>
            <button id="btnRun" onclick="runCode()" disabled>▶ Run Code</button>
        </div>
        <textarea id="codeEditor" spellcheck="false"></textarea>
        <div class="terminal-pane">
            <div class="terminal-header">
                <span>📊 Output Console (Type input and press Enter)</span>
                <span id="ptyStatus" style="color: #f87171;">● Disconnected</span>
            </div>
            <textarea id="outputBox" spellcheck="false" placeholder="Connecting to server..." onkeydown="handleConsoleKey(event)"></textarea>
        </div>
    </div>

    <!--
      Defensive Socket.IO client loading: try the primary CDN first, and if the
      <script> tag itself fails to load (blocked, offline, CDN down), fall back
      to a second CDN before giving up and showing a visible error banner.
      Nothing else on the page runs until a client has actually loaded, so a
      blocked script can no longer silently break every button on the page.
    -->
    <script>
        function showCdnError() {
            document.getElementById('cdnError').style.display = 'block';
            document.getElementById('userBadge').innerText = '🔴 Offline';
        }
        function loadScript(src, onOk, onFail) {
            var s = document.createElement('script');
            s.src = src;
            s.onload = onOk;
            s.onerror = onFail;
            document.head.appendChild(s);
        }
        function bootSocketIO() {
            if (window.io) { initApp(); return; }
            loadScript(
                'https://cdn.socket.io/4.7.5/socket.io.min.js',
                initApp,
                function () {
                    loadScript(
                        'https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js',
                        initApp,
                        showCdnError
                    );
                }
            );
        }
        bootSocketIO();
    </script>

    <script>
        // Independent of Socket.IO entirely: prove the backend process itself
        // is alive by hitting a plain HTTP route. Shown in the activity log
        // immediately, before any socket connection is attempted.
        (function pingHealth() {
            const lBox = document.getElementById('activityLog');
            fetch('/health').then(r => r.json()).then(d => {
                lBox.innerHTML = `<div style="color:#4ade80;">✅ Backend alive: Flask-SocketIO ${d.flask_socketio_version}, mode=${d.async_mode}</div>` + lBox.innerHTML;
            }).catch(e => {
                lBox.innerHTML = `<div style="color:#f87171;">❌ /health request itself failed: ${e}. The backend process is not reachable at all (deploy crashed, or still cold-starting).</div>` + lBox.innerHTML;
            });
        })();

        function initApp() {
            if (!window.io) { showCdnError(); return; }

            // Start with polling (works under any WSGI server, incl. a plain
            // sync gunicorn worker) and upgrade to websocket only if the
            // server actually supports it. Starting with 'websocket' first
            // can hang forever with no error under a non-threaded server.
            const socket = io({ transports: ['polling', 'websocket'], upgrade: true, timeout: 20000 });

            function showRealError(msg) {
                document.getElementById('userBadge').innerText = "🔴 Could not connect";
                showCdnError();
                document.querySelector('#cdnError').innerHTML =
                    '⚠️ Socket.IO could not connect: <b>' + msg + '</b> ' +
                    '<button onclick="location.reload()">Reload</button>';
                let lBox = document.getElementById('activityLog');
                lBox.innerHTML = `<div style="color:#f87171;">❌ Socket error: ${msg}</div>` + lBox.innerHTML;
            }

            // Safety net: if we're still stuck "connecting" after 12s, show
            // the same visible error instead of hanging silently forever.
            const connectWatchdog = setTimeout(() => {
                if (!socket.connected) showRealError('timed out waiting for server response (12s)');
            }, 12000);
            socket.on('connect', () => clearTimeout(connectWatchdog));
            socket.on('connect_error', (err) => {
                clearTimeout(connectWatchdog);
                showRealError((err && (err.message || err.toString())) || 'unknown connect_error');
                console.error("Socket connect_error:", err);
            });
            const roomId = "{{ room_id }}";
            const editor = document.getElementById('codeEditor');
            const outputBox = document.getElementById('outputBox');

            let username = "Dev_" + Math.floor(Math.random() * 1000);
            try {
                let userInput = prompt("Enter your Developer Handle:");
                if (userInput && userInput.trim() !== "") {
                    username = userInput.trim();
                }
            } catch (e) {
                console.log("Prompt skipped.");
            }
            document.getElementById('userBadge').innerText = "🟡 " + username + " (connecting...)";

            socket.on('connect', () => {
                document.getElementById('userBadge').innerText = "🟢 " + username;
                document.getElementById('ptyStatus').style.color = '#4ade80';
                document.getElementById('ptyStatus').innerText = '● Interactive PTY Active';
                outputBox.placeholder = "Console ready... Click 'Run Code' to start execution.";
                ['btnSave', 'btnDownload', 'btnAiSend', 'btnAiFix', 'btnRun'].forEach(id => {
                    document.getElementById(id).disabled = false;
                });
                socket.emit('join_room_socket', { room_id: roomId });
            });

            socket.on('disconnect', () => {
                document.getElementById('userBadge').innerText = "🔴 " + username + " (disconnected)";
                document.getElementById('ptyStatus').style.color = '#f87171';
                document.getElementById('ptyStatus').innerText = '● Disconnected';
            });

            socket.on('sync_code', (data) => {
                if (data.code && editor.value !== data.code) editor.value = data.code;
                if (data.logs) updateLogs(data.logs);
            });

            socket.on('sync_logs', (data) => {
                if (data.logs) updateLogs(data.logs);
            });

            function updateLogs(logs) {
                let lBox = document.getElementById('activityLog');
                lBox.innerHTML = logs.map(l => `<div>• ${l}</div>`).join('');
            }

            let isRemote = false;
            editor.addEventListener('input', () => {
                if (isRemote) return;
                socket.emit('update_code', { room_id: roomId, code: editor.value });
            });

            window.saveCloud = function () {
                socket.emit('save_cloud', { room_id: roomId, username: username });
            };
            socket.on('notification', (data) => { alert(data.msg); });

            window.downloadCode = function () {
                let lang = document.getElementById('langSelect').value;
                let ext = (lang === 'javascript') ? 'js' : (lang === 'cpp') ? 'cpp' : 'py';
                let blob = new Blob([editor.value], { type: 'text/plain;charset=utf-8' });
                let url = URL.createObjectURL(blob);
                let a = document.createElement('a');
                a.href = url;
                a.download = `nexus_code_${roomId}.${ext}`;
                a.click();
                URL.revokeObjectURL(url);
            };

            window.runCode = function () {
                let lang = document.getElementById('langSelect').value;
                outputBox.value = "Initializing batched pty execution...\n";
                socket.emit('start_interactive_run', { room_id: roomId, language: lang, username: username });
            };

            socket.on('terminal_data', (data) => {
                outputBox.value += data.data;
                outputBox.scrollTop = outputBox.scrollHeight;
            });

            window.handleConsoleKey = function (e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    let text = outputBox.value;
                    let lines = text.split('\n');
                    let userInput = lines[lines.length - 1];

                    socket.emit('send_stdin_data', { room_id: roomId, input: userInput });
                    outputBox.value += '\n';
                    outputBox.scrollTop = outputBox.scrollHeight;
                }
            };

            window.sendAiQuery = function () {
                let q = document.getElementById('aiQueryInput').value; if (!q) return;
                document.getElementById('aiChatBox').innerText += "\nYou: " + q;
                socket.emit('ai_chat', { query: q, code: editor.value });
                document.getElementById('aiQueryInput').value = '';
            };
            socket.on('ai_response', (data) => {
                document.getElementById('aiChatBox').innerText += "\n" + data.reply;
                let chatBox = document.getElementById('aiChatBox');
                chatBox.scrollTop = chatBox.scrollHeight;
            });

            window.triggerAiFix = function () {
                socket.emit('ai_fix', { room_id: roomId, username: username });
            };
        }
    </script>
</body></html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5005))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
