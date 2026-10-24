/* 三态K线绘图（行情页与个股详情页共用）
 * 一张图里同时说明：均线定出的三态波段背景、趋势切换点、量能 / ADX / MACD（详情页）或 RSI（行情页）副图。
 * 数据来源：app.py 的 _chart_bundle()（行情页）与 /api/kline/<code>（详情页），字段同名。
 * 只做行情识别展示 + 今日区间纪律提示（震荡段 %B 上下沿价位 / 硬止损位），不画历史买卖点。
 */
function kg(v) { return (v === null || v === undefined) ? '-' : v; }

function renderKChart(el, c) {
  if (!window.echarts || !el || !c || !c.dates || !c.dates.length) return;
  var up = c.k.map(function (r) { return r[1] >= r[0] ? 1 : -1; });   // 阳线/阴线（[开,收,低,高]）
  var areaByDate = {}, flipByDate = {};
  // 波段是连续区间：把区间内每一天都映射到该波段，悬停才不用挑端点
  (c.areas || []).forEach(function (a) {
    for (var i = c.dates.indexOf(a.x0); i >= 0 && i < c.dates.length && c.dates[i] <= a.x1; i++) {
      areaByDate[c.dates[i]] = a;
    }
  });
  (c.flips || []).forEach(function (f) { flipByDate[f.date] = f; });
  var nArea = (c.areas || []).length;
  var isRsi = (c.sub4 === 'rsi');       // 第4副图：行情页给 RSI，详情页未设标记仍走 MACD
  var hh = 540;
  var grids = [], xAxes = [], yAxes = [], series = [];
  var top = [30, 0.56 * hh, 0.70 * hh, 0.84 * hh];
  var hgt = ['42%', '11%', '11%', '11%'];
  var names = ['K线', '量能', 'ADX', isRsi ? 'RSI' : 'MACD'];
  for (var g = 0; g < 4; g++) {
    grids.push({left: 58, right: 16, top: top[g], height: hgt[g]});
    xAxes.push({type: 'category', data: c.dates, boundaryGap: true, gridIndex: g,
                axisLabel: {show: g === 3}, axisTick: {show: g === 3},
                splitLine: {show: false}, axisLine: {lineStyle: {color: '#cbd5e1'}}});
    yAxes.push({scale: true, gridIndex: g, name: names[g], nameTextStyle: {color: '#7a8698'},
                axisLabel: {show: g === 0, fontSize: 11},
                splitLine: {show: true, lineStyle: {color: '#f0f2f6'}}});
  }
  if (isRsi) { yAxes[3].min = 0; yAxes[3].max = 100; yAxes[3].scale = false; }
  // 三态波段背景（markArea 的两端必须是真实日期，已由 _regime_paint 保证）
  var maData = (c.areas || []).map(function (a) {
    return [{xAxis: a.x0, itemStyle: {color: a.fill},
             name: (a.level_cn || a.label_cn) + ' ' + a.days + '日'}, {xAxis: a.x1}];
  });
  series.push({name: 'K线', type: 'candlestick', data: c.k, xAxisIndex: 0, yAxisIndex: 0,
               itemStyle: {color: '#ef5350', color0: '#26a69a',
                           borderColor: '#ef5350', borderColor0: '#26a69a'},
               markArea: nArea ? {silent: true, data: maData,
                                  label: {show: false}} : undefined,
               z: 2});
  [{n: 'MA5', d: c.ma5, col: '#f59e0b'},
   {n: 'MA20', d: c.ma20, col: '#2563eb'},
   {n: 'MA60', d: c.ma60, col: '#8b5cf6'}].forEach(function (m) {
    series.push({name: m.n, type: 'line', data: m.d, xAxisIndex: 0, yAxisIndex: 0,
                 showSymbol: false, lineStyle: {color: m.col, width: 1},
                 itemStyle: {color: m.col}, z: 3});
  });
  ['boll_up', 'boll_mid', 'boll_low'].forEach(function (k) {
    series.push({name: k, type: 'line', data: c[k], xAxisIndex: 0, yAxisIndex: 0,
                 showSymbol: false, lineStyle: {color: '#cbd5e1', width: 1, type: 'dashed'},
                 itemStyle: {color: '#cbd5e1'}, tooltip: {show: false}, z: 3});
  });
  // 震荡段的今日纪律位：只沿窗口尾部画一段，不把轨道当历史支撑
  var tp = c.tip || {};
  if (tp.kind === 'range') {
    var lvl = [], i0 = Math.max(0, c.dates.length - 25), i1 = c.dates.length - 1;
    var push = function (v, col, txt) {
      if (v === null || v === undefined) return;
      lvl.push([{coord: [c.dates[i0], v], lineStyle: {color: col, type: 'dashed', width: 1},
                 label: {show: true, position: 'insideEndTop', formatter: txt,
                         color: col, fontSize: 9}}, {coord: [c.dates[i1], v]}]);
    };
    push(tp.t_buy_px, '#15803d', '下沿提示位');
    push(tp.t_sell_px, '#c62828', '上沿提示位');
    push(tp.stop_price, '#c2410c', '硬止损');
    if (lvl.length) {
      series.push({name: '今日提示位', type: 'line', data: [], xAxisIndex: 0, yAxisIndex: 0,
                   markLine: {silent: true, symbol: 'none', data: lvl}});
    }
  }
  // 趋势切换点：菱形（分级标记在悬停里） + 新状态文字
  series.push({name: '趋势切换', type: 'scatter', data: (c.flips || []).map(function (f) {
                 return {value: f.value, text: f.text, color: f.color, grade: f.grade,
                         mark: f.mark, label_cn: f.label_cn, prev_cn: f.prev_cn, desc: f.desc};
               }), xAxisIndex: 0, yAxisIndex: 0, symbol: 'diamond', symbolSize: 13,
               itemStyle: {color: function (p) { return p.data.color || '#334155'; }},
               label: {show: true, position: 'bottom', distance: 3, fontSize: 9,
                       color: '#334155', formatter: function (p) { return p.data.label_cn; }},
               z: 12});
  series.push({name: '量能', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
               data: (c.vol || []).map(function (v, i) {
                 return {value: v, itemStyle: {color: up[i] > 0 ? '#f3a7a5' : '#8fd3ce'}};
               })});
  series.push({name: 'ADX', type: 'line', data: c.adx, xAxisIndex: 2, yAxisIndex: 2,
               showSymbol: false, lineStyle: {color: '#0f766e', width: 1},
               areaStyle: {color: 'rgba(15,118,110,0.06)'},
               markLine: {silent: true, symbol: 'none', label: {show: false},
                          data: [{yAxis: (c.adx_min === undefined || c.adx_min === null) ? 25
                                        : c.adx_min,
                                 lineStyle: {color: '#cbd5e1', type: 'dashed'}}]}});
  if (isRsi) {
    series.push({name: 'RSI', type: 'line', data: c.rsi, xAxisIndex: 3, yAxisIndex: 3,
                 showSymbol: false, lineStyle: {color: '#7c3aed', width: 1},
                 areaStyle: {color: 'rgba(124,58,237,0.06)'},
                 markLine: {silent: true, symbol: 'none',
                            label: {show: true, position: 'insideEndTop', fontSize: 9},
                            data: [{yAxis: 70, lineStyle: {color: '#ef5350', type: 'dashed'},
                                    label: {formatter: '70 超买', color: '#ef5350'}},
                                   {yAxis: 30, lineStyle: {color: '#26a69a', type: 'dashed'},
                                    label: {formatter: '30 超卖', color: '#26a69a'}}]}});
  } else {
    series.push({name: 'MACD柱', type: 'bar', xAxisIndex: 3, yAxisIndex: 3,
                 data: (c.hist || []).map(function (v) {
                   return {value: v, itemStyle: {color: v >= 0 ? '#ef5350' : '#26a69a'}};
                 })});
    series.push({name: 'DIF', type: 'line', data: c.dif, xAxisIndex: 3, yAxisIndex: 3,
                 showSymbol: false, lineStyle: {color: '#111827', width: 1}});
    series.push({name: 'DEA', type: 'line', data: c.dea, xAxisIndex: 3, yAxisIndex: 3,
                 showSymbol: false, lineStyle: {color: '#f59e0b', width: 1}});
  }

  var fmt = function (ps) {
    var arr = (ps && ps.length) ? ps : [ps];
    if (!arr[0] || arr[0].dataIndex === undefined || arr[0].dataIndex === null) return '';
    var i = arr[0].dataIndex, d = c.dates[i], o = c.k[i] || [];
    var t = '<b>' + d + '</b>　开 ' + kg(o[0]) + ' 收 ' + kg(o[1]) +
            ' 低 ' + kg(o[2]) + ' 高 ' + kg(o[3]);
    var a = areaByDate[d];
    if (a) t += '<br><span style="color:' + a.color + '">■</span> ' +
               (a.level_cn || a.label_cn) +
               '　本波段第 ' + (i - c.dates.indexOf(a.x0) + 1) + '/' + a.days +
               ' 日（段内强 ' + a.n_strong + ' 日，累计 ' + a.chg_pct + '%）';
    var f = flipByDate[d];
    if (f) t += '<br><b>趋势变化</b>：' + f.text;
    // 今日提示只挂在窗口最后一天（提示层给的是“今天该提醒什么”，不是历史信号）
    var tip = c.tip || {};
    if (tip.action && i === c.dates.length - 1) {
      t += '<br><b>今日提示</b>：' + tip.mark + ' ' + tip.action_cn;
      t += '<br>' + (tip.reason || '');
      if (tip.t_buy_px || tip.t_sell_px || tip.stop_price) {
        t += '<br>下沿 ' + kg(tip.t_buy_px) + ' / 上沿 ' + kg(tip.t_sell_px) +
             ' / 硬止损 ' + kg(tip.stop_price);
      }
      if (tip.hold_days) t += '　第 ' + tip.hold_days + '/' + kg(tip.max_hold) + ' 日';
      if (tip.risk) t += '<br><span style="color:#c2410c">⚠️ ' + tip.risk + '</span>';
    }
    t += '<br>MA5 ' + kg(c.ma5[i]) + ' / MA20 ' + kg(c.ma20[i]) + ' / MA60 ' + kg(c.ma60[i]);
    t += '<br>布林 ' + kg(c.boll_low[i]) + ' / ' + kg(c.boll_mid[i]) + ' / ' + kg(c.boll_up[i]) +
         '　区间位置 %B ' + kg(c.pctb ? c.pctb[i] : null) +
         '　ADX ' + kg(c.adx[i]) + (c.adx_min ? '(强档>' + kg(c.adx_min) + ')' : '') +
         '　ATR ' + kg(c.atr[i]);
    t += isRsi ? ('<br>RSI(14) ' + kg(c.rsi[i]))
               : ('<br>MACD ' + kg(c.dif[i]) + ' / ' + kg(c.dea[i]) + ' 柱 ' + kg(c.hist[i]));
    t += '　量能 ' + kg(c.vol[i]);
    return t;
  };
  var chart = echarts.init(el);
  chart.setOption({
    animation: false,
    legend: {data: ['K线', 'MA5', 'MA20', 'MA60', '趋势切换'],
             top: 0, itemWidth: 12, itemHeight: 8, textStyle: {fontSize: 11}},
    tooltip: {trigger: 'axis', axisPointer: {type: 'cross', link: [{xAxisIndex: [0]}]},
              formatter: fmt, confine: true, textStyle: {fontSize: 12}},
    axisPointer: {link: [{xAxisIndex: 'all'}]},
    grid: grids, xAxis: xAxes, yAxis: yAxes,
    dataZoom: [{type: 'inside', xAxisIndex: [0, 1, 2, 3]},
               {type: 'slider', xAxisIndex: [0, 1, 2, 3], bottom: 2, height: 16}],
    series: series
  });
  return chart;
}

/* 批量渲染：容器 id 为 k-<代码>，数据挂在页面里的 JSON 节点上 */
function renderKCharts(jsonId, key) {
  var node = document.getElementById(jsonId);
  if (!node) return [];
  var data = JSON.parse(node.textContent || '{}');
  var out = [];
  (data[key] || []).forEach(function (c) {
    var el = document.getElementById('k-' + c.code);
    if (el) out.push(renderKChart(el, c));
  });
  return out;
}
