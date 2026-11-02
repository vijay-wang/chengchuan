from pathlib import Path
import csv
import json
import math
import datetime

ROOT = Path(__file__).resolve().parent
read = lambda name: json.loads((ROOT / name).read_text())
quote_data = read('valuation_quotes.json')
quotes = {x['code']: x for x in quote_data['quotes']}
original = {x['code']: x for x in csv.DictReader((ROOT / 'v2_company_screen.csv').open(encoding='utf-8-sig'))}
companies = {}
models = {}
RATE = 0.12
FACTOR = (1 + RATE) ** 2


def add(code, score, components, opportunity, risk, period, sources, group, facts, excluded=False):
    q = quotes[code]
    band = '估值剔除' if excluded else ('优先研究' if score >= 75 else '条件研究' if score >= 65 else '观察' if score >= 55 else '暂不纳入')
    companies[code] = dict(code=code, name=q['name'], price=q['price'], a_price_equivalent_cap_yi=q['market_cap_yi_provider'],
                           quote_timestamp=q['quote_timestamp'], pe_ttm_provider=q['pe_ttm_provider'],
                           old_group=original[code]['group'], status=band, hard_excluded=excluded,
                           score=score, score_components=components, opportunity=opportunity, risk=risk,
                           latest_period_used=period, sources=sources, sector_group=group, facts=facts)


power = read('research_power.json')
for c in power['companies']:
    code = c['code']
    opportunities = {
        '688676': '数据中心在手订单及收入已有规模，交付仍有增量',
        '002353': '有约束采购单、预付款和主机测试进展，2027交付',
        '300811': 'TLVR及AI电感升级已形成收入、扣非利润增长',
        '002484': '电容多应用升级，Q3扣非增长改善',
        '000338': '主业现金流与分红支撑，发电产品增加成长来源',
        '600563': '薄膜电容认证壁垒、现金流扎实，主业价值可独立成立',
        '002851': '服务器电源生态及产品交付', '300870': '服务器电源规格与集中供电升级',
        '002364': 'HVDC及数据中心供电产品', '002138': '高端电感、磁性元件及AI配套',
        '002335': '电源、IDC及储能业务', '002922': '变压器与磁性器件，SST产品储备',
        '603989': '服务器电源电容及固液混合产品', '002318': '特种管材、核能长期需求',
    }
    fact = {k: c.get(k) for k in ['revenue_yi', 'net_profit_yi', 'core_profit_yi', 'cfo_yi', 'capex_yi', 'simplified_fcf_yi']}
    add(code, c['score'], c['score_components'], opportunities[code], c['reason'], c['period'], c['sources'], '供电与元件', fact, c['score'] is None)
    if 'scenario' in c:
        sc = {}
        for key, label in [('stress', '承压'), ('base', '基准'), ('strong', '较强')]:
            x = c['scenario'][key]
            sc[label] = dict(revenue_2027=x.get('revenue_2027_yi'), profit_2027=x['profit_2027_yi'],
                             revenue_2028=x.get('revenue_2028_yi'), profit_2028=x['profit_2028_yi'], pe=x['exit_pe_2028'])
        known_revenue_2028 = {
            '688676': {'承压': 110, '基准': 150, '较强': 180},
            '002353': {'基准': 445},
            '300811': {'承压': 35, '基准': 60, '较强': 75},
            '002484': {'基准': 90, '较强': 110},
            '600563': {'基准': 68},
        }
        for label, revenue in known_revenue_2028.get(code, {}).items():
            sc[label]['revenue_2028'] = revenue
        models[code] = dict(scenarios=sc, assumptions=c['scenario_assumptions'])

semis = read('research_semis.json')
for c in semis['primary']:
    code = c['code']
    opp = {'688008': 'DDR5迭代、MRDIMM和Retimer等互连新品', '300604': '国内测试设备替代、SoC及存储测试升级', '688123': 'SOCAMM等新模组SPD及高可靠EEPROM'}[code]
    risk = {'688008': '归母含较多投资等收益；经营估值高，路线与新品渗透不确定', '300604': '存货及应收增长、验收周期；现金转换和设备资本开支周期', '688123': '前三季扣非下降，投资损益扭曲PE；SPD单价与用量约束'}[code]
    sources = [semis['sources'][k] for k in c['sources']]
    add(code, c['score_total'], c['score'], opp, risk, c['period'], sources, '芯片与封测', c['actual'])
    models[code] = dict(scenarios={x['label']: dict(revenue_2027=x['revenue_2027_yi'], profit_2027=x['normalized_profit_2027_yi'], revenue_2028=x['revenue_2028_yi'], profit_2028=x['normalized_profit_2028_yi'], pe=x['exit_pe']) for x in c['cases']}, assumptions=[])

semi_opps = {'688041': '国产CPU/DCU需求', '688498': '光互连激光器与CW光源放量', '300394': '光器件与光引擎规模升级', '688627': '存储测试国产化和显示检测', '301308': '企业级存储增长与存储主业弹性', '002156': 'HPC及AMD生态封测', '600584': '先进封装与异构集成', '300408': 'MLCC及光通信陶瓷工艺平台', '000636': '高端MLCC与被动元件结构改善'}
for c in semis['others']:
    code = c['code']
    add(code, c['score_total'], c.get('score'), semi_opps[code], c['reason'], '详见对应公告及底稿；H1/Q3/预告分列', c['sources'], '芯片与封测', {}, c['score_total'] is None)

pcb = read('research_pcb.json')
for c in pcb['companies']:
    code = c['code']
    comp = c['score_components']
    if isinstance(comp, dict):
        comp = [comp[k] for k in ['valuation_30', 'profit_increment_25', 'moat_20', 'cash_balance_15', 'evidence_10']]
    add(code, c['score'], comp, '；'.join(c['opportunities']), '；'.join(c['risks']), c['facts']['period'], c['sources'], 'PCB与服务器', c['facts'], c['score'] is None)
    if c.get('scenarios'):
        models[code] = dict(scenarios={label: dict(revenue_2027=x['2027_revenue_yi'], profit_2027=x['2027_profit_yi'], revenue_2028=x['2028_revenue_yi'], profit_2028=x['2028_profit_yi'], pe=x['2028_terminal_pe']) for label, x in c['scenarios'].items()}, assumptions=[])

for c in read('research_battery.json'):
    f = c['facts']
    add(c['code'], c['score'], c['score_components'], f['opportunity'], f['risk'], f['period'], [x['url'] for x in c['sources']], '电池材料', f, c['hard_excluded'])

# Final editorial assessment after independent review: the base earnings hurdle is
# almost exactly met, while cash conversion and the 337 inquiry limit tolerance.
companies['601138']['score_components'] = [20, 22, 18, 5, 8]
companies['601138']['score'] = 73
companies['601138']['status'] = '条件研究'
companies['601138']['final_score_adjustment'] = '分组初评75；综合复核因现金转换、337及基准门槛余量很薄，估值和现金各减1分，最终73'

assert set(companies) == set(original), (set(original) - set(companies), set(companies) - set(original))
for c in companies.values():
    if c['score'] is not None:
        assert len(c['score_components']) == 5, c['name']
        assert sum(c['score_components']) == c['score'], c['name']
        assert all(0 <= x <= m for x, m in zip(c['score_components'], [30, 25, 20, 15, 10]))

for code, m in models.items():
    cap = companies[code]['a_price_equivalent_cap_yi']
    price = companies[code]['price']
    for label, x in m['scenarios'].items():
        x['terminal_equity_yi'] = x['profit_2028'] * x['pe']
        x['two_year_price_return_pct'] = (x['terminal_equity_yi'] / cap - 1) * 100
        x['cagr_pct'] = (math.sqrt(x['terminal_equity_yi'] / cap) - 1) * 100
    base = m['scenarios']['基准']
    m['profit_for_flat_price_yi'] = cap / base['pe']
    m['profit_for_12pct_annual_two_year_yi'] = cap * FACTOR / base['pe']
    m['base_discounted_reference_price'] = price * base['terminal_equity_yi'] / FACTOR / cap
    m['base_reference_price_with_20pct_margin'] = m['base_discounted_reference_price'] * 0.8
    m['base_reference_price_at_15pct_rate'] = price * base['terminal_equity_yi'] / (1.15 ** 2) / cap

ranked = sorted([c for c in companies.values() if not c['hard_excluded']], key=lambda c: (-c['score'], c['code']))
excluded = [c for c in companies.values() if c['hard_excluded']]
assert len(ranked) == 35 and len(excluded) == 14
for i, c in enumerate(ranked, 1):
    c['rank'] = i

metadata = dict(research_date='2026-10-28', quote_retrieved_at=quote_data['retrieved_at'], horizon='2027—2028，统一两年近似',
                assumed_annual_required_return=RATE, assumed_extra_margin_of_safety=0.2,
                profit_definition='可持续归母利润近似值；实际以扣非为起点，仍需剔除周期高利润及不可持续经营因素',
                model_limit='收入×有效归母净利率的估值压力测试；没有完整产品订单、ASP、份额与现金流输入，不作为盈利预测',
                count=49, valuation_excluded_count=14, scored_count=35)
(ROOT / 'A股投资价值_研究数据.json').write_text(json.dumps(dict(metadata=metadata, companies=list(companies.values()), scenarios=models), ensure_ascii=False, indent=2))
fields = ['rank', 'code', 'name', 'old_group', 'status', 'price', 'a_price_equivalent_cap_yi', 'pe_ttm_provider', 'quote_timestamp', 'score', 'valuation_30', 'growth_25', 'moat_20', 'cash_15', 'evidence_10', 'latest_period_used', 'opportunity', 'risk', 'sources']
with (ROOT / 'A股投资价值_49家公司评分与剔除表.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for c in ranked + excluded:
        row = {k: c.get(k, '') for k in fields}
        for k, v in zip(fields[10:15], c['score_components'] or ['', '', '', '', '']):
            row[k] = v
        row['sources'] = ' | '.join(c['sources'])
        w.writerow(row)


def fmt(v, digits=2):
    return '—' if v is None else f'{v:.{digits}f}'


def src(c, limit=2):
    return '、'.join(f'[公告{n + 1}]({url})' for n, url in enumerate(c['sources'][:limit]))


def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |'] + ['| ' + ' | '.join(map(str, r)) + ' |' for r in rows])


parts = []
parts.append('''# AI物理瓶颈与A股公司投资价值报告

**研究日：2026年10月28日｜范围：交接文件中的49家A股公司｜观察期：2027—2028年**

**结论：14家因估值透支直接剔除，35家完成投资价值评分。当前优先研究沪电股份、铂科新材、潍柴动力；法拉电子、工业富联列为条件候选。**其中，沪电的增长与价格匹配相对更好；其余公司的基准估值保护较薄，进入优先池也需要价格纪律。没有必要为了覆盖每条AI产业链而买入高估值公司。

本报告读取了《AI物理瓶颈与A股公司研究交接.md》及其49家公司清单，并重新核对原始财报、业绩预告、公司交流记录和价格。交接中的A/B/C分组仅作研究起点。此次把潍柴、法拉、生益科技等恢复到可比较的价值名单，也下调了部分原A组公司的现价吸引力。

## 1. 先看最终排序与价格纪律

评分衡量**当前价格下的投资吸引力**，不是公司技术水平、涨幅预测或成功概率。估值与安全边际30分、可持续利润增量25分、壁垒与份额20分、现金流与资产负债15分、证据与兑现10分。75分以上优先研究，65—74分条件研究，55—64分观察，低于55分暂不纳入。分数相差1—3分不代表可精确排序的统计优势。
''')
parts.append(table(['排序', '公司/代码', '评估价格/元', '分数', '判断'], [[c['rank'], f"{c['name']} {c['code']}", fmt(c['price']), c['score'], c['status']] for c in ranked[:16]]))
parts.append('''
同分可按风格理解：铂科偏成长，潍柴偏综合价值；工业富联规模利润有支撑但现金转换弱；法拉增长慢而现金质量较好。上述是同一框架下的不同风险来源，不能用AI业务纯度代替投资价值。

**最需要警惕的共同问题：**排名靠前也普遍存在扩产和营运资金占款。利润增长最终须转成可归属于股东的现金；低经营现金转换会降低可以接受的估值。全名单、五项评分及出处见第8节和配套CSV。

## 2. 估值透支：这14家公司直接剔除

这里把“泡沫/过度提前体现价值”操作化为：**现价要求2028年出现缺乏足够订单、利润率或现金证据支持的跨越式盈利提升。**这是本报告的估值判断，不是对未来股价走势的确定断言。没有仅因涨幅大、高TTM PE或业务占比小就剔除。

表中利润要求包含两年每年12%的研究回报门槛：`所需2028持续归母利润＝当前等价市值×1.12²÷2028退出PE`。12%是统一比较参数，不是用户指定收益或保证；PE是研究者设定的估值约束。盈利规模参照的简单年化不构成预测。
''')
exclude_metrics = {
    '002851': (25, 'H1扣非1.18亿；同比下降，现金弱'),
    '300870': (25, '前三季扣非亏损，Q3亦亏损'),
    '002364': (25, '前三季扣非0.75亿，经营现金-3.21亿'),
    '688041': (35, '前三季扣非25.84亿，经营现金-24.43亿'),
    '688498': (35, 'Q3扣非预告4—4.5亿，强季年化仍须再翻倍以上'),
    '300394': (35, 'H1扣非11.86亿；需达H1年化约3.3倍'),
    '688627': (35, 'H1扣非0.445亿；需达H1年化约11倍'),
    '601208': (25, 'Q3扣非年化约4.96亿；门槛约3.8倍'),
    '603256': (30, 'Q3扣非年化约15.09亿；高净利率后仍需约2.2倍'),
    '301511': (25, 'Q3扣非年化约7.57亿；现金-10.22亿'),
    '301217': (25, 'Q3扣非年化约4.81亿；门槛约6.7倍'),
    '002741': (25, '前三季扣非1.24亿；Q3下降、现金负流出'),
    '603663': (25, 'H1扣非0.74亿；门槛为H1年化约6.7倍'),
    '002971': (25, '前三季扣非0.388亿；门槛为年化约6.9倍'),
}
rows = []
for c in excluded:
    pe, why = exclude_metrics[c['code']]
    rows.append([f"{c['name']} {c['code']}", fmt(c['price']), fmt(c['a_price_equivalent_cap_yi'], 1), pe, fmt(c['a_price_equivalent_cap_yi'] * FACTOR / pe), why + '；' + src(c, 1)])
parts.append(table(['公司/代码', '价格', '等价市值/亿', '退出PE', '所需利润/亿', '剔除依据/原始出处'], rows))
parts.append('''
麦格、欧陆、中恒、精智达、铜冠、三祥、和远的已验证利润与价格之间尤其悬殊。源杰、天孚、宏和、德福等存在真实技术和增长，本次仍按严格的现价回报要求剔除；未证明增长虚假，也未把企业质量低与价格过高混为一谈。它们若未来利润证据或价格显著改变，应重新计算，而不是永久拉黑。

**江波龙、赣锋、天赐没有被按“极度泡沫”直接剔除。**它们的主要问题是周期正常化及回款，不能一边反对高PE，一边把涨价周期峰值利润按低PE长期资本化。

## 3. 最值得继续研究的五家公司

### 沪电股份（002463）｜80分｜增长与价格匹配较好，现金是主要制约

**事实。**前三季收入232.25亿、扣非54.74亿；Q3扣非26.80亿。2016.69亿等价市值相当于前三季扣非简单年化约27.6倍，按更强的Q3年化约18.8倍。两者都只是规模参照，不选择更便宜的一个当成全年预测。[三季报](https://static.cninfo.com.cn/finalpage/2026-10-28/1225627804.PDF)

**机会。**AI服务器与高速交换机提高板材和制造规格，已有实际利润支持；泰国工厂爬坡与新增合格产能可能为2027—2028提供下一段增长。相比纯材料涨价，客户工艺、良率和高端供货能力更有机会留下经营利润。

**风险。**前三季经营现金27.04亿，购建长期资产52.77亿，简化自由现金为-25.73亿；应收96.50亿、存货71.39亿。Q3扣非净利率约28.1%，不能永久外推。订单集中、海外产能良率、原料和云厂商资本开支均会改变回报。

**估值判断。**基准情景2028收入520亿、可持续归母率24%、利润124.8亿，25倍退出对应两年价格变化约+55%；承压情景利润66.5亿、20倍PE，对应约-34%。基准要求订单、产能和份额继续增长，不是已锁定事实。只有现金回款与盈利同步改善，当前排序才能保持。

### 铂科新材（300811）｜77分｜芯片供电增量较实，现价仍需增长兑现

**事实。**前三季收入20.71亿、扣非4.13亿；Q3收入8.88亿、扣非1.896亿，扣非同比+88.08%。半年报所称TLVR超过30%，分母是AI芯片电感收入，而非公司总收入。[三季报](https://static.cninfo.com.cn/finalpage/2026-10-28/1225628072.PDF)、[半年报](https://static.cninfo.com.cn/finalpage/2026-08-26/1225503598.PDF)

**机会。**芯片附近大电流、损耗与空间约束支持高性能磁材和电感升级；粉末、粉芯到电感的制造链有助于技术迭代和成本控制。既有扣非增长比单纯生态合作更能支撑投资逻辑。

**风险与价格。**前三季经营现金2.71亿，简化自由现金约0.46亿，应收、存货增长；扩产后ASP、良率、客户份额和集成化都可能影响利润。现价对Q3扣非年化仍约39倍。基准2028收入60亿、净利率22.5%、利润13.5亿，已经要求收入较Q3年化提高约69%；30倍退出的两年价格空间约37%。如果只以Q3扣非年化为起点、未来两年利润每年增长20%，2028约10.92亿，即便30倍退出也只有约5.4%价格年化回报。若收入只能停在35亿、净利率20%，叠加25倍退出，价格情景约-41%。77分依赖较快增长继续兑现，不能把当前高增当作安全边际。

### 潍柴动力（000338）｜77分｜成熟盈利与现金流支撑，发电作为增量

**事实。**H1扣非61.19亿、同比+18.86%，经营现金113.49亿，简化自由现金88.65亿；归母77.01亿高于扣非，差额不作永久盈利。21.35元A价对应全股本等价估值1844.69亿，约为H1扣非年化15.1倍。[半年报](https://static.cninfo.com.cn/finalpage/2026-08-28/1225520941.PDF)

**机会。**已有发动机、整车和物流装备基本盘，发电需求可以叠加成长。H1发电产品销量6.5万台、增长31.3%，其中数据中心发电销量超过2025全年；6.5万台不是全部数据中心产品。H1每股现金分红0.517元已有公告支持，未来分红仍须由利润和决议决定。

**风险与价格。**重卡及物流周期、海外业务与少数股东仍决定大部分价值；不能给全集团AI高倍数。基准2028持续归母170亿、14倍PE，两年价格变化约29%，尚未计未来分红；承压110亿、11倍PE约-34%。170亿含主业约12%复合增长及电力能源结构改善的假设，未单列披露的AI净利润不能写成已确认增量。它更适合作为综合价值候选，而非纯AI弹性标的。

### 工业富联（601138）｜73分｜基准仅略过回报门槛，降为条件研究

**事实。**本轮采用已核实H1，原报表单位为人民币千元，换算收入5578.61亿、扣非229.84亿；等价估值9652.17亿约为H1扣非年化21倍。H1云计算收入+75.7%、云服务商AI服务器收入增长2.3倍属于不同口径，不相加。[半年报](https://static.cninfo.com.cn/finalpage/2026-08-12/1225468066.PDF)

**机会。**GPU、ASIC和CPU服务器及机柜集成覆盖多条技术路线，电源、散热和网络升级可能提高交付价值量；规模大不等于没有增量，但必须核实利润属于A股上市主体，不能挪用鸿海集团订单。

**风险。**H1扣非归母率仅4.12%；经营现金73.91亿，低于扣非利润，购建长期资产90.92亿；存货1921.09亿、应收1275.22亿。假设年收入1.6万亿，毛利率改变0.5个百分点，会改变税前毛利约80亿，低利润率放大了误差。另有10月12日披露的Vicor垂直供电专利337调查：公司称相关产品当前处于内部验证评估、目前无实质影响，最终裁决尚不确定。[公司专项公告](https://static.cninfo.com.cn/finalpage/2026-10-12/1225600466.PDF)

**估值判断。**基准2028收入1.6万亿、归母率4.25%、利润680亿，18倍退出对应两年价格变化约27%、年化约12.6%；680亿仅比672.65亿回报门槛高约1.1%。若2028利润385亿且估值14倍，价格情景约-44%。综合复核因此将分组初评75下调至最终73，估值和现金项各减1分；先等现金、正式报表和价格带来更多容错。

### 法拉电子（600563）｜74分｜现金质量备选，增长要求要克制

**事实与机会。**H1扣非5.73亿、经营现金8.25亿、简化自由现金5.31亿，薄膜电容的工艺和客户认证支撑主业价值。约19—20倍简单年化经营估值不算过热，值得从旧C组恢复。[半年报](https://static.cninfo.com.cn/finalpage/2026-08-22/1225489178.PDF)

**风险与价格。**H1扣非只增长3.06%，新能源车、光伏、储能竞争与原料成本仍重要；AI收入未单列，不能用AI叙事给全集团估值加倍。基准2028收入68亿、归母率21.5%、利润14.62亿、20倍退出，两年价格变化约26%；承压约-24%。已实施的2025年度分红2.30元/股是历史事实，未加入未来收益模型。现价较接近基准折现值，74分反映质量与价格平衡，不是显著低估。

## 4. 原优先名单中，其余六家怎样处理

**金盘科技｜73分，条件研究。**前三季数据中心收入23.98亿、AIDC在手55.65亿，订单真实；全公司扣非5.19亿，但经营现金仅0.91亿，铜成本、股份支付、汇率及CBP退税都影响利润质量。退税金额未单列，无法声称已精确去除。基准2028利润12亿、25倍退出，而当前价格要求11.60亿才达两年12%门槛，余量很薄。SST样机不额外加估值；提高评分要看到订单回款和净利润率一起改善。[三季报](https://static.cninfo.com.cn/finalpage/2026-10-14/1225603297.PDF)

**杰瑞股份｜70分，订单研究保留，现价等待更厚回报。**14.65亿美元订单约99.5亿，约定2027年11月前分批交付；已包含于H1披露的累计31亿美元订单中，不能重复相加。10月26日交流记录有预付款和FTAI样机关键测试进展。集团70.51亿合同负债包含多项目，该单具体预付款未披露，不能反推。基准2028原业务195亿收入×14%归母率，加供电250亿×7%有效归母率，合计44.8亿；2027/2028供电收入180/250亿还要求已披露约209亿之外继续取得补单，供电净利率与JV利润分配仍未验证。25倍退出时现价要求48.24亿，基准两年价格年化只有约7.9%，大单并不自动等于高投资回报。[合同](https://static.cninfo.com.cn/finalpage/2026-07-23/1225436739.PDF)、[三季报](https://static.cninfo.com.cn/finalpage/2026-10-22/1225613985.PDF)、[10月26日公司交流](https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12659632&stockid=002353)

**聚辰股份｜66分，不能按归母PE当便宜股。**前三季归母4.86亿、扣非仅2.34亿且同比-18.79%；Q3扣非1.40亿，但金融资产波动令归母亏损。前三季扣非年化对应约60倍、Q3年化约33倍，择一都不能当预测。SOCAMM模组SPD有真实机会，但用量不随DRAM容量线性增加。基准2028可持续利润7.2亿、28倍退出，两年价格空间仅8%左右；需达到8.34亿才满足两年12%。继续验证SPD收入、单价、非PC客户和现金回款。[三季报](https://static.cninfo.com.cn/finalpage/2026-10-24/1225620951.PDF)、[半年报](https://static.cninfo.com.cn/finalpage/2026-08-15/1225475815.PDF)

**江海股份｜66分，下调AI利润预期。**前三季扣非6.10亿，Q3扣非增长37.89%；但公司6月18日公告明确，Q1超容收入1.06亿，其中AI服务器电源端仅约1200万元；MLPC Q1收入2200万元，占全公司1.59%。不能把整个超容/MLPC板块都算AI利润。基准2028利润12.6亿、25倍退出，低于现价所需14.97亿门槛。公司整体可研究，但需用可识别AI分项或多业务增长证明当前价格。[三季报](https://static.cninfo.com.cn/finalpage/2026-10-23/1225615320.PDF)、[异常波动公告](https://disc.static.szse.cn/disc/disk03/finalpage/2026-06-18/02a38f6e-ca19-441f-90db-30a1b0694042.PDF)

**澜起科技｜64分，壁垒高而经营估值偏贵。**H1归母19.97亿、扣非13.22亿；Q3目前采用扣非9.5—10.5亿的预告，不能把归母与经营利润混同。DDR5、MRDIMM、Retimer机会真实，LPDDR/SOCAMM路线可能改变传统RCD/MDB价值量。基准2028利润62亿、35倍退出，两年价格空间仅约2%；现价要求约76.42亿才达12%年化。新品放量若不超过当前预期，利润大涨也可能没有相应股价收益。[半年报](https://static.cninfo.com.cn/finalpage/2026-08-29/1225530486.PDF)、[Q3预告](https://static.cninfo.com.cn/finalpage/2026-10-21/1225612915.PDF)

**长川科技｜64分，增长兑现，价格容错有限。**前三季扣非17.17亿、经营现金10.39亿，Q3扣非7.95亿；存货61.80亿、应收27.01亿，验收、减值和设备周期都应进入估值。基准2028利润42亿、30倍退出，两年价格空间同样约2%；现价要求51.75亿才达12%年化。优质增长候选仍可等待回款验证或更低价格。[三季报](https://static.cninfo.com.cn/finalpage/2026-10-27/1225627248.PDF)

## 5. 重新纳入的PCB与材料，以及电池路线

**生益科技68分、深南电路68分、生益电子67分，恢复条件研究。**生益科技10月17日已有前三季扣非61.2—65.3亿预告，按简单年化约28.6—30.5倍，不应继续拿滞后的47.9倍接口PE判泡沫。其高端CCL升级与普通品涨价必须分开，且合并生益电子，不能重复计算两家收入和价值。[生益科技预告](https://static.cninfo.com.cn/finalpage/2026-10-17/1225608514.PDF)

深南前三季扣非40.98亿，但经营现金19.59亿、资本现金支出76.13亿，拟融资与折旧回报使每股价值承压；生益电子前三季扣非预告18.65—19.48亿，仍须正式现金和资产表验证。前者基准2028利润97.2亿、25倍PE几乎贴着12%门槛；后者基准46.2亿相对34.83亿门槛有空间，但缺正式Q3验证，不能给予同等置信度。[深南三季报](https://static.cninfo.com.cn/finalpage/2026-10-23/1225616402.PDF)、[生益电子预告](https://static.cninfo.com.cn/finalpage/2026-10-17/1225608856.PDF)

**胜宏63分、南亚61分，观察而非直接判泡沫。**胜宏H1扣非只增长12.5%，低于归母33.3%，经营现金29.11亿虽好，资本支出70.76亿仍高；2028正常利润需88.91亿才达25倍退出的12%门槛。南亚Q3扣非9.85亿显著抬高盈利平台，不能只因一年涨幅大剔除；但前三季经营现金-1.00亿，应收43.41亿，Q3高利润率尚需连续回款验证。用Q3年化就会显得很便宜，依赖这一季度长期化风险很大。[胜宏半年报](https://static.cninfo.com.cn/finalpage/2026-08-28/1225522515.PDF)、[南亚三季报](https://static.cninfo.com.cn/finalpage/2026-10-28/1225630419.PDF)

固态电池另作技术升级主题，不把全产业链都归入AI采购。该组没有一家仅凭全固态预期进入优先池：

- **天赐材料63分：**H1扣非28.07亿真实修复，但经营现金仅3.97亿、应收107.36亿。重点是电解液与锂盐周期正常利润、回款及潜在H股发行股数；中试线不计规模固态利润。[半年报](https://static.cninfo.com.cn/finalpage/2026-08-21/1225486883.PDF)
- **当升科技62分：**全固态正极累计50吨、固液千吨级供货分开确认，阶段比纯送样更靠前；但Q3扣非同比-10.42%，前三季简化自由现金约-30.37亿，海外及铁锂扩产可能吞噬利润。当前估值首先依赖成熟正极业务。[半年报](https://static.cninfo.com.cn/finalpage/2026-08-26/1225502782.PDF)、[三季报](https://static.cninfo.com.cn/finalpage/2026-10-27/1225624902.PDF)
- **天奈科技62分：**一般单壁产品已经批供，可受益快充与硅负极，不用只押全固态；但适配湿法/干法/固态的易分散产品仍有小试项目。前三季扣非2.04亿，Q3年化对应约36倍，价格仍要求较强增长。[半年报](https://static.cninfo.com.cn/finalpage/2026-08-25/1225495086.PDF)、[三季报](https://static.cninfo.com.cn/finalpage/2026-10-27/1225625740.PDF)
- **赣锋56分、东方锆业51分、容百46分、瑞泰39分、天铁33分：**分别受锂价周期、全固态贡献未拆、利润与产能回报、非经常损益及合格供货证据约束，详细机会、风险、出处逐家列于第8节。扣非仍可能是周期高位，不等于已完成正常化。

## 6. 估值模型：增长多少，才足以覆盖现价

采用两年简化权益模型，所有2027、2028数字均为**研究压力测试假设**，不是公司指引或一致预期。收入×有效归母净利率包含正常研发、股份支付、折旧、税费和少数股东影响，不把补贴/卖股/公允价值收益永久化。扣非是起点，仍可能含周期、汇兑或经营性退税等不可持续因素。

估值公式：`两年价格变化＝2028可持续归母利润×退出PE÷当前等价市值－1`。表内不计未来股息，也未预设尚未发行的股份；若股本再增5%且募集资金回报未抵消，每股终值还需÷1.05。没有再把账上现金全额加回，以免现金收益与PE重复计价。

PE参数来自商业模式与风险折价：综合装备约11—16倍；成熟优质元件约16—23倍；发电/变配电、PCB约20—30倍；较高壁垒磁性制造25—35倍；测试设备25—35倍；互连芯片28—40倍。它们不是已核验的行业公允常数，须观察盈利和现金后调整。
''')
model_order = ['002463', '300811', '000338', '601138', '600563', '688676', '002353', '002916', '600183', '688183', '688123', '002484', '688008', '300604', '300476', '688519']
rows = []
for code in model_order:
    c, m = companies[code], models[code]
    b = m['scenarios']['基准']
    rows.append([c['name'], fmt(b['profit_2027'], 1), fmt(b['profit_2028'], 1), b['pe'], fmt(m['profit_for_12pct_annual_two_year_yi'], 1), f"{b['cagr_pct']:+.1f}%", f"{m['scenarios']['承压']['two_year_price_return_pct']:+.0f}% / {b['two_year_price_return_pct']:+.0f}% / {m['scenarios']['较强']['two_year_price_return_pct']:+.0f}%"])
parts.append(table(['公司', '2027基准利润/亿', '2028基准利润/亿', '基准PE', '12%所需2028利润/亿', '基准价格年化', '承压/基准/较强两年价格变化'], rows))
parts.append('''
**解读：**沪电的基准利润高于回报门槛较多；铂科有一定余量，但依赖未来两年收入较强增长；工业富联、潍柴、法拉、金盘、深南、生益科技的基准利润与12%门槛相近，模型误差足以吞掉收益；杰瑞、江海、澜起、长川、聚辰的基准情景尚未达到门槛。生益电子、南亚的数值空间较大，但盈利平台、产能和现金证据较弱，因此没有只按情景涨幅打高分。

承压、基准、较强没有赋予概率，不能把中间列理解成“最可能收益”。承压情景也不是最坏情况；极端贸易、客户或技术事件可能造成更差结果。

### 价格敏感性：基准折现值与额外安全垫

“12%折现参考价”是基准2028终值折回今天的机械数；再打八折用来给预测错误留余量。**这不是技术支撑位、保证买点或固定目标价。**盈利假设下降，参考价也同步下降。提高所需年回报至15%，同一终值的参考价还会下降。
''')
rows = []
for code in model_order[:14]:
    c, m = companies[code], models[code]
    rows.append([c['name'], fmt(c['price'], 1), fmt(m['base_discounted_reference_price'], 1), fmt(m['base_reference_price_with_20pct_margin'], 1), fmt(m['base_reference_price_at_15pct_rate'], 1)])
parts.append(table(['公司', '现价/元', '12%折现参考价', '再留20%余量', '15%折现参考价'], rows))
parts.append('''
如果严格执行“基准折现后还须有20%安全边际”，多数公司当前价格都不满足；即便沪电接近这一条件，也仍依赖较强的2027—2028经营假设。报告的价值在于减少追逐高价概念，并给出可复核的等待条件，而非强行凑出一组现价买入名单。

## 7. 后续最重要的验证节点

1. **利润与现金：**下一次正式财报先看扣非、经营现金、应收、存货和购建长期资产。沪电/金盘/铂科要看增长是否带来回款；工业富联关注营运资本与低利润率的脆弱性；长川关注库存结构和验收。
2. **订单与技术兑现：**杰瑞核主机测试、分批交付、验收及JV权益；江海核AI真实分项；澜起/聚辰核DDR与LPDDR模组区别、新品量价与客户份额。送样、认证、框架、约束采购、交付、收入、回款分阶段跟踪。
3. **报告披露与稀释：**本轮检索中，工业富联、胜宏、生益电子、生益科技、潍柴等正式Q3尚未取得，部分有预告；市场预约披露日期不等于已披露。天孚、精智达同样只使用本轮取得的H1。新财报、H股或定增实际股数改变时，须重算每股估值。

AI资本开支下修、接网/主机/许可延迟、产能扩张压价、软件效率提升、路线替代与贸易限制，都会同时影响多个环节。持有多个上下游公司仍可能承受同一资本开支周期。生益科技与生益电子尤其存在母子经营敞口重叠。

## 8. 通过估值硬筛选的35家：逐家机会、风险与完整分数

以下每家均有投资价值评分，包括暂不纳入者；“没进优先池”与“已证明泡沫”分开处理。五项依次为估值/利润增量/壁垒/现金/证据，满分30/25/20/15/10。详细现金、情景和原始公告另存研究数据JSON及分组底稿。
''')
for group in ['供电与元件', '芯片与封测', 'PCB与服务器', '电池材料']:
    parts.append(f'### {group}\n')
    rows = []
    for c in ranked:
        if c['sector_group'] != group:
            continue
        score = '/'.join(map(str, c['score_components']))
        risk = c['risk']
        # Long semis notes retain their quantitative reasoning in the dedicated memo.
        if len(risk) > 210:
            risk = risk[:208] + '…（完整理由见配套CSV/JSON）'
        score_cell = f"<strong>{c['score']}分</strong><br>{fmt(c['price'])}元<br><span style='white-space:nowrap;font-size:.92em'>{score}</span>"
        name_cell = f"<span style='white-space:nowrap'>{c['name']}</span><br><span style='white-space:nowrap'>{c['code']}</span>"
        rows.append([name_cell, score_cell, c['opportunity'], risk + '；' + src(c, 1)])
    parts.append(table(['公司/代码', '总分/价格/五项', '机会', '主要风险及出处'], rows))

parts.append('''
## 9. 可复算情景明细

下表全部为假设，单位亿元。2027/2028列为“收入/有效可持续归母利润”，不构成订单明细。有效净利率可用利润÷收入复算；本轮缺乏完整产品ASP、份额、订单与产能良率，因此没有补造精密产品模型。杰瑞的收入需按原业务与供电业务分别计算；潍柴采用利润基数法。收入留空的部分情景只代表总利润压力测试，不暗示掌握未披露的量价输入。
''')
rows = []
for code in model_order:
    c, m = companies[code], models[code]
    for label in ['承压', '基准', '较强']:
        x = m['scenarios'][label]
        rows.append([c['name'], label, f"{fmt(x.get('revenue_2027'), 1)}/{fmt(x['profit_2027'], 1)}", f"{fmt(x.get('revenue_2028'), 1)}/{fmt(x['profit_2028'], 1)}", x['pe'], fmt(x['terminal_equity_yi'], 1)])
parts.append(table(['公司', '情景', '2027收入/利润', '2028收入/利润', '2028PE', '两年末权益价值'], rows))
parts.append('''
## 10. 数据口径、证据边界与交付文件

- **价格：**2026年10月28日盘后获取腾讯行情接口快照，接口时间16:11:51—16:12:41。它取代交接中14:52盘中价；本轮未另以交易所数据独立确认正式收盘价。原始响应在 `valuation_quotes_raw.txt`，结构化行情在 `valuation_quotes.json`。[行情来源示例](https://qt.gtimg.cn/q=sh688676,sz002463,sh601138)
- **估值：**统一采用A股价格与全体已发行普通股本相对应的等价权益估值，便于与全公司归母盈利比较。对A+H公司，它不是两类股票按不同价格加总的实际交易市值。澜起、潍柴、胜宏、三环等H股已发行情况在分组底稿核对；赣锋同按每股盈利逻辑，不因H股折价降低A股买入所付价格。未来可转债、拟定增和待完成H股发行不视为已经完成；新股数与募集资金回报都需另算。
- **利润与现金：**正式财报、预告、公司表述与研究假设分列；季度报表和多数H1未经审计。简化自由现金＝经营现金－购建固定资产、无形资产和其他长期资产现金，不等于归母股东自由现金，也不含全部并购、租赁及供应链融资调整。
- **证据：**公司公告是原始披露证据，不代表匿名客户或技术前景已独立验证。订单台账未覆盖全部未公开客户、取消条款或2028补单；未披露分部净利率与成本拆分时，给出达标条件，不把假设写成已取得利润。行业预测、客户总资本开支和框架协议不计公司订单。
- **评分边界：**财报信息充分程度不同，缺口已在现金和证据项扣分。不存在能由公开信息证明“股价已完全体现全部未来价值”的客观精确线；14家剔除是本次严格估值纪律的结果。报告不推断用户仓位、杠杆或承受损失能力，也不据此配置具体仓位。

交付包括：本报告Markdown、便于阅读/打印的PDF、浏览版HTML；49家公司评分与剔除CSV；研究数据JSON（含16家公司共48组情景）；原始行情和各组公告PDF/文本。分组工作底稿为 `research_power.md`、`research_semis.md`、`research_pcb.md`、`research_battery.md`，可用于逐条追溯本报告，完整原交接文件保持原样。
''')

report_path = ROOT / 'A股投资价值报告_2026-10-28.md'
report_path.write_text('\n\n'.join(parts))
print(json.dumps(dict(report=str(report_path), company_count=len(companies), excluded=len(excluded), scored=len(ranked), models=len(models), scenarios=sum(len(x['scenarios']) for x in models.values()), score_bands={key: sum(c['status'] == key for c in companies.values()) for key in ['优先研究', '条件研究', '观察', '暂不纳入', '估值剔除']}), ensure_ascii=False, indent=2))
