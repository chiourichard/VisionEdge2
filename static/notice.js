(function () {
  let timer = null;
  let notice;
  let icon;
  let text;
  let close;
  let autoHide = false;
  let hovering = false;
  let focused = false;

  function closeLabel() {
    const lang = window.VisionEdgeI18n?.getLanguage?.() || 'zh-Hant';
    return ({'zh-Hant': '關閉通知', 'zh-Hans': '关闭通知', en: 'Close notification', es: 'Cerrar aviso'})[lang] || '關閉通知';
  }

  function scheduleHide() {
    clearTimeout(timer);
    timer = null;
    if (autoHide && !hovering && !focused) timer = setTimeout(hide, 8000);
  }

  function ensure() {
    if (notice) return;
    notice = document.createElement('div');
    notice.className = 've-notice';
    notice.setAttribute('role', 'status');
    notice.setAttribute('aria-live', 'polite');
    notice.setAttribute('aria-atomic', 'true');
    icon = document.createElement('span');
    icon.className = 've-notice-icon';
    icon.setAttribute('aria-hidden', 'true');
    text = document.createElement('span');
    text.className = 've-notice-text';
    close = document.createElement('button');
    close.className = 've-notice-close';
    close.type = 'button';
    close.textContent = '×';
    close.setAttribute('aria-label', closeLabel());
    close.addEventListener('click', hide);
    notice.append(icon, text, close);
    notice.addEventListener('mouseenter', () => { hovering = true; clearTimeout(timer); timer = null; });
    notice.addEventListener('mouseleave', () => { hovering = false; scheduleHide(); });
    notice.addEventListener('focusin', () => { focused = true; clearTimeout(timer); timer = null; });
    notice.addEventListener('focusout', () => { focused = false; scheduleHide(); });
    document.body.appendChild(notice);
  }

  function hide() {
    clearTimeout(timer);
    timer = null;
    autoHide = false;
    if (notice) notice.classList.remove('is-visible');
    if (document.activeElement === close) close.blur();
  }

  function show(message, kind = 'info') {
    ensure();
    const value = String(message || '').trim();
    if (!value) return hide();
    clearTimeout(timer);
    const type = ({ok: 'success', err: 'error', warn: 'warning'})[kind] || kind;
    const normalized = ['success', 'error', 'warning'].includes(type) ? type : 'info';
    close.setAttribute('aria-label', closeLabel());
    notice.dataset.kind = normalized;
    notice.setAttribute('role', normalized === 'error' ? 'alert' : 'status');
    notice.setAttribute('aria-live', normalized === 'error' ? 'assertive' : 'polite');
    icon.textContent = ({success: '✓', error: '!', warning: '!', info: 'i'})[normalized];
    text.textContent = value;
    notice.classList.add('is-visible');
    autoHide = normalized === 'success' || normalized === 'info';
    scheduleHide();
  }

  window.VisionEdgeNotice = {show, hide};
  if (document.body) ensure();
  else document.addEventListener('DOMContentLoaded', ensure, {once: true});
})();
