const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element() {
  const classes = new Set();
  return {
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
    },
    attrs: {},
    children: [],
    dataset: {},
    listeners: {},
    setAttribute(name, value) { this.attrs[name] = value; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.children.push(child); },
  };
}

const body = element();
const tasks = new Map();
let nextTimer = 1;
const context = {
  window: {VisionEdgeI18n: {getLanguage: () => 'en'}},
  document: {body, createElement: element},
  setTimeout(fn, delay) { const id = nextTimer++; tasks.set(id, {fn, delay}); return id; },
  clearTimeout(id) { tasks.delete(id); },
};
vm.runInNewContext(fs.readFileSync('static/notice.js', 'utf8'), context);
const notice = body.children[0];
const [icon, message, close] = notice.children;
const api = context.window.VisionEdgeNotice;
assert.equal(close.attrs['aria-label'], 'Close notification');

api.show('樣板已儲存', 'ok');
assert.equal(notice.dataset.kind, 'success');
assert.equal(message.textContent, '樣板已儲存');
assert.equal(notice.attrs.role, 'status');
assert.equal(notice.classList.contains('is-visible'), true);
assert.equal([...tasks.values()][0].delay, 8000);
notice.listeners.mouseenter();
assert.equal(tasks.size, 0, 'hovering pauses automatic dismissal');
notice.listeners.mouseleave();
assert.equal([...tasks.values()][0].delay, 8000);

api.show('儲存失敗', 'err');
assert.equal(notice.dataset.kind, 'error');
assert.equal(notice.attrs.role, 'alert');
assert.equal(message.textContent, '儲存失敗');
assert.equal(tasks.size, 0, 'errors must remain visible until dismissed');
close.listeners.click();
assert.equal(notice.classList.contains('is-visible'), false);

api.show('<script>不是 HTML</script>', 'warn');
assert.equal(notice.dataset.kind, 'warning');
assert.equal(message.textContent, '<script>不是 HTML</script>');
assert.equal(tasks.size, 0, 'warnings must remain visible until dismissed');

for (const page of ['edge_dashboard.html', 'flow_studio.html', 'template_workspace.html']) {
  const html = fs.readFileSync(`static/${page}`, 'utf8');
  assert.match(html, /notice\.css\?v=1/);
  assert.match(html, /notice\.js\?v=2/);
}
console.log('NOTICE UI PASS: visible success, persistent errors, dismiss, safe text, all pages wired');
