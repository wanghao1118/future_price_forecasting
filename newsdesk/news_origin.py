"""Issuing organization or analysis account, distinct from the collected feed.

Legacy enrichment is deliberately limited to explicit attribution. Normal
ingestion uses the per-item agent decision and a verbatim supporting excerpt.
"""
import re
import hashlib
import json

FIELDS = ('origin_name', 'origin_evidence', 'origin_method')
REVIEW_VERSION = 'issuer-semantic-v2-analysis-account'
ACCOUNT_ORIGIN_POLICY = '''自媒体分析采用账号来源口径：账号发布的分析、观点或评论，origin_name 填实际发布该内容的自媒体账号名称；明确的个人账号名可以包含个人姓名，不要求追溯其引用材料的最初机构。必须用 title/body 中可逐字核对的作者、账号署名或发布归属证明该账号发布了本篇内容；仅被提及的人名、账号名不能当来源，也不能把承载平台名称当成作者账号。例如钛媒体上“市值观察”发布的分析，来源为市值观察；36氪上“远川科技评论”发布的分析，来源为远川科技评论。分析引用 EIA 数据或高盛观点，不因此将整篇分析归给 EIA 或高盛。普通新闻转述高盛发布一份报告，来源仍为高盛。这一规则不放宽原油相关性、正文证据或十类分类要求。'''
STATUSES = ('identified', 'unresolved', 'multiple_sources', 'not_applicable')
REVIEW_FIELDS = (*FIELDS, 'origin_status', 'origin_reason', 'origin_reviewed_at', 'origin_review_version', 'origin_content_hash')
DIRECT_PUBLISHERS = {'institution_18', 'institution_45', 'institution_46', 'institution_51', 'goldman_sachs', 'zijin_mining'}
ALIASES = {
    '高盛': ('高盛集团', '高盛', 'Goldman Sachs'),
    '摩根士丹利': ('摩根士丹利', 'Morgan Stanley', '大摩'),
    '摩根大通': ('摩根大通', 'JPMorgan', 'J.P. Morgan'),
    '花旗': ('花旗集团', '花旗', 'Citigroup'),
    '美国银行': ('美国银行', '美银', 'Bank of America'),
    '瑞银': ('瑞银集团', '瑞银', 'UBS'), '巴克莱': ('巴克莱', 'Barclays'),
    '德意志银行': ('德意志银行', '德银', 'Deutsche Bank'),
    '野村': ('野村', 'Nomura'), '汇丰': ('汇丰', 'HSBC'),
    '渣打银行': ('渣打银行', '渣打', 'Standard Chartered'),
    '国际能源署（IEA）': ('国际能源署', 'IEA'),
    '美国能源信息署（EIA）': ('美国能源信息署', '美国能源信息局', 'EIA'),
    '美国石油协会（API）': ('美国石油协会', '美国石油学会', 'API'),
    'OPEC+': ('欧佩克+', 'OPEC+'), 'OPEC': ('欧佩克', '石油输出国组织', 'OPEC'),
    '美国商品期货交易委员会（CFTC）': ('美国商品期货交易委员会', 'CFTC'),
    '美联储': ('美国联邦储备委员会', '美联储', 'Federal Reserve'),
    '欧洲央行': ('欧洲中央银行', '欧洲央行', 'ECB'),
    '中国人民银行': ('中国人民银行', '中国央行'), '日本央行': ('日本央行', '日本银行'),
    '英国央行': ('英国央行', '英格兰银行'),
    '国家统计局': ('国家统计局',), '美国劳工统计局': ('美国劳工统计局', 'BLS'),
    '美国商务部': ('美国商务部',), '美国财政部': ('美国财政部',),
    '美国能源部': ('美国能源部',), '美国国务院': ('美国国务院',),
    '白宫': ('美国白宫', '白宫'), '国家发展改革委': ('国家发展改革委', '国家发改委'),
    '国家能源局': ('国家能源局',), '商务部': ('中国商务部', '商务部'),
    '海关总署': ('海关总署',), '北京市人民政府': ('北京市人民政府',),
    '沙特能源部': ('沙特能源部',), '俄罗斯能源部': ('俄罗斯能源部',),
    '伊朗石油部': ('伊朗石油部',), '伊拉克石油部': ('伊拉克石油部',),
    '沙特阿美': ('沙特阿美', 'Saudi Aramco'),
    '埃克森美孚': ('埃克森美孚', 'ExxonMobil'), '雪佛龙': ('雪佛龙', 'Chevron'),
    '壳牌': ('壳牌', 'Shell'), '道达尔能源': ('道达尔能源', 'TotalEnergies'),
    '英国石油（BP）': ('英国石油', 'BP'), '印度石油公司': ('印度石油公司',),
    '贝克休斯': ('贝克休斯', 'Baker Hughes'),
    '中国石油': ('中国石油天然气集团', '中国石油'),
    '中国石化': ('中国石油化工集团', '中国石化'),
    '中国海油': ('中国海洋石油集团', '中国海油'),
    '美国国家飓风中心（NHC）': ('美国国家飓风中心', 'NHC'),
    '美国国家海洋和大气管理局（NOAA）': ('美国国家海洋和大气管理局', 'NOAA'),
}
_alias_to_name = {alias: name for name, aliases in ALIASES.items() for alias in aliases}
_org = '|'.join(re.escape(alias) for alias in sorted(_alias_to_name, key=len, reverse=True))
_attribution = re.compile(
    r'(?<![A-Za-z\u4e00-\u9fff])(?:据|根据)?(?P<org>' + _org + r')'
    r'(?:[（(][^()（）\n]{1,45}[）)])?\s*'
    r'(?:(?:最新|今日|昨日|周[一二三四五六日])\s*)?'
    r'(?:[:：]|发布|公布|表示|指出|宣布|称|认为|上调|下调|维持|重申|警告|估计|测算|研报|报告|数据显示|的报告|的数据显示)')


def direct_publisher(record):
    meta = record.get('ingestion') or record
    if meta.get('source_id') not in DIRECT_PUBLISHERS:
        return ''
    return (meta.get('publisher_name') or record.get('source_name') or '').strip()


def validate_origin(name, evidence, record):
    if not isinstance(name, str) or not isinstance(evidence, str) or len(name) > 200 or len(evidence) > 1000:
        raise ValueError('来源机构和溯源依据必须为长度合法的文本')
    name, evidence = name.strip(), evidence.strip()
    if bool(name) != bool(evidence):
        raise ValueError('来源机构和原文溯源依据必须同时填写或同时留空')
    if evidence and not any(evidence in (text or '') for text in
                            [record.get('title'), record.get('body'), direct_publisher(record)]):
        raise ValueError('来源机构的溯源依据必须能在原文或明确机构署名中逐字核对')
    return name, evidence


def origin_fingerprint(record):
    value = {key: record.get(key, '') for key in ('title', 'body', 'source_name', 'source_url')}
    value['direct_publisher'] = direct_publisher(record)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_origin_status(value, record):
    name, evidence = validate_origin(value.get('origin_name'), value.get('origin_evidence'), record)
    status, reason = value.get('origin_status'), value.get('origin_reason')
    if status not in STATUSES or (status == 'identified') != bool(name):
        raise ValueError('溯源状态与来源机构不一致，只有 identified 可以且必须填写机构')
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 1000:
        raise ValueError('必须说明来源机构的归属依据，或无法确认的具体原因')
    return name, evidence


def explicit_origin(record):
    publisher = direct_publisher(record)
    if publisher:
        return dict(origin_name=publisher, origin_evidence=publisher, origin_method='publisher_metadata')
    title = record.get('title', '').strip()
    # Feed titles sometimes contain the entire body. Only the actual heading
    # may resolve priority among multiple organizations mentioned in the text.
    match = re.match(r'^【([^】]+)】', title)
    heading = match[1] if match else title if len(title) <= 180 else ''
    body = re.sub(r'^\s*【[^】]+】\s*', '', record.get('body', ''))
    body = re.sub(r'^(?:财联社|金十(?:数据)?)\d{1,2}月\d{1,2}日[电讯][，,：:\s]*', '', body)
    for index, text in enumerate([heading, body]):
        if index == 1 and (re.search(r'[:：]', heading) or re.search(r'整理|汇总|精选|投资日历|今日重点|新闻综述', title)):
            break
        matches = list(_attribution.finditer(text))
        if index == 1:
            # A quoted statistic deep in a report is not its issuing institution.
            # Only a direct attribution in the opening clause may supply it.
            matches = [m for m in matches if m.start() <= 45 and '。' not in text[:m.start()]]
        names = {_alias_to_name[m['org']] for m in matches}
        if len(names) == 1:
            m = matches[0]
            excerpt = text[m.start():min(len(text), m.end() + 100)].split('\n')[0]
            return dict(origin_name=names.pop(), origin_evidence=excerpt, origin_method='explicit_attribution')
        if names:  # A mixed heading/digest needs semantic review, not first-match wins.
            break
    return dict(origin_name='', origin_evidence='', origin_method='')
