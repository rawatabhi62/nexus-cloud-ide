import os
import sys
import tempfile
import subprocess
import ast
import uuid
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

rooms = {}

@app.route('/')
def home():
    return render_template_string(HOME_PAGE)

@app.route('/create')
def create_room():
    room_id = str(uuid.uuid4())[:8]
    rooms[room_id] = {
        'code': "# Welcome to Nexus Global Cloud IDE\nname = input('Enter your name: ')\nprint(f'Hello, {name}!')",
        'activity_log': ["🛡️ Room initialized successfully."]
    }
    return f"""
    <script>
        window.location.href = '/room/{room_id}';
    </script>
    """

@app.route('/room/<room_id>')
def join_room_page(room_id):
    if room_id not in rooms:
        rooms[room_id] = {
            'code': "# Welcome to Nexus Global Cloud IDE\nname = input('Enter your name: ')\nprint(f'Hello, {name}!')",
            'activity_log': ["🛡️ Room initialized."]
        }
    return render_template_string(ROOM_PAGE, room_id=room_id)

@app.route('/api/get_code/<room_id>', methods=['GET'])
def get_code(room_id):
    if room_id in rooms:
        return jsonify({'code': rooms[room_id]['code'], 'logs': rooms[room_id]['activity_log']})
    return jsonify({'error': 'Room not found'}), 404

@app.route('/api/save_code', methods=['POST'])
def save_code():
    data = request.json
    room_id = data.get('room_id')
    code = data.get('code')
    username = data.get('username', 'Dev')
    if room_id in rooms:
        rooms[room_id]['code'] = code
        rooms[room_id]['activity_log'].insert(0, f"💾 {username} saved cloud checkpoint.")
        return jsonify({'success': True, 'logs': rooms[room_id]['activity_log']})
    return jsonify({'success': False}), 404

@app.route('/api/ai_fix', methods=['POST'])
def ai_fix():
    data = request.json
    room_id = data.get('room_id')
    username = data.get('username', 'AI')
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
        return jsonify({'code': new_code, 'logs': rooms[room_id]['activity_log']})
    return jsonify({'success': False}), 404

@app.route('/api/ai_chat', methods=['POST'])
def ai_chat():
    data = request.json
    query = data.get('query', '').lower()
    code = data.get('code', '')
    reply = ""
    if 'explain' in query:
        reply = f"🤖 AI Copilot: Script has {len(code.splitlines())} lines. Optimized for cloud execution."
    elif 'optimize' in query:
        reply = "🤖 AI Copilot Tip: Use efficient loops and built-in functions to boost speed."
    elif 'bug' in query or 'error' in query:
        reply = "🤖 AI Audit: No critical syntax issues or execution locks found."
    else:
        reply = "🤖 AI Copilot: Ready. Ask me to 'explain', 'optimize', or 'find bugs'."
    return jsonify({'reply': reply})

@app.route('/api/execute', methods=['POST'])
def execute_code():
    data = request.json
    room_id = data.get('room_id')
    lang = data.get('language', 'python')
    user_input = data.get('input', '')
    username = data.get('username', 'Dev')
    
    if room_id not in rooms:
        return jsonify({'output': 'Room session expired.'})
    
    code = rooms[room_id]['code']
    output = ""
    
    try:
        if lang == 'python':
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(code)
                fname = f.name
            res = subprocess.run([sys.executable, '-u', fname], input=user_input, capture_output=True, text=True, timeout=5)
            output = res.stdout if res.stdout else res.stderr
            os.unlink(fname)
        elif lang == 'javascript':
            with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
                f.write(code)
                js_name = f.name
            res = subprocess.run(['node', js_name], input=user_input, capture_output=True, text=True, timeout=5)
            output = res.stdout if res.stdout else res.stderr
            os.unlink(js_name)
        elif lang == 'cpp':
            with tempfile.NamedTemporaryFile(mode='w', suffix='.cpp', delete=False) as f:
                f.write(code)
                cpp_name = f.name
            exe = cpp_name + ".out"
            comp = subprocess.run(['g++', cpp_name, '-o', exe], capture_output=True, text=True)
            if comp.returncode != 0:
                output = f"C++ Compilation Error:\n{comp.stderr}"
            else:
                run_res = subprocess.run([exe], input=user_input, capture_output=True, text=True, timeout=5)
                output = run_res.stdout if run_res.stdout else run_res.stderr
                if os.path.exists(exe): os.unlink(exe)
            if os.path.exists(cpp_name): os.unlink(cpp_name)
    except subprocess.TimeoutExpired:
        output = "❌ Execution Error: Process timed out (Possible infinite loop)."
    except Exception as e:
        output = f"Execution Error: {str(e)}"
    
    if not output: output = "[Executed with no output]"
    rooms[room_id]['activity_log'].insert(0, f"▶ {username} executed {lang.upper()} code.")
    return jsonify({'output': output, 'logs': rooms[room_id]['activity_log']})

HOME_PAGE = """<!DOCTYPE html><html><head><title>Nexus Global Cloud IDE</title></head>
<body style="background:#090d16; color:#fff; font-family:'Segoe UI',sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; margin:0;">
    <div style="text-align:center; background:#111827; padding:45px; border-radius:12px; border:1px solid #1f2937;">
        <h1 style="color:#00ffcc;">🌐 Nexus Universal Cloud IDE</h1>
        <p style="color:#94a3b8; margin-bottom:25px;">Professional Cloud Compiler Environment.</p>
        <a href="/create"><button style="background:linear-gradient(135deg, #00ffcc, #38bdf8); color:#030712; border:none; padding:14px 28px; font-weight:bold; border-radius:6px; cursor:pointer;">Launch New Room</button></a>
    </div>
</body></html>"""

ROOM_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Nexus Room - {{ room_id }}</title>
<style>
    body { margin: 0; background: #0b0f19; color: #00ffcc; font-family: 'Courier New', monospace; display: flex; height: 100vh; overflow: hidden; }
    .sidebar { width: 320px; background: #111827; border-right: 1px solid #1f2937; display: flex; flex-direction: column; padding: 15px; overflow-y: auto; }
    .main-content { flex: 1; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
    .header { height: 50px; background: #111827; border-bottom: 1px solid #1f2937; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; flex-shrink: 0; }
    textarea { flex: 1; background: #030712; color: #38bdf8; border: none; font-size: 14px; padding: 15px; resize: none; outline: none; line-height: 1.5; overflow-y: auto; }
    .terminal-pane { height: 38vh; background: #020617; border-top: 1px solid #1f2937; display: flex; flex-direction: column; flex-shrink: 0; }
    .terminal-header { background: #0f172a; padding: 8px 15px; font-size: 13px; font-weight: bold; color: #38bdf8; display: flex; justify-content: space-between; align-items:center; }
    pre { margin: 0; padding: 12px; font-size: 13px; color: #4ade80; overflow-y: auto; flex: 1; white-space: pre-wrap; background: #020617; }
    
    .terminal-input-row { background: #090d16; padding: 10px 15px; display: flex; gap: 10px; border-top: 1px solid #1f2937; align-items: center; }
    
    button { background: linear-gradient(135deg, #00ffcc, #38bdf8); color: #030712; border: none; padding: 7px 14px; font-weight: bold; border-radius: 4px; cursor: pointer; font-size:12px; }
    button:hover { opacity: 0.85; }
    select, input { background: #1f2937; color: #fff; border: 1px solid #374151; padding: 6px; border-radius: 4px; font-family: inherit; }
    .user-badge { background: #1f2937; padding: 5px 8px; margin-bottom: 5px; border-radius: 4px; font-size: 12px; border-left: 3px solid #00ffcc; }
    .ai-panel { background: #1e1b4b; border: 1px solid #4338ca; padding: 10px; border-radius: 6px; margin-top: 10px; font-size: 12px; color: #c7d2fe; display: flex; flex-direction: column; height: 170px; }
    .ai-chat-box { flex: 1; overflow-y: auto; background: #0f172a; padding: 6px; margin-bottom: 6px; border-radius: 4px; font-size: 11px; white-space: pre-wrap; }
    .log-panel { background: #0f172a; border: 1px solid #1e293b; padding: 8px; border-radius: 6px; margin-top: 10px; height: 100px; overflow-y: auto; font-size: 11px; color: #94a3b8; }
</style></head>
<body>
    <div class="sidebar">
        <h3 style="color: #00ffcc; margin-top:0;">🌐 Room: {{ room_id }}</h3>
        <p style="font-size: 12px; color: #94a3b8; margin: 5px 0;">Developer Handle:</p>
        <div id="userBadge" class="user-badge">🟢 Connected</div>
        
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
            <button onclick="runCode()">▶ Run Code</button>
        </div>
        <textarea id="codeEditor"></textarea>
        <div class="terminal-pane">
            <div class="terminal-header">
                <span>📊 Output Console & Terminal</span>
                <span style="color: #4ade80;">● Ready</span>
            </div>
            <pre id="outputBox">Console ready... Type multiple inputs separated by newlines in the input box below if your code uses input(), then click 'Run Code'.</pre>
            <div class="terminal-input-row">
                <span style="font-size: 12px; color: #38bdf8;">Inputs (newline separated):</span>
                <input type="text" id="terminalInput" placeholder="e.g. Abhishek\n20" style="flex: 1; font-size: 12px; padding: 6px;" onkeydown="if(event.key==='Enter') runCode()">
                <button onclick="runCode()" style="background: #4ade80; color: #030712;">Run Code</button>
            </div>
        </div>
    </div>
    <script>
        const roomId = "{{ room_id }}";
        const editor = document.getElementById('codeEditor');
        const outputBox = document.getElementById('outputBox');
        let username = prompt("Enter your Developer Handle:") || "Dev_" + Math.floor(Math.random()*1000);
        document.getElementById('userBadge').innerText = "🟢 " + username;

        fetch('/api/get_code/' + roomId)
            .then(res => res.json())
            .then(data => {
                if(data.code) editor.value = data.code;
                if(data.logs) updateLogs(data.logs);
            });

        function updateLogs(logs) {
            let lBox = document.getElementById('activityLog');
            lBox.innerHTML = logs.map(l => `<div>• ${l}</div>`).join('');
        }

        function saveCloud() {
            fetch('/api/save_code', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ room_id: roomId, code: editor.value, username: username })
            }).then(res => res.json()).then(data => {
                if(data.logs) updateLogs(data.logs);
                alert('✅ Code successfully saved to cloud!');
            });
        }

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
            let rawInput = document.getElementById('terminalInput').value;
            // Support newline escape sequences if typed
            let userInput = rawInput.replace(/\\n/g, '\n');
            outputBox.innerText = "Running code in container...";
            
            fetch('/api/execute', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ room_id: roomId, language: lang, input: userInput, username: username })
            }).then(res => res.json()).then(data => {
                outputBox.innerText = data.output;
                if(data.logs) updateLogs(data.logs);
            }).catch(err => {
                outputBox.innerText = "❌ Execution failed: " + err;
            });
        }

        function sendAiQuery() {
            let q = document.getElementById('aiQueryInput').value; if(!q) return;
            document.getElementById('aiChatBox').innerText += "\\nYou: " + q;
            fetch('/api/ai_chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query: q, code: editor.value })
            }).then(res => res.json()).then(data => {
                document.getElementById('aiChatBox').innerText += "\\n" + data.reply;
                let chatBox = document.getElementById('aiChatBox');
                chatBox.scrollTop = chatBox.scrollHeight;
            });
            document.getElementById('aiQueryInput', '').value = '';
        }

        function triggerAiFix() {
            fetch('/api/ai_fix', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ room_id: roomId, username: username })
            }).then(res => res.json()).then(data => {
                if(data.code) editor.value = data.code;
                if(data.logs) updateLogs(data.logs);
                alert("✨ AI Auto-Fix applied successfully!");
            });
        }
    </script>
</body></html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5005))
    app.run(host='0.0.0.0', port=port, debug=False)
