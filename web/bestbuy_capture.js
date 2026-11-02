async () => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const findBtn = re => [...document.querySelectorAll('button,a')]
    .find(b => re.test((b.textContent || '').trim()));
  const survey = findBtn(/^no, thanks$/i);
  if (survey) survey.click();
  for (let i = 0; i < 20 && document.querySelector('[data-testid="fulfillment-selector-loading"]'); i++) await sleep(300);
  // Sau navigation "commit", chỉ chờ phần nội dung cần lấy hydrate,
  // không chờ quảng cáo/analytics. Thoát ngay khi nút thông số xuất hiện.
  const contentReady = () => !!findBtn(/see all specifications/i);
  if (!contentReady()) await new Promise(resolve => {
    const observer = new MutationObserver(() => { if (contentReady()) finish(); });
    const finish = () => { observer.disconnect(); clearTimeout(timer); resolve(); };
    const timer = setTimeout(finish, 6000);
    observer.observe(document.body, {childList:true, subtree:true});
    if (contentReady()) finish();
  });
  let specs = '';
  const sb = findBtn(/see all specifications/i);
  if (sb) {
    sb.scrollIntoView({block: 'center'});
    await sleep(400);
    sb.click();
    for (let i = 0; i < 20 && !specs; i++) {
      await sleep(300);
      specs = [...document.querySelectorAll('[role="dialog"]')].map(x => x.innerText || '')
        .find(t => /^Specifications\b/i.test(t.trim())) || '';
      if (!specs) {
        const t = document.body.innerText, a = t.indexOf('Key Specs'), b = t.indexOf('Report missing', a);
        if (a >= 0 && b > a) specs = t.slice(a, b);
      }
    }
    const close = [...document.querySelectorAll('[role="dialog"] button')]
      .find(b => /close/i.test((b.getAttribute('aria-label') || '') + (b.title || '')));
    if (close) close.click();
  }
  const flatten = x => Array.isArray(x) ? x.flatMap(flatten) :
    x && typeof x === 'object' ? [x, ...flatten(x['@graph'] || [])] : [];
  const ld = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .flatMap(s => {try { return flatten(JSON.parse(s.textContent)); } catch (_) { return []; }});
  const prod = ld.find(x => [x['@type']].flat().includes('Product')) || null;
  if (!prod) return null;
  const gal = [...new Set([...document.images].filter(i => /bbystatic/.test(i.src) && /maxHeight=1920/.test(i.src))
    .map(i => i.src.split(';')[0]))];
  const buy = [...document.querySelectorAll('button')].map(b => ({t:(b.textContent || '').trim(), d:!!b.disabled}))
    .filter(b => /add to cart|sold out|unavailable|coming soon|check stores/i.test(b.t));
  const fh = [...document.querySelectorAll('h3')].find(x => x.textContent.trim() === 'Features');
  const feat = fh ? [...fh.parentElement.querySelectorAll('*')].filter(e => !e.children.length)
    .map(e => (e.textContent || '').trim()).filter(t => t && t !== 'Features' && !/^see all features$/i.test(t)) : [];
  const ship = [...new Set([...document.querySelectorAll('button,label,[role="radio"],div,span')]
    .map(e => e.innerText || '')
    .filter(t => t.length < 140 && /^shipping\b/i.test(t.trim()))
    .map(t => t.replace(/\s+/g, ' ').trim()))];
  return {v:1, url:location.href.split('?')[0].split('#')[0], saved_at:new Date().toISOString(), prod, gal, buy, feat,
    more:!!findBtn(/^see all features$/i), ship, specs_text:specs,
    h1:document.querySelector('h1')?.textContent || '',
    crumbs:[...document.querySelectorAll('nav[aria-label*="readcrumb"] a')].map(a => a.textContent.trim())};
}
