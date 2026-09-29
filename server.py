# -*- coding: utf-8 -*-
"""server.py — 北斗代理事项闭环看板后端"""
from flask import Flask, jsonify, request, Response, abort, send_file
from pathlib import Path
from datetime import datetime
import json, re, threading, os, io, base64, hmac, hashlib, time, smtplib

BASE           = Path(__file__).parent
DATA           = BASE / 'data.json'
LOG_FILE       = BASE / 'oplog.json'
CONTACTS_FILE  = BASE / 'contacts.json'
BACKUP         = BASE / 'backup'
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'livox2026')
TEMPLATE_FILE  = BASE / 'template.xlsx'
# GitHub 自动同步配置（在 Render 环境变量中设置）
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO  = os.environ.get('GITHUB_REPO', 'Dandychen-livox/beidou-kanban')
# 会话签名密钥（用于邮箱登录后签发免密会话token，建议在 Render 环境变量单独配置 SESSION_SECRET）
SESSION_SECRET = os.environ.get('SESSION_SECRET') or ADMIN_PASSWORD or 'beidou-kanban-session-secret'
SESSION_TTL    = 30 * 86400  # 会话有效期30天
# 兜底管理员邮箱（即使 contacts.json 被误改，这些邮箱依然保留管理员权限，避免管理员被意外锁定）
FALLBACK_ADMIN_EMAILS = set(
    e.strip().lower() for e in os.environ.get(
        'ADMIN_EMAILS',
        'xiaodanchen@livoxtech.com,songzhiyu@livoxtech.com,jessica.li@livoxtech.com'
    ).split(',') if e.strip()
)

# ── 邮件通知配置（全部通过 Render 环境变量注入，不写入代码/仓库） ──
SMTP_HOST      = os.environ.get('SMTP_HOST', '')
SMTP_PORT      = int(os.environ.get('SMTP_PORT', '465') or '465')
SMTP_USER      = os.environ.get('SMTP_USER', '')
SMTP_PASS      = os.environ.get('SMTP_PASS', '')
SMTP_SSL       = os.environ.get('SMTP_SSL', '1') == '1'   # 1=SSL(465)，0=STARTTLS(587)
MAIL_FROM      = os.environ.get('MAIL_FROM', '') or SMTP_USER
MAIL_FROM_NAME = os.environ.get('MAIL_FROM_NAME', '北斗代理事项闭环看板')
SITE_URL       = os.environ.get('SITE_URL', 'https://beidou-kanban.onrender.com')
CRON_SECRET    = os.environ.get('CRON_SECRET', '')        # 定时任务调用密钥

_lock        = threading.Lock()
_sync_lock   = threading.Lock()   # 防止并发 push

app = Flask(__name__, static_folder=str(BASE), static_url_path='')

# ── 数据读写 ──
def read_data():
    if not DATA.exists(): return []
    return json.loads(DATA.read_text(encoding='utf-8'))

def write_data(rows):
    BACKUP.mkdir(exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    (BACKUP / f'data_{ts}.json').write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    DATA.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    # 异步同步到 GitHub（不阻塞请求）
    threading.Thread(target=lambda: _sync_file_to_github(DATA, 'data.json'), daemon=True).start()

# ── GitHub 双向同步（Render 免费版磁盘为临时磁盘，休眠/重启后本地文件会被重置为
#    上一次部署镜像内打包的旧数据；因此必须做到：启动时从 GitHub 拉取最新数据
#    写回本地，每次写入后再把本地最新数据推回 GitHub，才能保证数据不丢失） ──

def _github_get(path):
    """读取 GitHub 仓库中某文件的内容与 sha，失败返回 (None, '')"""
    import urllib.request
    api_url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{path}'
    req = urllib.request.Request(api_url)
    req.add_header('Authorization', f'token {GITHUB_TOKEN}')
    req.add_header('Accept', 'application/vnd.github.v3+json')
    with urllib.request.urlopen(req, timeout=10) as r:
        info = json.loads(r.read())
    content = base64.b64decode(info['content'])
    return content, info.get('sha', '')

def _github_put(path, content_bytes, sha, message):
    import urllib.request
    api_url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{path}'
    body = json.dumps({
        'message': message,
        'content': base64.b64encode(content_bytes).decode(),
        'sha': sha
    }).encode()
    req = urllib.request.Request(api_url, data=body, method='PUT')
    req.add_header('Authorization', f'token {GITHUB_TOKEN}')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Accept', 'application/vnd.github.v3+json')
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status

def _sync_file_to_github(local_path, github_path):
    """把本地文件推送到 GitHub（每次写入后异步调用，不阻塞请求）"""
    if not GITHUB_TOKEN:
        return
    try:
        with _sync_lock:
            content_raw = local_path.read_bytes()
            try:
                _, sha = _github_get(github_path)
            except Exception:
                sha = ''
            msg = f'Auto-sync {github_path} {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
            try:
                _github_put(github_path, content_raw, sha, msg)
            except Exception as e:
                # sha 可能因并发写入而过期，重试一次
                try:
                    _, sha2 = _github_get(github_path)
                    _github_put(github_path, content_raw, sha2, msg)
                except Exception as e2:
                    print(f'[sync-to-github] 推送 {github_path} 失败：{e2}', flush=True)
    except Exception as e:
        print(f'[sync-to-github] 推送 {github_path} 异常：{e}', flush=True)

def _sync_file_from_github(local_path, github_path):
    """启动时从 GitHub 拉取最新文件覆盖本地（GitHub 才是持久化的数据源，
       本地磁盘随时可能因 Render 重启/休眠被重置为旧的部署快照）"""
    if not GITHUB_TOKEN:
        print(f'[sync-from-github] 未配置 GITHUB_TOKEN，跳过拉取 {github_path}，使用本地文件', flush=True)
        return
    try:
        content, _ = _github_get(github_path)
        rows = json.loads(content.decode('utf-8'))  # 校验 JSON 合法
        local_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'[sync-from-github] 启动拉取 {github_path} 成功，共 {len(rows)} 条', flush=True)
    except Exception as e:
        print(f'[sync-from-github] 启动拉取 {github_path} 失败，使用本地文件：{e}', flush=True)

# 服务启动时立即从 GitHub 拉取最新数据（在任何请求处理之前执行）
_sync_file_from_github(DATA, 'data.json')
_sync_file_from_github(LOG_FILE, 'oplog.json')
_sync_file_from_github(CONTACTS_FILE, 'contacts.json')

# ── 通讯录 / 登录权限（管理员可在“权限管理”里增删邮箱） ──
def read_contacts():
    if not CONTACTS_FILE.exists(): return []
    try:
        return json.loads(CONTACTS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []

def write_contacts(contacts):
    CONTACTS_FILE.write_text(json.dumps(contacts, ensure_ascii=False, indent=2), encoding='utf-8')
    threading.Thread(target=lambda: _sync_file_to_github(CONTACTS_FILE, 'contacts.json'), daemon=True).start()

def contact_by_email(email):
    email = (email or '').strip().lower()
    for c in read_contacts():
        if (c.get('email') or '').strip().lower() == email:
            return c
    return None

def is_admin_email(email):
    email = (email or '').strip().lower()
    if email in FALLBACK_ADMIN_EMAILS:
        return True
    c = contact_by_email(email)
    return bool(c and c.get('role') == 'admin')

# ── 会话签名（邮箱登录成功后签发，30天内免密） ──
def make_session(email):
    email = email.strip().lower()
    exp = int(time.time()) + SESSION_TTL
    payload = f'{email}|{exp}'
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    raw = f'{payload}|{sig}'
    return base64.urlsafe_b64encode(raw.encode()).decode()

def verify_session(token):
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        email, exp, sig = raw.rsplit('|', 2)
        expect = hmac.new(SESSION_SECRET.encode(), f'{email}|{exp}'.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect):
            return None
        if int(exp) < time.time():
            return None
    except Exception:
        return None
    c = contact_by_email(email)
    if is_admin_email(email):
        role = 'admin'
        name = (c or {}).get('name') or email.split('@')[0]
    elif c:
        role = 'person'
        name = c.get('name') or email.split('@')[0]
    else:
        return None  # 邮箱已被从名单中移除，会话失效
    return {'email': email, 'role': role, 'name': name}

def require_session(req):
    sess = verify_session(req.headers.get('X-Session', ''))
    if not sess:
        abort(401)
    return sess

# ── 邮件发送 ──
def mail_configured():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)

def send_mail(to_list, subject, html, bcc=False):
    """发送一封 HTML 邮件。to_list 为收件邮箱列表；bcc=True 时收件人互相不可见。"""
    to_list = [e for e in dict.fromkeys([ (t or '').strip() for t in (to_list or []) ]) if e]
    if not to_list:
        return False, '没有收件人'
    if not mail_configured():
        return False, 'SMTP 未配置'
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.header import Header
    from email.utils import formataddr
    msg = MIMEMultipart('alternative')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = formataddr((str(Header(MAIL_FROM_NAME, 'utf-8')), MAIL_FROM))
    if bcc:
        msg['To'] = MAIL_FROM
    else:
        msg['To'] = ', '.join(to_list)
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    try:
        if SMTP_SSL:
            srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=25)
        else:
            srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=25)
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
        srv.login(SMTP_USER, SMTP_PASS)
        srv.sendmail(MAIL_FROM, to_list, msg.as_string())
        srv.quit()
        return True, ''
    except Exception as e:
        print(f'[mail] 发送失败：{e}', flush=True)
        return False, str(e)

def email_for_person(name):
    """把事项里的责任人姓名，映射到通讯录里的邮箱"""
    name = (name or '').strip()
    if not name:
        return None
    contacts = read_contacts()
    for c in contacts:  # 先精确匹配
        cn = (c.get('name') or '').strip()
        ce = (c.get('email') or '').strip()
        if ce and cn and cn.lower() == name.lower():
            return ce
    for c in contacts:  # 再匹配邮箱前缀
        ce = (c.get('email') or '').strip()
        if ce and ce.split('@')[0].lower() == name.lower():
            return ce
    for c in contacts:  # 最后做宽松包含匹配
        cn = (c.get('name') or '').strip().lower()
        ce = (c.get('email') or '').strip()
        if not ce or not cn:
            continue
        if (len(name) >= 2 and name.lower() in cn) or (len(cn) >= 2 and cn in name.lower()):
            return ce
    return None

def all_contact_emails():
    return [ (c.get('email') or '').strip() for c in read_contacts() if (c.get('email') or '').strip() ]

# ── 统计与到期判断（与前端口径保持一致） ──
def parse_ddl_date(ddl):
    s = (ddl or '').strip()
    if not s:
        return None
    m = re.match(r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$', s)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
    except Exception:
        return None

def ddl_status(row):
    """返回 ('overdue'|'soon'|'normal'|'none', 剩余天数)。已完成/挂起不算逾期。"""
    if row.get('status') in ('完成', '挂起'):
        return ('done', None)
    d = parse_ddl_date(row.get('ddl'))
    if d is None:
        return ('none', None)
    days = (d - datetime.now().date()).days
    if days < 0:
        return ('overdue', days)
    if days <= 3:
        return ('soon', days)
    return ('normal', days)

def is_row_done(row):
    return row.get('status') == '完成' and row.get('livox_confirm') == '完成'

def split_persons(person):
    return [p.strip() for p in re.split(r'[&/、,，]', person or '') if p.strip()]

def compute_overall(rows):
    st = {'total': 0, 'recur': 0, 'todo': 0, 'done': 0, 'hold': 0, 'doing': 0, 'overdue': 0, 'soon': 0, 'normal': 0}
    for r in rows:
        st['total'] += 1
        if r.get('recurring'):
            st['recur'] += 1
        else:
            st['todo'] += 1
        if is_row_done(r):
            st['done'] += 1
        elif r.get('status') == '挂起':
            st['hold'] += 1
        else:
            st['doing'] += 1
            t, _ = ddl_status(r)
            if t == 'overdue':
                st['overdue'] += 1
            elif t == 'soon':
                st['soon'] += 1
            else:
                st['normal'] += 1
    return st

# ── 操作日志 ──
def read_log():
    if not LOG_FILE.exists(): return []
    try:
        return json.loads(LOG_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []

def write_log(entry):
    logs = read_log()
    logs.append(entry)
    logs = logs[-500:]
    LOG_FILE.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding='utf-8')
    threading.Thread(target=lambda: _sync_file_to_github(LOG_FILE, 'oplog.json'), daemon=True).start()

def add_log(operator, action, item_id=None, item_name=None, detail=None):
    try:
        write_log({
            'time':      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'operator':  operator,
            'action':    action,
            'item_id':   item_id,
            'item_name': item_name,
            'detail':    detail or ''
        })
    except Exception:
        pass

def get_caller(req):
    """从会话中获取当前登录人姓名（登录邮箱对应的通讯录姓名）"""
    sess = verify_session(req.headers.get('X-Session', ''))
    return sess['name'] if sess else '匿名'

def is_admin(req):
    sess = verify_session(req.headers.get('X-Session', ''))
    return bool(sess and sess['role'] == 'admin')

# ── 优先级 / 完成时间 ──
PRIORITIES = ('高', '中', '低')
PERIOD_RE  = re.compile(r'每|周|月')
DATE_RE    = re.compile(r'^\d{4}-\d{1,2}-\d{1,2}$')

def norm_priority(v):
    return v if v in PRIORITIES else '中'

def norm_bool(v):
    if isinstance(v, bool): return v
    return str(v).strip() in ('1', 'true', 'True', '是', 'TRUE', 'yes', 'Yes', 'on')

def apply_completion_state(row):
    """维护 completed_at：状态与Livox确认双重变为“完成”时记录完成时间，
       任一方离开“完成”则清空，供前端判断“完成超过一周”是否默认隐藏"""
    done = row.get('status') == '完成' and row.get('livox_confirm') == '完成'
    if done:
        if not row.get('completed_at'):
            row['completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
    else:
        row['completed_at'] = ''

def apply_status_completion(row):
    """记录责任人/参与人把闭环状态填写为“完成”的时间（status_done_at），
       不依赖Livox是否二次确认；后续再编辑进展等其它字段也不会覆盖这个时间，
       只有状态离开“完成”再重新变回“完成”时才会更新为新的时间"""
    if row.get('status') == '完成':
        if not row.get('status_done_at'):
            row['status_done_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
    else:
        row['status_done_at'] = ''

def _migrate_data():
    """兼容旧数据：补齐 priority / completed_at / status_done_at / recurring / recur_note 字段
       （旧的已完成事项用 updated_at 作为完成时间近似值；旧的“点状待办”规则——DDL 含“每/周/月”
       字样或为空——统一识别为“重复性待办”，原描述文字迁移到 recur_note，DDL 清空待人工填写
       真实的下次到期日期，不再用文字描述冒充截止时间）"""
    rows = read_data()
    changed = False
    for row in rows:
        if row.get('priority') not in PRIORITIES:
            row['priority'] = '中'
            changed = True
        done = row.get('status') == '完成' and row.get('livox_confirm') == '完成'
        if done:
            if not row.get('completed_at'):
                row['completed_at'] = row.get('updated_at') or datetime.now().strftime('%Y-%m-%d %H:%M')
                changed = True
        elif row.get('completed_at'):
            row['completed_at'] = ''
            changed = True
        if row.get('status') == '完成':
            if not row.get('status_done_at'):
                row['status_done_at'] = row.get('updated_at') or datetime.now().strftime('%Y-%m-%d %H:%M')
                changed = True
        elif row.get('status_done_at'):
            row['status_done_at'] = ''
            changed = True
        if 'recurring' not in row:
            ddl = (row.get('ddl') or '').strip()
            if ddl and PERIOD_RE.search(ddl) and not DATE_RE.match(ddl):
                row['recurring']  = True
                row['recur_note'] = ddl
                row['ddl'] = ''
            else:
                row['recurring']  = False
                row.setdefault('recur_note', '')
            changed = True
        if 'initiator' not in row:
            row['initiator'] = ''
            changed = True
    if changed:
        write_data(rows)
        print(f'[migrate] 已为 {len(rows)} 条事项补齐 priority/completed_at/status_done_at/recurring/initiator 字段', flush=True)

_migrate_data()

# ── 路由 ──

@app.route('/')
def index():
    content = (BASE / 'kanban.html').read_text(encoding='utf-8')
    return Response(content, mimetype='text/html; charset=utf-8')

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/api/data')
def api_data():
    require_session(request)
    return jsonify(read_data())

@app.route('/api/auth', methods=['POST'])
def api_auth():
    """邮箱登录：
       - 普通联系人邮箱：直接登录，身份为“责任人”
       - 管理员邮箱：先返回 need_password，前端再带上密码二次提交完成双重验证
       - 不在名单里的邮箱：拒绝登录"""
    body     = request.get_json(force=True) or {}
    email    = (body.get('email') or '').strip().lower()
    password = body.get('password')
    if not email:
        return jsonify({'ok': False, 'msg': '请输入邮箱'}), 400
    contact = contact_by_email(email)
    admin_candidate = is_admin_email(email)
    if not contact and not admin_candidate:
        return jsonify({'ok': False, 'msg': '该邮箱未在授权名单中，请联系管理员开通权限'}), 403
    name = (contact or {}).get('name') or email.split('@')[0]
    if admin_candidate:
        if password is None:
            return jsonify({'ok': True, 'need_password': True})
        if password != ADMIN_PASSWORD:
            return jsonify({'ok': False, 'msg': '管理员密码错误'}), 401
        token = make_session(email)
        add_log(name, '登录', detail=email + '（管理员）')
        return jsonify({'ok': True, 'role': 'admin', 'name': name, 'token': token})
    token = make_session(email)
    add_log(name, '登录', detail=email)
    return jsonify({'ok': True, 'role': 'person', 'name': name, 'token': token})

@app.route('/api/log')
def api_log():
    if not is_admin(request): abort(403)
    logs = read_log()
    logs.reverse()
    return jsonify(logs)

@app.route('/api/contacts')
def api_contacts():
    if not is_admin(request): abort(403)
    return jsonify(read_contacts())

@app.route('/api/contacts', methods=['POST'])
def api_contacts_add():
    if not is_admin(request): abort(403)
    body  = request.get_json(force=True) or {}
    email = (body.get('email') or '').strip().lower()
    name  = (body.get('name') or '').strip()
    role  = body.get('role') if body.get('role') in ('admin', 'person') else 'person'
    if not email or not name:
        return jsonify({'ok': False, 'msg': '姓名和邮箱不能为空'}), 400
    with _lock:
        contacts = read_contacts()
        if any((c.get('email') or '').strip().lower() == email for c in contacts):
            return jsonify({'ok': False, 'msg': '该邮箱已存在'}), 400
        contacts.append({'name': name, 'email': email, 'role': role})
        write_contacts(contacts)
    add_log(get_caller(request), '新增权限', None, name, f'邮箱:{email}；角色:{"管理员" if role=="admin" else "责任人"}')
    return jsonify({'ok': True})

@app.route('/api/contacts/<path:email>', methods=['DELETE'])
def api_contacts_del(email):
    if not is_admin(request): abort(403)
    email = email.strip().lower()
    if email in FALLBACK_ADMIN_EMAILS:
        return jsonify({'ok': False, 'msg': '该邮箱为系统兜底管理员，不能移除'}), 400
    with _lock:
        contacts = read_contacts()
        remain = [c for c in contacts if (c.get('email') or '').strip().lower() != email]
        if len(remain) == len(contacts): abort(404)
        write_contacts(remain)
    add_log(get_caller(request), '删除权限', None, None, f'邮箱:{email}')
    return jsonify({'ok': True})

# ── 邮件通知：周报 & DDL 提醒 ──
def _cron_ok(req):
    """定时任务鉴权：校验 CRON_SECRET，或管理员登录态（便于手动触发测试）"""
    secret = req.headers.get('X-Cron-Secret', '') or req.args.get('secret', '')
    if CRON_SECRET and secret and hmac.compare_digest(secret, CRON_SECRET):
        return True
    return is_admin(req)

def _mail_style():
    return ("body{font-family:'Microsoft YaHei',Arial,sans-serif;font-size:14px;color:#222}"
            "table{border-collapse:collapse;width:100%;margin:10px 0}"
            "th{background:#1a3a8f;color:#fff;padding:8px 10px;font-size:13px;text-align:center}"
            "td{border:1px solid #e5eaf5;padding:7px 10px;font-size:13px;text-align:center}"
            "td.l{text-align:left}.red{color:#c62828;font-weight:700}.orange{color:#ef6c00;font-weight:700}"
            ".green{color:#2e7d32;font-weight:700}.title{font-size:17px;font-weight:700;color:#1a3a8f}"
            ".sub{color:#888;font-size:12px;margin:6px 0 14px}")

def build_weekly_html(rows):
    st = compute_overall(rows)
    person_map = {}
    for r in rows:
        for name in split_persons(r.get('person')):
            m = person_map.setdefault(name, {'total': 0, 'done': 0, 'doing': 0, 'overdue': 0, 'soon': 0})
            m['total'] += 1
            if is_row_done(r):
                m['done'] += 1
            elif r.get('status') != '挂起':
                m['doing'] += 1
                t, _ = ddl_status(r)
                if t == 'overdue':
                    m['overdue'] += 1
                elif t == 'soon':
                    m['soon'] += 1
    today = datetime.now().strftime('%Y-%m-%d')
    rows_html = ''.join(
        f'<tr><td class="l">{n}</td><td>{m["total"]}</td><td>{m["done"]}</td>'
        f'<td>{m["doing"]}</td><td class="{"red" if m["overdue"] else ""}">{m["overdue"]}</td>'
        f'<td class="{"orange" if m["soon"] else ""}">{m["soon"]}</td></tr>'
        for n, m in sorted(person_map.items())
    )
    return f"""<html><head><style>{_mail_style()}</style></head><body>
<div class="title">【北斗代理事项闭环看板】本周待办完成情况</div>
<div class="sub">统计日期：{today} ｜ 数据来源：<a href="{SITE_URL}">{SITE_URL}</a></div>
<table>
<tr><th>待办总数</th><th>待办事项</th><th>重复性待办</th><th>已完成</th><th>挂起</th><th>进行中</th><th>逾期</th><th>临期(≤3天)</th></tr>
<tr><td>{st['total']}</td><td>{st['todo']}</td><td>{st['recur']}</td>
<td class="green">{st['done']}</td><td>{st['hold']}</td><td>{st['doing']}</td>
<td class="red">{st['overdue']}</td><td class="orange">{st['soon']}</td></tr>
</table>
<div style="margin-top:16px;font-weight:700">按责任人统计</div>
<table>
<tr><th>责任人</th><th>待办总数</th><th>已完成</th><th>进行中</th><th>逾期</th><th>临期</th></tr>
{rows_html}
</table>
<div class="sub">注：已完成 = 责任人标记完成且对接人确认完成；逾期 = 已过 DDL 且未完成。</div>
</body></html>"""

def collect_reminders(rows):
    """返回 {责任人: [提醒条目]}。触发条件：距 DDL 恰好 2 天，或已逾期（未完成、未挂起）。"""
    today = datetime.now().date()
    result = {}
    for r in rows:
        if r.get('status') in ('完成', '挂起'):
            continue
        d = parse_ddl_date(r.get('ddl'))
        if d is None:
            continue
        days = (d - today).days
        if days == 2:
            kind, label = 'soon', f'还有 2 天到期（{r.get("ddl")}）'
        elif days < 0:
            kind, label = 'overdue', f'已逾期 {-days} 天（DDL {r.get("ddl")}）'
        else:
            continue
        for name in split_persons(r.get('person')):
            result.setdefault(name, []).append({
                'item': r.get('item', ''), 'ddl': r.get('ddl', ''),
                'days': days, 'kind': kind, 'label': label,
                'progress': r.get('progress', ''),
            })
    return result

def build_reminder_html(name, items):
    li = ''.join(
        f'<tr><td class="l">{it["item"]}</td><td class="l">{it["label"]}</td>'
        f'<td class="l" style="color:#666">{(it["progress"] or "暂无进展")[:60]}</td></tr>'
        for it in items
    )
    return f"""<html><head><style>{_mail_style()}</style></head><body>
<div class="title">【待办提醒】以下事项即将到期或已逾期</div>
<div class="sub">{name} 您好，请及时跟进处理：</div>
<table><tr><th>事项</th><th>到期情况</th><th>当前进展</th></tr>{li}</table>
<div class="sub">请登录看板更新进展：<a href="{SITE_URL}">{SITE_URL}</a></div>
</body></html>"""

@app.route('/api/notify/weekly', methods=['POST', 'GET'])
def api_notify_weekly():
    if not _cron_ok(request): abort(403)
    rows = read_data()
    recipients = all_contact_emails()
    if not recipients:
        return jsonify({'ok': False, 'msg': '通讯录中没有邮箱'}), 400
    subject = f'【北斗看板】本周待办完成情况（{datetime.now().strftime("%Y-%m-%d")}）'
    ok, err = send_mail(recipients, subject, build_weekly_html(rows), bcc=True)
    add_log('系统', '发送周报', None, None, f'收件{len(recipients)}人；{"成功" if ok else "失败:" + err}')
    return jsonify({'ok': ok, 'recipients': len(recipients), 'msg': err})

@app.route('/api/notify/reminders', methods=['POST', 'GET'])
def api_notify_reminders():
    if not _cron_ok(request): abort(403)
    rows = read_data()
    grouped = collect_reminders(rows)
    sent, skipped, failed = 0, [], []
    for name, items in grouped.items():
        email = email_for_person(name)
        if not email:
            skipped.append(name)
            continue
        subject = f'【待办提醒】{name} 有 {len(items)} 项待办临近/逾期'
        ok, err = send_mail([email], subject, build_reminder_html(name, items))
        if ok:
            sent += 1
        else:
            failed.append(name)
    detail = f'通知{len(grouped)}人，成功{sent}'
    if skipped:
        detail += f'；未匹配邮箱:{"/".join(skipped)}'
    if failed:
        detail += f'；发送失败:{"/".join(failed)}'
    add_log('系统', '发送DDL提醒', None, None, detail)
    return jsonify({'ok': True, 'notified_people': len(grouped), 'sent': sent,
                    'skipped_no_email': skipped, 'failed': failed})

@app.route('/api/notify/test', methods=['POST'])
def api_notify_test():
    sess = require_session(request)
    if sess['role'] != 'admin': abort(403)
    if not mail_configured():
        return jsonify({'ok': False, 'msg': '邮件服务尚未配置（缺少 SMTP_HOST/SMTP_USER/SMTP_PASS 环境变量）'}), 400
    body = request.get_json(silent=True) or {}
    to = (body.get('email') or sess['email']).strip()
    html = (f'<html><head><style>{_mail_style()}</style></head><body>'
            f'<div class="title">✅ 邮件发送测试成功</div>'
            f'<div class="sub">如果您收到这封邮件，说明看板的邮件通知功能已配置正确。</div>'
            f'<div class="sub">发送时间：{datetime.now().strftime("%Y-%m-%d %H:%M")}</div></body></html>')
    ok, err = send_mail([to], '【北斗看板】邮件通知测试', html)
    add_log(sess['name'], '邮件测试', None, None, f'发送至{to}：{"成功" if ok else "失败:" + err}')
    return jsonify({'ok': ok, 'msg': err, 'to': to})

@app.route('/api/public_update/<int:row_id>', methods=['POST'])
def api_public_update(row_id):
    sess   = require_session(request)
    caller = sess['name']
    body   = request.get_json(force=True) or {}
    allowed = {k: v for k, v in body.items() if k in ('person', 'progress', 'submit_url', 'status', 'priority')}
    if not allowed: abort(400)
    if 'priority' in allowed: allowed['priority'] = norm_priority(allowed['priority'])
    with _lock:
        rows = read_data()
        idx  = next((i for i, r in enumerate(rows) if str(r.get('id')) == str(row_id)), None)
        if idx is None: abort(404)
        row = rows[idx]
        row.update(allowed)
        row['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
        row['updated_by'] = caller
        apply_status_completion(row)
        apply_completion_state(row)
        rows[idx] = row
        write_data(rows)
    parts = []
    if 'progress'   in allowed: parts.append('进展:' + allowed['progress'][:30])
    if 'person'     in allowed: parts.append('责任人→' + allowed['person'])
    if 'status'     in allowed: parts.append('状态→' + allowed['status'])
    if 'priority'   in allowed: parts.append('优先级→' + allowed['priority'])
    if 'submit_url' in allowed: parts.append('提交物:' + allowed['submit_url'][:30])
    add_log(caller, '公开填写', row_id, row.get('item', '')[:20], '；'.join(parts))
    return jsonify({'ok': True, 'row': row})

@app.route('/api/update/<int:row_id>', methods=['POST'])
def api_update(row_id):
    sess   = require_session(request)
    admin  = sess['role'] == 'admin'
    caller = sess['name']
    body   = request.get_json(force=True) or {}
    with _lock:
        rows = read_data()
        idx  = next((i for i, r in enumerate(rows) if str(r.get('id')) == str(row_id)), None)
        if idx is None: abort(404)
        row = rows[idx]
        if not admin:
            ps = [p.strip() for p in re.split(r'[&/、,，]', row.get('person', '')) if p.strip()]
            if caller not in ps: abort(403)
            body = {k: v for k, v in body.items() if k in ('progress', 'status', 'livox_confirm', 'priority')}
        if 'priority' in body: body['priority'] = norm_priority(body['priority'])
        if 'recurring' in body: body['recurring'] = norm_bool(body['recurring'])
        row.update(body)
        row['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
        row['updated_by'] = caller
        apply_status_completion(row)
        apply_completion_state(row)
        rows[idx] = row
        write_data(rows)
    parts = []
    if 'progress'      in body: parts.append('进展:' + body['progress'][:30])
    if 'status'        in body: parts.append('状态→' + body['status'])
    if 'livox_confirm' in body: parts.append('Livox确认→' + body['livox_confirm'])
    if 'person'        in body: parts.append('责任人→' + body['person'])
    if 'priority'      in body: parts.append('优先级→' + body['priority'])
    add_log(caller, '编辑事项', row_id, row.get('item', '')[:20], '；'.join(parts))
    return jsonify({'ok': True, 'row': row})

@app.route('/api/add', methods=['POST'])
def api_add():
    if not is_admin(request): abort(403)
    caller = get_caller(request)
    body = request.get_json(force=True) or {}
    with _lock:
        rows   = read_data()
        new_id = max((r.get('id', 0) for r in rows), default=0) + 1
        row = {
            'id':           new_id,
            'date':         datetime.now().strftime('%Y-%m-%d'),
            'item':         body.get('item', ''),
            'submit':       body.get('submit', ''),
            'ddl':          body.get('ddl', ''),
            'initiator':    body.get('initiator', ''),
            'person':       body.get('person', ''),
            'livox':        body.get('livox', 'Dandy'),
            'priority':     norm_priority(body.get('priority', '中')),
            'recurring':    norm_bool(body.get('recurring', False)),
            'recur_note':   body.get('recur_note', ''),
            'progress':     '',
            'submit_url':   body.get('submit_url', ''),
            'status':       '未完成',
            'livox_confirm':'未完成',
            'completed_at': '',
            'status_done_at': '',
            'updated_at':   datetime.now().strftime('%Y-%m-%d %H:%M'),
            'updated_by':   caller
        }
        rows.append(row)
        write_data(rows)
    add_log(caller, '新增事项', new_id, row['item'][:20])
    return jsonify({'ok': True, 'row': row})

@app.route('/api/delete/<int:row_id>', methods=['DELETE'])
def api_delete(row_id):
    if not is_admin(request): abort(403)
    caller = get_caller(request)
    with _lock:
        rows = read_data()
        idx  = next((i for i, r in enumerate(rows) if str(r.get('id')) == str(row_id)), None)
        if idx is None: abort(404)
        item_name = rows[idx].get('item', '')[:20]
        rows.pop(idx)
        write_data(rows)
    add_log(caller, '删除事项', row_id, item_name)
    return jsonify({'ok': True})

def _xlsx_bytes_stdlib(headers, data_rows, widths, sheet_name='事项备份'):
    """不依赖第三方库，直接用 Python 标准库生成 .xlsx 文件（Excel 2007+ 格式）。
       这样即使服务器没装上 openpyxl，备份下载也能正常工作。"""
    import zipfile
    from xml.sax.saxutils import escape

    def clean(v):
        s = '' if v is None else str(v)
        return ''.join(ch for ch in s if ch >= ' ' or ch in '\t\n\r')

    def colref(n):
        s = ''
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s

    def cell_xml(ref, val, style):
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return f'<c r="{ref}" s="{style}"><v>{val}</v></c>'
        return (f'<c r="{ref}" t="inlineStr" s="{style}">'
                f'<is><t xml:space="preserve">{escape(clean(val))}</t></is></c>')

    rows_xml = []
    head_cells = ''.join(cell_xml(f'{colref(i+1)}1', h, 1) for i, h in enumerate(headers))
    rows_xml.append(f'<row r="1">{head_cells}</row>')
    for ri, row in enumerate(data_rows, start=2):
        cells = ''.join(cell_xml(f'{colref(ci+1)}{ri}', v, 2) for ci, v in enumerate(row))
        rows_xml.append(f'<row r="{ri}">{cells}</row>')
    cols_xml = ''.join(
        f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(widths)
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<cols>{cols_xml}</cols>'
        f'<sheetData>{"".join(rows_xml)}</sheetData>'
        '</worksheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="2">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1A3A8F"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="1" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types)
        z.writestr('_rels/.rels', root_rels)
        z.writestr('xl/workbook.xml', workbook)
        z.writestr('xl/_rels/workbook.xml.rels', wb_rels)
        z.writestr('xl/styles.xml', styles)
        z.writestr('xl/worksheets/sheet1.xml', sheet_xml)
    return buf.getvalue()

@app.route('/api/export')
def api_export():
    """一键备份：把当前全部事项导出为 Excel，供管理员下载存档"""
    if not is_admin(request): abort(403)
    rows = read_data()
    headers = ['ID', '事项名称', '发起人', '类型', '周期说明', '提交内容/要求', 'DDL', '责任人', 'LIVOX对接人',
               '优先级', '进展', '提交物链接', '闭环状态', 'Livox确认', '完成时间(责任人标记)',
               '完成时间(双重确认)', '最近更新时间', '最近更新人', '创建日期']
    widths = [6, 26, 14, 12, 12, 30, 12, 14, 12, 8, 30, 26, 10, 10, 16, 16, 16, 12, 12]
    data_rows = [[
        r.get('id', ''), r.get('item', ''), r.get('initiator', ''),
        '重复性待办' if r.get('recurring') else '待办事项',
        r.get('recur_note', ''), r.get('submit', ''), r.get('ddl', ''),
        r.get('person', ''), r.get('livox', ''), r.get('priority', '中'),
        r.get('progress', ''), r.get('submit_url', ''), r.get('status', ''),
        r.get('livox_confirm', ''), r.get('status_done_at', ''),
        r.get('completed_at', ''), r.get('updated_at', ''), r.get('updated_by', ''),
        r.get('date', ''),
    ] for r in rows]
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '事项备份'
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True, color='FFFFFF')
            c.fill = PatternFill('solid', fgColor='1A3A8F')
        for dr in data_rows:
            ws.append(dr)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
        buf = io.BytesIO()
        wb.save(buf)
        data = buf.getvalue()
    except Exception:
        # 未安装 openpyxl 时，用标准库兜底生成 Excel，保证备份功能始终可用
        data = _xlsx_bytes_stdlib(headers, data_rows, widths)
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    add_log(get_caller(request), '导出备份', None, None, f'共导出{len(rows)}条事项')
    return Response(data,
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment;filename=beidou-kanban-backup-{ts}.xlsx'})

@app.route('/api/template')
def api_template():
    if not is_admin(request): abort(403)
    if TEMPLATE_FILE.exists():
        return send_file(str(TEMPLATE_FILE), as_attachment=True,
                         download_name='事项明细模板.xlsx',
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '事项明细'
        headers = ['事项名称*', '提交内容/要求', 'DDL', '责任人', '发起人', 'LIVOX对接人', '优先级', '重复性待办', '周期说明', '进展', '提交物链接', '闭环状态']
        notes   = ['必填', '提交要求', '如：2026-08-15（一次性事项截止日/重复性待办下次到期日）', '如：赵云飞', '任务发起人，如：Dandy', '如：Dandy', '高/中/低，留空默认中', '是/否，留空默认否', '如：每月31日，仅重复性待办填写', '进展说明', 'https://...', '未完成/完成/挂起']
        for i, (h, n) in enumerate(zip(headers, notes), 1):
            ws.cell(1, i, h)
            ws.cell(2, i, n)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return Response(buf.read(),
                        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': 'attachment;filename=template.xlsx'})
    except Exception as e:
        abort(500, str(e))

@app.route('/api/batch_add', methods=['POST'])
def api_batch_add():
    if not is_admin(request): abort(403)
    parse_only = request.headers.get('X-Parse-Only', '') == '1'
    if request.content_type and 'application/json' in request.content_type:
        body = request.get_json(force=True) or {}
        if isinstance(body, list):
            return _do_batch(body)
        b64 = body.get('file_base64', '')
        po  = body.get('parse_only', parse_only)
        if b64:
            try:
                items = _parse_excel(io.BytesIO(base64.b64decode(b64)))
                if po: return jsonify({'ok': True, 'rows': items})
                return _do_batch(items)
            except Exception as e:
                return jsonify({'ok': False, 'msg': '解析失败：' + str(e)}), 400
        abort(400)
    f = request.files.get('file')
    if not f: abort(400)
    try:
        items = _parse_excel(f.stream)
        if parse_only: return jsonify({'ok': True, 'rows': items})
        return _do_batch(items)
    except Exception as e:
        return jsonify({'ok': False, 'msg': '解析失败：' + str(e)}), 400

def _parse_excel(stream):
    import openpyxl
    wb  = openpyxl.load_workbook(stream, data_only=True)
    ws  = wb.active
    col_map = {}
    for c in range(1, ws.max_column + 1):
        v = str(ws.cell(1, c).value or '').strip()
        if v: col_map[v] = c
    FIELD_MAP = {
        '事项名称*': 'item', '事项名称': 'item',
        '提交内容/要求': 'submit', '提交内容': 'submit',
        'DDL': 'ddl', '截止日期': 'ddl',
        '责任人': 'person',
        '发起人': 'initiator', '任务发起人': 'initiator',
        'LIVOX对接人': 'livox', 'Livox对接人': 'livox',
        '优先级': 'priority',
        '重复性待办': 'recurring', '是否重复': 'recurring',
        '周期说明': 'recur_note', '周期': 'recur_note',
        '进展': 'progress',
        '提交物链接': 'submit_url', '提交物': 'submit_url',
        '闭环状态': 'status', '状态': 'status',
    }
    items = []
    for r in range(3, ws.max_row + 1):
        row_vals = {k: ws.cell(r, v).value for k, v in col_map.items()}
        if not any(row_vals.values()): continue
        item = {}
        for col_name, field in FIELD_MAP.items():
            if col_name in row_vals and row_vals[col_name] is not None:
                item[field] = str(row_vals[col_name]).strip()
        if not item.get('item'): continue
        items.append(item)
    return items

def _do_batch(items):
    if not items: return jsonify({'ok': False, 'msg': '没有可导入的数据'})
    caller = get_caller(request)
    added = []
    with _lock:
        rows   = read_data()
        cur_id = max((r.get('id', 0) for r in rows), default=0)
        for item in items:
            cur_id += 1
            status = item.get('status', '未完成')
            if status not in ('完成', '未完成', '挂起'): status = '未完成'
            row = {
                'id': cur_id, 'date': datetime.now().strftime('%Y-%m-%d'),
                'item': item.get('item', ''), 'submit': item.get('submit', ''),
                'ddl':  item.get('ddl', ''),  'person': item.get('person', ''),
                'initiator': item.get('initiator', ''),
                'livox': item.get('livox', 'Dandy'), 'priority': norm_priority(item.get('priority', '中')),
                'recurring': norm_bool(item.get('recurring', False)), 'recur_note': item.get('recur_note', ''),
                'progress': item.get('progress', ''),
                'submit_url': item.get('submit_url', ''), 'status': status,
                'livox_confirm': '未完成',
                'completed_at': '',
                'status_done_at': status=='完成' and datetime.now().strftime('%Y-%m-%d %H:%M') or '',
                'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
                'updated_by': caller + '(批量导入)',
            }
            rows.append(row)
            added.append(row)
        write_data(rows)
    add_log(caller, '批量导入', None, None, '导入' + str(len(added)) + '条事项')
    return jsonify({'ok': True, 'added': len(added), 'rows': added})

if __name__ == '__main__':
    print('=' * 50)
    print('北斗代理事项闭环看板 已启动')
    print('本机：http://127.0.0.1:8080')
    print('内网：http://192.168.255.10:8080')
    print('=' * 50)
    app.run(host='0.0.0.0', port=8080, debug=False)
