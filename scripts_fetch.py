# -*- coding: utf-8 -*-
"""数据拉取冒烟测试"""
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

from core import data

codes = sys.argv[1:] or ['600519', '000001', '002475', '300124', '002139', '300257']
for code in codes:
    try:
        df = data.get_kline(code, '2015-01-01')
        name = data.get_name(code)
        print(code, name, len(df),
              df['date'].iloc[0] if len(df) else '-',
              df['date'].iloc[-1] if len(df) else '-')
    except Exception as e:
        print(code, 'FAIL', repr(e))
