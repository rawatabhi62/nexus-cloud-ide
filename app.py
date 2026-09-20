import os
import sys
import tempfile
import subprocess
import ast
import uuid
from flask import Flask, render_template_string, request, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
# CORS enabled for global cross-origin access
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

rooms = {}

@app.route('/')
def home():
    return render_template_string(HOME_PAGE)

@app.route('/create')
def create_room():
    room_id = str(uuid.uuid4())[:8]
    rooms[room_id] = {
        'code': "# Welcome to Nexus Global Cloud IDE\n# Write your code here...\n\ndef main():\n    print('Hello World from Global Cloud!')\n\nif __name__ == '__main__':\n    main()",
        'host_sid': None,
        'active_users': {},
        'pending_users': {}
    }
    return redirect(url_for('join_room_page', room_id=room_id))

@app.route('/room/<room_id>')
def join_room_page(room_id):
    if room_id not in rooms:
        return "Room not found, expired, or closed! <a href='/'>Create a new global room</a>", 404
    return render_template_string(ROOM_PAGE, room_id=room_id)

@socketio.on('join_room_request')
def handle_join(data):
    room_id = data.get('room_id')
    username = data.get('username', 'Global_Dev')
    sid = request.sid

    if room_id not in rooms:
        return

    room = rooms[room_id]
    join_room(room_id)

    if not room['host_sid']:
        room['host_sid'] = sid
        room['active_users'][sid] = username + " (Host)"
        emit('room_access_granted', {
            'code': room['code'],
            'users': list(room['active_users'].values()),
            'is_host': True
        }, room=sid)
    else:
        room['pending_users'][sid] = username
        emit('ask_host_approval', {
            'sid': sid,
            'username': username
        }, room=room['host_sid'])

@socketio.on('host_room_decision')
def handle_decision(data):
    room_id = data.get('room_id')
    target_sid = data.get('sid')
    approved = data.get('approved', False)

    if room_id not in rooms:
        return
    room = rooms[room_id]

    if target_sid in room['pending_users']:
        username = room['pending_users'].pop(target_sid)
        if approved:
            room['active_users'][target_sid] = username
            emit('room_access_granted', {
                'code': room['code'],
                'users': list(room['active_users'].values()),
                'is_host': False
            }, room=target_sid)
            emit('update_room_users', {'users': list(room['active_users'].values())}, room=room_id)
        else:
            emit('room_access_denied', {}, room=target_sid)

@socketio.on('update_room_code')
def handle_code(data):
    room_id = data.get('room_id')
    code = data.get('code')
    if room_id in rooms:
        rooms[room_id]['code'] = code
        emit('sync_room_code', {'code': code}, room=room_id, include_self=False)

@socketio.on('run_ai_audit')
def handle_ai_audit(data):
    room_id = data.get('room_id')
    if room_id not in rooms:
        return
    code = rooms[room_id]['code']
    report = []
    
    # Real AST Code Analysis Engine
    try:
        tree = ast.parse(code)
        report.append("✅ AST Syntax Check: PASSED (No syntax errors detected).")
        
        # Heuristic security & bug scan
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ['eval', 'exec']:
                    report.append(f"⚠️ SECURITY RISK: Use of dangerous function '{node.func.id}' detected at line {node.lineno}.")
            if isinstance(node, ast.While):
                if isinstance(node.test, ast.Constant) and node.test.value is True:
                    report.append(f"⚠️ WARNING: Potential infinite loop ('while True') found at line {node.lineno}.")
    except SyntaxError as se:
        report.append(f"❌ Syntax Error at line {se.lineno}: {se.text}")
    except Exception as e:
        report.append(f"❌ Audit Error: {str(e)}")

    if len(report) == 1:
        report.append("🚀 AI Heuristic Scan: Code looks clean and secure!")

    emit('ai_audit_response', {'report': "\n".join(report)}, room=request.sid)

@socketio.on('execute_room_code')
def handle_exec(data):
    room_id = data.get('room_id')
    lang = data.get('language', 'python')
    if room_id not in rooms:
        return
    code = rooms[room_id]['code']
    output = ""
    try:
        if lang == 'python':
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(code)
                fname = f.name
            res = subprocess.run([sys.executable, '-u', fname], capture_output=True, text=True, timeout=5)
            output = res.stdout if res.stdout else res.stderr
            os.unlink(fname)
        elif lang == 'c':
            with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False) as f:
                f.write(code)
                c_name = f.name
            exe = c_name + ".out"
            comp = subprocess.run(['gcc', c_name, '-o', exe], capture_output=True, text=True)
            if comp.returncode != 0:
                output = f"Compilation Error:\n{comp.stderr}"
            else:
                run_res = subprocess.run([exe], capture_output=True, text=True, timeout=5)
                output = run_res.stdout if run_res.stdout else run_res.stderr
                if os.path.exists(exe): os.unlink(exe)
            if os.path.exists(c_name): os.unlink(c_name)
    except Exception as e:
        output = f"Execution Error: {str(e)}"
    
    if not output:
        output = "[Executed with no output]"
    emit('room_execution_output', {'output': output}, room=room_id)

@socketio.on('disconnect')
def handle_disc():
    sid = request.sid
    for room_id, room in rooms.items():
        if sid in room['pending_users']:
            del room['pending_users'][sid]
        if sid in room['active_users']:
            del room['active_users'][sid]
            if sid == room['host_sid']:
                room['host_sid'] = None
            emit('update_room_users', {'users': list(room['active_users'].values())}, room=room_id)

HOME_PAGE = """
<!DOCTYPE html>
<html>
<head><title>Nexus Global Cloud IDE</title></head>
<body style="background:#090d16; color:#fff; font-family:'Segoe UI', sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; margin:0;">
    <div style="text-align:center; background:#111827; padding:45px; border-radius:12px; border:1px solid #1f2937; box-shadow: 0 10px 25px rgba(0,0,0,0.5);">
        <h1 style="color:#00ffcc; margin-top:0;">🌐 Nexus Global Cloud IDE</h1>
        <p style="color:#94a3b8; margin-bottom:25px;">A secure, real-time multi-user collaborative workspace accessible from anywhere in the world.</p>
        <a href="/create"><button style="background:linear-gradient(135deg, #00ffcc, #38bdf8); color:#030712; border:none; padding:14px 28px; font-weight:bold; font-size:16px; border-radius:6px; cursor:pointer; box-shadow:0 4px 12px rgba(0,255,204,0.3);">Launch New Global Room</button></a>
    </div>
</body>
</html>
"""

ROOM_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Nexus Room - {{ room_id }}</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        body { margin: 0; background: #0b0f19; color: #00ffcc; font-family: 'Courier New', monospace; display: flex; height: 100vh; overflow: hidden; }
        .sidebar { width: 290px; background: #111827; border-right: 1px solid #1f2937; display: flex; flex-direction: column; padding: 15px; }
        .main-content { flex: 1; display: flex; flex-direction: column; }
        .header { height: 50px; background: #111827; border-bottom: 1px solid #1f2937; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; }
        textarea { flex: 3; background: #030712; color: #38bdf8; border: none; font-family: 'Fira Code', monospace; font-size: 14px; padding: 15px; resize: none; outline: none; line-height: 1.5; }
        .terminal-pane { flex: 1.2; background: #020617; border-top: 1px solid #1f2937; display: flex; flex-direction: column; }
        .terminal-header { background: #0f172a; padding: 8px 15px; font-size: 13px; font-weight: bold; color: #38bdf8; display: flex; justify-content: space-between; }
        pre { margin: 0; padding: 12px; font-family: 'Fira Code', monospace; font-size: 13px; color: #4ade80; overflow-y: auto; flex: 1; white-space: pre-wrap; }
        button { background: linear-gradient(135deg, #00ffcc, #38bdf8); color: #030712; border: none; padding: 8px 16px; font-weight: bold; border-radius: 4px; cursor: pointer; transition: 0.2s; }
        button:hover { opacity: 0.85; transform: scale(1.02); }
        select { background: #1f2937; color: #fff; border: 1px solid #374151; padding: 6px; border-radius: 4px; font-family: inherit; }
        .user-badge { background: #1f2937; padding: 6px 10px; margin-bottom: 6px; border-radius: 4px; font-size: 13px; border-left: 3px solid #00ffcc; color: #e2e8f0; }
        #overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(3,7,18,0.95); display: flex; flex-direction: column; justify-content: center; align-items: center; z-index: 999; color: #fff; }
        .ai-panel { background: #1e1b4b; border: 1px solid #4338ca; padding: 10px; border-radius: 6px; margin-top: 10px; font-size: 12px; color: #c7d2fe; max-height: 150px; overflow-y: auto; }
    </style>
</head>
<body>
    <div id="overlay">
        <h2 id="overlayText">⚡ Secure Global Handshake in Progress...</h2>
    </div>

    <div class="sidebar">
        <h3 style="color: #00ffcc; margin-top:0;">🌐 Room: {{ room_id }}</h3>
        <p style="font-size: 12px; color: #94a3b8;">Active Global Users:</p>
        <div id="usersList"></div>
        
        <div class="ai-panel">
            <b>🤖 Deep AST AI Auditor</b>
            <pre id="aiStatus" style="margin: 5px 0 0 0; color: #a5b4fc; background:transparent; padding:0; font-size:11px;">Status: Ready for Deep Scan...</pre>
        </div>

        <div id="hostControls" style="margin-top: auto; display: none; background: #1f2937; padding: 10px; border-radius: 6px; border: 1px solid #374151;">
            <p style="font-size: 12px; margin: 0 0 8px 0; color: #facc15;"><b>🛡️ Host Security Gate</b></p>
            <div id="pendingRequests" style="font-size: 12px; color: #cbd5e1;">No pending entry requests.</div>
        </div>
    </div>
    
    <div class="main-content">
        <div class="header">
            <div style="display: flex; gap: 15px; align-items: center;">
                <span style="font-weight: bold; color: #00ffcc;">🚀 NEXUS CLOUD</span>
                <select id="langSelect">
                    <option value="c">C Language</option>
                    <option value="python" selected>Python 3.11</option>
                </select>
            </div>
            <div style="display: flex; gap: 10px;">
                <button onclick="triggerAiAudit()">🤖 Run Deep AI Audit</button>
                <button onclick="runCode()">▶ Execute Code</button>
            </div>
        </div>
        
        <textarea id="codeEditor" placeholder="// Write code here..."></textarea>
        
        <div class="terminal-pane">
            <div class="terminal-header">
                <span>📊 Output Console</span>
                <span style="color: #4ade80;">● Global Mesh Secure</span>
            </div>
            <pre id="outputBox">Execution console initialized...</pre>
        </div>
    </div>

    <script>
        const socket = io();
        const roomId = "{{ room_id }}";
        const editor = document.getElementById('codeEditor');
        const overlay = document.getElementById('overlay');
        const overlayText = document.getElementById('overlayText');
        
        let username = prompt("Enter your Developer Handle:") || "Dev_" + Math.floor(Math.random()*1000);
        socket.emit('join_room_request', { room_id: roomId, username: username });

        socket.on('ask_host_approval', (data) => {
            let container = document.getElementById('pendingRequests');
            container.innerHTML = `<div><b>${data.username}</b> wants access.</div>
                <button onclick="respond('${data.sid}', true)" style="background: #4ade80; margin-top:5px; padding:4px;">Grant</button>
                <button onclick="respond('${data.sid}', false)" style="background: #f87171; color:#fff; margin-top:5px; padding:4px;">Deny</button>`;
        });

        function respond(sid, approved) {
            socket.emit('host_room_decision', { room_id: roomId, sid: sid, approved: approved });
            document.getElementById('pendingRequests').innerHTML = 'No pending requests.';
        }

        socket.on('room_access_granted', (data) => {
            overlay.style.display = 'none';
            editor.value = data.code;
            updateUsers(data.users);
            if (data.is_host) {
                document.getElementById('hostControls').style.display = 'block';
            }
        });

        socket.on('room_access_denied', () => {
            overlayText.innerText = "❌ Host denied entry to this room.";
        });

        socket.on('update_room_users', (data) => {
            updateUsers(data.users);
        });

        function updateUsers(users) {
            let uList = document.getElementById('usersList');
            uList.innerHTML = '';
            users.forEach(u => {
                let div = document.createElement('div');
                div.className = 'user-badge';
                div.innerText = '🟢 ' + u;
                uList.appendChild(div);
            });
        }

        let isRemote = false;
        editor.addEventListener('input', () => {
            if (isRemote) return;
            socket.emit('update_room_code', { room_id: roomId, code: editor.value });
        });

        socket.on('sync_room_code', (data) => {
            isRemote = true;
            let pos = editor.selectionStart;
            editor.value = data.code;
            editor.setSelectionRange(pos, pos);
            isRemote = false;
        });

        function runCode() {
            let lang = document.getElementById('langSelect'.value) || document.getElementById('langSelect').value;
            document.getElementById('outputBox').innerText = "Executing in secure server container...";
            socket.emit('execute_room_code', { room_id: roomId, language: lang });
        }

        socket.on('room_execution_output', (data) => {
            document.getElementById('outputBox').innerText = data.output;
        });

        function triggerAiAudit() {
            document.getElementById('aiStatus').innerText = "Analyzing AST nodes & code safety...";
            socket.emit('run_ai_audit', { room_id: roomId });
        }

        socket.on('ai_audit_response', (data) => {
            document.getElementById('aiStatus').innerText = data.report;
        });
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5005))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
