const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const sourcePath = path.join(__dirname, '..', 'sites', 'prana_browser.py');
const source = fs.readFileSync(sourcePath, 'utf8');
const match = source.match(/PAGE_STATE\s*=\s*r"""([\s\S]*?)"""/);
assert.ok(match, 'The transport must expose its actual PAGE_STATE script');

// Relevant opening tag captured from the normal prAna product page.
const productMarkup = "<div class='product-detail product-wrapper' data-pid='2162341-020-RX-M' data-master-pid='2162341'>";

function elementFromMarkup(markup) {
  const attributes = new Map();
  for (const attribute of markup.matchAll(/([\w-]+)=['"]([^'"]*)['"]/g)) {
    attributes.set(attribute[1], attribute[2]);
  }
  return {
    getAttribute: name => attributes.get(name) ?? null,
    matches: selector => {
      const classes = (attributes.get('class') || '').split(/\s+/);
      const requiredClasses = Array.from(selector.matchAll(/\.([\w-]+)/g), item => item[1]);
      const requiredAttributes = Array.from(selector.matchAll(/\[([\w-]+)\]/g), item => item[1]);
      return requiredClasses.every(name => classes.includes(name))
        && requiredAttributes.every(name => attributes.has(name));
    },
  };
}

function pageState({ markup = productMarkup, text = 'Cuyamaca Pant', captcha = null } = {}) {
  const product = markup ? elementFromMarkup(markup) : null;
  const document = {
    body: { innerText: text },
    querySelector: selector => product && selector.split(',').some(part => product.matches(part)) ? product : null,
    querySelectorAll: () => captcha ? [captcha] : [],
  };
  return vm.runInNewContext(`(${match[1]})()`, {
    document,
    getComputedStyle: element => element.style,
  });
}

let result = pageState();
assert.equal(result.challenge, false);
assert.equal(result.ready, true);
assert.equal(result.pid, '2162341-020-RX-M');
assert.equal(result.master_pid, '2162341', 'Expose the master identity separately from the selected variant');

result = pageState({ markup: "<div class='product-detail' data-pid='2162341-020-RX-M'>" });
assert.equal(result.challenge, false);
assert.equal(result.ready, true);
assert.equal(result.pid, '2162341-020-RX-M', 'Retain the variant PID when no master attribute exists');
assert.equal(result.master_pid, '');

const visibleCaptcha = {
  getBoundingClientRect: () => ({ width: 320, height: 100 }),
  style: { visibility: 'visible', display: 'block' },
};
result = pageState({ captcha: visibleCaptcha });
assert.equal(result.challenge, true);
assert.equal(result.ready, false, 'A real visible captcha takes precedence over product markup');

result = pageState({ markup: '', text: 'Please press & hold to verify you are human' });
assert.equal(result.challenge, true);
assert.equal(result.ready, false);
assert.equal(result.pid, '');

const hiddenCaptcha = {
  getBoundingClientRect: () => ({ width: 0, height: 0 }),
  style: { visibility: 'hidden', display: 'none' },
};
result = pageState({ captcha: hiddenCaptcha });
assert.equal(result.challenge, false);
assert.equal(result.ready, true);

result = pageState({ markup: '', text: 'Loading product...' });
assert.equal(result.challenge, false);
assert.equal(result.ready, false, 'A page with no product detail is not ready');
assert.equal(result.pid, '');

console.log('PASS: actual prAna page script recognizes master/variant PIDs and distinguishes product, captcha and loading states');
