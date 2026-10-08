/** Bounded, dependency-free reading derivative. Raw HTML is always inert text. */
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';

const escape = value => String(value).replace(/[&<>"']/g, char =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function inline(raw) {
  const pattern = /\x60([^\x60]+)\x60|\*\*([^*]+)\*\*|\[([^\]]+)\]\((?:<([^>]+)>|([^\s)]+))\)/g;
  let html='', start=0;
  for (const match of raw.matchAll(pattern)) {
    html += escape(raw.slice(start,match.index));
    if(match[1]) html += '<code>'+escape(match[1])+'</code>';
    else if(match[2]) html += '<strong>'+escape(match[2])+'</strong>';
    else {
      const href=match[4]??match[5];
      const safe=!/[\x00-\x20]/.test(href) && !href.startsWith('//') &&
        (!/^[a-z][a-z0-9+.-]*:/i.test(href) || /^https?:/i.test(href));
      html += safe?'<a href="'+escape(href)+'" rel="noreferrer">'+escape(match[3])+'</a>':escape(match[0]);
    }
    start=match.index+match[0].length;
  }
  return html+escape(raw.slice(start));
}
export function renderReading(record) {
  const lines=record.markdown.split(/\r?\n/), nav=[], body=[];
  let index=0, list=null, details=false, heading=0;
  const closeList=()=>{if(list){body.push('</'+list+'>');list=null;}};
  while(index<lines.length) {
    const line=lines[index++];
    if(line.startsWith('```')) {
      closeList(); const code=[];
      while(index<lines.length && !lines[index].startsWith('```')) code.push(lines[index++]);
      if(index<lines.length) index++;
      body.push('<pre><code>'+escape(code.join('\n'))+'</code></pre>'); continue;
    }
    const title=/^(#{1,6})\s+(.+)$/.exec(line);
    if(title) {
      closeList(); const level=title[1].length, id='s'+(++heading);
      if(level===2 && details){body.push('</details>');details=false;}
      if(level===2 && /附录|技术审计|证据索引|工程诊断|原始证据/.test(title[2])){
        body.push('<details id="'+id+'"><summary>'+inline(title[2])+'</summary>'); details=true;
      } else body.push('<h'+level+' id="'+id+'">'+inline(title[2])+'</h'+level+'>');
      if(level===2) nav.push('<a href="#'+id+'">'+escape(title[2])+'</a>');
      continue;
    }
    if(line.includes('|') && index<lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[index])){
      closeList(); const cells=value=>value.trim().replace(/^\||\|$/g,'').split('|');
      const headers=cells(line); index++;
      const rows=[];
      while(index<lines.length && lines[index].trim() && lines[index].includes('|'))
        rows.push('<tr>'+cells(lines[index++]).map(cell=>'<td>'+inline(cell.trim())+'</td>').join('')+'</tr>');
      body.push('<div class="table-scroll"><table><thead><tr>'+headers.map(cell=>'<th>'+inline(cell.trim())+'</th>').join('')+
        '</tr></thead><tbody>'+rows.join('')+'</tbody></table></div>'); continue;
    }
    const item=/^\s*(?:([-*])|\d+\.)\s+(.+)$/.exec(line);
    if(item) {
      const kind=item[1]?'ul':'ol';
      if(list!==kind){closeList();list=kind;body.push('<'+kind+'>');}
      body.push('<li>'+inline(item[2])+'</li>');continue;
    }
    closeList();
    if(!line.trim()) continue;
    if(/^---+$/.test(line.trim())) body.push('<hr>');
    else body.push('<p>'+inline(line.replace(/^>\s?/,''))+'</p>');
  }
  closeList(); if(details)body.push('</details>');
  const phase=record.phase==='premarket'?'盘前研究':'盘后复盘';
  return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'+
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src \'none\'; base-uri \'none\'">'+
    '<title>'+escape(record.trade_date+' '+phase)+'</title><style>'+
    ':root{color-scheme:light dark;--bg:#fafaf8;--fg:#202730;--line:#dce2e8;--muted:#596471;--link:#2163bb}'+
    '@media(prefers-color-scheme:dark){:root{--bg:#111820;--fg:#dce5ee;--line:#354251;--muted:#a5b3c2;--link:#86b7f8}}'+
    '*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.85 system-ui,-apple-system,"PingFang SC",sans-serif}'+
    'main{max-width:920px;margin:auto;padding:32px 28px 80px}h1{font-size:29px;line-height:1.4}h2{font-size:23px;margin-top:36px;border-bottom:1px solid var(--line);padding-bottom:10px}'+
    'h3{font-size:19px}p,li{overflow-wrap:anywhere}a{color:var(--link);text-underline-offset:3px}nav{display:flex;gap:10px 18px;flex-wrap:wrap;font-size:14px;padding:14px 0}'+
    '.meta{font-size:14px;color:var(--muted)}.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:15px}th,td{padding:10px;border:1px solid var(--line);text-align:left;vertical-align:top}'+
    'code,pre{font-size:13px}pre{overflow:auto;padding:14px;border:1px solid var(--line)}details{margin-top:24px;border:1px solid var(--line);padding:12px 16px}summary{cursor:pointer;font-weight:600}'+
    '@media(max-width:640px){main{padding:20px 16px 50px}h1{font-size:25px}h2{font-size:21px}body{font-size:16px}}'+
    '@media print{nav{display:none}main{max-width:none;padding:0}details{display:block}body{font-size:12pt}}'+
    '</style></head><body><main><div class="meta">'+escape(record.trade_date+' · '+phase+' · 截止 '+record.as_of+' · '+record.status)+
    '<br>阅读衍生版，不改变原始证据或报告状态。</div><nav aria-label="报告目录">'+nav.join('')+'</nav>'+body.join('\n')+
    '<footer class="meta">研究不是下单授权。报告ID：'+escape(record.report_id)+'</footer></main></body></html>\n';
}
async function publish(destination, text) {
  try {
    const info=await fs.lstat(destination);
    if(!info.isFile() || info.size>2*1024*1024 || await fs.readFile(destination,'utf8')!==text)
      throw new Error('reading_derivative_collision');
    return;
  } catch(error){if(error.code!=='ENOENT')throw error;}
  const temporary=path.join(path.dirname(destination),'.reading-'+crypto.randomUUID()+'.tmp');
  const handle=await fs.open(temporary,'wx',0o600);
  try{await handle.writeFile(text);await handle.sync();}finally{await handle.close();}
  try{
    try{await fs.link(temporary,destination);}
    catch(error){if(error.code!=='EEXIST')throw error;await publish(destination,text);}
  }finally{await fs.unlink(temporary);}
}
export async function ensureReadingFiles(record, canonicalPath) {
  const stem=canonicalPath.slice(0,-5)+'-reading-v1';
  const markdown_path=stem+'.md', html_path=stem+'.html';
  try {
    await publish(markdown_path,record.markdown+'\n');
    await publish(html_path,renderReading(record));
    return {reading_status:'available',markdown_path,html_path};
  } catch {
    // A failed derivative must not invalidate already published canonical truth.
    return {reading_status:'unavailable',reading_reason:'derivative_publish_requires_review'};
  }
}
