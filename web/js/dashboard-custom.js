/* User-authored cards are static documents, never part of Homestead's DOM.
   Rebuild a small HTML vocabulary, then isolate it with an opaque-origin sandbox
   and a network-denying CSP. No raw markup is inserted into a live document. */
const DashboardCustom = (() => {
  const limit=16384;
  const tags=new Set('p div span h1 h2 h3 h4 h5 h6 br hr strong b em i u s small sub sup ul ol li blockquote pre code table thead tbody tfoot tr th td caption dl dt dd'.split(' '));
  const discard=new Set('script style template svg math iframe object embed form input button select textarea video audio picture img source link meta base noscript'.split(' '));
  const properties=new Set('color background-color font-size font-weight font-style text-align text-decoration line-height letter-spacing margin margin-top margin-bottom margin-left margin-right padding padding-top padding-bottom padding-left padding-right border border-color border-width border-style border-radius'.split(' '));
  const escape=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function sanitize(source) {
    const template=document.createElement('template');template.innerHTML=String(source).slice(0,limit);
    let count=0;
    function visit(node,depth=0){
      if(++count>1500 || depth>40)return '';
      if(node.nodeType===3)return escape(node.nodeValue);
      if(node.nodeType!==1 || node.namespaceURI!=='http://www.w3.org/1999/xhtml')return '';
      const tag=node.localName;if(discard.has(tag))return '';
      const children=()=>Array.from(node.childNodes).map(child=>visit(child,depth+1)).join('');
      if(!tags.has(tag))return children();
      let style='';
      for(const property of node.style){const value=node.style.getPropertyValue(property);
        // CSSOM serializes hex colours as rgb(). Permit only numeric colour
        // functions on colour properties; no URLs, variables or escapes.
        const colorFunction=['color','background-color','border-color'].includes(property) && /^(?:rgba?|hsla?)\([\d.%+,\s/-]+\)$/.test(value);
        if(properties.has(property) && (/^[#\w\s.,%+-]+$/.test(value) || colorFunction) && value.length<=80)style+=`${property}:${value};`;
      }
      const attrs=style?` style="${escape(style)}"`:'';
      return `<${tag}${attrs}>`+(['br','hr'].includes(tag)?'':children()+`</${tag}>`);
    }
    return Array.from(template.content.childNodes).map(node=>visit(node)).join('');
  }
  function documentHTML(source,light=false){
    const policy="default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; font-src 'none'; media-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
    return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${policy}"><meta name="referrer" content="no-referrer"><style>:root{color-scheme:${light?'light':'dark'}}*{box-sizing:border-box}body{margin:0;padding:2px;font:13px/1.5 system-ui,sans-serif;color:${light?'#202026':'#e9e9ef'};overflow-wrap:anywhere}h1,h2,h3,h4,p,ul,ol,pre,blockquote,table{margin:0 0 12px}h1{font-size:24px}h2{font-size:20px}h3{font-size:16px}pre{white-space:pre-wrap}table{width:100%;border-collapse:collapse}th,td{padding:6px;border:1px solid ${light?'#ddd':'#444'}}blockquote{border-left:3px solid #888;padding-left:12px}body{scrollbar-width:thin}</style></head><body>${sanitize(source)}</body></html>`;
  }
  function render(item){
    const title=item.title || 'Custom text / HTML',content=item.content || '';
    const body=!content?'<div class="empty small">Add your content in Widget settings.</div>':item.format==='html'?
      `<iframe class="dashboard-custom-frame" title="${escape(title)}" sandbox="" referrerpolicy="no-referrer" srcdoc="${escape(documentHTML(content,document.documentElement.dataset.theme==='light'))}" style="height:${Math.max(160,(item.height || 360)-80)}px"></iframe>`:
      `<div class="dashboard-custom-text dashboard-resource-scroll">${escape(content)}</div>`;
    return `<div class="card flat dashboard-custom">${UI.moduleHeader(escape(title),'','')}<div class="dashboard-custom-body">${body}</div></div>`;
  }
  function fields(item){return `<label class="dashboard-custom-field">Title<input aria-label="Custom widget title" maxlength="80" value="${escape(item.title || '')}" onchange="Dashboard.custom('title',this.value)"></label>
    <label class="dashboard-custom-field">Format<select aria-label="Custom widget format" onchange="Dashboard.custom('format',this.value)"><option value="text" ${item.format!=='html'?'selected':''}>Plain text</option><option value="html" ${item.format==='html'?'selected':''}>Static HTML</option></select></label>
    <label class="dashboard-custom-field">Content<textarea aria-label="Custom widget content" rows="8" maxlength="${limit}" spellcheck="false" onchange="Dashboard.custom('content',this.value)">${escape(item.content || '')}</textarea></label>
    <div class="dashboard-custom-field ui-help">Text and formatting only. Scripts, links, forms, embeds and external resources are blocked.</div>
    <button type="button" class="btn sm" onclick="Dashboard.select(${jsq(item.id)})">Update preview</button>`;}
  return {limit,sanitize,documentHTML,render,fields};
})();
if(typeof window!=='undefined')window.DashboardCustom=DashboardCustom;
