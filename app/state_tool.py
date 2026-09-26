#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""水位线运维工具（容器内运行）

补偿扫描靠 data/state.json 里的水位线（各源已处理到的最大消息 id）做对账。
bump_watermark 是「只增不减」的，正常情况下没问题；但当水位线被异常抬高
（例如误判、上游异常）时，漏掉的组就永远不会再被补扫。本工具用于人工干预。

用法（容器内）：
    python state_tool.py show                       # 查看所有源的水位线
    python state_tool.py set <源频道id> <max_id>    # 强制设定水位线（可回退）
    python state_tool.py reset <源频道id>           # 删除该源的水位线（下次扫描将重新初始化）
    python state_tool.py bump <源频道id> <max_id>   # 正常抬高（只增）

注意：set 是「直接覆盖」，会绕过只增逻辑，属于运维手段，请谨慎使用。
"""
import importlib.util
import os
import sys

MAIN = os.environ.get('MAIN_DIR', '/app')
spec = importlib.util.spec_from_file_location('m', os.path.join(MAIN, 'main.py'))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def usage():
    print(__doc__)
    return 2


def main():
    argv = sys.argv[1:]
    if not argv:
        return usage()
    cmd = argv[0]

    if cmd == 'show':
        st = M.load_state()
        proc = st.get('processed') or {}
        if not proc:
            print('（state.json 里没有任何水位线）')
            return 0
        print('%-16s %-10s %s' % ('源频道(id)', 'max_id', '更新时间'))
        for k, v in sorted(proc.items()):
            print('%-16s %-10s %s' % (k, v.get('max_id'), v.get('updated_at')))
        return 0

    if cmd == 'set':
        if len(argv) < 3:
            return usage()
        src, mid = argv[1], int(argv[2])
        st = M.load_state()
        k = M._norm_chan(src)
        rec = st['processed'].setdefault(k, {})
        old = rec.get('max_id')
        rec['max_id'] = mid
        rec['updated_at'] = 'manual-set'
        M.save_state()
        print('已设定 源=%s 水位 %s → %d' % (src, old, mid))
        return 0

    if cmd == 'bump':
        if len(argv) < 3:
            return usage()
        src, mid = argv[1], int(argv[2])
        old = M.get_watermark(src)
        M.bump_watermark(src, mid, flush=True)
        print('已抬高 源=%s 水位 %s → %s' % (src, old, M.get_watermark(src)))
        return 0

    if cmd == 'reset':
        if len(argv) < 2:
            return usage()
        src = argv[1]
        st = M.load_state()
        k = M._norm_chan(src)
        if k in (st.get('processed') or {}):
            del st['processed'][k]
            M.save_state()
            print('已删除 源=%s 的水位线（下次扫描会重新初始化）' % src)
        else:
            print('源=%s 本来就没有水位线' % src)
        return 0

    return usage()


if __name__ == '__main__':
    sys.exit(main())
