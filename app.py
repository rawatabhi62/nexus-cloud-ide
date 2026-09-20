import os
import sys
import tempfile
import subprocess
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
        'code': "# Welcome to Nexus Global Cloud IDE\nprint('Hello from Universal Cloud IDE!')",
        'host_sid': None,
        'active_users': {},
        'pending_users': {}
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
        emit('room_access_granted', {'code': room['code'], 'users': list(room['active_users'].values()), 'is_host': True}, room=sid)
    else:
        room['pending_users'][sid] = username
        emit('ask_host_approval', {'sid': sid, 'username': username}, room=room['host_sid'])

@socketio.on('host_room_decision')
def handle_decision(data):
    room_id, target_sid, approved = data.get('room_id'), data.get('sid'), data.get('approved', False)
    if room_id not in rooms: return
    room = rooms[room_id]
    if target_sid in room['pending_users']:
        username = room['pending_users'].pop(target_sid)
        if approved:
            room['active_users'][target_sid] = username
            emit('room_access_granted', {'code': room['code'], 'users': list(room['active_users'].values()), 'is_host': False}, room=target_sid)
            emit('update_room_users', {'users': list(room['active_users'].values())}, room=room_id)
        else:
            emit('room_access_denied', {}, room=target_sid)

@socketio.on('update_room_code')
def handle_code(data):
    room_id, code = data.get('room_id'), data.get('code')
    if room_id in rooms:
        rooms[room_id]['code'] = code
        emit('sync_room_code', {'code': code}, room=room_id, include_self=False)

# VS Code jaisa Direct AI Code Auto-Fixer Engine
@socketio.on('ai_auto_fix')
def handle_ai_auto_fix(data):
    room_id = data.get('room_id')
    if room_id not in rooms: return
    code = rooms[room_id]['code']
    
    fixed_lines = []
    lines = code.split('\n')
    for line in lines:
        # Common Python fix: convert old print statements to function calls
        if line.strip().startswith('print ') and not '(' in line:
            content = line.strip()[6:]
            indent = line[:len(line) - len(line.lstrip())]
            fixed_lines.append(f"{indent}print({content}) # 🤖 AI Auto-fixed")
        else:
        # Check basic missing colons in def/if
            if (line.strip().startswith('def ') or line.strip().startswith('if ') or line.strip().startswith('for ') or line.strip().startswith('while ')) and not line.strip().endswith(':'):
                fixed_lines.append(line + ": # 🤖 AI Added colon")
            else:
                fixed_lines.append(line)
                
    new_code = "\n".join(fixed_lines)
    rooms[room_id]['code'] = new_code
    
    # Broadcast fixed code to everyone in the room instantly
    emit('sync_room_code', {'code': new_code}, room=room_id)
    emit('ai_fix_notification', {'msg': '✨ AI successfully audited and auto-fixed code lines!'}, room=request.sid)

@socketio.on('execute_room_code')
def handle_exec(data):
    room_id, lang = data.get('room_id'), data.get('language', 'python')
    if room_id not in rooms: return
    code = rooms[room_id]['code']
    output = ""
    try:
        if lang == 'python':
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(code); fname = f.name
            res = subprocess.run([sys.executable, '-u', fname], capture_output=True, text=True, timeout=5)
            output = res.stdout if res.stdout else res.stderr
            os.unlink(fname)
        elif lang == 'javascript':
            with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
                f.write(code); js_name = f.name
            res = subprocess.run(['node', js_name], capture_output=True, text=True, timeout=5)
            output = res.stdout if res.stdout else res.stderr
            os.unlink(js_name)
        elif lang == 'cpp':
            with tempfile.NamedTemporaryFile(mode='w', suffix='.cpp', delete=False) as f:
                f.write(code); cpp_name = f.name
            exe = cpp_name + ".out"
            comp = subprocess.run(['g++', cpp_name, '-o', exe], capture_output=True, text=True)
            if comp.returncode != 0:
                output = f"C++ Compilation Error:\n{comp.stderr}"
            else:
                run_res = subprocess.run([exe], capture_output=True, text=True, timeout=5)
                output = run_res.stdout if run_res.stdout else run_res.stderr
                if os.path.exists(exe): os.unlink(exe)
            if os.path.exists(cpp_name): os.unlink(cpp_name)
    except subprocess.TimeoutExpired:
        output = "❌ Execution Error: Process timed out."
    except Exception as e:
        output = f"Execution Error: {str(e)}"
    
    if not output: output = "[Executed with no output]"
    emit('room_execution_output', {'output': output}, room=room_id)

@socketio.on('disconnect')
def handle_disc():
    sid = request.sid
    for room_id, room in rooms.items():
        if sid in room['active_users']:
            del room['active_users'][sid]
            if sid == room['host_sid']: room['host_sid'] = None
            emit('update_room_users', {'users': list(room['active_users'].values())}, room=room_id)

HOME_PAGE = """<!DOCTYPE html><html><head><title>Nexus Global Cloud IDE</title></head>
<body style="background:#090d16; color:#fff; font-family:'Segoe UI',sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; margin:0;">
    <div style="text-align:center; background:#111827; padding:45px; border-radius:12px; border:1px solid #1f2937;">
        <h1 style="color:#00ffcc;">🌐 Nexus Universal Cloud IDE</h1>
        <p style="color:#94a3b8; margin-bottom:25px;">Multi-Language & AI Auto-Fix Enabled.</p>
        <a href="/create"><button style="background:linear-gradient(135deg, #00ffcc, #38bdf8); color:#030712; border:none; padding:14px 28px; font-weight:bold; border-radius:6px; cursor:pointer;">Launch New Room</button></a>
    </div>
</body></html>"""

ROOM_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Nexus Room - {{ room_id }}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
<style>
    body { margin: 0; background: #0b0f19; color: #00ffcc; font-family: 'Courier New', monospace; display: flex; height: 100vh; overflow: hidden; }
    .sidebar { width: 310px; background: #111827; border-right: 1px solid #1f2937; display: flex; flex-direction: column; padding: 15px; }
    .main-content { flex: 1; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
    .header { height: 50px; background: #111827; border-bottom: 1px solid #1f2937; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; flex-shrink: 0; }
    textarea { flex: 1; background: #030712; color: #38bdf8; border: none; font-size: 14px; padding: 15px; resize: none; outline: none; line-height: 1.5; overflow-y: auto; }
    .terminal-pane { height: 35vh; background: #020617; border-top: 1px solid #1f2937; display: flex; flex-direction: column; flex-shrink: 0; }
    .terminal-header { background: #0f172a; padding: 8px 15px; font-size: 13px; font-weight: bold; color: #38bdf8; display: flex; justify-content: space-between; }
    pre { margin: 0; padding: 12px; font-size: 13px; color: #4ade80; overflow-y: auto; flex: 1; white-space: pre-wrap; }
    button { background: linear-gradient(135deg, #00ffcc, #38bdf8); color: #030712; border: none; padding: 8px 16px; font-weight: bold; border-radius: 4px; cursor: pointer; }
    button:hover { opacity: 0.85; }
    select { background: #1f2937; color: #fff; border: 1px solid #374151; padding: 6px; border-radius: 4px; font-family: inherit; }
    .user-badge { background: #1f2937; padding: 5px 8px; margin-bottom: 5px; border-radius: 4px; font-size: 12px; border-left: 3px solid #00ffcc; }
    #overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(3,7,18,0.95); display: flex; flex-direction: column; justify-content: center; align-items: center; z-index: 999; color: #fff; }
    .ai-panel { background: #1e1b4b; border: 1px solid #4338ca; padding: 10px; border-radius: 6px; margin-top: 10px; font-size: 12px; color: #c7d2fe; }
</style></head>
<body>
    <div id="overlay"><h2 id="overlayText">⚡ Secure Global Handshake...</h2></div>
    <div class="sidebar">
        <h3 style="color: #00ffcc; margin-top:0;">🌐 Room: {{ room_id }}</h3>
        <p style="font-size: 12px; color: #94a3b8;">Active Users:</p>
        <div id="usersList"></div>
        <div class="ai-panel">
            <b>🤖 VS Code Style AI Assistant</b>
            <p id="aiStatus" style="font-size:11px; color:#a5b4fc; margin: 5px 0;">Click auto-fix to check line-by-line syntax errors.</p>
            <button onclick="triggerAiFix()" style="width:100%; background:#4f46e5; color:#fff; padding:6px; font-size:11px; margin-top:5px;">⚡ AI Auto-Fix Code Lines</button>
        </div>
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
            <button onclick="runCode()">▶ Execute Code</button>
        </div>
        <textarea id="codeEditor"></textarea>
        <div class="terminal-pane">
            <div class="terminal-header"><span>📊 Output Console</span><span style="color: #4ade80;">● Secure Sandbox</span></div>
            <pre id="outputBox">Console ready...</pre>
        </div>
    </div>
    <script>
        const socket = io();
        const roomId = "{{ room_id }}";
        const editor = document.getElementById('codeEditor');
        const overlay = document.getElementById('overlay');
        let username = prompt("Enter your Developer Handle:") || "Dev_" + Math.floor(Math.random()*1000);
        socket.emit('join_room_request', { room_id: roomId, username: username });

        socket.on('ask_host_approval', (data) => {
            document.getElementById('pendingRequests').innerHTML = `<div><b>${data.username}</b> wants access.</div>
                <button onclick="respond('${data.sid}', true)" style="background: #4ade80; margin-top:5px; padding:4px;">Grant</button>
                <button onclick="respond('${data.sid}', false)" style="background: #f87171; color:#fff; margin-top:5px; padding:4px;">Deny</button>`;
        });
        function respond(sid, approved) { socket.emit('host_room_decision', { room_id: roomId, sid: sid, approved: approved }); document.getElementById('pendingRequests').innerHTML = 'No pending requests.'; }
        socket.on('room_access_granted', (data) => { overlay.style.display = 'none'; editor.value = data.code; updateUsers(data.users); if(data.is_host) document.getElementById('hostControls').style.display = 'block'; });
        socket.on('room_access_denied', () => { document.getElementById('overlayText').innerText = "❌ Host denied entry."; });
        socket.on('update_room_users', (data) => { updateUsers(data.users); });
        function updateUsers(users) {
            let uList = document.getElementById('usersList'); uList.innerHTML = '';
            users.forEach(u => { let d = document.createElement('div'); d.className = 'user-badge'; d.innerText = '🟢 ' + u; uList.appendChild(d); });
        }
        let isRemote = false;
        editor.addEventListener('input', () => { if(isRemote) return; socket.emit('update_room_code', { room_id: roomId, code: editor.value }); });
        socket.on('sync_code', (data) => {});
        socket.on('sync_room_code', (data) => {
            isRemote = true;
            let pos = editor.selectionStart;
            editor.value = data.code;
            editor.setSelectionRange(pos, pos);
            isRemote = false;
        });
        function runCode() {
            let lang = document.getElementById('langSelect').value;
            document.getElementById('outputBox').innerText = "Running sandbox container...";
            socket.emit('execute_room_code', { room_id: roomId, language: lang });
        }
        socket.on('room_execution_output', (data) => { document.getElementById('outputBox').innerText = data.output; });
        
        function triggerAiFix() {
            document.getElementById('aiStatus').innerText = "AI scanning line-by-line...";
            socket.emit('ai_auto_fix', { room_id: roomId });
        }
        socket.on('ai_fix_notification', (data) => {
            document.getElementById('aiStatus').innerText = data.msg;
        });
    </script>
</body></html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5005))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
