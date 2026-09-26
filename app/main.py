#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG 频道转发小工具 —— 事件驱动，多规则，正则黑白名单
数据目录（挂载到 /data）: config.json / tg.session / forward.log
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime

from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   session, stream_with_context)
from telethon import TelegramClient, errors, events

DATA_DIR = os.environ.get('DATA_DIR', '/data')
CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')
SESSION_PATH = os.path.join(DATA_DIR, 'tg')
LOG_PATH = os.path.join(DATA_DIR, 'forward.log')
STATE_PATH = os.path.join(DATA_DIR, 'state.json')      # 补偿扫描的水位线（各源已处理到的最大消息 id）
# 用可重入锁：load_config() 在首次运行（还没有 config.json）时会调用 save_config()，
# 两者都要这把锁；普通 Lock 会在这里自锁死（表现为面板打不开、config.json 一直不生成）。
CONFIG_LOCK = threading.RLock()

LOG_KEEP_DEFAULT = 500          # 最近记录默认最多保留多少条（超出自动清理，0=不限）
RECONCILE_DEFAULT = 300         # 补偿扫描默认间隔（秒）；0=关闭

DEFAULT_CONFIG = {
    'web': {'password': os.environ.get('WEB_PASSWORD', 'tgforward'),
            'recovery_hash': '', 'recovery_salt': '', 'pw_ver': 1,
            'pw_changed_at': ''},
    'tg': {'api_id': int(os.environ.get('TG_API_ID', '0') or 0),
           'api_hash': os.environ.get('TG_API_HASH', ''),
           'phone': ''},
    'rules': [],
    'media_max_mb': 1800,
    'log_max_lines': LOG_KEEP_DEFAULT,
    'reconcile_seconds': RECONCILE_DEFAULT,   # 补偿扫描间隔（秒）；0=关闭
}

MIN_PW_LEN = 6
RESET_SCRIPT = os.environ.get('RESET_SCRIPT', '/data/reset_panel_password.sh')
LOG_LOCK = threading.Lock()
LOG_EPOCH = 0                   # 清空/自动清理时自增 → SSE 客户端收到会重新拉取
_log_count = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('tgforwarder')

os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------- config
def load_config():
    with CONFIG_LOCK:
        if not os.path.exists(CONFIG_PATH):
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            save_config(cfg)
            return cfg
        try:
            cfg = json.load(open(CONFIG_PATH, encoding='utf-8'))
        except Exception as e:
            log.error('读取 config.json 失败: %s', e)
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        for k, v in DEFAULT_CONFIG['web'].items():
            cfg['web'].setdefault(k, v)
        for k, v in DEFAULT_CONFIG['tg'].items():
            cfg['tg'].setdefault(k, v)
        return cfg


def save_config(cfg):
    with CONFIG_LOCK:
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except Exception:
            pass


# ---------------------------------------------------------------- state（补偿扫描水位线）
STATE_LOCK = threading.RLock()
_STATE = None


def load_state():
    """读取 state.json（各源频道已处理到的最大消息 id）。缺失/损坏都返回空结构，绝不抛。"""
    global _STATE
    with STATE_LOCK:
        if _STATE is not None:
            return _STATE
        try:
            with open(STATE_PATH, encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data.setdefault('processed', {})
        _STATE = data
        return _STATE


def save_state():
    """原子落盘 state.json（权限 600）。失败只记日志，不影响转发。"""
    with STATE_LOCK:
        if _STATE is None:
            return
        try:
            tmp = STATE_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(_STATE, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_PATH)
            try:
                os.chmod(STATE_PATH, 0o600)
            except Exception:
                pass
        except Exception as e:
            log.warning('写入 state.json 失败：%s', e)


def _norm_chan(chat_id):
    """把 -1001234567890 / 1234567890 归一化成同一串数字。
    与 Worker._norm_id 同规则，但做成模块级函数供水位线复用（避免调用方传原始 id 导致 key 不一致）。"""
    s = str(chat_id or '').strip()
    d = ''.join(ch for ch in s if ch.isdigit())
    if d.startswith('100') and len(d) > 11:
        d = d[3:]
    return d


def get_watermark(src_key):
    """取某个源频道「已处理到的最大消息 id」；没有返回 0。"""
    st = load_state()
    rec = (st.get('processed') or {}).get(_norm_chan(src_key)) or {}
    try:
        return int(rec.get('max_id') or 0)
    except Exception:
        return 0


def bump_watermark(src_key, max_id, flush=False):
    """抬高水位线（只增不减）。flush=True 时立即落盘。key 内部统一归一化。"""
    if not max_id:
        return
    k = _norm_chan(src_key)
    if not k:
        return
    with STATE_LOCK:
        st = load_state()
        rec = st['processed'].setdefault(k, {})
        try:
            cur = int(rec.get('max_id') or 0)
        except Exception:
            cur = 0
        if int(max_id) > cur:
            rec['max_id'] = int(max_id)
            rec['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if flush:
        save_state()



def log_keep():
    """最近记录上限（条）；0 表示不限"""
    try:
        n = int(load_config().get('log_max_lines', LOG_KEEP_DEFAULT))
    except Exception:
        n = LOG_KEEP_DEFAULT
    return max(0, n)


def log_count():
    global _log_count
    with LOG_LOCK:
        if _log_count is None:
            try:
                with open(LOG_PATH, encoding='utf-8', errors='replace') as f:
                    _log_count = sum(1 for _ in f)
            except Exception:
                _log_count = 0
        return _log_count


def trim_log(keep=None, force=False):
    """只保留最新 keep 条（默认取配置）；返回清理掉的条数"""
    global _log_count, LOG_EPOCH
    keep = log_keep() if keep is None else max(0, int(keep))
    if not os.path.exists(LOG_PATH):
        with LOG_LOCK:
            _log_count = 0
        return 0
    try:
        with open(LOG_PATH, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        log.error('读取 forward.log 失败: %s', e)
        return 0
    total = len(lines)
    if not force and (keep == 0 or total <= keep):
        with LOG_LOCK:
            _log_count = total
        return 0
    tail = lines[-keep:] if keep else []
    tmp = LOG_PATH + '.trim'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.writelines(tail)
        os.replace(tmp, LOG_PATH)
    except Exception as e:
        log.error('清理 forward.log 失败: %s', e)
        return 0
    with LOG_LOCK:
        _log_count = len(tail)
        LOG_EPOCH += 1
    log.info('最近记录自动清理：%d → %d 条', total, len(tail))
    return total - len(tail)


def clear_logs():
    """清空最近记录（面板「清空记录」按钮）；返回清掉多少条"""
    global _log_count, LOG_EPOCH
    n = log_count()
    try:
        with open(LOG_PATH, 'w', encoding='utf-8'):
            pass
    except Exception as e:
        log.error('清空 forward.log 失败: %s', e)
        raise
    with LOG_LOCK:
        _log_count = 0
        LOG_EPOCH += 1
    return n


def append_log(line):
    global _log_count
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write('[%s] %s\n' % (ts, line))
    RECENT.appendleft('[%s] %s' % (ts, line))
    keep = log_keep()
    with LOG_LOCK:
        _log_count = (0 if _log_count is None else _log_count) + 1
        over = bool(keep) and _log_count > keep
    if over:
        trim_log(keep)


def recent_lines(n=30):
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, encoding='utf-8') as f:
            lines = f.readlines()[-n:]
        return [l.rstrip('\n') for l in lines][::-1]
    except Exception:
        return list(RECENT)[:n]


RECENT = deque(maxlen=200)


# ---------------------------------------------------------------- 面板密码 / 恢复码
def gen_recovery_code():
    """生成人类可抄写的恢复码，如 A1B2-C3D4-E5F6-7890"""
    return '-'.join(secrets.token_hex(2).upper() for _ in range(4))


def _norm_code(code):
    return re.sub(r'[^0-9a-z]', '', (code or '').lower())


def hash_recovery(code, salt):
    return hashlib.sha256(('%s|%s' % (salt or '', _norm_code(code))).encode()).hexdigest()


def check_new_password(p1, p2, current=''):
    """校验新密码，返回中文错误信息或 None"""
    if not p1:
        return '新密码不能为空'
    if len(p1) < MIN_PW_LEN:
        return '新密码太短：至少 %d 位' % MIN_PW_LEN
    if p1 != p2:
        return '两次输入的新密码不一样'
    if current and p1 == current:
        return '新密码和当前密码一样，不用改'
    if p1.strip() != p1:
        return '新密码首尾不能有空格'
    return None


def cfg_pw_ver(cfg=None):
    cfg = cfg or load_config()
    try:
        return int(cfg['web'].get('pw_ver') or 1)
    except Exception:
        return 1


def set_panel_password(cfg, new_pw, rotate_recovery=True):
    """写入新密码（立即生效）；默认同时轮换恢复码，返回新恢复码（不轮换则返回 None）"""
    code = None
    if rotate_recovery:
        code = gen_recovery_code()
        salt = secrets.token_hex(8)
        cfg['web']['recovery_hash'] = hash_recovery(code, salt)
        cfg['web']['recovery_salt'] = salt
    cfg['web']['password'] = new_pw
    cfg['web']['pw_ver'] = cfg_pw_ver(cfg) + 1     # 其它设备上的旧会话立即失效
    cfg['web']['pw_changed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_config(cfg)
    return code


# ---------------------------------------------------------------- 正则
def compile_list(items):
    out = []
    for p in items or []:
        p = (p or '').strip()
        if not p:
            continue
        out.append(re.compile(p, re.I))
    return out


def rule_matches(rule, text):
    """白名单：为空=全部通过；否则任一命中即可。黑名单：任一命中即拒绝。"""
    try:
        wl = compile_list(rule.get('whitelist'))
        bl = compile_list(rule.get('blacklist'))
    except re.error as e:
        return False, '正则错误：%s' % e
    for rx in bl:
        if rx.search(text):
            return False, '命中黑名单 /%s/' % rx.pattern
    if wl:
        for rx in wl:
            if rx.search(text):
                return True, '命中白名单 /%s/' % rx.pattern
        return False, '未命中白名单'
    return True, '白名单为空，全部转发'


# ---------------------------------------------------------------- 错误中文化
def friendly_error(e):
    """把 Telethon 的报错翻译成中文大白话"""
    try:
        E = errors
        if isinstance(e, E.PhoneCodeInvalidError):
            return '验证码不对。请点「发送验证码」重新获取，再输入**最新收到**的那个验证码（重新获取后旧验证码会失效）'
        if isinstance(e, E.PhoneCodeExpiredError):
            return '验证码已过期。请点「发送验证码」重新获取'
        if isinstance(e, E.SessionPasswordNeededError):
            return '需要两步验证密码：填在「两步验证密码」框里，再点一次「完成登录」'
        if isinstance(e, E.PasswordHashInvalidError):
            return '两步验证密码不对'
        if isinstance(e, E.PhoneNumberInvalidError):
            return '手机号格式不对：要带国家码，例如 +8613800000000'
        if isinstance(e, E.PhoneNumberBannedError):
            return '该手机号已被 Telegram 封禁'
        if isinstance(e, E.ApiIdInvalidError):
            return 'API ID / Hash 无效（改 data/config.json 的 tg.api_id / api_hash）'
        if isinstance(e, E.FloodWaitError):
            return '操作太频繁，请等 %d 秒后再试' % getattr(e, 'seconds', 0)
        if isinstance(e, E.AuthKeyUnregisteredError):
            return '会话已失效，请重新登录'
    except Exception:
        pass
    return str(e)


# ---------------------------------------------------------------- worker
class Worker:
    def __init__(self):
        self.loop = None
        self.client = None
        self.thread = None
        self.ready = threading.Event()
        self.state = {'authorized': False, 'me': None, 'error': None,
                      'pending_phone': None, 'need_password': False}
        self._phone_code_hash = None
        self._albums = {}          # (来源, grouped_id) -> {'msgs': {id: msg}, 'task': asyncio.Task}

    # ---- 线程/事件循环
    def start(self):
        self.thread = threading.Thread(target=self._run, name='tg-loop', daemon=True)
        self.thread.start()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._boot())
        self.ready.set()
        self.loop.run_forever()

    async def _boot(self):
        cfg = load_config()
        api_id, api_hash = cfg['tg'].get('api_id') or 0, cfg['tg'].get('api_hash') or ''
        if not api_id or not api_hash:
            self.state['error'] = '未配置 TG API ID/Hash'
            log.error(self.state['error'])
            return
        self.state['error'] = None          # 配置补上了：清掉这条提示
        try:
            self.client = TelegramClient(SESSION_PATH, api_id, api_hash,
                                         device_model='tg-forwarder', system_version='1.0')
            await self.client.connect()
            if await self.client.is_user_authorized():
                me = await self.client.get_me()
                self.state.update(authorized=True, me=self._me_dict(me), error=None)
                log.info('已登录：%s', self.state['me'])
            else:
                log.info('未登录，等待在面板完成登录')
            self.client.add_event_handler(self._on_message, events.NewMessage())
            log.info('消息监听已注册（NewMessage + 自管相册聚合）')
            # 启动补偿扫描：治「重启/重连窗口内漏掉的消息」
            try:
                for rule in (cfg.get('rules') or []):
                    if rule.get('enabled'):
                        try:
                            await self._reconcile_source(rule)
                        except Exception as e:
                            log.warning('启动补偿扫描失败（%s）：%s', rule.get('name'), e)
            except Exception as e:
                log.warning('启动补偿扫描出错：%s', e)
            # 定时补偿扫描：治「运行期事件推送漏帧」
            asyncio.ensure_future(self._reconcile_loop())
        except Exception as e:
            self.state['error'] = '连接失败：%s' % e
            log.exception('启动失败')

    @staticmethod
    def _me_dict(me):
        if not me:
            return None
        return {'id': me.id, 'name': (getattr(me, 'first_name', '') or '') +
                (' ' + me.last_name if getattr(me, 'last_name', None) else ''),
                'username': getattr(me, 'username', None), 'phone': getattr(me, 'phone', None)}

    def call(self, coro, timeout=45):
        if not self.loop:
            raise RuntimeError('内核未就绪')
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout)

    # ---- 登录
    async def _send_code(self, phone):
        now = time.time()
        last = self.state.get('code_sent_at') or 0
        if now - last < 45 and self.state.get('pending_phone') == phone:
            return {'reused': True, 'wait': int(45 - (now - last))}
        if not self.client:
            await self._boot()
        sent = await self.client.send_code_request(phone)
        self._phone_code_hash = sent.phone_code_hash
        self.state['pending_phone'] = phone
        self.state['code_sent_at'] = now
        self.state['need_password'] = False
        cfg = load_config()
        cfg['tg']['phone'] = phone
        cfg['tg']['phone_code_hash'] = sent.phone_code_hash   # 落盘：容器重启后仍能完成登录
        save_config(cfg)
        append_log('📨 已向 %s 发送登录验证码' % phone)
        return {'reused': False}

    async def _verify(self, code, password=None):
        cfg = load_config()
        phone = self.state.get('pending_phone') or cfg['tg'].get('phone')
        if not phone:
            raise RuntimeError('请先输入手机号并点「发送验证码」')
        if not self._phone_code_hash:
            self._phone_code_hash = cfg['tg'].get('phone_code_hash')
        if not self._phone_code_hash:
            raise RuntimeError('请先点「发送验证码」（换过容器或超时后需要重新获取）')
        try:
            await self.client.sign_in(phone=phone, code=code, phone_code_hash=self._phone_code_hash)
        except errors.SessionPasswordNeededError:
            if not password:
                self.state['need_password'] = True
                return {'need_password': True}
            await self.client.sign_in(password=password)
        me = await self.client.get_me()
        self.state.update(authorized=True, me=self._me_dict(me), error=None, need_password=False)
        append_log('✅ 登录成功：%s (id=%s)' % (self.state['me']['name'], self.state['me']['id']))
        return {'ok': True, 'me': self.state['me']}

    async def _logout(self):
        try:
            await self.client.log_out()
        except Exception:
            pass
        self.state.update(authorized=False, me=None)
        return True

    # ---- 消息处理
    @staticmethod
    def _norm_id(chat_id):
        """把 -1001234567890 / 1234567890 归一化成同一串数字，便于比较"""
        s = str(chat_id or '').strip()
        d = ''.join(ch for ch in s if ch.isdigit())
        if d.startswith('100') and len(d) > 11:
            d = d[3:]
        return d

    def _same_chat(self, a, b):
        na, nb = self._norm_id(a), self._norm_id(b)
        return bool(na) and na == nb

    @staticmethod
    def _msg_text(msg):
        parts = [msg.message or '']
        f = getattr(msg, 'file', None)
        if f is not None and getattr(f, 'name', None):
            parts.append(f.name)
        return '\n'.join([p for p in parts if p])

    async def _on_message(self, event):
        try:
            if not isinstance(event, events.NewMessage.Event):
                return
            msg = event.message
            src = getattr(event, 'chat_id', '')
            cfg = load_config()
            rules = cfg.get('rules') or []
            # 只看规则里出现过的来源频道，避免无关频道刷屏
            is_src = any(str(r.get('source') or '').strip() and self._same_chat(r.get('source'), src)
                         for r in rules)
            if not is_src:
                if cfg.get('debug'):
                    log.info('（忽略）消息来自 chat=%s', src)
                return
            # 相册（聚合消息）：先缓存，等同一组都到齐（1.2 秒）再整组转发，避免被拆成散条
            if getattr(msg, 'grouped_id', None):
                if cfg.get('debug'):
                    log.info('相册分片 chat=%s id=%s gid=%s', src, msg.id, msg.grouped_id)
                self._buffer_album(msg, src)
                return
            log.info('来源消息 chat=%s | %s', src, (self._msg_text(msg) or '')[:60].replace('\n', ' '))
            for idx, rule in enumerate(rules):
                if not rule.get('enabled'):
                    continue
                if not self._same_chat(rule.get('source'), src):
                    continue
                text = self._msg_text(msg)
                ok, why = rule_matches(rule, text)
                if not ok:
                    append_log('跳过 [%s] %s' % (rule.get('name') or idx + 1, why))
                    continue
                await self._deliver(rule, msg, why)
            # 单条消息也抬水位线（含被规则过滤掉的），避免补偿扫描反复重扫
            bump_watermark(self._norm_id(src), msg.id)
        except Exception as e:
            log.exception('处理消息出错')
            append_log('❌ 处理消息出错：%s' % e)

    # ---- 自管相册聚合（不依赖 Telethon 的 Album 事件，它对"自己发的相册"不触发）
    def _buffer_album(self, msg, src):
        key = (self._norm_id(src), str(msg.grouped_id))
        slot = self._albums.get(key)
        if slot is None:
            slot = {'msgs': {}, 'task': None, 'flushing': False, 'last_seen': 0.0}
            self._albums[key] = slot
        slot['msgs'][msg.id] = msg
        slot['last_seen'] = time.monotonic()   # 只刷新时间戳，不去 cancel 定时器
        if slot.get('flushing'):
            # 这一组正在转发：只并进缓存就够了（_flush_album 转发前会再取一次最新快照）。
            return
        if not slot['task']:
            # 只在「该组首次分片」时起一个常驻收尾协程。
            # 旧实现是「每个分片都 cancel 上一个计时器再重建」，当取消恰好落在
            # _flush_album 的 await 挂起期间时，CancelledError 会穿透到 finally 把
            # slot 从字典里 pop 掉，导致该组后续分片另起一个全新 slot、而本次转发被
            # 中途丢弃 —— 这是相册「整组漏转」的直接原因之一。
            slot['task'] = asyncio.ensure_future(self._settle_album(key))

    async def _settle_album(self, key, quiet=1.5, hard=8.0, tick=0.25):
        """静默收尾：距最后一次分片到达超过 quiet 秒即转发；最长 hard 秒强制收尾。

        用「时间戳轮询」替代「cancel + 重排」，既没有取消竞态，又能在长视频组
        分片持续到达时靠 hard 上限保底，不会像固定 1.2 秒窗口那样把长组拆条。
        """
        start = time.monotonic()
        while True:
            try:
                await asyncio.sleep(tick)
            except asyncio.CancelledError:
                return
            slot = self._albums.get(key)
            if not slot:
                return
            now = time.monotonic()
            if now - float(slot.get('last_seen') or 0) >= quiet or now - start >= hard:
                break
        await self._flush_album(key)

    @staticmethod
    def _album_msgs(slot):
        return sorted(slot['msgs'].values(), key=lambda m: m.id)

    async def _complete_album(self, msgs, slot=None):
        """按 grouped_id 回服务端把整组捞齐，避免相册被拆成散条。

        相册分片是逐条推送的，固定静默窗口偶尔会漏片：先到的几片会被当成一整组转发出去，
        后到的分片又组成新的一组再转一次，观感上就是"相册被拆开"。这里以首片为锚点，
        取 id 邻近范围内所有同 grouped_id 的消息补齐；捞不回来就原样返回，绝不影响正常转发。
        """
        if not msgs:
            return msgs
        gid = getattr(msgs[0], 'grouped_id', None)
        if not gid:
            return msgs
        try:
            anchor = msgs[0]
            # 回捞范围：向前留 3 条余量，向后最多探 20 条。
            # 旧实现是固定 ±12，一是会无谓地跨到邻组（虽然后面按 gid 过滤掉了，
            # 但请求量放大一倍），二是当一组超过 13 片时向后覆盖不够。
            lo = max(1, min(m.id for m in msgs) - 3)
            hi = max(m.id for m in msgs) + 20
            ids = list(range(lo, hi + 1))
            around = await self.client.get_messages(anchor.chat_id, ids=ids)
            merged = {m.id: m for m in msgs}
            added = 0
            for x in (around or []):
                if x is None or getattr(x, 'grouped_id', None) != gid:
                    continue
                if slot is not None:
                    slot['msgs'][x.id] = x          # 同步回缓存，供下一次取快照时用
                if x.id not in merged:
                    merged[x.id] = x
                    added += 1
            if added:
                log.info('相册补全 gid=%s：补回 %d 片 → 共 %d 片', gid, added, len(merged))
            return sorted(merged.values(), key=lambda m: m.id)
        except Exception as e:
            log.warning('相册补全失败（按已聚合的 %d 片转发）：%s', len(msgs), e)
            return msgs

    async def _flush_album(self, key):
        slot = self._albums.get(key)
        if not slot:
            return
        slot['flushing'] = True        # 之后的同组新分片只并入缓存，不再另起一次转发
        slot['task'] = None
        try:
            # 先按 grouped_id 回服务端把整组捞齐（分片晚到也不会漏）
            msgs = await self._complete_album(self._album_msgs(slot), slot)
            if len(msgs) == 1:
                # 只聚合到 1 片，几乎必然是漏片（相册至少 2 片）：缓 1 秒再补捞一次
                await asyncio.sleep(1.0)
                msgs = await self._complete_album(self._album_msgs(slot), slot)
            if not msgs:
                return
            src_norm, gid = key
            cfg = load_config()
            text = self._group_text(msgs)
            log.info('来源相册 gid=%s | %d 条 | %s', gid, len(msgs), text[:60].replace('\n', ' '))
            for idx, rule in enumerate(cfg.get('rules') or []):
                if not rule.get('enabled'):
                    continue
                if self._norm_id(rule.get('source')) != src_norm:
                    continue
                ok, why = rule_matches(rule, text)
                if not ok:
                    append_log('跳过 [%s] 相册(%d条) %s' % (rule.get('name') or idx + 1, len(msgs), why))
                    continue
                await self._deliver_group(rule, msgs, why)
            # 整组处理完（无论命中与否）都把水位线抬到该组最大 id：
            # 未命中的组也算「已处理」，否则补偿扫描会一遍遍重扫同一批。
            bump_watermark(src_norm, max(m.id for m in msgs))
        except Exception as e:
            log.exception('处理相册出错')
            append_log('❌ 处理相册出错：%s' % e)
        finally:
            self._albums.pop(key, None)

    def _group_text(self, msgs):
        return '\n'.join([self._msg_text(m) for m in msgs if self._msg_text(m)])

    # ---- 补偿扫描（水位线对账）
    async def _reconcile_source(self, rule, limit=200):
        """按水位线回扫源频道，把「事件推送漏掉、但服务端确实存在」的组补转。

        动机：相册聚合的入口只有 events.NewMessage 推送。源频道在静默期后是「批量推」的
        （前面还常带一条无 grouped_id 的广告图），首批分片存在被削顶/丢帧的可能，此时该组
        永远不会进 _buffer_album，_complete_album 也就永远不会以它为锚点触发 —— 表现为
        「整组相册完全没转发」。这里用 min_id 水位线做兜底对账。
        """
        src = str(rule.get('source') or '').strip()
        target = str(rule.get('target') or '').strip()
        name = rule.get('name') or '规则'
        if not src or not target:
            return 0
        src_key = self._norm_id(src)
        entity = await self._resolve(src)
        wm = get_watermark(src_key)
        if not wm:
            # 首次运行：只把水位线对齐到当前最新一条，不回补历史（避免刷屏）
            latest = await self.client.get_messages(entity, limit=1)
            if latest:
                bump_watermark(src_key, latest[0].id, flush=True)
                log.info('补偿扫描首次运行：源 %s 水位线初始化为 %d', src, latest[0].id)
            return 0

        msgs = await self.client.get_messages(entity, min_id=wm, limit=limit)
        if not msgs:
            return 0
        # 按 grouped_id 分组（无 gid 的各自成组）
        groups, order = {}, []
        for m in sorted(msgs, key=lambda x: x.id):
            gid = getattr(m, 'grouped_id', None)
            k = ('g', str(gid)) if gid else ('m', m.id)
            if k not in groups:
                groups[k] = []
                order.append(k)
            groups[k].append(m)

        healed = 0
        top = wm
        for k in order:
            grp = sorted(groups[k], key=lambda x: x.id)
            top = max(top, max(m.id for m in grp))
            # 正在缓存里等收尾的组交给正常流程，别重复转发
            if k[0] == 'g' and (src_key, k[1]) in self._albums:
                continue
            text = self._group_text(grp) if len(grp) > 1 else self._msg_text(grp[0])
            ok, why = rule_matches(rule, text)
            if not ok:
                continue
            _kind = '相册%d条' % len(grp) if len(grp) > 1 else '单条'
            log.info('补偿扫描命中漏组：源=%s %s ids=%d..%d', src, _kind,
                     grp[0].id, grp[-1].id)
            if len(grp) > 1:
                await self._deliver_group(rule, grp, '补扫｜' + why)
            else:
                await self._deliver(rule, grp[0], '补扫｜' + why)
            healed += 1
        bump_watermark(src_key, top, flush=True)
        if healed:
            log.info('补偿扫描完成：源=%s 水位 %d→%d，补转 %d 组', src, wm, top, healed)
        return healed

    async def _reconcile_loop(self):
        """定时补偿扫描：每 reconcile_seconds 秒跑一次（0=关闭）。"""
        while True:
            try:
                secs = int(load_config().get('reconcile_seconds', RECONCILE_DEFAULT) or 0)
            except Exception:
                secs = RECONCILE_DEFAULT
            if secs <= 0:
                await asyncio.sleep(60)
                continue
            await asyncio.sleep(secs)
            try:
                if not (self.client and self.client.is_connected()):
                    continue
                for rule in (load_config().get('rules') or []):
                    if not rule.get('enabled'):
                        continue
                    try:
                        await self._reconcile_source(rule)
                    except Exception as e:
                        log.warning('补偿扫描失败（%s）：%s', rule.get('name'), e)
            except Exception as e:
                log.warning('补偿扫描循环出错：%s', e)

    async def _deliver_group(self, rule, msgs, why=''):
        """整组转发（保持相册格式）；失败时按规则降级"""
        name = rule.get('name') or '规则'
        target = str(rule.get('target') or '').strip()
        if not target:
            append_log('❌ [%s] 目标频道为空，跳过' % name)
            return
        mode = (rule.get('mode') or 'forward').lower()
        head = '→ %s ｜ 相册%d条 ｜ %s' % (target, len(msgs), why)
        try:
            if mode == 'text':
                await self._send_group_text(target, msgs)
            else:
                try:
                    await self.client.forward_messages(int(target), msgs)
                except Exception as e:
                    append_log('⚠️ [%s] 转发相册失败（%s）' % (name, str(e)[:80]))
                    if rule.get('media_fallback'):
                        await self._send_group_media(target, msgs)
                    else:
                        await self._send_group_text(target, msgs)
            append_log('✅ [%s] 已转发 %s' % (name, head))
        except Exception as e:
            append_log('❌ [%s] 转发相册失败：%s' % (name, e))

    async def _send_group_text(self, target, msgs):
        text = self._group_text(msgs).strip()
        if not text:
            raise RuntimeError('这组消息没有文本内容（且转发方式=只发文本）')
        await self.client.send_message(int(target), text)

    async def _send_group_media(self, target, msgs):
        """下载整组媒体并作为一条相册重新上传（媒体重传开关打开时）"""
        cfg = load_config()
        limit = int(cfg.get('media_max_mb') or 1800) * 1024 * 1024
        total = 0
        files = []
        for m in msgs:
            if not m.media:
                continue
            size = getattr(getattr(m, 'file', None), 'size', 0) or 0
            total += size
            if total > limit:
                raise RuntimeError('相册媒体合计超过 %d MB 上限' % (limit // 1024 // 1024))
            files.append(await self.client.download_media(m, file=bytes))
        if not files:
            return await self._send_group_text(target, msgs)
        caption = (msgs[0].message or '')
        await self.client.send_file(int(target), files, caption=caption)

    async def _deliver(self, rule, msg, why=''):
        name = rule.get('name') or '规则'
        target = str(rule.get('target') or '').strip()
        if not target:
            append_log('❌ [%s] 目标频道为空，跳过' % name)
            return
        mode = (rule.get('mode') or 'forward').lower()
        head = '→ %s ｜ %s' % (target, why)
        try:
            if mode == 'text':
                await self._send_text(target, msg)
            else:
                try:
                    await self.client.forward_messages(int(target), msg)
                except Exception as e:
                    reason = str(e)
                    append_log('⚠️ [%s] 转发原消息失败（%s），改用文本' % (name, reason[:80]))
                    if rule.get('media_fallback'):
                        await self._send_media(target, msg)
                    else:
                        await self._send_text(target, msg)
            append_log('✅ [%s] 已转发 %s' % (name, head))
        except Exception as e:
            append_log('❌ [%s] 转发失败：%s' % (name, e))

    async def _send_text(self, target, msg):
        text = (msg.message or '').strip()
        if not text:
            raise RuntimeError('该消息没有文本内容（相册/媒体且无说明）')
        await self.client.send_message(int(target), text)

    async def _send_media(self, target, msg):
        if not msg.media:
            return await self._send_text(target, msg)
        size = getattr(getattr(msg, 'file', None), 'size', 0) or 0
        cfg = load_config()
        limit = int(cfg.get('media_max_mb') or 1800) * 1024 * 1024
        if size and size > limit:
            raise RuntimeError('媒体 %0.1fMB 超过上限' % (size / 1024 / 1024))
        data = await self.client.download_media(msg, file=bytes)
        await self.client.send_file(int(target), data, caption=(msg.message or ''))

    async def _resolve(self, chat_id):
        try:
            return await self.client.get_entity(int(str(chat_id).strip()))
        except Exception as e:
            raise RuntimeError('无法解析 %s（请确认该账号已加入/可访问）：%s' % (chat_id, e))

    async def _fetch_group(self, entity, m):
        """把与 m 同属一个相册的消息整组取出来（按 id 排序）"""
        gid = getattr(m, 'grouped_id', None)
        if not gid:
            return [m]
        ids = [m.id + k for k in range(-12, 13) if m.id + k > 0]
        around = await self.client.get_messages(entity, ids=ids)
        group = sorted([x for x in (around or []) if x is not None and getattr(x, 'grouped_id', None) == gid],
                       key=lambda x: x.id)
        return group or [m]

    @staticmethod
    def _kind(m):
        if getattr(m, 'grouped_id', None):
            return '相册'
        try:
            from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
            if isinstance(getattr(m, 'media', None), MessageMediaPhoto):
                return '图片'
            doc = getattr(m, 'file', None)
            mime = (getattr(doc, 'mime_type', '') or '') if doc else ''
            if mime.startswith('video') or (doc and getattr(doc, 'ext', '') in ('.mp4', '.mkv', '.mov')):
                return '视频'
            if isinstance(getattr(m, 'media', None), MessageMediaDocument):
                return '文件'
        except Exception:
            pass
        return '文本'

    async def _preview_rule(self, rule, limit=15):
        """只看不转：列出来源频道最近的消息 + 每条会不会被这条规则转发（只读，安全）"""
        src = str(rule.get('source') or '').strip()
        if not src:
            return {'ok': False, 'error': '来源频道为空'}
        entity = await self._resolve(src)
        msgs = await self.client.get_messages(entity, limit=max(1, min(int(limit), 30)))
        items, seen = [], set()
        for m in msgs:
            gid = getattr(m, 'grouped_id', None)
            if gid:
                if gid in seen:
                    continue
                seen.add(gid)
                group = await self._fetch_group(entity, m)
                text = self._group_text(group)
                ok, why = rule_matches(rule, text)
                items.append({'id': m.id, 'type': '相册(%d条)' % len(group), 'text': text[:150],
                              'will_forward': ok, 'why': why})
            else:
                text = self._msg_text(m)
                ok, why = rule_matches(rule, text)
                items.append({'id': m.id, 'type': self._kind(m), 'text': text[:150],
                              'will_forward': ok, 'why': why})
        return {'ok': True, 'items': items,
                'summary': '最近 %d 条里会转发 %d 条' % (len(items), sum(1 for i in items if i['will_forward']))}

    async def _test_rule(self, rule):
        src = str(rule.get('source') or '').strip()
        if not src:
            return {'ok': False, 'error': '来源频道为空'}
        entity = await self._resolve(src)
        msgs = await self.client.get_messages(entity, limit=20)
        if not msgs:
            return {'ok': False, 'error': '该频道没有可读取的消息'}
        seen = set()
        for m in msgs:
            gid = getattr(m, 'grouped_id', None)
            if gid:
                if gid in seen:
                    continue
                seen.add(gid)
                group = await self._fetch_group(entity, m)
                text = self._group_text(group)
                ok, why = rule_matches(rule, text)
                if ok:
                    await self._deliver_group(rule, group, '测试｜' + why)
                    return {'ok': True, 'message': '已整组转发一个相册（%d 条，%s）' % (len(group), why)}
            else:
                ok, why = rule_matches(rule, self._msg_text(m))
                if ok:
                    await self._deliver(rule, m, '测试｜' + why)
                    return {'ok': True, 'message': '已按规则转发一条（%s）' % why}
        return {'ok': False, 'error': '最近 20 条消息都被规则过滤掉了（白名单/黑名单）'}


WORKER = Worker()

# ---------------------------------------------------------------- Flask
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(16).hex()
if not os.environ.get('SECRET_KEY') or os.environ.get('SECRET_KEY') == 'change-me-please':
    log.warning('未设置 SECRET_KEY（或还是默认值）：容器重启后需要重新登录面板。'
                '建议在 .env 里设一串随机字符。')
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',     # 顺带挡掉跨站 POST（CSRF）
)

# 登录/恢复码 失败退避：连续失败时下一次尝试前等更久（防爆破）
_login_guard = {'n': 0, 't': 0.0}


def login_guard(penalize=False):
    """失败则累加惩罚延迟（最多 3 秒）；正常/成功则清零。"""
    now = time.time()
    with LOG_LOCK:
        if now - _login_guard['t'] > 600:
            _login_guard['n'] = 0
        if penalize:
            _login_guard['n'] += 1
            _login_guard['t'] = now
            delay = min(0.4 * _login_guard['n'], 3.0)
        else:
            delay = 0.0
            _login_guard['n'] = 0
    if delay:
        time.sleep(delay)
    return delay


def logged_in():
    """会话有效 = 密码过 + 会话里的版本号等于当前版本（改过密码的旧会话自动失效）"""
    if not session.get('ok'):
        return False
    ver = cfg_pw_ver()
    return session.get('v', ver) == ver


def need_auth():
    if logged_in():
        return None
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': '未登录', 'need_login': True}), 401
    return redirect('/login')


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    cfg = load_config()
    ctx = {'has_recovery': bool(cfg['web'].get('recovery_hash')), 'reset_script': RESET_SCRIPT}
    if request.method == 'POST':
        pw = (request.form.get('password') or '').strip()
        if pw and hmac.compare_digest(pw, cfg['web'].get('password') or ''):
            login_guard()                       # 成功：清掉失败计数
            session['ok'] = True
            session['v'] = cfg_pw_ver(cfg)
            return redirect('/')
        login_guard(penalize=True)              # 失败：下一次尝试前等更久
        append_log('⚠️ 面板登录失败：密码不正确')
        return render_template('login.html', error='密码不正确，请重试（或点下面的「忘记密码？」用恢复码重置）', **ctx)
    return render_template('login.html', error=None, **ctx)


@app.route('/forgot', methods=['POST'])
def forgot_page():
    """登录页「忘记密码？」：用恢复码重置面板密码（不需要登录）"""
    cfg = load_config()
    has = bool(cfg['web'].get('recovery_hash'))
    ctx = {'has_recovery': has, 'reset_script': RESET_SCRIPT, 'forgot_open': True,
           'error': None, 'keep_code': True}
    code = (request.form.get('recovery_code') or '').strip()
    p1 = (request.form.get('new_password') or '').strip()
    p2 = (request.form.get('new_password2') or '').strip()
    if not has:
        ctx['forgot_error'] = '这台机器还没设置过恢复码，没法用恢复码重置。'
        return render_template('login.html', **ctx)
    if not code or not hmac.compare_digest(
            hash_recovery(code, cfg['web'].get('recovery_salt')), cfg['web'].get('recovery_hash') or ''):
        login_guard(penalize=True)              # 失败：下一次尝试前等更久（防爆破）
        append_log('⚠️ 忘记密码：恢复码不正确')
        ctx['forgot_error'] = ('恢复码不对。大小写不敏感、中间的「-」可省略；'
                               '如果之后改过密码，要用**最新**的那个恢复码。')
        return render_template('login.html', **ctx)
    err = check_new_password(p1, p2)
    if err:
        ctx['forgot_error'] = err
        return render_template('login.html', **ctx)
    newcode = set_panel_password(cfg, p1)
    append_log('🔑 通过恢复码重置了面板密码')
    ctx.update(forgot_open=False, forgot_done=True, new_password=p1, new_recovery=newcode)
    return render_template('login.html', **ctx)


@app.route('/logout')
def logout_page():
    """退出面板（清掉会话，回到登录页）"""
    session.clear()
    return redirect('/login')


@app.route('/api/web/logout', methods=['POST'])
def api_web_logout():
    if (r := need_auth()):
        return r
    session.clear()
    append_log('👋 已退出面板登录')
    return jsonify({'ok': True})


@app.route('/api/web/password', methods=['POST'])
def api_web_password():
    """修改面板密码（需要当前密码）：立即生效，并同时轮换恢复码"""
    if (r := need_auth()):
        return r
    body = request.json or {}
    cfg = load_config()
    cur = (body.get('current') or '').strip()
    p1 = (body.get('new_password') or '').strip()
    p2 = (body.get('new_password2') or '').strip()
    if not cur:
        return jsonify({'ok': False, 'error': '请先填当前密码'})
    if not hmac.compare_digest(cur, cfg['web'].get('password') or ''):
        return jsonify({'ok': False, 'error': '当前密码不对'})
    err = check_new_password(p1, p2, cfg['web'].get('password') or '')
    if err:
        return jsonify({'ok': False, 'error': err})
    code = set_panel_password(cfg, p1)
    session['ok'] = True
    session['v'] = cfg_pw_ver(cfg)          # 本机当前会话继续有效，其它设备需重新登录
    append_log('🔑 面板密码已修改（恢复码已重新生成）')
    return jsonify({'ok': True, 'message': '密码已修改，立即生效（其它设备需用新密码重新登录）',
                    'recovery_code': code,
                    'note': '恢复码只显示这一次，请抄下来存好。忘记密码时在登录页点「忘记密码？」用它重置。'})


@app.route('/api/web/recovery', methods=['POST'])
def api_web_recovery():
    """重新生成恢复码（需要当前密码；旧恢复码立即作废）"""
    if (r := need_auth()):
        return r
    body = request.json or {}
    cfg = load_config()
    if not hmac.compare_digest((body.get('current') or '').strip(), cfg['web'].get('password') or ''):
        return jsonify({'ok': False, 'error': '当前密码不对'})
    code = gen_recovery_code()
    salt = secrets.token_hex(8)
    cfg['web']['recovery_hash'] = hash_recovery(code, salt)
    cfg['web']['recovery_salt'] = salt
    save_config(cfg)
    append_log('🔑 重新生成了面板恢复码')
    return jsonify({'ok': True, 'message': '已生成新的恢复码（旧恢复码立即作废）', 'recovery_code': code,
                    'note': '恢复码只显示这一次，请抄下来存好。'})


@app.route('/')
def index():
    r = need_auth()
    return r or render_template('index.html')


@app.route('/api/state')
def api_state():
    if (r := need_auth()):
        return r
    cfg = load_config()
    st = dict(WORKER.state)
    st['ready'] = WORKER.ready.is_set()
    return jsonify({'ok': True, 'state': st, 'rules': cfg.get('rules') or [],
                    'phone': cfg['tg'].get('phone') or '',
                    'api_id_set': bool(cfg['tg'].get('api_id')),
                    'panel': {'recovery_set': bool(cfg['web'].get('recovery_hash')),
                              'pw_changed_at': cfg['web'].get('pw_changed_at') or '',
                              'min_pw_len': MIN_PW_LEN,
                              'reset_script': RESET_SCRIPT,
                              'log_keep': log_keep(),
                              'log_count': log_count()},
                    'logs': recent_lines(30)})


@app.route('/api/tg/send_code', methods=['POST'])
def api_send_code():
    if (r := need_auth()):
        return r
    phone = (request.json or {}).get('phone', '').strip()
    if not phone:
        return jsonify({'ok': False, 'error': '请填手机号（国际格式，如 +8613800000000）'})
    if not re.match(r'^\+\d{6,15}$', phone.replace(' ', '')):
        return jsonify({'ok': False, 'error': '手机号格式不对：要带国家码且以 + 开头，例如 +8613800000000'})
    try:
        res = WORKER.call(WORKER._send_code(phone))
        if res and res.get('reused'):
            return jsonify({'ok': True, 'message': '刚刚已发送过验证码，请用**最新收到**的那个（%d 秒后可重新发送）' % res.get('wait', 0)})
        return jsonify({'ok': True, 'message': '验证码已发送，请查看你的 Telegram（注意用最新收到的那个）'})
    except Exception as e:
        log.exception('send_code 失败')
        return jsonify({'ok': False, 'error': friendly_error(e)})


@app.route('/api/tg/verify', methods=['POST'])
def api_verify():
    if (r := need_auth()):
        return r
    body = request.json or {}
    try:
        res = WORKER.call(WORKER._verify((body.get('code') or '').strip(), (body.get('password') or '').strip() or None))
        if res and res.get('need_password'):
            return jsonify({'ok': False, 'need_password': True,
                            'error': '这个账号开了两步验证：请在下方「两步验证密码」里填密码，再点一次「完成登录」'})
        return jsonify({'ok': True, **(res or {})})
    except Exception as e:
        log.exception('verify 失败')
        append_log('❌ 登录失败：%s' % e)
        return jsonify({'ok': False, 'error': friendly_error(e)})


@app.route('/api/tg/logout', methods=['POST'])
def api_tg_logout():
    if (r := need_auth()):
        return r
    try:
        WORKER.call(WORKER._logout())
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/rules', methods=['POST'])
def api_rules():
    """保存规则：按 id 合并（不会因为某个浏览器里的卡片少而把别处的规则冲掉）；
    删除必须显式放在 deleted 列表里（面板点「删除」时会带上）。"""
    if (r := need_auth()):
        return r
    body = request.json or {}
    rules = body.get('rules')
    deleted = [str(x) for x in (body.get('deleted') or [])]
    if not isinstance(rules, list):
        return jsonify({'ok': False, 'error': '规则格式不对'})
    errors_out = []
    clean = []
    for i, r0 in enumerate(rules):
        name = (r0.get('name') or '').strip() or ('规则%d' % (i + 1))
        src = str(r0.get('source') or '').strip()
        tgt = str(r0.get('target') or '').strip()
        if not src or not tgt:
            errors_out.append('%s：来源和目标都要填' % name)

        wl, bl = [], []
        for key, label, bucket in (('whitelist', '白名单', wl), ('blacklist', '黑名单', bl)):
            vals = r0.get(key)
            if isinstance(vals, str):
                vals = vals.split('\n')
            for p in (vals or []):
                p = str(p or '').strip()
                if not p:
                    continue
                try:
                    re.compile(p)
                except re.error as e:
                    errors_out.append('%s：%s正则 /%s/ 有错（%s）' % (name, label, p, e))
                    continue
                bucket.append(p)

        mode = (r0.get('mode') or 'forward').lower()
        if mode not in ('forward', 'text'):
            mode = 'forward'
        clean.append({
            'id': r0.get('id') or ('r%d_%d' % (int(time.time() * 1000), i)),
            'enabled': bool(r0.get('enabled', True)),
            'name': name, 'source': src, 'target': tgt, 'mode': mode,
            'media_fallback': bool(r0.get('media_fallback')),
            'whitelist': wl, 'blacklist': bl,
        })
    if errors_out:
        return jsonify({'ok': False, 'error': '；'.join(errors_out)})
    cfg = load_config()
    old = cfg.get('rules') or []
    by_id = {r.get('id'): r for r in old}
    order = [r.get('id') for r in old]
    for r in clean:                       # 按 id 覆盖或追加，其它规则原样保留
        if r['id'] not in by_id:
            order.append(r['id'])
        by_id[r['id']] = r
    merged = [by_id[i] for i in order if i in by_id and i not in set(deleted)]
    cfg['rules'] = merged
    save_config(cfg)
    extra = '（删除 %d 条）' % len(deleted) if deleted else ''
    append_log('💾 规则已更新，共 %d 条%s（立即生效）' % (len(merged), extra))
    return jsonify({'ok': True, 'rules': merged, 'message': '已保存，立即生效（共 %d 条）' % len(merged)})


@app.route('/api/rules/test', methods=['POST'])
def api_test_rule():
    if (r := need_auth()):
        return r
    rule = (request.json or {}).get('rule') or {}
    if not WORKER.state.get('authorized'):
        return jsonify({'ok': False, 'error': '请先在面板完成 Telegram 登录'})
    try:
        return jsonify(WORKER.call(WORKER._test_rule(rule)))
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/rules/preview', methods=['POST'])
def api_preview_rule():
    """只读预览：列出来源频道最近消息、标明会不会被转发（不发送任何东西）"""
    if (r := need_auth()):
        return r
    body = request.json or {}
    rule = body.get('rule') or {}
    if not WORKER.state.get('authorized'):
        return jsonify({'ok': False, 'error': '请先在面板完成 Telegram 登录'})
    try:
        return jsonify(WORKER.call(WORKER._preview_rule(rule, body.get('limit') or 15)))
    except Exception as e:
        log.exception('preview 失败')
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/logs')
def api_logs():
    if (r := need_auth()):
        return r
    return jsonify({'ok': True, 'logs': recent_lines(50), 'count': log_count(), 'keep': log_keep()})


@app.route('/api/logs/clear', methods=['POST'])
def api_logs_clear():
    """清空最近记录（不影响转发和规则）"""
    if (r := need_auth()):
        return r
    n = clear_logs()
    log.info('面板清空了最近记录（%d 条）', n)
    return jsonify({'ok': True, 'message': '已清空 %d 条记录' % n, 'count': 0})


@app.route('/api/logs/limit', methods=['POST'])
def api_logs_limit():
    """设置最近记录条数上限；超出的立刻清理（0=不限，不自动清理）"""
    if (r := need_auth()):
        return r
    try:
        n = int((request.json or {}).get('limit'))
    except Exception:
        return jsonify({'ok': False, 'error': '条数不合法'})
    if n != 0 and not (20 <= n <= 100000):
        return jsonify({'ok': False, 'error': '请填 0（不限）或 20~100000 之间的条数'})
    cfg = load_config()
    cfg['log_max_lines'] = n
    save_config(cfg)
    removed = trim_log(n, force=True) if n else 0
    msg = ('已设为最多保留 %d 条（已清理 %d 条旧记录）' % (n, removed)) if n else '已设为不自动清理'
    append_log('⚙️ 最近记录上限设为 %s' % ('%d 条' % n if n else '不限'))
    return jsonify({'ok': True, 'message': msg, 'keep': n, 'count': log_count(), 'removed': removed})


@app.route('/api/logs/stream')
def api_logs_stream():
    """实时推送「最近记录」（SSE）：连上先给最近 30 条，之后一有新记录立刻推；
    面板「清空记录」或自动清理后，会重新推一次最新内容（客户端据 event:init 覆盖显示）。"""
    if (r := need_auth()):
        return r

    def payload(lines=None, count=None, keep=None):
        return json.dumps({'lines': lines if lines is not None else recent_lines(30),
                           'count': log_count() if count is None else count,
                           'keep': log_keep() if keep is None else keep},
                          ensure_ascii=False)

    def gen():
        yield 'event: init\ndata: %s\n\n' % payload()
        try:
            pos = os.path.getsize(LOG_PATH)
        except Exception:
            pos = 0
        epoch = LOG_EPOCH
        buf = ''
        last_ping = time.time()
        while True:
            time.sleep(0.4)
            if LOG_EPOCH != epoch:               # 清空 / 自动清理过 → 让客户端重新对齐
                epoch = LOG_EPOCH
                buf = ''
                yield 'event: init\ndata: %s\n\n' % payload(lines=recent_lines(30))
                try:
                    pos = os.path.getsize(LOG_PATH)
                except Exception:
                    pos = 0
                continue
            try:
                size = os.path.getsize(LOG_PATH)
            except Exception:
                size = 0
            if size < pos:                      # 日志被重建/轮转
                pos, buf = 0, ''
            if size > pos:
                try:
                    with open(LOG_PATH, encoding='utf-8', errors='replace') as f:
                        f.seek(pos)
                        buf += f.read()
                        pos = f.tell()
                except Exception:
                    pos, buf = size, ''
                while '\n' in buf:               # 只推完整行，半行留在缓冲区
                    line, buf = buf.split('\n', 1)
                    if line.strip():
                        yield 'event: line\ndata: %s\n\n' % json.dumps(
                            {'line': line, 'count': log_count(), 'keep': log_keep()},
                            ensure_ascii=False)
            if time.time() - last_ping > 15:
                last_ping = time.time()
                yield ': ping\n\n'

    return Response(stream_with_context(gen()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


if __name__ == '__main__':
    log.info('数据目录 %s，面板端口 %s', DATA_DIR, os.environ.get('PORT', '9020'))
    WORKER.start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '9020')), threaded=True)
