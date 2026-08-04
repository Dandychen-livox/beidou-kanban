# -*- coding: utf-8 -*-
"""server.py — 北斗代理事项闭环看板后端（本地 + Render 云端通用）"""
from flask import Flask, jsonify, request, send_from_directory, abort, Response
from pathlib import Path
from datetime import datetime
import json, re, threading, os

BASE   = Path(__file__).parent
DATA   = BASE / 'data.json'
BACKUP = BASE / 'backup'
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'livox2026')
_lock = threading.Lock()

app = Flask(__name__, static_folder=str(BASE), static_url_path='')

def read_data():
    if not DATA.exists(): return []
    return json.loads(DATA.read_text(encoding='utf-8'))

def write_data(rows):
    BACKUP.mkdir(exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    (BACKUP / f'data_{ts}.json').write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    DATA.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')

def is_admin(req):
    return req.headers.get('X-Admin-Token') == ADMIN_PASSWORD

@app.route('/')
def index():
    content = (BASE / 'kanban.html').read_text(encoding='utf-8')
    return Response(content, mimetype='text/html; charset=utf-8')

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/api/data')
def api_data():
    return jsonify(read_data())

@app.route('/api/auth', methods=['POST'])
def api_auth():
    body = request.get_json(force=True) or {}
    if body.get('password') == ADMIN_PASSWORD:
        return jsonify({'ok': True, 'token': ADMIN_PASSWORD})
    return jsonify({'ok': False, 'msg': '密码错误'}), 401

@app.route('/api/public_update/<int:row_id>', methods=['POST'])
def api_public_update(row_id):
    """公开编辑接口：任何人可填写 person / progress，无需登录"""
    body   = request.get_json(force=True) or {}
    caller = request.headers.get('X-Person', '').strip() or body.get('caller','匿名')
    # 只允许修改这两个字段
    allowed = {k:v for k,v in body.items() if k in ('person','progress','submit_url')}
    if not allowed:
        abort(400)
    with _lock:
        rows = read_data()
        idx  = next((i for i,r in enumerate(rows) if str(r.get('id'))==str(row_id)), None)
        if idx is None: abort(404)
        row = rows[idx]
        row.update(allowed)
        row['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
        row['updated_by'] = caller
        rows[idx] = row
        write_data(rows)
    return jsonify({'ok': True, 'row': row})



def api_update(row_id):
    body   = request.get_json(force=True) or {}
    admin  = is_admin(request)
    caller = request.headers.get('X-Person', '').strip()
    with _lock:
        rows = read_data()
        idx  = next((i for i,r in enumerate(rows) if str(r.get('id'))==str(row_id)), None)
        if idx is None: abort(404)
        row = rows[idx]
        if not admin:
            ps = [p.strip() for p in re.split(r'[&/、,，]', row.get('person','')) if p.strip()]
            if caller not in ps:
                abort(403)
            body = {k:v for k,v in body.items() if k in ('progress','status','livox_confirm')}
        row.update(body)
        row['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
        row['updated_by'] = caller or ('管理员' if admin else '?')
        rows[idx] = row
        write_data(rows)
    return jsonify({'ok': True, 'row': row})

@app.route('/api/add', methods=['POST'])
def api_add():
    if not is_admin(request): abort(403)
    body = request.get_json(force=True) or {}
    with _lock:
        rows   = read_data()
        new_id = max((r.get('id',0) for r in rows), default=0) + 1
        row = {'id':new_id,'date':datetime.now().strftime('%Y-%m-%d'),
               'item':body.get('item',''),'submit':body.get('submit',''),
               'ddl':body.get('ddl',''),'person':body.get('person',''),
               'livox':body.get('livox','Dandy'),'progress':'',
               'submit_url':body.get('submit_url',''),'status':'未完成',
               'updated_at':datetime.now().strftime('%Y-%m-%d %H:%M'),'updated_by':'管理员'}
        rows.append(row)
        write_data(rows)
    return jsonify({'ok': True, 'row': row})

@app.route('/api/delete/<int:row_id>', methods=['DELETE'])
def api_delete(row_id):
    if not is_admin(request): abort(403)
    with _lock:
        rows = read_data()
        idx  = next((i for i,r in enumerate(rows) if str(r.get('id'))==str(row_id)), None)
        if idx is None: abort(404)
        rows.pop(idx)
        write_data(rows)
    return jsonify({'ok': True})

if __name__ == '__main__':
    print('='*50)
    print('北斗代理事项闭环看板 已启动')
    print('本机：http://127.0.0.1:8080')
    print('内网：http://192.168.255.10:8080')
    print('管理员密码：livox2026')
    print('='*50)
    app.run(host='0.0.0.0', port=8080, debug=False)
