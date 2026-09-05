/*
 * Shared combobox: a filter-as-you-type text field with a keyboard-navigable
 * listbox. Built for the Speakers modal (Manage tab Voice Library picker) but
 * deliberately generic so any surface that needs "type a name or pick an
 * existing one" can reuse it.
 *
 *   const combo = uiCombobox({ mount, items, onSelect });
 *
 * Options (all optional except mount):
 *   mount        element the combobox is rendered into (its contents are replaced)
 *   items        [{ id, label, sublabel, color }]
 *   value        initial text value
 *   placeholder  input placeholder
 *   ariaLabel    accessible name for the input
 *   emptyText    shown when the filter matches nothing
 *   allowTyped   when true, a "use what I typed" row leads the list
 *   typedLabel   label for that row (default "Use typed name")
 *   onSelect(item, meta)  item.id === '__typed__' for the typed row
 *   onInput(text)         fires on every keystroke
 *
 * Returns { element, input, setItems, setValue, getValue, focus, close, destroy }.
 */
(function () {
  'use strict';

  let comboSequence = 0;

  function uiCombobox(options) {
    const o = options || {};
    const mount = o.mount;
    if (!mount) throw new Error('uiCombobox requires a mount element');

    const uid = `ui-combobox-${++comboSequence}`;
    let items = Array.isArray(o.items) ? o.items.slice() : [];
    let filtered = [];
    let activeIndex = -1;
    let open = false;

    mount.replaceChildren();
    mount.classList.add('ui-combobox');

    const field = document.createElement('div');
    field.className = 'ui-combobox-field';

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'ui-combobox-input';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', `${uid}-list`);
    if (o.placeholder) input.placeholder = String(o.placeholder);
    if (o.ariaLabel) input.setAttribute('aria-label', String(o.ariaLabel));
    if (o.value != null) input.value = String(o.value);

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'ui-combobox-toggle';
    toggle.tabIndex = -1;
    toggle.setAttribute('aria-label', 'Show suggestions');
    toggle.innerHTML = '<i class="fa-solid fa-chevron-down" aria-hidden="true"></i>';

    field.append(input, toggle);

    const list = document.createElement('div');
    list.className = 'ui-combobox-list';
    list.id = `${uid}-list`;
    list.setAttribute('role', 'listbox');
    list.hidden = true;

    mount.append(field, list);

    function typedRow() {
      const typed = input.value.trim();
      if (!o.allowTyped || !typed) return null;
      const clash = items.some(it => String(it.label || '').toLowerCase() === typed.toLowerCase());
      if (clash) return null;
      return { id: '__typed__', label: typed, sublabel: o.typedLabel || 'Use typed name', typed: true };
    }

    function computeFiltered() {
      const q = input.value.trim().toLowerCase();
      const matches = q
        ? items.filter(it => String(it.label || '').toLowerCase().includes(q)
                          || String(it.sublabel || '').toLowerCase().includes(q))
        : items.slice();
      const typed = typedRow();
      filtered = typed ? [typed, ...matches] : matches;
    }

    function renderList() {
      list.replaceChildren();
      if (!filtered.length) {
        const empty = document.createElement('div');
        empty.className = 'ui-combobox-empty';
        empty.textContent = o.emptyText || 'No matches';
        list.appendChild(empty);
        return;
      }
      filtered.forEach((item, index) => {
        const row = document.createElement('div');
        row.className = 'ui-combobox-item' + (item.typed ? ' is-typed' : '')
                      + (index === activeIndex ? ' is-active' : '');
        row.id = `${uid}-opt-${index}`;
        row.setAttribute('role', 'option');
        row.setAttribute('aria-selected', index === activeIndex ? 'true' : 'false');

        const dot = document.createElement('span');
        dot.className = 'ui-combobox-dot';
        if (item.typed) dot.classList.add('is-typed');
        else dot.style.backgroundColor = item.color || 'var(--fg-subtle, #6e7681)';
        row.appendChild(dot);

        const label = document.createElement('span');
        label.className = 'ui-combobox-label';
        label.textContent = String(item.label == null ? '' : item.label);
        row.appendChild(label);

        if (item.sublabel) {
          const sub = document.createElement('span');
          sub.className = 'ui-combobox-sub';
          sub.textContent = String(item.sublabel);
          row.appendChild(sub);
        }

        // mousedown (not click) so the choice lands before the input blurs.
        row.addEventListener('mousedown', ev => { ev.preventDefault(); choose(index); });
        list.appendChild(row);
      });
    }

    function syncActiveDescendant() {
      if (open && activeIndex >= 0 && filtered[activeIndex]) {
        input.setAttribute('aria-activedescendant', `${uid}-opt-${activeIndex}`);
      } else {
        input.removeAttribute('aria-activedescendant');
      }
    }

    function openList(resetActive) {
      computeFiltered();
      if (resetActive) activeIndex = filtered.length ? 0 : -1;
      if (activeIndex >= filtered.length) activeIndex = filtered.length - 1;
      open = true;
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      renderList();
      syncActiveDescendant();
      scrollActiveIntoView();
    }

    function closeList() {
      open = false;
      activeIndex = -1;
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      syncActiveDescendant();
    }

    function scrollActiveIntoView() {
      const el = list.querySelector('.ui-combobox-item.is-active');
      if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' });
    }

    function move(delta) {
      if (!open) { openList(true); return; }
      if (!filtered.length) return;
      activeIndex = (activeIndex + delta + filtered.length) % filtered.length;
      renderList();
      syncActiveDescendant();
      scrollActiveIntoView();
    }

    function choose(index) {
      const item = filtered[index];
      if (!item) return;
      input.value = String(item.label == null ? '' : item.label);
      closeList();
      if (typeof o.onSelect === 'function') o.onSelect(item, { typed: !!item.typed });
    }

    input.addEventListener('input', () => {
      if (typeof o.onInput === 'function') o.onInput(input.value);
      openList(true);
    });
    // Deliberately no open-on-focus: the list is taller than most dialogs and
    // would cover the buttons below it the moment the field takes focus. It
    // opens on typing, ArrowUp/ArrowDown, or the toggle.
    input.addEventListener('blur', () => { setTimeout(closeList, 0); });
    input.addEventListener('keydown', ev => {
      switch (ev.key) {
        case 'ArrowDown': ev.preventDefault(); move(1); break;
        case 'ArrowUp':   ev.preventDefault(); move(-1); break;
        case 'Home':      if (open) { ev.preventDefault(); activeIndex = 0; renderList(); syncActiveDescendant(); scrollActiveIntoView(); } break;
        case 'End':       if (open) { ev.preventDefault(); activeIndex = filtered.length - 1; renderList(); syncActiveDescendant(); scrollActiveIntoView(); } break;
        case 'Enter':
          if (open && activeIndex >= 0) { ev.preventDefault(); choose(activeIndex); }
          break;
        case 'Escape':
          if (open) { ev.preventDefault(); ev.stopPropagation(); closeList(); }
          break;
        default: break;
      }
    });
    toggle.addEventListener('mousedown', ev => {
      ev.preventDefault();
      if (open) { closeList(); input.focus(); }
      else { input.focus(); openList(false); }
    });

    return {
      element: mount,
      input,
      setItems(next) {
        items = Array.isArray(next) ? next.slice() : [];
        if (open) openList(false);
      },
      setValue(value) {
        input.value = value == null ? '' : String(value);
        if (open) openList(true);
      },
      getValue() { return input.value; },
      focus() { input.focus(); },
      close: closeList,
      destroy() { closeList(); mount.replaceChildren(); mount.classList.remove('ui-combobox'); },
    };
  }

  window.uiCombobox = uiCombobox;
})();
