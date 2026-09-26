# -*- coding: utf-8 -*-
"""v1.0.2 漏转修复 单测：水位线补偿扫描 + 静默收尾聚合

不依赖 Telegram 网络：用假 client / 假消息对象验证纯逻辑。
运行方式（容器内 or 本机有 telethon 的环境）：
    MAIN_DIR=/tmp/tgtest python test_reconcile_fix.py
"""

import asyncio
import importlib.util
import os
import sys
import types

MAIN_DIR = os.environ.get('MAIN_DIR', '/app')
sys.path.insert(0, MAIN_DIR)

# ---- 最小桩：给 main.py 导入时需要的 flask/telethon 占位
if 'flask' not in sys.modules:
    flask = types.ModuleType('flask')

    class _App:
        def __init__(self, *a, **k):
            self.config = {}
            self.secret_key = None

        def route(self, *a, **k):
            def deco(fn):
                return fn
            return deco

        def __getattr__(self, n):
            def any_fn(*a, **k):
                return None
            return any_fn

    flask.Flask = _App
    for n in ('Response', 'jsonify', 'redirect', 'render_template', 'request', 'session', 'stream_with_context'):
        if n == 'request':
            flask.request = types.SimpleNamespace(args={}, form={}, get_json=lambda *a, **k: {}, remote_addr='127.0.0.1')
        elif n == 'session':
            flask.session = {}
        else:
            setattr(flask, n, lambda *a, **k: None)
    sys.modules['flask'] = flask

if 'telethon' not in sys.modules:
    telethon = types.ModuleType('telethon')
    errors = types.ModuleType('telethon.errors')
    events = types.ModuleType('telethon.events')

    class _Ev:
        class Event:
            pass
    events.NewMessage = _Ev
    telethon.errors = errors
    telethon.events = events
    telethon.TelegramClient = object
    sys.modules['telethon'] = telethon
    sys.modules['telethon.errors'] = errors
    sys.modules['telethon.events'] = events

spec = importlib.util.spec_from_file_location('tgmain', os.path.join(MAIN_DIR, 'main.py'))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

PASS, FAIL = [], []


def check(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print(('  ✅ ' if cond else '  ❌ ') + name + ('  ' + extra if extra else ''))


# ---------------------------------------------------------------- 假对象
class FakeFile:
    def __init__(self, name=''):
        self.name = name
        self.size = 100


class FakeMsg:
    def __init__(self, mid, gid=None, text='', chat_id=-1003436294431):
        self.id = mid
        self.grouped_id = gid
        self.message = text
        self.chat_id = chat_id
        self.media = None
        self.file = FakeFile()
        from datetime import datetime
        self.date = datetime(2026, 9, 26, 13, 0, 0)


class FakeClient:
    """只实现 get_messages 的两种调用形态：limit= 和 min_id= / ids="""

    def __init__(self, store):
        self.store = store      # list[FakeMsg]，按 id 升序
        self.connected = True

    def is_connected(self):
        return True

    async def get_messages(self, entity, limit=None, min_id=None, ids=None):
        if ids is not None:
            idx = {m.id: m for m in self.store}
            return [idx.get(i) for i in ids]
        if min_id is not None:
            return [m for m in self.store if m.id > min_id][:limit] if limit else [m for m in self.store if m.id > min_id]
        if limit:
            return self.store[-limit:]
        return list(self.store)


# ---------------------------------------------------------------- 环境
TMP = '/tmp/tgstate_test'
os.makedirs(TMP, exist_ok=True)
M.DATA_DIR = TMP
M.STATE_PATH = os.path.join(TMP, 'state.json')
for f in ('state.json',):
    p = os.path.join(TMP, f)
    if os.path.exists(p):
        os.remove(p)
M._STATE = None

SRC = '-1003436294431'
TGT = '-1003632724343'
RULE = {'id': 'r_fc2', 'enabled': True, 'name': 'FC2', 'source': SRC, 'target': TGT,
        'mode': 'forward', 'whitelist': ['fc2'], 'blacklist': []}

M.rule_matches = lambda rule, text: (True, '命中白名单 /fc2/') if 'fc2' in (text or '').lower() \
    else (False, '未命中白名单')

delivered = []

# 让 load_config() 返回带规则的内存配置（否则 rules 为空、循环体不执行）
_FAKE_CFG = {'web': {}, 'tg': {}, 'rules': [RULE], 'media_max_mb': 1800,
             'log_max_lines': 500, 'reconcile_seconds': 300}
M.load_config = lambda: _FAKE_CFG


def reset_state():
    """每个用例前清空内存缓存 + 磁盘 state.json"""
    p = os.path.join(TMP, 'state.json')
    if os.path.exists(p):
        os.remove(p)
    M._STATE = {'processed': {}}
    M.save_state()


def make_worker(store):
    w = M.Worker()
    w.client = FakeClient(store)

    async def fake_deliver_group(rule, msgs, why=''):
        delivered.append(('group', tuple(m.id for m in msgs), why))

    async def fake_deliver(rule, msg, why=''):
        delivered.append(('single', (msg.id,), why))

    w._deliver_group = fake_deliver_group
    w._deliver = fake_deliver

    async def fake_resolve(chat_id):
        return chat_id
    w._resolve = fake_resolve
    return w


# ---------------------------------------------------------------- 用例
async def t1_watermark_basic():
    print('\n[1] 水位线读写')
    reset_state()
    check('初始水位=0', M.get_watermark(SRC) == 0)
    M.bump_watermark(SRC, 1642, flush=True)
    check('抬到 1642', M.get_watermark(SRC) == 1642)
    M.bump_watermark(SRC, 1600, flush=True)
    check('不回落（仍 1642）', M.get_watermark(SRC) == 1642)
    M._STATE = None                      # 强制从磁盘重读
    check('落盘可重读', M.get_watermark(SRC) == 1642)
    check('state.json 已生成', os.path.exists(os.path.join(TMP, 'state.json')))


async def t2_reconcile_heals_missing_group():
    print('\n[2] 补偿扫描：源侧有、事件漏推的组被补转')
    reset_state()
    delivered.clear()
    # 源侧：广告(1500) + 相册A(1501-1502, fc2) + 相册B(1503-1505, fc2)
    store = [
        FakeMsg(1500, None, 'Telegram必备的搜索引擎 极搜JISOU'),
        FakeMsg(1501, f'gA', 'FC2PPV-1111 fc2'),
        FakeMsg(1502, 'gA', ''),
        FakeMsg(1503, 'gB', 'FC2PPV-2222 fc2'),
        FakeMsg(1504, 'gB', ''),
        FakeMsg(1505, 'gB', ''),
    ]
    M.bump_watermark(SRC, 1500, flush=True)   # 水位在广告处，A 组「漏推」
    w = make_worker(store)
    n = await w._reconcile_source(RULE)
    check('补转 2 组', n == 2, '实际 %d' % n)
    check('A 组整组转发(1501,1502)', ('group', (1501, 1502), '补扫｜命中白名单 /fc2/') in delivered)
    check('B 组整组转发(1503,1504,1505)', ('group', (1503, 1504, 1505), '补扫｜命中白名单 /fc2/') in delivered)
    check('水位抬到 1505', M.get_watermark(SRC) == 1505)


async def t3_reconcile_skips_rule_miss():
    print('\n[3] 补偿扫描：不命中的组不转发但抬水位')
    reset_state()
    delivered.clear()
    store = [
        FakeMsg(2000, None, 'FC2PPV-9999 fc2'),
        FakeMsg(2001, 'gX', '无关内容 xxx'),
        FakeMsg(2002, 'gX', ''),
    ]
    M.bump_watermark(SRC, 2000, flush=True)
    w = make_worker(store)
    n = await w._reconcile_source(RULE)
    check('补转 0 组', n == 0, '实际 %d' % n)
    check('未产生转发', len(delivered) == 0)
    check('水位仍抬到 2002', M.get_watermark(SRC) == 2002)


async def t4_reconcile_first_run_no_backfill():
    print('\n[4] 补偿扫描：首次运行只对齐水位、不回补历史')
    reset_state()
    delivered.clear()
    store = [FakeMsg(i, 'gZ', 'FC2PPV-0001 fc2') for i in range(3000, 3020)]
    w = make_worker(store)
    n = await w._reconcile_source(RULE)
    check('首次不回补', n == 0)
    check('水位对齐到最新', M.get_watermark(SRC) == 3019)
    check('未产生转发', len(delivered) == 0)


async def t5_reconcile_skips_in_flight():
    print('\n[5] 补偿扫描：正在聚合中的组不重复转发')
    reset_state()
    delivered.clear()
    store = [
        FakeMsg(4000, None, 'ad'),
        FakeMsg(4001, 'gLIVE', 'FC2PPV-7777 fc2'),
        FakeMsg(4002, 'gLIVE', ''),
    ]
    M.bump_watermark(SRC, 4000, flush=True)
    w = make_worker(store)
    w._albums[(M.Worker._norm_id(SRC), 'gLIVE')] = {'msgs': {}, 'task': None,
                                                    'flushing': False, 'last_seen': 0.0}
    n = await w._reconcile_source(RULE)
    check('在飞组不重复转', n == 0, '实际 %d' % n)


async def t6_settle_no_cancel_race():
    print('\n[6] 静默收尾：分片间隔小于 quiet 时整组一次转发')
    reset_state()
    delivered.clear()
    store = [
        FakeMsg(5001, 'gS', 'FC2PPV-5555 fc2'),
        FakeMsg(5002, 'gS', ''),
        FakeMsg(5003, 'gS', ''),
    ]
    w = make_worker(store)
    w._albums = {}
    M.rule_matches = lambda rule, text: (True, '命中白名单 /fc2/')

    async def rec_group(rule, msgs, why=''):
        delivered.append(('group', tuple(m.id for m in msgs), why))

    async def rec_single(rule, msg, why=''):
        delivered.append(('single', (msg.id,), why))

    w._deliver_group = rec_group
    w._deliver = rec_single

    for m in store:
        w._buffer_album(m, SRC)
        await asyncio.sleep(0.4)          # 每片间隔 0.4s < quiet 1.5s
    await asyncio.sleep(2.5)
    check('整组一次转发（3 片）',
          ('group', (5001, 5002, 5003), '命中白名单 /fc2/') in delivered,
          'delivered=%s' % (delivered,))
    check('只转发一次', len([d for d in delivered if d[0] == 'group']) == 1)
    check('没有拆成多次', not any(d[0] == 'single' for d in delivered),
          'delivered=%s' % (delivered,))


async def t7_settle_hard_cap():
    print('\n[7] 静默收尾：持续来片时靠 hard 上限保底收尾')
    reset_state()
    delivered.clear()
    w = make_worker([])
    w._albums = {}
    M.rule_matches = lambda rule, text: (True, '命中白名单 /fc2/')

    async def rec_group(rule, msgs, why=''):
        delivered.append(('group', tuple(m.id for m in msgs), why))

    w._deliver_group = rec_group

    key = (M.Worker._norm_id(SRC), 'gH')
    w._buffer_album(FakeMsg(6000, 'gH', 'FC2PPV-6666 fc2'), SRC)   # 建 slot
    # 用缩短的 hard 验证（直接调用 _settle_album 传参）
    task = asyncio.ensure_future(w._settle_album(key, quiet=1.5, hard=1.0, tick=0.1))
    for i in range(15):                  # 持续喂 3 秒 > hard 1.0s
        m = FakeMsg(6000 + i, 'gH', 'FC2PPV-6666 fc2' if i == 0 else '')
        w.client.store = [m]
        w._buffer_album(m, SRC)
        await asyncio.sleep(0.2)
    await asyncio.sleep(1.5)
    check('hard 上限触发收尾', len([d for d in delivered if d[0] == 'group']) >= 1,
          'delivered=%s' % (delivered,))


async def t8_complete_album_range():
    print('\n[8] 回捞范围：只捞同组，不把邻组算进来')
    # 锚点 7002，同组 7001-7003；邻组 7004-7005 不应被并入
    store = [
        FakeMsg(7001, 'g1', 'a'),
        FakeMsg(7002, 'g1', 'b'),
        FakeMsg(7003, 'g1', 'c'),
        FakeMsg(7004, 'g2', 'x'),
        FakeMsg(7005, 'g2', 'y'),
        FakeMsg(7020, 'g3', 'z'),
    ]
    w = M.Worker()
    w.client = FakeClient(store)
    msgs = [store[1]]                     # 只拿到中间一片 7002
    out = await w._complete_album(msgs)
    ids = [m.id for m in out]
    check('补全为 7001-7003', ids == [7001, 7002, 7003], '实际 %s' % ids)
    check('未混入 g2', 7004 not in ids and 7005 not in ids)


async def main():
    print('=' * 60)
    print('v1.0.2 漏转修复 单测')
    print('=' * 60)
    for fn in (t1_watermark_basic, t2_reconcile_heals_missing_group,
               t3_reconcile_skips_rule_miss, t4_reconcile_first_run_no_backfill,
               t5_reconcile_skips_in_flight, t6_settle_no_cancel_race,
               t7_settle_hard_cap, t8_complete_album_range):
        try:
            await fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append(fn.__name__ + '(异常)')
    print('\n' + '=' * 60)
    print('通过 %d / 失败 %d' % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print('  ❌ ' + f)
    print('=' * 60)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
