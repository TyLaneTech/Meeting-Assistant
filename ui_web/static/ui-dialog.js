/*
 * Shared UI feedback API. All options are optional.
 * uiToast(options) returns { dismiss() }.
 * uiConfirm(options) resolves to true or false.
 * uiAlert(options) resolves when dismissed.
 * uiPrompt(options) resolves to the entered string, or null when cancelled.
 */
(function () {
  'use strict';

  const modalQueue = [];
  let modalOpen = false;
  let toastSequence = 0;
  const icons = {
    info: 'fa-circle-info', success: 'fa-circle-check',
    warn: 'fa-triangle-exclamation', error: 'fa-circle-exclamation',
  };

  function text(value, fallback) {
    return value == null ? fallback : String(value);
  }

  function uiToast(options) {
    const o = options || {};
    const kind = ['info', 'success', 'warn', 'error'].includes(o.kind) ? o.kind : 'info';
    let container = document.querySelector('.ui-toast-container');
    if (!container) {
      container = document.createElement('div');
      container.className = 'ui-toast-container';
      container.setAttribute('aria-live', 'polite');
      container.setAttribute('aria-relevant', 'additions removals');
      document.body.appendChild(container);
    }
    if (o.id) [...container.children].find(item => item.dataset.toastId === String(o.id))?.remove();
    while (container.children.length >= 4) container.firstElementChild?.remove();

    const toast = document.createElement('div');
    toast.className = `ui-toast ui-toast-${kind}`;
    toast.dataset.toastId = text(o.id, `ui-toast-${++toastSequence}`);
    toast.setAttribute('role', 'status');
    const icon = document.createElement('i');
    icon.className = `fa-solid ${icons[kind]} ui-toast-icon`;
    icon.setAttribute('aria-hidden', 'true');
    const message = document.createElement('div');
    message.className = 'ui-toast-message';
    message.textContent = text(o.message, '');
    toast.append(icon, message);
    if (o.action && typeof o.action.onClick === 'function') {
      const action = document.createElement('button');
      action.type = 'button';
      action.className = 'ui-toast-action';
      action.textContent = text(o.action.label, 'Action');
      action.addEventListener('click', () => { o.action.onClick(); dismiss(); });
      toast.appendChild(action);
    }
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'ui-toast-close';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.innerHTML = '<i class="fa-solid fa-xmark" aria-hidden="true"></i>';
    toast.appendChild(close);
    container.appendChild(toast);

    let timer = null;
    let remaining = Number.isFinite(Number(o.duration)) ? Math.max(0, Number(o.duration)) : 4500;
    let started = 0;
    let dismissed = false;
    function dismiss() {
      if (dismissed) return;
      dismissed = true;
      clearTimeout(timer);
      toast.classList.add('ui-toast-leaving');
      setTimeout(() => { toast.remove(); if (!container.children.length) container.remove(); }, 180);
    }
    function startTimer() {
      if (!remaining) return;
      started = Date.now();
      timer = setTimeout(dismiss, remaining);
    }
    toast.addEventListener('mouseenter', () => {
      if (!timer) return;
      clearTimeout(timer);
      timer = null;
      remaining = Math.max(0, remaining - (Date.now() - started));
    });
    toast.addEventListener('mouseleave', startTimer);
    close.addEventListener('click', dismiss);
    startTimer();
    return { dismiss };
  }

  function enqueue(type, options) {
    return new Promise(resolve => {
      modalQueue.push({ type, options: options || {}, resolve });
      showNextModal();
    });
  }

  function showNextModal() {
    if (modalOpen || !modalQueue.length) return;
    modalOpen = true;
    const job = modalQueue.shift();
    const o = job.options;
    const previousFocus = document.activeElement;
    const overlay = document.createElement('div');
    overlay.className = 'ui-dialog-overlay';
    const dialog = document.createElement('div');
    const alertKind = job.type === 'alert' && ['info', 'success', 'warn', 'error'].includes(o.kind) ? ` ui-dialog-${o.kind}` : '';
    dialog.className = `ui-dialog${o.danger ? ' ui-dialog-danger' : ''}${alertKind}`;
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const titleId = `ui-dialog-title-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    dialog.setAttribute('aria-labelledby', titleId);

    const title = document.createElement('h2');
    title.id = titleId;
    title.className = 'ui-dialog-title';
    const defaultTitle = job.type === 'alert' ? 'Notice' : job.type === 'prompt' ? 'Enter a value' : 'Confirm';
    title.textContent = text(o.title, defaultTitle);
    dialog.appendChild(title);
    if (o.message != null && String(o.message)) {
      const message = document.createElement('p');
      message.className = 'ui-dialog-message';
      message.textContent = String(o.message);
      dialog.appendChild(message);
    }
    if (Array.isArray(o.details) && o.details.length) {
      const list = document.createElement('ul');
      list.className = 'ui-dialog-details';
      o.details.forEach(value => {
        const item = document.createElement('li');
        item.textContent = String(value);
        list.appendChild(item);
      });
      dialog.appendChild(list);
    }

    let input = null;
    let error = null;
    if (job.type === 'prompt') {
      input = document.createElement('input');
      input.type = 'text';
      input.className = 'ui-dialog-input';
      input.placeholder = text(o.placeholder, '');
      input.value = text(o.value, '');
      input.setAttribute('autocomplete', 'off');
      error = document.createElement('div');
      error.className = 'ui-dialog-error';
      error.setAttribute('aria-live', 'polite');
      dialog.append(input, error);
    }

    const buttons = document.createElement('div');
    buttons.className = 'ui-dialog-buttons';
    let cancel = null;
    if (job.type !== 'alert') {
      cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'ui-dialog-button ui-dialog-cancel';
      cancel.textContent = text(o.cancelLabel, 'Cancel');
      buttons.appendChild(cancel);
    }
    const primary = document.createElement('button');
    primary.type = 'button';
    primary.className = `ui-dialog-button ui-dialog-primary${o.danger ? ' ui-dialog-button-danger' : ''}`;
    primary.textContent = text(o.confirmLabel, job.type === 'alert' ? 'OK' : 'Confirm');
    buttons.appendChild(primary);
    dialog.appendChild(buttons);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    let finished = false;
    function finish(value) {
      if (finished) return;
      finished = true;
      document.removeEventListener('keydown', onKey, true);
      overlay.classList.add('ui-dialog-overlay-leaving');
      setTimeout(() => {
        overlay.remove();
        modalOpen = false;
        if (previousFocus && previousFocus.isConnected && typeof previousFocus.focus === 'function') previousFocus.focus();
        job.resolve(value);
        showNextModal();
      }, 150);
    }
    function submit() {
      if (job.type === 'prompt') {
        const value = input.value;
        const problem = typeof o.validate === 'function' ? o.validate(value) : null;
        if (problem) {
          error.textContent = String(problem);
          input.setAttribute('aria-invalid', 'true');
          input.focus();
          return;
        }
        finish(value);
      } else finish(job.type === 'confirm' ? true : undefined);
    }
    function cancelModal() { finish(job.type === 'confirm' ? false : null); }
    function onKey(event) {
      // While a dialog is open the page underneath is inert: no page-level
      // shortcut (space to toggle playback, etc.) may fire from these keys.
      event.stopImmediatePropagation();
      if (event.key === 'Escape') { event.preventDefault(); cancelModal(); return; }
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        // Enter on a focused Cancel means cancel, never the primary action.
        if (cancel && document.activeElement === cancel) { cancelModal(); return; }
        submit();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled])')];
      if (!focusable.length) { event.preventDefault(); return; }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    primary.addEventListener('click', submit);
    cancel?.addEventListener('click', cancelModal);
    overlay.addEventListener('click', event => {
      if (event.target === overlay && !(job.type === 'confirm' && o.danger)) cancelModal();
    });
    input?.addEventListener('input', () => { error.textContent = ''; input.removeAttribute('aria-invalid'); });
    document.addEventListener('keydown', onKey, true);
    requestAnimationFrame(() => { overlay.classList.add('ui-dialog-overlay-visible'); (input || primary).focus(); });
  }

  window.uiToast = uiToast;
  window.uiConfirm = options => enqueue('confirm', options);
  window.uiAlert = options => enqueue('alert', options).then(() => undefined);
  window.uiPrompt = options => enqueue('prompt', options);
}());
