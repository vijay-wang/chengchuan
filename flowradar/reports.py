from __future__ import annotations

import json
from pathlib import Path


def money(value):
    if value is None:
        return "缺少数据"
    return f"{value/1e8:,.3f}亿元" if abs(value) >= 1e8 else f"{value/1e4:,.2f}万元"


def markdown(report):
    m = report["metrics"]
    rows = [f"# {report['name']}（{report['symbol']}）资金行为研究", "",
            f"请求区间：{report['requested_window']['start']} 至 {report['requested_window']['end']}（北京时间）。",
            f"分析区间：{report['actual_window']['start']} 至 {report['actual_window']['end']}（北京时间）。",
            "", f"**形态候选：{report['summary']}。** 规则分数未校准，不代表主力身份识别或概率。", "",
            "| 指标 | 结果 |", "|---|---:|",
            f"| 区间价格变化 | {m['return_pct']:.2f}% |",
            f"| 价格变化参考价 | {m['reference_price']:.4f}元（{m['reference_definition']}） |",
            f"| 成交额 | {money(m['amount_yuan'])} |",
            f"| 成交量 | {m['volume_shares']:,.0f}股 |",
            f"| 机构净买入金额 | 无法从当前数据识别 |",
            f"| 成交均价 | {m['vwap']:.4f}元 |" if m["vwap"] is not None else "| 成交均价 | 缺少成交额 |",
            "", "## 多种解释及证据", ""]
    for item in report["stages"]:
        rows.extend([f"### {item['name']}：{item['evidence_score']}/100（规则分数）", ""])
        rows.extend([f"- {x}" for x in item["evidence"]] or ["- 当前规则没有支持证据。"])
        rows.append("")
    rows.extend(["## 平台订单金额分类", ""])
    if m["platform_flow_yuan"]:
        labels = {"main_yuan": "大单＋超大单净额", "super_yuan": "超大单净额",
                  "large_yuan": "大单净额", "medium_yuan": "中单净额", "small_yuan": "小单净额"}
        rows.extend(["| 分类 | 净额 |", "|---|---:|"])
        rows.extend(f"| {label} | {money(m['platform_flow_yuan'][key])} |" for key, label in labels.items())
        rows.extend(["", f"所选行情时间戳覆盖率：{m['flow_coverage']:.1%}。这些分类不等于真实投资者身份。"])
    else:
        rows.append("所选区间没有可匹配的订单金额分类，未以零代替缺失值。")
    rows.extend(["", "## 金额估计与执行算法", "",
                 report["institutional_net_purchase_estimate"]["reason"], "",
                 report["execution_model_identification"]["reason"], "",
                 "## 其他解释", ""])
    rows.extend(f"- {x}" for x in report["counter_hypotheses"])
    rows.extend(["", "## 数据质量", ""])
    rows.extend(f"- {x}" for x in report["quality_notes"])
    rows.extend(["", "## 来源与复算", "",
                 f"模型版本：`{report['model_version']}`。JSON报告保留原始指标、规则证据和数据来源。", ""])
    for source in report["provenance"]:
        if "url" in source:
            rows.append(f"- [{source['url'].split('?')[0]}]({source['url']})；取得时间 {source['retrieved_at_utc']}。")
        else:
            rows.append(f"- 本地输入：{source.get('path', '')}")
    return "\n".join(rows) + "\n"


HTML = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FlowRadar · 资金行为研究</title>
<style>
:root{color-scheme:light;--ink:#172f43;--muted:#586b7a;--line:#d9e3e9;--accent:#087e8b}
*{box-sizing:border-box}body{margin:0;background:#f2f6f8;color:var(--ink);font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif}
main{max-width:1120px;margin:32px auto;padding:0 22px 48px}h1{font-size:29px;letter-spacing:-.7px;margin:8px 0}h2{font-size:20px;margin:0 0 14px}h3{margin:0}
.eyebrow{color:var(--accent);font-weight:750;letter-spacing:1.5px}.muted,small{color:var(--muted)}.notice{background:#fff4d6;border:1px solid #e3cb83;border-radius:10px;padding:13px 17px;margin:20px 0}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card,section{background:white;border:1px solid var(--line);border-radius:14px;padding:20px}.card strong{display:block;font-size:25px;margin-top:7px}
section{margin-top:20px}.two{display:grid;grid-template-columns:1.2fr 1fr;gap:20px}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:500}
.stage{margin:14px 0}.track{height:8px;background:#edf2f4;border-radius:4px;overflow:hidden}.fill{height:100%;background:var(--accent)}summary{cursor:pointer;font-weight:650}li{margin:7px 0}
#chart{width:100%;height:320px;display:block}.controls{display:flex;gap:20px;flex-wrap:wrap}.controls label{flex:1;min-width:200px}.controls input{width:100%}a{color:var(--accent);overflow-wrap:anywhere}.sources{font-size:12px;max-height:240px;overflow:auto}
.tag{border:1px solid #b8dadd;background:#edfafa;padding:3px 8px;border-radius:6px;font-size:12px}.footer{padding-top:16px;font-size:12px;color:var(--muted)}button{background:var(--ink);color:white;border:0;border-radius:8px;padding:9px 16px;cursor:pointer}
@media(max-width:720px){.grid{grid-template-columns:repeat(2,1fr)}.two{grid-template-columns:1fr}.card strong{font-size:20px}main{margin-top:20px}}
</style><main>
<div class="eyebrow">FLOWRADAR / 资金行为研究原型</div><h1 id="heading"></h1><div id="window" class="muted"></div>
<div class="notice"><b>研究模式 · 规则尚未校准</b>　“吸筹、洗盘、派发”为候选解释；下方分数表示规则支持程度。机构真实净买入额当前无法识别。</div>
<div class="grid"><div class="card"><small>区间价格变化</small><strong id="ret"></strong></div><div class="card"><small>成交额</small><strong id="amount"></strong></div><div class="card"><small>相对基准变化</small><strong id="relative"></strong></div><div class="card"><small>机构净买入估计</small><strong style="font-size:21px">无法识别</strong><small>缺少账户或母单标识</small></div></div>
<section><h2>价格路径与成交均价</h2><div class="muted" id="chart-label"></div><svg id="chart" viewBox="0 0 1040 320" role="img" aria-label="价格及区间成交均价图"></svg>
<div class="controls"><label>图表起点<input id="left" type="range" min="0" value="0"></label><label>图表终点<input id="right" type="range" min="1"></label></div>
<small>滑块只缩放图表；指标、规则评分和成交均价仍对应上方完整分析区间。</small></section>
<div class="two"><section><h2>候选阶段与证据</h2><div id="stages"></div><small>各项分数互不排斥，不能相加，也不是概率。</small></section>
<section><h2>订单金额分类</h2><p class="muted" id="coverage"></p><table><tbody id="flows"></tbody></table><p class="muted">大小单分类不揭示投资者身份；被动买入也可能被统计成主动卖出方向。</p></section></div>
<section><h2>金额与算法识别</h2><p id="estimate"></p><p id="execution"></p><span class="tag">随机TWAP / VWAP / POV / 限价执行 / 分散交易</span></section>
<div class="two"><section><h2>需同时考虑的解释</h2><ul id="counter"></ul></section><section><h2>数据质量与适用范围</h2><ul id="quality"></ul></section></div>
<section><h2>数据来源与复算</h2><div class="sources" id="sources"></div><p><button id="download">下载完整JSON报告</button></p></section>
<div class="footer" id="version"></div></main>
<script>
const R=__REPORT_JSON__;
const $=id=>document.getElementById(id), money=v=>v==null?'缺少数据':Math.abs(v)>=1e8?(v/1e8).toFixed(3)+'亿元':(v/1e4).toFixed(2)+'万元';
const pct=v=>v==null?'缺少基准':(v>=0?'+':'')+v.toFixed(2)+'%';
$('heading').textContent=R.name+' · '+R.symbol;
$('window').textContent=R.actual_window.start.replace('T',' ')+' — '+R.actual_window.end.replace('T',' ')+'（北京时间）';
const requested=document.createElement('div');requested.textContent='请求区间 '+R.requested_window.start.replace('T',' ')+' — '+R.requested_window.end.replace('T',' ')+'；涨跌参考 '+R.metrics.reference_price.toFixed(4)+' 元（'+R.metrics.reference_definition+'）';$('window').append(requested);
$('ret').textContent=pct(R.metrics.return_pct);$('amount').textContent=money(R.metrics.amount_yuan);$('relative').textContent=R.metrics.relative_return_pct==null?'缺少基准':R.metrics.relative_return_pct.toFixed(2)+'个百分点';
$('estimate').textContent=R.institutional_net_purchase_estimate.reason;$('execution').textContent=R.execution_model_identification.reason;
$('coverage').textContent='订单分类对行情时间戳覆盖率：'+(100*R.metrics.flow_coverage).toFixed(1)+'%';
for(const [key,label] of Object.entries({main_yuan:'大单＋超大单净额',super_yuan:'超大单净额',large_yuan:'大单净额',medium_yuan:'中单净额',small_yuan:'小单净额'})){
 const tr=document.createElement('tr'),a=document.createElement('td'),b=document.createElement('td');a.textContent=label;b.textContent=money(R.metrics.platform_flow_yuan?.[key]);tr.append(a,b);$('flows').append(tr);
}
for(const s of R.stages){const d=document.createElement('details');d.className='stage';const title=document.createElement('summary');title.textContent=s.name+' · '+s.evidence_score+'/100';const track=document.createElement('div');track.className='track';const fill=document.createElement('div');fill.className='fill';fill.style.width=s.evidence_score+'%';track.append(fill);const list=document.createElement('ul');for(const e of s.evidence){const li=document.createElement('li');li.textContent=e;list.append(li)}d.append(title,track,list);$('stages').append(d)}
for(const [id,items] of [['counter',R.counter_hypotheses],['quality',R.quality_notes]])for(const t of items){const li=document.createElement('li');li.textContent=t;$(id).append(li)}
for(const s of R.provenance){const p=document.createElement('p');if(s.url){const a=document.createElement('a');a.href=s.url;a.textContent=s.url.split('?')[0];a.rel='noreferrer';p.append(a,document.createTextNode(' · 获取时间 '+s.retrieved_at_utc))}else p.textContent=s.source+' · '+s.path;$('sources').append(p)}
$('version').textContent='模型 '+R.model_version+' · 仅用于行为研究；历史观察评估不等于主力身份识别准确率。';
$('download').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(R,null,2)],{type:'application/json'}));a.download=R.symbol+'-report.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};
const ns='http://www.w3.org/2000/svg',svg=$('chart'),B=R.chart_bars;
function node(tag,attrs,txt){const n=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);if(txt!=null)n.textContent=txt;svg.append(n);return n}
$('left').max=Math.max(0,B.length-2);$('right').max=B.length-1;$('right').value=B.length-1;
function draw(){let l=+$('left').value,r=+$('right').value;if(r<=l){r=Math.min(B.length-1,l+1);$('right').value=r}const a=B.slice(l,r+1);svg.replaceChildren();const values=a.map(b=>b.close);if(R.metrics.vwap!=null)values.push(R.metrics.vwap);let lo=Math.min(...values),hi=Math.max(...values),pad=Math.max((hi-lo)*.1,.01);lo-=pad;hi+=pad;const X=i=>66+i*940/Math.max(1,a.length-1),Y=p=>276-(p-lo)/(hi-lo)*245;
for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4,y=Y(v);node('line',{x1:66,x2:1006,y1:y,y2:y,stroke:'#e1e9ed'});node('text',{x:8,y:y+4,fill:'#647582','font-size':12},v.toFixed(2))}
node('polyline',{points:a.map((b,i)=>X(i)+','+Y(b.close)).join(' '),fill:'none',stroke:'#087e8b','stroke-width':2.5});
if(R.metrics.vwap!=null)node('line',{x1:66,x2:1006,y1:Y(R.metrics.vwap),y2:Y(R.metrics.vwap),stroke:'#c48a24','stroke-width':1.5,'stroke-dasharray':'6 5'});
node('text',{x:66,y:308,fill:'#647582','font-size':12},a[0].timestamp.replace('T',' '));node('text',{x:1006,y:308,fill:'#647582','font-size':12,'text-anchor':'end'},a.at(-1).timestamp.replace('T',' '));
$('chart-label').textContent='青色：采样价格；虚线：完整区间成交均价。当前缩放 '+a.length+' 条记录。'}
$('left').oninput=draw;$('right').oninput=draw;draw();
</script></html>'''


def write_report(report, output):
    prefix = Path(output)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    paths = {}
    for suffix, text in (("json", encoded + "\n"), ("md", markdown(report)),
                         ("html", HTML.replace("__REPORT_JSON__", encoded.replace("<", "\\u003c")))):
        path = Path(str(prefix) + "." + suffix)
        path.write_text(text, encoding="utf-8")
        paths[suffix] = str(path.resolve())
    return paths
