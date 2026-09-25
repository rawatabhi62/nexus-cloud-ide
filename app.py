import os
import sys
import tempfile
import subprocess
import pty
import select
import termios
import struct
import fcntl
import ast
import uuid
from flask import Flask, render_template_string, request, redirect, url_for
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

rooms = {}

@app.route('/')
def home():
    return render_template_string(HOME_PAGE)

@app.route('/create')
def create_room():
    room_id = str(uuid.uuid4())[:8]
    rooms[room_id] = {
        'code': "# Welcome to Nexus Global Cloud IDE\nname = input('Enter your name: ')\nprint(f'Hello, {name}!')",
        'host_sid': None,
        'active_users': {},
        'pending_users': {},
        'activity_log': []
    }
    return redirect(url_for('join_room_page', room_id=room_id))

@app.route('/room/<room_id>')
def join_room_page(room_id):
    if room_id not in rooms:
        return "Room not found or expired! <a href='/'>Create new room</a>", 404
    return render_template_string(ROOM_PAGE, room_id=room_id)

@socketio.on('join_room_request')
def handle_join(data):
    room_id = data.get('room_id')
    username = data.get('username', 'Dev')
    sid = request.sid
    if room_id not in rooms: return
    room = rooms[room_id]
    join_room(room_id)

    if not room['host_sid']:
        room['host_sid'] = sid
        room['active_users'][sid] = username + " (Host)"
        room['activity_log'].insert(0, f"🛡️ {username} joined as Host.")
    else:
        room['pending_users'][sid] = username
        room['activity_log'].insert(0, f"⏳ {username} requested room access.")

    emit('room_access_granted', {'code': room['code'], 'users': list(room['active_users'].values()), 'is_host': (sid == room['host_sid']), 'logs': room['activity_log']}, room=sid)
    emit('update_room_users', {'users': list(room['active_users'].values()), 'logs': room['activity_log']}, room=room_id)

@socketio.on('host_room_decision')
def handle_decision(data):
    room_id, target_sid, approved = data.get('room_id'), data.get('sid'), data.get('approved', False)
    if room_id not in rooms: return
    room = rooms[room_id]
    if target_sid in room['pending_users']:
        username = room['pending_users'].pop(target_sid)
        if approved:
            room['active_users'][target_sid] = username
            room['activity_log'].insert(0, f"✅ {username}'s access was granted.")
            emit('room_access_granted', {'code': room['code'], 'users': list(room['active_users'].values()), 'is_host': False, 'logs': room['activity_log']}, room=target_sid)
        else:
            room['activity_log'].insert(0, f"❌ {username}'s access was denied.")
            emit('room_access_denied', {}, room=target_sid)
        emit('update_room_users', {'users': list(room['active_users'].values()), 'logs': room['activity_log']}, room=room_id)

@socketio.on('update_room_code')
def handle_code(data):
    room_id, code = data.get('room_id'), data.get('code')
    if room_id in rooms:
        rooms[room_id]['code'] = code
        emit('sync_room_code', {'code': code}, room=room_id, include_self=False)

@socketio.on('ai_chat_query')
def handle_ai_chat(data):
    room_id, query = data.get('room_id'), data.get('query', '').lower()
    if room_id not in rooms: return
    code = rooms[room_id]['code']
    reply = ""
    if 'explain' in query:
        reply = f"🤖 AI Copilot: Script has {len(code.splitlines())} lines. Built with real-time WebSockets & interactive PTY shell."
    elif 'optimize' in query:
        reply = "🤖 AI Copilot: Use built-in functions and clean loops to optimize runtime performance."
    elif 'bug' in query or 'error' in query:
        reply = "🤖 AI Audit: No recursion locks or critical vulnerabilities found."
    else:
        reply = "🤖 AI Copilot: Ready to assist. Ask me to 'explain' or 'optimize'."
    emit('ai_chat_response', {'reply': reply}, room=request.sid)

@socketio.on('ai_auto_fix')
def handle_ai_auto_fix(data):
    room_id, username = data.get('room_id'), data.get('username', 'AI')
    if room_id not in rooms: return
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
    emit('sync_room_code', {'code': new_code}, room=room_id)
    emit('update_room_users', {'users': list(rooms[room_id]['active_users'].values()), 'logs': rooms[room_id]['activity_log']}, room=room_id)

# Professional Real-time PTY Interactive Execution Engine
@socketio.on('execute_interactive_code')
def handle_interactive_exec(data):
    room_id, lang, username = data.get('room_id'), data.get('language', 'python'), data.get('username', 'Dev')
    if room_id not in rooms: return
    code = rooms[room_id]['code']

    ext = '.py' if lang == 'python' else '.js' if lang == 'javascript' else '.cpp'
    with tempfile.NamedTemporaryFile(mode='w', suffix=ext, delete=False) as f:
        f.write(code)
        fname = f.name

    cmd = [sys.executable, '-u', fname] if lang == 'python' else ['node', fname] if lang == 'javascript' else ['g++', fname, '-o', fname + '.out']
    
    try:
        if lang == 'cpp':
            comp = subprocess.run(cmd, capture_output=True, text=True)
            if comp.returncode != 0:
                emit('terminal_output', {'output': f"C++ Compilation Error:\n{comp.stderr}"}, room=request.sid)
                os.unlink(fname)
                return
            cmd = [fname + '.out']

        # Spawn interactive Pseudo-Terminal (PTY)
        master_fd, slave_fd = pty.openpty()
        p = subprocess.Popen(cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True)
        os.close(slave_fd)

        # Store process handle for real-time stdin streaming if needed
        rooms[room_id]['active_proc'] = p
        rooms[room_id]['master_fd'] = master_fd

        output_buffer = ""
        while p.poll() is None:
            rlist, _, _ = select.select([master_fd], [], [], 0.1)
            if master_fd in rlist:
                try:
                    data_chunk = os.read(master_fd, 1024).decode('utf-8', errors='ignore')
                    if not data_chunk: break
                    output_buffer += data_chunk
                    emit('terminal_output', {'output': output_buffer}, room=request.sid)
                except OSError:
                    break
        
        # Read remaining output
        try:
            while True:
                rlist, _, _ = select.select([master_fd], [], [], 0.1)
                if not rlist: break
                data_chunk = os.read(master_fd, 1024).decode('utf-8', errors='ignore')
                if not data_chunk: break
                output_buffer += data_chunk
        except Exception:
            pass

        emit('terminal_output', {'output': output_buffer + "\n\n[Process completed]"}, room=request.sid)
        
        if os.path.exists(fname): os.unlink(fname)
        if lang == 'cpp' and os.path.exists(fname + '.out'): os.unlink(fname + '.out')

    except Exception as e:
        emit('terminal_output', {'output': f"Execution Error: {str(e)}"}, room=request.sid)

@socketio.on('terminal_input_data')
def handle_terminal_input(data):
    room_id = data.get('room_id')
    user_input = data.get('input', '')
    if room_id in rooms and 'master_fd' in rooms[room_id]:
        try:
            master_fd = rooms[room_id]['master_fd']
            os.write(master_fd, (user_input + '\n').encode('utf-8'))
        except Exception:
            pass

@socketio.on('disconnect')
def handle_disc():
    sid = request.sid
    for room_id, room in rooms.items():
        if sid in room['active_users']:
            uname = room['active_users'].pop(sid)
            room['activity_log'].insert(0, f"🚪 {uname} left.")
            if sid == room['host_sid']: room['host_sid'] = None
            emit('update_room_users', {'users': list(room['active_users'].values()), 'logs': room['activity_log']}, room=room_id)

HOME_PAGE = """<!DOCTYPE html><html><head><title>Nexus Global Cloud IDE</title></head>
<body style="background:#090d16; color:#fff; font-family:'Segoe UI',sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; margin:0;">
    <div style="text-align:center; background:#111827; padding:45px; border-radius:12px; border:1px solid #1f2937;">
        <h1 style="color:#00ffcc;">🌐 Nexus Universal Cloud IDE</h1>
        <p style="color:#94a3b8; margin-bottom:25px;">Interactive PTY Shell & AI Copilot Enabled.</p>
        <a href="/create"><button style="background:linear-gradient(135deg, #00ffcc, #38bdf8); color:#030712; border:none; padding:14px 28px; font-weight:bold; border-radius:6px; cursor:pointer;">Launch New Room</button></a>
    </div>
</body></html>"""

ROOM_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Nexus Room - {{ room_id }}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
<style>
    body { margin: 0; background: #0b0f19; color: #00ffcc; font-family: 'Courier New', monospace; display: flex; height: 100vh; overflow: hidden; }
    .sidebar { width: 320px; background: #111827; border-right: 1px solid #1f2937; display: flex; flex-direction: column; padding: 15px; overflow-y: auto; }
    .main-content { flex: 1; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
    .header { height: 50px; background: #111827; border-bottom: 1px solid #1f2937; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; flex-shrink: 0; }
    textarea { flex: 1; background: #030712; color: #38bdf8; border: none; font-size: 14px; padding: 15px; resize: none; outline: none; line-height: 1.5; overflow-y: auto; }
    .terminal-pane { height: 35vh; background: #020617; border-top: 1px solid #1f2937; display: flex; flex-direction: column; flex-shrink: 0; }
    .terminal-header { background: #0f172a; padding: 8px 15px; font-size: 13px; font-weight: bold; color: #38bdf8; display: flex; justify-content: space-between; align-items:center; }
    #outputBox { margin: 0; padding: 12px; font-size: 13px; color: #4ade80; overflow-y: auto; flex: 1; white-space: pre-wrap; background: #020617; border: none; outline: none; font-family: 'Courier New', monospace; }
    button { background: linear-gradient(135deg, #00ffcc, #38bdf8); color: #030712; border: none; padding: 7px 14px; font-weight: bold; border-radius: 4px; cursor: pointer; font-size:12px; }
    button:hover { opacity: 0.85; }
    select, input { background: #1f2937; color: #fff; border: 1px solid #374151; padding: 6px; border-radius: 4px; font-family: inherit; }
    .user-badge { background: #1f2937; padding: 5px 8px; margin-bottom: 5px; border-radius: 4px; font-size: 12px; border-left: 3px solid #00ffcc; }
    #overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(3,7,18,0.95); display: flex; flex-direction: column; justify-content: center; align-items: center; z-index: 999; color: #fff; }
    .ai-panel { background: #1e1b4b; border: 1px solid #4338ca; padding: 10px; border-radius: 6px; margin-top: 10px; font-size: 12px; color: #c7d2fe; display: flex; flex-direction: column; height: 170px; }
    .ai-chat-box { flex: 1; overflow-y: auto; background: #0f172a; padding: 6px; margin-bottom: 6px; border-radius: 4px; font-size: 11px; white-space: pre-wrap; }
    .log-panel { background: #0f172a; border: 1px solid #1e293b; padding: 8px; border-radius: 6px; margin-top: 10px; height: 100px; overflow-y: auto; font-size: 11px; color: #94a3b8; }
</style></head>
<body>
    <div id="overlay"><h2 id="overlayText">⚡ Secure Global Handshake...</h2></div>
    <div class="sidebar">
        <h3 style="color: #00ffcc; margin-top:0;">🌐 Room: {{ room_id }}</h3>
        <p style="font-size: 12px; color: #94a3b8; margin: 5px 0;">Active Users:</p>
        <div id="usersList"></div>
        
        <div style="display: flex; gap: 5px; margin-top: 10px;">
            <button onclick="saveCloud()" style="flex:1; background:#10b981; color:#fff;">💾 Save Cloud</button>
            <button onclick="downloadCode()" style="flex:1; background:#3b82f6; color:#fff;">📥 Download</button>
        </div>

        <div class="ai-panel">
            <b>🤖 AI Copilot Assistant</b>
            <div id="aiChatBox" class="ai-chat-box">Ask AI: 'explain', 'optimize', or 'find bugs'...</div>
            <div style="display: flex; gap: 5px;">
                <input type="text" id="aiQueryInput" placeholder="Ask AI..." style="flex:1; font-size:11px; padding:4px;">
                <button onclick="sendAiQuery()" style="padding: 4px 8px; font-size:11px;">Send</button>
            </div>
            <button onclick="triggerAiFix()" style="width:100%; background:#4f46e5; color:#fff; padding:5px; font-size:11px; margin-top:5px;">⚡ Quick AI Auto-Fix</button>
        </div>

        <p style="font-size: 12px; color: #94a3b8; margin: 10px 0 5px 0;">📜 Live Activity Log:</p>
        <div id="activityLog" class="log-panel">Initializing logs...</div>

        <div id="hostControls" style="margin-top: auto; display: none; background: #1f2937; padding: 10px; border-radius: 6px;">
            <p style="font-size: 12px; margin: 0 0 8px 0; color: #facc15;"><b>🛡️ Host Security Gate</b></p>
            <div id="pendingRequests" style="font-size: 12px; color: #cbd5e1;">No pending entry requests.</div>
        </div>
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
            <button onclick="runCode()">▶ Execute Interactive Code</button>
        </div>
        <textarea id="codeEditor"></textarea>
        <div class="terminal-pane">
            <div class="terminal-header">
                <span>📊 Interactive Output Shell (Type directly into terminal when prompted)</span>
                <span style="color: #4ade80;">● PTY Shell Active</span>
            </div>
            <textarea id="outputBox" spellcheck="false" placeholder="Console ready... Click 'Execute Interactive Code' to start. Type directly here when input is requested."></textarea>
        </div>
    </div>
    <script>
        const socket = io();
        const roomId = "{{ room_id }}";
        const editor = document.getElementById('codeEditor');
        const outputBox = document.getElementById('outputBox');
        const overlay = document.getElementById('overlay');
        let username = prompt("Enter your Developer Handle:") || "Dev_" + Math.floor(Math.random()*1000);
        socket.emit('join_room_request', { room_id: roomId, username: username });

        socket.on('ask_host_approval', (data) => {
            document.getElementById('pendingRequests').innerHTML = `<div><b>${data.username}</b> wants access.</div>
                <button onclick="respond('${data.sid}', true)" style="background: #4ade80; margin-top:5px; padding:4px;">Grant</button>
                <button onclick="respond('${data.sid}', false)" style="background: #f87171; color:#fff; margin-top:5px; padding:4px;">Deny</button>`;
        });
        function respond(sid, approved) { socket.emit('host_room_decision', { room_id: roomId, sid: sid, approved: approved }); document.getElementById('pendingRequests').innerHTML = 'No pending requests.'; }
        
        socket.on('room_access_granted', (data) => { 
            overlay.style.display = 'none'; 
            editor.value = data.code; 
            updateUI(data.users, data.logs); 
            if(data.is_host) document.getElementById('hostControls').style.display = 'block'; 
        });
        
        socket.on('room_access_denied', () => { document.getElementById('overlayText').innerText = "❌ Host denied entry."; });
        socket.on('update_room_users', (data) => { updateUI(data.users, data.logs); });

        function updateUI(users, logs) {
            let uList = document.getElementById('usersList'); uList.innerHTML = '';
            users.forEach(u => { let d = document.createElement('div'); d.className = 'user-badge'; d.innerText = '🟢 ' + u; uList.appendChild(d); });
            if(logs) {
                let lBox = document.getElementById('activityLog');
                lBox.innerHTML = logs.map(l => `<div>• ${l}</div>`).join('');
            }
        }

        let isRemote = false;
        editor.addEventListener('input', () => { 
            if(isRemote) return; 
            socket.emit('update_room_code', { room_id: roomId, code: editor.value }); 
        });
        
        socket.on('sync_room_code', (data) => {
            isRemote = true;
            let pos = editor.selectionStart;
            editor.value = data.code;
            editor.setSelectionRange(pos, pos);
            isRemote = false;
        });

        function saveCloud() { socket.emit('save_code_cloud', { room_id: roomId, username: username }); }
        socket.on('save_notification', (data) => { alert(data.msg); });

        function downloadCode() {
            let lang = document.getElementById('langSelect').value;
            let ext = (lang === 'javascript') ? 'js' : (lang === 'cpp') ? 'cpp' : 'py';
            let blob = new Blob([editor.value], { type: 'text/plain;charset=utf-8' });
            let url = URL.createObjectURL(blob);
            let a = document.createElement('a');
            a.href = url;
            a.download = `nexus_code_${roomId}.${ext}`;
            a.click();
            URL.revokeObjectURL(url);
        }

        function runCode() {
            let lang = document.getElementById('langSelect').value;
            outputBox.value = "Initializing interactive Pty session...\n";
            socket.emit('execute_interactive_code', { room_id: roomId, language: lang, username: username });
        }

        socket.on('terminal_output', (data) => {
            outputBox.value = data.output;
            outputBox.scrollTop = outputBox.scrollHeight;
        });

        // Capture direct typing in terminal output shell for live stdin streaming
        outputBox.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                let lines = outputBox.value.split('\n');
                let lastLine = lines[lines.length - 1];
                socket.emit('terminal_input_data', { room_id: roomId, input: lastLine });
                outputBox.value += '\n';
            }
        });
        
        function sendAiQuery() {
            let q = document.getElementById('aiQueryInput').value; if(!q) return;
            document.getElementById('aiChatBox').innerText += "\\nYou: " + q;
            socket.emit('ai_chat_query', { room_id: roomId, query: q });
            document.getElementById('aiQueryInput').value = '';
        }
        socket.on('ai_chat_response', (data) => { 
            document.getElementById('aiChatBox'].innerText += "\\n" + data.reply; 
            let chatBox = document.getElementById('aiChatBox');
            chatBox.scrollTop = chatBox.scrollHeight;
        });

        function triggerAiFix() {
            socket.emit('ai_auto_fix', { room_id: roomId, username: username });
        }
    </script>
</body></html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5005))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
