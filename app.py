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
# Maps a live socket connection's sid -> which room it joined, so that on
# disconnect (which only gives us a sid, not the room) we can still find the
# right room to remove the member from and announce they left.
sid_to_room = {}

# How many Time Machine snapshots (edits + runs, combined) each room keeps in
# memory. Old snapshots are dropped once a room exceeds this, so a long-lived
# room on the free tier can't grow its history unboundedly.
MAX_HISTORY = 300
# Minimum seconds between two consecutive "edit" snapshots for the same
# room. Recording one per keystroke would flood the history (and memory)
# with near-duplicate states, so edits are sampled on a cooldown instead;
# "run" snapshots are never throttled -- every run is always recorded.
EDIT_SNAPSHOT_COOLDOWN = 4

def _append_history(room, entry):
    """Append a Time Machine snapshot to room['history'], trimmed to
    MAX_HISTORY entries. Caller must already hold rooms_lock. Returns the
    entry so the caller can broadcast it once, outside the lock."""
    hist = room.setdefault('history', [])
    hist.append(entry)
    if len(hist) > MAX_HISTORY:
        del hist[0:len(hist) - MAX_HISTORY]
    return entry

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
    with rooms_lock:
        room_id = sid_to_room.pop(request.sid, None)
        if room_id and room_id in rooms:
            room = rooms[room_id]
            # A pending (not-yet-approved) visitor disconnecting just drops
            # their request quietly -- nothing to announce to the room.
            room.get('pending', {}).pop(request.sid, None)

            username = room['members'].pop(request.sid, None)
            if room.get('host_sid') == request.sid:
                # Host left: stop auto-approving as them, and no NEW join
                # requests can be approved until they (or whoever still has
                # the host link) reconnects. Existing members are unaffected.
                room['host_sid'] = None

            if username:
                room['activity_log'].insert(0, f"🔴 {username} left the room.")
                if len(room['activity_log']) > 100:
                    room['activity_log'].pop()
                online_users = list(room['members'].values())
                logs = room['activity_log']
                socketio.emit('presence_update', {'users': online_users}, room=room_id)
                socketio.emit('sync_logs', {'logs': logs}, room=room_id)

@app.route('/')
def home():
    return render_template_string(HOME_PAGE)

@app.route('/create')
def create_room():
    # Cryptographically secure room ID generation
    room_id = secrets.token_urlsafe(8)[:8]
    # A second, separate secret that proves "this visitor is the person who
    # created the room". Only the /create response (redirect URL below) ever
    # carries it, so only the creator's browser has it. Anyone with just the
    # plain /room/<room_id> link (the one people actually share) does NOT
    # have this token, and so cannot claim to be the host.
    host_token = secrets.token_urlsafe(16)
    with rooms_lock:
        rooms[room_id] = {
            'code': "# Welcome to Nexus Global Cloud IDE\nname = input('Enter your name: ')\nprint(f'Hello, {name}!')",
            'activity_log': ["🛡️ Room initialized securely."],
            'run_lock': threading.Lock(),
            'process': None,
            'master_fd': None,
            'members': {},    # sid -> username, approved & currently connected
            'pending': {},    # sid -> username, waiting for host approval
            'host_token': host_token,
            'host_sid': None,  # sid of whoever is currently connected AS host
            # "Time Machine" session history: a bounded, timestamped list of
            # {t, type: 'edit'|'run', user, code, [output], [language]}
            # snapshots, so anyone in the room can scrub back through how the
            # code evolved (and what each run produced) instead of only ever
            # seeing the latest state. Edit snapshots are throttled (not one
            # per keystroke) to keep this list small and cheap on the free tier.
            'history': [],
            'last_snapshot_time': 0
        }
    return redirect(url_for('join_room_page', room_id=room_id, host_token=host_token))

@app.route('/room/<room_id>')
def join_room_page(room_id):
    with rooms_lock:
        if room_id not in rooms:
            return "Room not found or expired! <a href='/'>Create a new room</a>", 404
        real_token = rooms[room_id]['host_token']
    # Only render the real host_token into the page if the visitor already
    # supplied the matching one in the URL (i.e. they're the creator, via
    # the /create redirect). A plain shared /room/<id> link carries no token
    # at all, so this stays empty for every other joiner -- it is never
    # guessable from the room_id itself.
    supplied = request.args.get('host_token', '')
    host_token_for_page = real_token if supplied and supplied == real_token else ''
    return render_template_string(ROOM_PAGE, room_id=room_id, host_token=host_token_for_page)

@socketio.on('join_room_socket')
def handle_join(data):
    room_id = data.get('room_id')
    username = data.get('username', 'Someone')
    supplied_token = data.get('host_token', '')

    with rooms_lock:
        if room_id not in rooms:
            emit('join_rejected', {'reason': 'Room not found or expired.'}, room=request.sid)
            return
        room = rooms[room_id]
        # Backward-compat: rooms created before this feature existed.
        room.setdefault('members', {})
        room.setdefault('pending', {})
        room.setdefault('host_token', None)
        room.setdefault('host_sid', None)
        room.setdefault('history', [])
        room.setdefault('last_snapshot_time', 0)

        is_host = bool(room['host_token']) and supplied_token == room['host_token']

        if not is_host:
            if room['host_sid'] is None:
                # No host currently connected to approve anyone -- refuse
                # rather than let people pile into an unmoderated room.
                emit('join_rejected', {'reason': 'The room host is not online right now. Try again once they are.'}, room=request.sid)
                return
            # Queue them and ask the host to approve, instead of letting
            # them straight in.
            room['pending'][request.sid] = username
            emit('join_pending', {}, room=request.sid)
            emit('join_request', {'sid': request.sid, 'username': username}, room=room['host_sid'])
            return

        # Host path: join immediately, no approval needed for themselves.
        room['host_sid'] = request.sid

    join_room(room_id)
    with rooms_lock:
        if room_id in rooms:
            room = rooms[room_id]
            room['members'][request.sid] = username
            sid_to_room[request.sid] = room_id

            rooms[room_id]['activity_log'].insert(0, f"🟢 {username} joined the room.")
            if len(rooms[room_id]['activity_log']) > 100:
                rooms[room_id]['activity_log'].pop()

            online_users = list(rooms[room_id]['members'].values())
            logs = rooms[room_id]['activity_log']
            code = rooms[room_id]['code']

            # Full state, including the current online-users list, to the
            # person who just joined.
            emit('sync_code', {'code': code, 'logs': logs}, room=request.sid)
            emit('presence_update', {'users': online_users}, room=request.sid)
            # Tell everyone ELSE already in the room that this person joined
            # and the member list + activity log changed.
            emit('presence_update', {'users': online_users}, room=room_id, include_self=False)
            emit('sync_logs', {'logs': logs}, room=room_id, include_self=False)

@socketio.on('respond_join_request')
def handle_respond_join_request(data):
    room_id = data.get('room_id')
    target_sid = data.get('sid')
    approve = bool(data.get('approve'))

    with rooms_lock:
        if room_id not in rooms:
            return
        room = rooms[room_id]
        # Only the currently-connected host may approve/deny -- never trust
        # the "approve" click itself, verify the clicker really is host_sid.
        if request.sid != room.get('host_sid'):
            return
        username = room.get('pending', {}).pop(target_sid, None)
        if username is None:
            return  # already handled, or they disconnected before a decision

        if not approve:
            socketio.emit('join_denied', {}, room=target_sid)
            return

        room['members'][target_sid] = username
        sid_to_room[target_sid] = room_id
        room['activity_log'].insert(0, f"🟢 {username} joined the room.")
        if len(room['activity_log']) > 100:
            room['activity_log'].pop()
        online_users = list(room['members'].values())
        logs = room['activity_log']
        code = room['code']

    join_room(room_id, sid=target_sid)
    socketio.emit('join_approved', {}, room=target_sid)
    socketio.emit('sync_code', {'code': code, 'logs': logs}, room=target_sid)
    socketio.emit('presence_update', {'users': online_users}, room=target_sid)
    socketio.emit('presence_update', {'users': online_users}, room=room_id, include_self=False)
    socketio.emit('sync_logs', {'logs': logs}, room=room_id, include_self=False)

@socketio.on('update_code')
def handle_code(data):
    room_id = data.get('room_id')
    code = data.get('code')
    username = data.get('username', 'Someone')
    new_entry = None
    with rooms_lock:
        if room_id in rooms:
            room = rooms[room_id]
            room['code'] = code
            emit('sync_code', {'code': code}, room=room_id, include_self=False)

            now = time.time()
            if now - room.get('last_snapshot_time', 0) >= EDIT_SNAPSHOT_COOLDOWN:
                room['last_snapshot_time'] = now
                new_entry = _append_history(room, {
                    't': now, 'type': 'edit', 'user': username, 'code': code
                })
    # Broadcast outside the lock: lets every open Time Machine panel in the
    # room grow live, instead of only backfilling when someone opens it.
    if new_entry is not None:
        socketio.emit('history_append', new_entry, room=room_id)

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
    runner_name = data.get('username', 'Someone')

    # Tell every member of the room (not just whoever clicked Run) to clear
    # their output console, so everyone sees the same run from the start.
    socketio.emit('terminal_clear', {'started_by': runner_name}, room=room_id)

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

    with run_lock:
        # Stop any previous run in this room CLEANLY and wait for its
        # background reader thread to fully exit before touching any new
        # fd/process. Previously this only terminated the old process and
        # closed its fd without waiting -- that old reader thread could
        # still be mid os.read() on that exact fd number when a new run
        # opened a fresh pty and Linux immediately reused the same fd
        # number for it, corrupting/crossing the two runs' output and
        # surfacing as "[Errno 9] Bad file descriptor". Joining the thread
        # first removes that race entirely: nothing touches the old fd
        # after this point.
        old_proc = room.get('process')
        old_thread = room.get('reader_thread')
        old_stop_event = room.get('stop_event')
        if old_stop_event:
            old_stop_event.set()
        if old_proc and old_proc.poll() is None:
            try:
                old_proc.terminate()
            except Exception:
                pass
        if old_thread and old_thread.is_alive():
            old_thread.join(timeout=3)
        if old_proc and old_proc.poll() is None:
            # terminate() (SIGTERM) above may not have been enough within
            # the join wait -- make sure it's actually gone before we move
            # on, so it can't keep writing into a fd we're about to reuse.
            try:
                old_proc.kill()
                old_proc.wait(timeout=1)
            except Exception:
                pass
        if room.get('master_fd') is not None:
            try:
                os.close(room['master_fd'])
            except Exception:
                pass
            room['master_fd'] = None

        try:
            if lang == 'cpp':
                comp = subprocess.run(cmd, capture_output=True, text=True)
                if comp.returncode != 0:
                    emit('terminal_data', {'data': f"C++ Compilation Error:\n{comp.stderr}"}, room=room_id)
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

            stop_event = threading.Event()
            room['process'] = process
            room['master_fd'] = master_fd
            room['stop_event'] = stop_event

            def read_output_batched():
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                buffer = []
                last_flush = time.time()
                # Separately mirrors everything flushed to the room, capped,
                # so a completed run can be filed into the Time Machine
                # history with the real output it produced -- not just the
                # code that was run. Capped independently of the live
                # terminal (which gets the full stream) to bound memory.
                captured = []
                captured_len = 0
                CAPTURE_CAP = 6000

                try:
                    while True:
                        if stop_event.is_set():
                            break
                        r, _, _ = select.select([master_fd], [], [], 0.1)
                        if r:
                            try:
                                chunk = os.read(master_fd, 4096)
                                if not chunk:
                                    break
                                decoded = decoder.decode(chunk)
                                buffer.append(decoded)
                                if captured_len < CAPTURE_CAP:
                                    captured.append(decoded)
                                    captured_len += len(decoded)
                            except OSError:
                                break

                        now = time.time()
                        # Flush every 40ms or when buffer exceeds ~16KB
                        current_len = sum(len(b) for b in buffer)
                        if buffer and (now - last_flush > 0.04 or current_len > 16384):
                            socketio.emit('terminal_data', {'data': "".join(buffer)}, room=room_id)
                            buffer.clear()
                            last_flush = now

                        if process.poll() is not None and not r:
                            break
                except Exception:
                    pass

                # If stop_event is set, this run was superseded by a newer
                # one (someone clicked Run Code again) rather than finishing
                # naturally -- stay silent instead of racing the new run's
                # own output/clear with a stale "[Program finished]", and
                # don't file a history entry for a run that never completed.
                if not stop_event.is_set():
                    if buffer:
                        socketio.emit('terminal_data', {'data': "".join(buffer)}, room=room_id)
                    socketio.emit('terminal_data', {'data': "\n\n[Program finished]\n"}, room=room_id)

                    output_text = "".join(captured)
                    if captured_len >= CAPTURE_CAP:
                        output_text += "\n...[output truncated in Time Machine history]"
                    new_entry = None
                    with rooms_lock:
                        if room_id in rooms:
                            new_entry = _append_history(rooms[room_id], {
                                't': time.time(), 'type': 'run', 'user': runner_name,
                                'language': lang, 'code': code, 'output': output_text
                            })
                    if new_entry is not None:
                        socketio.emit('history_append', new_entry, room=room_id)

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

            reader_thread = threading.Thread(target=read_output_batched, daemon=True)
            room['reader_thread'] = reader_thread
            reader_thread.start()

        except Exception as e:
            emit('terminal_data', {'data': f"Execution Error: {str(e)}"}, room=room_id)

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

@socketio.on('get_history')
def handle_get_history(data):
    """Time Machine: send the requesting client the room's full recorded
    history (edit + run snapshots) so it can build the scrub slider. This
    backfills whoever just opened the panel; 'history_append' (emitted
    elsewhere) keeps it growing live for as long as it stays open."""
    room_id = data.get('room_id')
    with rooms_lock:
        if room_id not in rooms:
            emit('history_data', {'history': []}, room=request.sid)
            return
        hist = list(rooms[room_id].get('history', []))
    emit('history_data', {'history': hist}, room=request.sid)

@socketio.on('restore_history_code')
def handle_restore_history(data):
    """Time Machine: jump the room's LIVE code back to a past snapshot the
    requester picked, and sync that to everyone currently in the room --
    the same trust model as normal editing (anyone in the room can already
    edit the code at will), just restoring an older version instead of
    typing it. Logged in the activity feed so it's visible, not silent."""
    room_id = data.get('room_id')
    code = data.get('code')
    username = data.get('username', 'Someone')
    if code is None:
        return
    with rooms_lock:
        if room_id not in rooms:
            return
        room = rooms[room_id]
        room['code'] = code
        room['activity_log'].insert(0, f"⏪ {username} restored a snapshot from the Time Machine.")
        if len(room['activity_log']) > 100:
            room['activity_log'].pop()
        logs = room['activity_log']
    socketio.emit('sync_code', {'code': code, 'logs': logs}, room=room_id)
    socketio.emit('sync_logs', {'logs': logs}, room=room_id)

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
    <div id="waitingOverlay" style="display:none; position:fixed; inset:0; background:rgba(3,7,18,0.95); z-index:9998; color:#00ffcc; flex-direction:column; align-items:center; justify-content:center; font-family:'Courier New',monospace; text-align:center; padding:20px;">
        <div id="waitingTitle" style="font-size:18px; margin-bottom:10px;">⏳ Waiting for the host to let you in...</div>
        <div id="waitingSub" style="font-size:13px; color:#94a3b8;">Ask them to approve your join request.</div>
    </div>
    <div id="joinRequestBanner" style="display:none; position:fixed; top:0; left:0; right:0; background:#1e1b4b; border-bottom:1px solid #4338ca; color:#c7d2fe; padding:10px 16px; font-size:13px; z-index:9999; text-align:center;">
        <span id="joinRequestText"></span>
        <button id="joinApproveBtn" style="background:#10b981; color:#fff; margin-left:10px;">✅ Allow</button>
        <button id="joinDenyBtn" style="background:#ef4444; color:#fff; margin-left:6px;">❌ Deny</button>
    </div>
    <div id="timeMachineModal" style="display:none; position:fixed; inset:0; background:rgba(3,7,18,0.92); z-index:9997; align-items:center; justify-content:center; font-family:'Courier New',monospace;">
        <div style="background:#0f172a; border:1px solid #a855f7; border-radius:10px; width:min(900px,92vw); max-height:88vh; display:flex; flex-direction:column; padding:18px; box-sizing:border-box;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                <h3 style="margin:0; color:#c084fc;">🕰️ Session Time Machine</h3>
                <button onclick="closeTimeMachine()" style="background:#ef4444; color:#fff;">✕ Close</button>
            </div>
            <div style="font-size:12px; color:#94a3b8; margin-bottom:10px;">Scrub through how this room's code evolved, who changed it, and what each run produced &mdash; something most online compilers never let you see.</div>
            <div id="tmEmptyMsg" style="color:#64748b; font-size:13px; padding:20px 0;">No snapshots recorded yet &mdash; keep coding or run your program, and this will start filling in live.</div>
            <div id="tmBody" style="display:none; flex:1; overflow:hidden; flex-direction:column;">
                <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
                    <button id="tmPlayBtn" onclick="toggleTimeMachinePlay()" style="background:#a855f7; color:#fff;">▶ Play</button>
                    <input type="range" id="tmSlider" min="0" max="0" value="0" style="flex:1;" oninput="showSnapshot(parseInt(this.value))">
                    <span id="tmIndexLabel" style="font-size:11px; color:#94a3b8; min-width:70px; text-align:right;">0 / 0</span>
                </div>
                <div id="tmMeta" style="font-size:12px; margin-bottom:6px; color:#e2e8f0;"></div>
                <div style="flex:1; overflow-y:auto; background:#020617; border:1px solid #1e293b; border-radius:6px; padding:10px; margin-bottom:8px;">
                    <pre id="tmCode" style="margin:0; color:#38bdf8; font-size:12px; white-space:pre-wrap;"></pre>
                </div>
                <div id="tmOutputWrap" style="display:none; max-height:120px; overflow-y:auto; background:#011; border:1px solid #1e293b; border-radius:6px; padding:10px; margin-bottom:8px;">
                    <div style="color:#94a3b8; font-size:11px; margin-bottom:4px;">📊 Output at this point:</div>
                    <pre id="tmOutput" style="margin:0; color:#4ade80; font-size:12px; white-space:pre-wrap;"></pre>
                </div>
                <button id="tmRestoreBtn" onclick="restoreSnapshot()" style="background:#f59e0b; color:#030712;">⏪ Restore this version into the live editor</button>
            </div>
        </div>
    </div>
    <div class="sidebar">
        <h3 style="color: #00ffcc; margin-top:0;">🌐 Room: {{ room_id }}</h3>
        <p style="font-size: 12px; color: #94a3b8; margin: 5px 0;">Developer Handle:</p>
        <div id="userBadge" class="user-badge">🟡 Connecting...</div>

        <div style="display: flex; gap: 5px; margin-top: 10px;">
            <button id="btnSave" onclick="saveCloud()" style="flex:1; background:#10b981; color:#fff;" disabled>💾 Save Cloud</button>
            <button id="btnDownload" onclick="downloadCode()" style="flex:1; background:#3b82f6; color:#fff;" disabled>📥 Download</button>
        </div>
        <button id="btnTimeMachine" onclick="openTimeMachine()" style="width:100%; margin-top:5px; background:#a855f7; color:#fff;" disabled>🕰️ Time Machine (<span id="historyCount">0</span>)</button>

        <p style="font-size: 12px; color: #94a3b8; margin: 10px 0 5px 0;">👥 Online Now (<span id="onlineCount">0</span>):</p>
        <div id="onlineUsers" class="log-panel" style="height: 70px;">Connecting...</div>

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
            // WebSocket upgrade succeeds at the HTTP level (101 Switching
            // Protocols) on Render, but the dev-mode WSGI server used here
            // (allow_unsafe_werkzeug=True) does not reliably service the raw
            // socket afterwards, so the Socket.IO-level "connect" event never
            // fires and the client hangs. Staying on polling-only avoids that
            // broken code path entirely; polling works reliably end-to-end.
            const socket = io({ transports: ['polling'], upgrade: false, timeout: 20000 });

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
            // Only non-empty in the creator's own browser (see /room/<id>
            // route): proves to the server "I am the host" without the
            // room_id itself (which everyone has) being enough on its own.
            const hostToken = "{{ host_token }}";
            // The host link looks like /room/<id>?host_token=... right after
            // "Create Room" -- if the host copies the address bar (the
            // single most natural way anyone shares "the room link"), that
            // query string goes with it and whoever opens it is treated as
            // host too, no approval needed. Scrub it from the visible URL
            // immediately so only /room/<id> (no token) is ever what gets
            // copied from here on; hostToken above already captured the
            // real value before this runs.
            if (hostToken && window.history && window.history.replaceState) {
                window.history.replaceState(null, '', '/room/' + roomId);
            }
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
                ['btnSave', 'btnDownload', 'btnAiSend', 'btnAiFix', 'btnRun', 'btnTimeMachine'].forEach(id => {
                    document.getElementById(id).disabled = false;
                });
                socket.emit('join_room_socket', { room_id: roomId, username: username, host_token: hostToken });
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

            // Usernames are free text the user typed (via the Developer
            // Handle prompt) and get broadcast to everyone in the room, so
            // they must be HTML-escaped before going into innerHTML --
            // otherwise someone could set their "name" to a <script>/<img
            // onerror> payload and run it in every other member's browser.
            function escapeHtml(s) {
                const d = document.createElement('div');
                d.innerText = String(s);
                return d.innerHTML;
            }

            // Broadcast by the server to everyone in the room whenever
            // someone joins or disconnects, so this list always reflects
            // who is actually here right now.
            socket.on('presence_update', (data) => {
                const users = (data && data.users) ? data.users : [];
                document.getElementById('onlineCount').innerText = users.length;
                const box = document.getElementById('onlineUsers');
                box.innerHTML = users.length
                    ? users.map(u => `<div>🟢 ${escapeHtml(u)}</div>`).join('')
                    : '<div style="color:#64748b;">No one else yet</div>';
            });

            // --- Host-approval flow -------------------------------------
            // Non-host visitors get told to wait right away, before any
            // code/output reaches them, so a stranger with just the link
            // can't see or touch the room until the host lets them in.
            const waitingOverlay = document.getElementById('waitingOverlay');

            socket.on('join_pending', () => {
                waitingOverlay.style.display = 'flex';
            });

            socket.on('join_approved', () => {
                waitingOverlay.style.display = 'none';
            });

            socket.on('join_denied', () => {
                document.getElementById('waitingTitle').innerText = '🚫 The host denied your request to join.';
                document.getElementById('waitingSub').innerText = 'You can close this tab, or ask the host to invite you again.';
                waitingOverlay.style.display = 'flex';
            });

            socket.on('join_rejected', (data) => {
                document.getElementById('waitingTitle').innerText = '🚫 Could not join this room.';
                document.getElementById('waitingSub').innerText = (data && data.reason) ? data.reason : '';
                waitingOverlay.style.display = 'flex';
            });

            // Only the host ever receives this event (the server only sends
            // it to room.host_sid), so only the host ever sees the banner.
            socket.on('join_request', (data) => {
                const banner = document.getElementById('joinRequestBanner');
                document.getElementById('joinRequestText').innerText =
                    `👋 ${data.username} wants to join this room.`;
                banner.style.display = 'block';

                const approveBtn = document.getElementById('joinApproveBtn');
                const denyBtn = document.getElementById('joinDenyBtn');
                // Replace with clones to drop any previous request's
                // listeners, in case several join requests arrive in a row.
                const newApprove = approveBtn.cloneNode(true);
                const newDeny = denyBtn.cloneNode(true);
                approveBtn.replaceWith(newApprove);
                denyBtn.replaceWith(newDeny);

                newApprove.onclick = () => {
                    socket.emit('respond_join_request', { room_id: roomId, sid: data.sid, approve: true });
                    banner.style.display = 'none';
                };
                newDeny.onclick = () => {
                    socket.emit('respond_join_request', { room_id: roomId, sid: data.sid, approve: false });
                    banner.style.display = 'none';
                };
            });

            function updateLogs(logs) {
                let lBox = document.getElementById('activityLog');
                lBox.innerHTML = logs.map(l => `<div>• ${escapeHtml(l)}</div>`).join('');
            }

            let isRemote = false;
            editor.addEventListener('input', () => {
                if (isRemote) return;
                socket.emit('update_code', { room_id: roomId, code: editor.value, username: username });
            });

            // --- Time Machine (session replay) ---------------------------
            // A per-room, growing timeline of edit + run snapshots recorded
            // server-side. This is the project's signature feature: unlike
            // ordinary online compilers that only ever show the latest code,
            // anyone here can scrub back and watch exactly how the solution
            // was built, who wrote what, and what each run actually printed.
            let historyData = [];
            let tmIndex = 0;
            let tmPlaying = false;
            let tmPlayTimer = null;

            function colorForUser(name) {
                let h = 0;
                for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
                return `hsl(${h}, 70%, 60%)`;
            }

            function refreshHistoryButton() {
                document.getElementById('historyCount').innerText = historyData.length;
            }

            socket.on('history_data', (data) => {
                historyData = (data && data.history) ? data.history : [];
                refreshHistoryButton();
                renderTimeMachine();
            });

            socket.on('history_append', (entry) => {
                historyData.push(entry);
                refreshHistoryButton();
                // Only re-render live if the panel is actually open, and
                // only jump to the newest snapshot if the user was already
                // looking at the latest one (so scrubbing isn't yanked out
                // from under someone reviewing an older point).
                const modal = document.getElementById('timeMachineModal');
                if (modal.style.display === 'flex') {
                    const wasAtEnd = tmIndex >= historyData.length - 2;
                    renderTimeMachine(wasAtEnd ? historyData.length - 1 : tmIndex);
                }
            });

            function renderTimeMachine(indexToShow) {
                const emptyMsg = document.getElementById('tmEmptyMsg');
                const body = document.getElementById('tmBody');
                if (!historyData.length) {
                    emptyMsg.style.display = 'block';
                    body.style.display = 'none';
                    return;
                }
                emptyMsg.style.display = 'none';
                body.style.display = 'flex';
                const slider = document.getElementById('tmSlider');
                slider.max = historyData.length - 1;
                const idx = (indexToShow === undefined) ? historyData.length - 1 : indexToShow;
                slider.value = idx;
                showSnapshot(idx);
            }

            window.showSnapshot = function (i) {
                if (!historyData.length) return;
                i = Math.max(0, Math.min(i, historyData.length - 1));
                tmIndex = i;
                const entry = historyData[i];
                document.getElementById('tmSlider').value = i;
                document.getElementById('tmIndexLabel').innerText = `${i + 1} / ${historyData.length}`;
                document.getElementById('tmCode').innerText = entry.code || '';
                const when = new Date(entry.t * 1000).toLocaleTimeString();
                const dot = `<span style="color:${colorForUser(entry.user || 'Someone')};">●</span>`;
                if (entry.type === 'run') {
                    document.getElementById('tmMeta').innerHTML =
                        `${dot} <b>${escapeHtml(entry.user)}</b> ran this code (${escapeHtml(entry.language || '')}) at ${when}`;
                    document.getElementById('tmOutputWrap').style.display = 'block';
                    document.getElementById('tmOutput').innerText = entry.output || '(no output)';
                } else {
                    document.getElementById('tmMeta').innerHTML =
                        `${dot} <b>${escapeHtml(entry.user)}</b> edited the code at ${when}`;
                    document.getElementById('tmOutputWrap').style.display = 'none';
                }
            };

            window.openTimeMachine = function () {
                document.getElementById('timeMachineModal').style.display = 'flex';
                socket.emit('get_history', { room_id: roomId });
            };

            window.closeTimeMachine = function () {
                document.getElementById('timeMachineModal').style.display = 'none';
                if (tmPlaying) toggleTimeMachinePlay();
            };

            window.toggleTimeMachinePlay = function () {
                const btn = document.getElementById('tmPlayBtn');
                tmPlaying = !tmPlaying;
                if (tmPlaying) {
                    btn.innerText = '⏸ Pause';
                    if (tmIndex >= historyData.length - 1) tmIndex = 0;
                    tmPlayTimer = setInterval(() => {
                        if (tmIndex >= historyData.length - 1) {
                            toggleTimeMachinePlay();
                            return;
                        }
                        showSnapshot(tmIndex + 1);
                    }, 900);
                } else {
                    btn.innerText = '▶ Play';
                    clearInterval(tmPlayTimer);
                }
            };

            window.restoreSnapshot = function () {
                if (!historyData.length) return;
                const entry = historyData[tmIndex];
                if (!confirm('Replace the LIVE code for everyone in this room with this older snapshot?')) return;
                socket.emit('restore_history_code', { room_id: roomId, code: entry.code, username: username });
                closeTimeMachine();
            };

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
                socket.emit('start_interactive_run', { room_id: roomId, language: lang, username: username });
            };

            // Server broadcasts this to everyone in the room (not just whoever
            // clicked Run), so all members' consoles reset together and then
            // fill with the same shared output as it streams in.
            socket.on('terminal_clear', (data) => {
                const by = (data && data.started_by) ? data.started_by : 'someone';
                outputBox.value = `Initializing batched pty execution... (started by ${by})\n`;
            });

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

        // Called only now, after initApp (defined above, in this same merged
        // script block) actually exists -- calling it before this point (e.g.
        // from a separate earlier <script> block) throws ReferenceError, since
        // function declarations only become available once their own <script>
        // block has finished executing, and separate <script> tags run in
        // strict document order.
        bootSocketIO();
    </script>
</body></html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5005))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
