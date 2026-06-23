#!/usr/bin/env node
/**
 * Qianwen Source Extractor
 *
 * Connects to an already-running Chrome instance via CDP, finds the open
 * Qianwen share page (https://www.qianwen.com/share/chat/...), and extracts
 * the reference / citation sources by calling the share/info API.
 *
 * The Qianwen share page renders sources as div.source-item-yI6DUI elements
 * in the DOM, but those elements only contain the platform name + domain
 * (e.g. "中国食品安全网 www.cfsn.cn"), NOT the real article URL. The actual
 * source URLs (e.g. "https://www.cfsn.cn/news/detail/2137/343342.html") are
 * only available via the share/info API:
 *
 *   POST https://chat2-api.qianwen.com/api/v1/share/info?pr=qwen&fr=mac
 *   Body: {"share_id":"<id>","biz_id":"ai_qwen"}
 *
 * The API response contains the full source list at:
 *   data.session.record_list[].response_messages[].meta_data.sources[].content.list[]
 *
 * Each source item includes: url, normalized_url, title, name (platform),
 * summary, publish_time, type, reliable, authority, icon, etc.
 *
 * This script replays that API call from the page context (so it carries the
 * correct cookies/origin) and extracts the real source URLs.
 */

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const DEFAULT_CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:9222';
const SHARE_INFO_API = 'https://chat2-api.qianwen.com/api/v1/share/info?pr=qwen&fr=mac';

function parseArgs(argv) {
  const args = {
    cdp: DEFAULT_CDP_URL,
    output: '',
    url: '',
    timeout: 15000,
  };
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--cdp') args.cdp = argv[++i];
    else if (arg === '--output') args.output = argv[++i];
    else if (arg === '--url') args.url = argv[++i];
    else if (arg === '--timeout') args.timeout = Number(argv[++i]);
    else if (arg === '--help' || arg === '-h') {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return args;
}

function printHelp() {
  console.log(`Usage: node extract-sources.js [options]

Extract reference sources from an open Qianwen share page via CDP.

This script calls the share/info API from the page context to obtain the real
source URLs (the DOM only contains platform names + domains, not article URLs).

Options:
  --cdp <url>       CDP endpoint. Default: ${DEFAULT_CDP_URL}
  --url <url>       Qianwen share URL (required). Used to locate the right tab
                   and extract the share_id.
  --timeout <ms>    Wait time for page readiness. Default: 15000
  --output <file>   Save JSON output to a file
  --help            Show this help

Example:
  node extract-sources.js --url "https://www.qianwen.com/share/chat/xxxx?biz_id=ai_qwen" --output sources.json
`);
}

function pickQianwenPage(contexts, shareUrl) {
  const pages = contexts.flatMap(context => context.pages());
  const shareId = extractShareId(shareUrl);
  if (shareId) {
    const byId = pages.find(page => page.url().includes(shareId));
    if (byId) return byId;
  }
  const qianwenPage = pages.find(page => /www\.qianwen\.com\/share\/chat\//.test(page.url()));
  if (qianwenPage) return qianwenPage;
  return pages.find(page => /qianwen\.com/.test(page.url())) || pages[0];
}

function extractShareId(url) {
  const match = String(url || '').match(/\/share\/chat\/([A-Za-z0-9]+)/);
  return match ? match[1] : '';
}

/**
 * Call the share/info API from the page context and extract all sources.
 * The API returns the full conversation data including real source URLs.
 */
async function extractQianwenSourcesViaApi(page, shareId) {
  return page.evaluate(async (params) => {
    const { apiUrl, sid } = params;
    const resp = await fetch(apiUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ share_id: sid, biz_id: 'ai_qwen' }),
    });
    if (!resp.ok) {
      return {
        ok: false,
        reason: 'api-request-failed',
        status: resp.status,
        statusText: resp.statusText,
      };
    }
    const json = await resp.json();

    if (!json || !json.data || !json.data.session) {
      return {
        ok: false,
        reason: 'unexpected-api-structure',
        topKeys: json ? Object.keys(json) : [],
      };
    }

    // Walk through all record_list entries and response_messages to find sources.
    // 支持两种 API 响应格式（不同账号返回结构不同）：
    //   格式 A（旧账号）：meta_data.sources[].content.list[]
    //   格式 B（新账号）：meta_data.multi_load[].content.docs[]
    const recordList = json.data.session.record_list || [];
    let sourcesList = null;
    let sourcesPath = '';
    let sourceFormat = '';

    for (let r = 0; r < recordList.length && !sourcesList; r += 1) {
      const record = recordList[r];
      const messages = record.response_messages || [];
      for (let m = 0; m < messages.length && !sourcesList; m += 1) {
        const msg = messages[m];
        const metaData = msg.meta_data;
        if (!metaData) continue;

        // 格式 A：meta_data.sources[].content.list[]
        if (Array.isArray(metaData.sources)) {
          for (let s = 0; s < metaData.sources.length && !sourcesList; s += 1) {
            const source = metaData.sources[s];
            if (source && source.content && Array.isArray(source.content.list) && source.content.list.length > 0) {
              sourcesList = source.content.list;
              sourcesPath = `data.session.record_list[${r}].response_messages[${m}].meta_data.sources[${s}].content.list`;
              sourceFormat = 'sources_content_list';
            }
          }
        }

        // 格式 B：meta_data.multi_load[].content.docs[]
        if (!sourcesList && Array.isArray(metaData.multi_load)) {
          for (let ml = 0; ml < metaData.multi_load.length && !sourcesList; ml += 1) {
            const loadItem = metaData.multi_load[ml];
            if (loadItem && loadItem.content && Array.isArray(loadItem.content.docs) && loadItem.content.docs.length > 0) {
              sourcesList = loadItem.content.docs;
              sourcesPath = `data.session.record_list[${r}].response_messages[${m}].meta_data.multi_load[${ml}].content.docs`;
              sourceFormat = 'multi_load_content_docs';
            }
          }
        }
      }
    }

    if (!sourcesList) {
      return {
        ok: false,
        reason: 'sources-not-found-in-api-response',
        recordCount: recordList.length,
      };
    }

    // 从标题文本中提取平台名（格式 B 的 docs item 没有 name/platform 字段）
    function cleanText(text) {
      return String(text || '').replace(/\s+/g, ' ').trim();
    }

    function sourceTextLines(source) {
      const parts = [source.title || '', source.summary || '', source.name || ''].filter(Boolean);
      return parts.join('\n').split(/\r?\n/).map(cleanText).filter(Boolean);
    }

    function extractSourcePlatformFromText(source) {
      const lines = sourceTextLines(source);
      for (let i = 0; i < lines.length; i += 1) {
        if (/^\d+$/.test(lines[i]) && i > 0) return lines[i - 1];
      }
      const title = cleanText(source.title || '');
      // 支持常见的标题-平台分隔符：空格、-、_、—、–、|、｜
      const dashMatch = title.match(/[\s\-_—–|｜]+([^\s\-_—–|｜]{2,12})$/u);
      if (dashMatch && !/[。？！；，,]/u.test(dashMatch[1])) return dashMatch[1];
      return '';
    }

    // 域名关键词 → 中文平台名映射表（兜底时使用，宽松匹配：host 包含 key 即命中）
    const DOMAIN_PLATFORM_MAP = {
      // 新闻媒体
      'bjnews': '新京报',
      'qianlong': '千龙网·中国首都网',
      'cnpiw': '中国报业网',
      'sina': '新浪',
      'sohu': '搜狐',
      '163.com': '网易',
      'ifeng': '凤凰网',
      'thepaper': '澎湃新闻',
      'caixin': '财新',
      'people.com.cn': '人民网',
      'xinhuanet': '新华网',
      'news.cn': '新华网',
      'chinanews': '中国新闻网',
      'china.com': '中国网',
      'china.com.cn': '中国网',
      'myzg.china.com.cn': '中国网母婴',
      'huanqiu': '环球网',
      'cctv': '央视网',
      'toutiao': '今日头条',
      'yidianzixun': '一点资讯',
      '36kr': '36氪',
      'xnnews': '咸宁新闻网',
      // 搜索引擎
      'baidu': '百度',
      'so.com': '360搜索',
      'bing': '必应',
      'sm.cn': '神马搜索',
      'sogou': '搜狗',
      // 社交/社区
      'weibo': '微博',
      'zhihu': '知乎',
      'douban': '豆瓣',
      'xiaohongshu': '小红书',
      'xhs.link': '小红书',
      'jianshu': '简书',
      // 视频
      'bilibili': '哔哩哔哩',
      'douyin': '抖音',
      'iqiyi': '爱奇艺',
      'youku': '优酷',
      // 电商
      'taobao': '淘宝',
      'tmall': '天猫',
      'jd.com': '京东',
      'pinduoduo': '拼多多',
      // 母婴/健康
      'nestlebaby': '雀巢母婴官网',
      'babytree': '宝宝树',
      'mama.cn': '妈妈网',
      '39.net': '39健康网',
      'dxy.cn': '丁香园',
      'smzdm': '什么值得买',
      // 科技/开发
      'csdn': 'CSDN',
      'juejin': '掘金',
      'segmentfault': '思否',
      // 百科
      'wikipedia': '维基百科',
      // 其他常见
      'qq.com': '腾讯网',
      'tencent': '腾讯',
      'weixin': '微信',
      'aliyun': '阿里云',
      'alibaba': '阿里巴巴',
      'amap': '高德地图',
      'dianping': '大众点评',
      'meituan': '美团',
      'eleme': '饿了么',
      'ctrip': '携程',
      'qunar': '去哪儿',
      'tianyancha': '天眼查',
      'qichacha': '企查查',
    };
    // 按 key 长度降序排列，避免短关键词误覆盖长关键词（如 china.com.cn 优先于 china）
    const DOMAIN_PLATFORM_KEYS = Object.keys(DOMAIN_PLATFORM_MAP).sort((a, b) => b.length - a.length);

    // 从 URL 提取域名，并映射为中文平台名（兜底，宽松匹配）
    function domainFromUrl(url) {
      try {
        const u = new URL(url);
        const host = u.hostname.replace(/^www\./, '');
        // 宽松匹配：host 包含关键词即命中
        for (const key of DOMAIN_PLATFORM_KEYS) {
          if (host.includes(key)) return DOMAIN_PLATFORM_MAP[key];
        }
        // 最终兜底：取域名的名字部分（去掉 com/cn/net 等后缀）
        const TLDS = new Set(['com', 'cn', 'net', 'org', 'gov', 'edu', 'info', 'biz', 'xyz', 'top', 'io', 'cc']);
        const parts = host.split('.').filter(p => !TLDS.has(p.toLowerCase()));
        return parts[parts.length - 1] || host;
      } catch {
        return '';
      }
    }

    // Map each source item to a clean object with the real URL
    // 两种格式的字段统一映射，缺失字段用默认值兜底
    // platform 链路：name → platform → 标题提取 → URL 域名兜底
    const sources = sourcesList.map((item, index) => ({
      index: index + 1,
      title: String(item.title || ''),
      url: String(item.url || item.normalized_url || ''),
      normalizedUrl: String(item.normalized_url || ''),
      rawUrl: String(item.raw_url || ''),
      platform: String(item.name || item.platform || extractSourcePlatformFromText(item) || domainFromUrl(item.url || item.normalized_url || '') || ''),
      summary: String(item.summary || '').slice(0, 2000),
      publishTime: String(item.publish_time || ''),
      type: String(item.type || ''),
      subType: String(item.sub_type || ''),
      reliable: String(item.reliable || ''),
      authority: String(item.authority || ''),
      tagName: String(item.tag_name || ''),
      icon: String(item.icon || ''),
      isWenkuDoc: Boolean(item.is_wenku_doc),
      shouldRenderRefer: String(item.should_render_refer || ''),
      academicId: String(item.academic_id || ''),
    }));

    return {
      ok: true,
      url: location.href,
      title: document.title,
      apiPath: sourcesPath,
      sourceFormat,
      shareId: sid,
      count: sources.length,
      sources,
    };
  }, { apiUrl: SHARE_INFO_API, sid: shareId });
}

async function waitForQianwenReady(page, timeout = 15000) {
  await page.waitForLoadState('domcontentloaded', { timeout }).catch(() => {});
}

/**
 * Extract sources from a Qianwen share page.
 * @param {string} cdpUrl - CDP endpoint URL
 * @param {string} shareUrl - Qianwen share URL
 * @param {number} timeout - Page readiness timeout in ms
 * @returns {Promise<object>} Extraction result with sources array
 */
async function extractSources(cdpUrl, shareUrl, timeout = 15000) {
  const browser = await chromium.connectOverCDP(cdpUrl);
  try {
    const page = pickQianwenPage(browser.contexts(), shareUrl);
    if (!page) throw new Error('No browser page found from CDP endpoint.');
    await page.bringToFront();
    await waitForQianwenReady(page, timeout);

    const shareId = extractShareId(page.url()) || extractShareId(shareUrl);
    if (!shareId) throw new Error('Could not extract share_id from page URL or --url argument.');

    return await extractQianwenSourcesViaApi(page, shareId);
  } finally {
    await browser.close().catch(() => {});
  }
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.url) throw new Error('--url is required (Qianwen share URL)');

  const result = await extractSources(args.cdp, args.url, args.timeout);
  const json = JSON.stringify(result, null, 2);
  if (args.output) fs.writeFileSync(args.output, `${json}\n`, 'utf8');
  console.log(json);
}

if (require.main === module) {
  main().catch(error => {
    console.error(`[extract-sources] failed: ${error.stack || error.message}`);
    process.exit(1);
  });
}

module.exports = { extractSources, extractQianwenSourcesViaApi, pickQianwenPage, waitForQianwenReady, extractShareId };
